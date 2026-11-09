"""Academy lab product domain: tables, RLS, grants, grading definer.

Assembly lineage root for the new Academy composition. Creates the
product-owned lab tables (templates, instances, console activity,
operations queue, result deliveries) with tenant RLS and the legacy
grant matrix (web tier observes/requests, worker claims/settles), then
the a012 lab-grading SECURITY DEFINER: the lab worker holds EXECUTE on
`record_claimed_lab_check` only and reaches the learning ledger solely
through it; Learning grants the worker nothing.

Ported from legacy migrations 0055/0056/0063 (tables/grants) and the D8
a012 capability (definer + deliveries), with the audit source
application renamed to this composition. Requires the
`academy_lab_worker` role to exist (deployment-owned, like the legacy
app); fails closed as non-owner otherwise.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a0001_lab_domain"
down_revision = None
branch_labels = ("assembly",)
depends_on = ("0030_credential_tenant_key", "lr_0012_completion_capability")

_LAB_TABLES = (
    "lab_templates",
    "lab_instances",
    "lab_console_activity",
    "lab_operations",
    "lab_result_deliveries",
)


def _tenant_fk() -> sa.Column:
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


_SIGNATURE = "public.record_claimed_lab_check(pg_catalog.uuid, pg_catalog.text, pg_catalog.jsonb)"
_FLOAT_ADD_SIGNATURE = "public._academy_py_float_add_v1(pg_catalog.float8, pg_catalog.float8)"
_JSON_FLOAT_SIGNATURE = "public._academy_py_json_float_v1(pg_catalog.numeric)"

_FLOAT_ADD_FUNCTION = """
CREATE FUNCTION public._academy_py_float_add_v1(a pg_catalog.float8, b pg_catalog.float8)
RETURNS pg_catalog.float8
LANGUAGE plpgsql IMMUTABLE STRICT SECURITY INVOKER PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $func$
BEGIN
    RETURN a + b;
EXCEPTION WHEN numeric_value_out_of_range THEN
    IF a > 0 AND b > 0 THEN
        RETURN 'Infinity'::pg_catalog.float8;
    ELSIF a < 0 AND b < 0 THEN
        RETURN '-Infinity'::pg_catalog.float8;
    ELSE
        RAISE EXCEPTION USING ERRCODE = '22003', MESSAGE = 'lab check float addition out of range';
    END IF;
END;
$func$;
"""


_JSON_FLOAT_FUNCTION = """
CREATE FUNCTION public._academy_py_json_float_v1(n pg_catalog.numeric)
RETURNS pg_catalog.float8
LANGUAGE plpgsql IMMUTABLE STRICT SECURITY INVOKER PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $func$
BEGIN
    RETURN n::pg_catalog.float8;
EXCEPTION WHEN numeric_value_out_of_range THEN
    IF n > -1 AND n < 1 THEN
        IF n < 0 THEN RETURN '-0'::pg_catalog.float8; END IF;
        RETURN '0'::pg_catalog.float8;
    ELSIF n > 0 THEN
        RETURN 'Infinity'::pg_catalog.float8;
    ELSIF n < 0 THEN
        RETURN '-Infinity'::pg_catalog.float8;
    ELSE
        RAISE EXCEPTION USING ERRCODE = '22003', MESSAGE = 'lab check JSON float out of range';
    END IF;
END;
$func$;
"""


_FUNCTION = """
CREATE FUNCTION public.record_claimed_lab_check(
    p_operation_id pg_catalog.uuid,
    p_claimed_by pg_catalog.text,
    p_per_item pg_catalog.jsonb
)
RETURNS TABLE (
    submission_id pg_catalog.uuid,
    score_id pg_catalog.uuid,
    score pg_catalog.float8,
    max_score pg_catalog.float8,
    passed pg_catalog.bool
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $func$
#variable_conflict error
DECLARE
    v_op pg_catalog.record;
    v_instance pg_catalog.record;
    v_template pg_catalog.record;
    v_activity pg_catalog.record;
    v_written pg_catalog.record;
    v_tenant pg_catalog.uuid;
    v_item pg_catalog.jsonb;
    v_check pg_catalog.jsonb;
    v_key pg_catalog.text;
    v_i pg_catalog.int4;
    -- CPython 3.12/3.13 sum states: fast integer, compensated float,
    -- generic integer and generic float. Max and passing streams are separate.
    v_modes pg_catalog.text[] := ARRAY['FI','FI']::pg_catalog.text[];
    v_ints pg_catalog.numeric[] := ARRAY[0,0]::pg_catalog.numeric[];
    v_floats pg_catalog.float8[] := ARRAY[0,0]::pg_catalog.float8[];
    v_comps pg_catalog.float8[] := ARRAY[0,0]::pg_catalog.float8[];
    v_weight_text pg_catalog.text;
    v_weight_num pg_catalog.numeric;
    v_weight_float pg_catalog.float8;
    v_submitted_weight_float pg_catalog.float8;
    v_weight_is_float pg_catalog.bool;
    v_j pg_catalog.int4;
    v_k pg_catalog.int4;
    v_t pg_catalog.float8;
    v_need_ratio pg_catalog.bool := false;
    v_ratio_overflow_raises pg_catalog.bool := false;
    v_negative pg_catalog.bool;
    v_nums pg_catalog.numeric[] := ARRAY[0,0]::pg_catalog.numeric[];
    v_dens pg_catalog.numeric[] := ARRAY[1,1]::pg_catalog.numeric[];
    v_bytes pg_catalog.bytea;
    v_exp pg_catalog.int4;
    v_power pg_catalog.int4;
    v_mantissa pg_catalog.numeric;
    v_n pg_catalog.numeric;
    v_d pg_catalog.numeric;
    v_q pg_catalog.numeric;
    v_r pg_catalog.numeric;
    v_tmp pg_catalog.numeric;
    v_unit pg_catalog.int4;
    v_bits pg_catalog.int4;
    v_bit pg_catalog.int4;
    v_guard pg_catalog.int4;
    v_sticky pg_catalog.bool;
    v_round_up pg_catalog.bool;
    v_scale pg_catalog.float8;
    v_threshold_text pg_catalog.text;
    v_threshold_num pg_catalog.numeric;
    v_threshold_is_float pg_catalog.bool;
    v_floor pg_catalog.numeric;
    v_above_threshold pg_catalog.bool;
    v_score pg_catalog.float8 := 0.0;
    v_max_score pg_catalog.float8 := 0.0;
    v_fraction pg_catalog.float8;
    v_threshold pg_catalog.float8;
    v_passed pg_catalog.bool;
BEGIN
    IF p_operation_id IS NULL OR p_claimed_by IS NULL OR p_per_item IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22004', MESSAGE = 'lab check arguments must not be null';
    END IF;
    SELECT operation.* INTO v_op FROM public.lab_operations AS operation
     WHERE operation.id = p_operation_id FOR NO KEY UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'lab check claim not held';
    END IF;
    IF v_op.kind IS DISTINCT FROM 'check' OR v_op.state IS DISTINCT FROM 'claimed'
       OR v_op.claimed_by IS DISTINCT FROM p_claimed_by
       OR v_op.claim_lease_seconds IS NULL OR v_op.claim_lease_seconds < 1
       OR v_op.heartbeat_at IS NULL
       OR v_op.heartbeat_at + v_op.claim_lease_seconds * '1 second'::pg_catalog.interval
          <= pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'lab check claim not held';
    END IF;
    v_tenant := v_op.tenant_id;
    IF public.app_current_tenant_id() IS DISTINCT FROM v_tenant THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'lab check tenant mismatch';
    END IF;
    SELECT instance.* INTO v_instance FROM public.lab_instances AS instance
     WHERE instance.id = v_op.instance_id AND instance.tenant_id = v_tenant FOR NO KEY UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'lab check instance unavailable';
    END IF;
    IF v_instance.status IS DISTINCT FROM 'active' OR v_instance.error IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'lab check instance unavailable';
    END IF;
    SELECT template.* INTO v_template FROM public.lab_templates AS template
     WHERE template.tenant_id = v_tenant AND template.activity_id = v_instance.activity_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'lab check template unavailable';
    END IF;
    SELECT activity.* INTO v_activity FROM mod_learning.activities AS activity
     WHERE activity.tenant_id = v_tenant AND activity.id = v_instance.activity_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'lab check activity unavailable';
    END IF;
    IF v_activity.type IS DISTINCT FROM 'lab' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'lab checks require a lab activity';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.parties AS person
        JOIN public.party_persons AS subtype ON subtype.party_id = person.id
        WHERE person.tenant_id = v_tenant AND person.id = v_instance.person_id AND person.party_type = 'person'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'lab check learner unavailable';
    END IF;
    IF pg_catalog.jsonb_typeof(p_per_item) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_template.checks) IS DISTINCT FROM 'array'
       OR pg_catalog.octet_length(p_per_item::pg_catalog.text) > 262144
       OR pg_catalog.pg_column_size(p_per_item) > 262144 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid lab check payload';
    END IF;
    IF pg_catalog.jsonb_array_length(p_per_item) <> pg_catalog.jsonb_array_length(v_template.checks) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid lab check payload';
    END IF;
    FOR v_i IN 0..pg_catalog.jsonb_array_length(p_per_item) - 1 LOOP
        v_item := p_per_item -> v_i;
        v_check := v_template.checks -> v_i;
        IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_check) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid lab check payload';
        END IF;
        IF pg_catalog.jsonb_typeof(v_item -> 'id') IS DISTINCT FROM 'string'
           OR v_item -> 'id' IS DISTINCT FROM v_check -> 'id'
           OR v_item -> 'label' IS DISTINCT FROM COALESCE(v_check -> 'label', '""'::pg_catalog.jsonb)
           OR pg_catalog.jsonb_typeof(v_item -> 'weight') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(v_item -> 'pass') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid lab check payload';
        END IF;
        FOR v_key IN SELECT pg_catalog.jsonb_object_keys(v_item) LOOP
            IF v_key <> ALL(ARRAY['id','label','weight','pass','actual','expected','detail','hint']
                            ::pg_catalog.text[]) THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid lab check payload';
            END IF;
            IF v_key = ANY(ARRAY['actual','expected','detail','hint']::pg_catalog.text[])
               AND (pg_catalog.jsonb_typeof(v_item -> v_key) NOT IN ('string', 'null')
                    OR pg_catalog.length(v_item ->> v_key) > 4096) THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid lab check payload';
            END IF;
        END LOOP;
        -- Payload numeric equality cannot choose Python int/float spelling.
        -- Only the stored template, as reloaded by the worker, selects the mode.
        IF v_check ? 'weight' AND pg_catalog.jsonb_typeof(v_check -> 'weight') IS DISTINCT FROM 'number' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid lab check payload';
        END IF;
        v_weight_text := COALESCE(v_check ->> 'weight', '1');
        v_weight_num := v_weight_text::pg_catalog.numeric;
        v_weight_is_float := pg_catalog.strpos(v_weight_text, '.') > 0;
        IF v_weight_is_float THEN
            v_weight_float := public._academy_py_json_float_v1(v_weight_num);
            IF v_weight_float IN ('NaN'::pg_catalog.float8, 'Infinity'::pg_catalog.float8,
                                  '-Infinity'::pg_catalog.float8) THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid lab check payload';
            END IF;
            v_submitted_weight_float := public._academy_py_json_float_v1((v_item ->> 'weight')::pg_catalog.numeric);
            IF v_submitted_weight_float IS DISTINCT FROM v_weight_float THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid lab check payload';
            END IF;
        ELSIF (v_item ->> 'weight')::pg_catalog.numeric <> v_weight_num THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid lab check payload';
        END IF;
        FOR v_j IN 1..2 LOOP
            IF v_j = 2 AND NOT (v_item ->> 'pass')::pg_catalog.bool THEN CONTINUE; END IF;
            IF v_modes[v_j] = 'FI' THEN
                IF NOT v_weight_is_float THEN
                    v_ints[v_j] := v_ints[v_j] + v_weight_num;
                    IF v_weight_num < -9223372036854775808 OR v_weight_num > 9223372036854775807
                       OR v_ints[v_j] < -9223372036854775808 OR v_ints[v_j] > 9223372036854775807 THEN
                        v_modes[v_j] := 'GI';
                    END IF;
                ELSE
                    v_floats[v_j] := public._academy_py_float_add_v1(v_ints[v_j]::pg_catalog.float8, v_weight_float);
                    v_comps[v_j] := 0;
                    v_modes[v_j] := 'CF';
                END IF;
            ELSIF v_modes[v_j] = 'GI' THEN
                IF NOT v_weight_is_float THEN
                    v_ints[v_j] := v_ints[v_j] + v_weight_num;
                ELSE
                    v_floats[v_j] := public._academy_py_float_add_v1(v_ints[v_j]::pg_catalog.float8, v_weight_float);
                    v_modes[v_j] := 'GF';
                END IF;
            ELSIF v_modes[v_j] = 'CF' THEN
                IF v_weight_is_float THEN
                    v_t := public._academy_py_float_add_v1(v_floats[v_j], v_weight_float);
                    IF v_floats[v_j] <> 'NaN'::pg_catalog.float8 AND v_weight_float <> 'NaN'::pg_catalog.float8
                       AND pg_catalog.abs(v_floats[v_j]) >= pg_catalog.abs(v_weight_float) THEN
                        v_comps[v_j] := public._academy_py_float_add_v1(v_comps[v_j],
                            public._academy_py_float_add_v1(
                                public._academy_py_float_add_v1(v_floats[v_j], -v_t), v_weight_float));
                    ELSE
                        v_comps[v_j] := public._academy_py_float_add_v1(v_comps[v_j],
                            public._academy_py_float_add_v1(
                                public._academy_py_float_add_v1(v_weight_float, -v_t), v_floats[v_j]));
                    END IF;
                    v_floats[v_j] := v_t;
                ELSIF v_weight_num BETWEEN -9223372036854775808 AND 9223372036854775807 THEN
                    v_floats[v_j] := public._academy_py_float_add_v1(
                        v_floats[v_j], v_weight_num::pg_catalog.float8);
                ELSE
                    IF v_comps[v_j] <> 0 AND v_comps[v_j] NOT IN (
                        'NaN'::pg_catalog.float8, 'Infinity'::pg_catalog.float8, '-Infinity'::pg_catalog.float8
                    ) THEN
                        v_floats[v_j] := public._academy_py_float_add_v1(v_floats[v_j], v_comps[v_j]);
                    END IF;
                    v_floats[v_j] := public._academy_py_float_add_v1(
                        v_floats[v_j], v_weight_num::pg_catalog.float8);
                    v_comps[v_j] := 0;
                    v_modes[v_j] := 'GF';
                END IF;
            ELSE
                v_floats[v_j] := public._academy_py_float_add_v1(v_floats[v_j],
                    CASE WHEN v_weight_is_float THEN v_weight_float ELSE v_weight_num::pg_catalog.float8 END);
            END IF;
        END LOOP;
    END LOOP;
    FOR v_j IN 1..2 LOOP
        IF v_modes[v_j] = 'CF' AND v_comps[v_j] <> 0 AND v_comps[v_j] NOT IN (
            'NaN'::pg_catalog.float8, 'Infinity'::pg_catalog.float8, '-Infinity'::pg_catalog.float8
        ) THEN
            v_floats[v_j] := public._academy_py_float_add_v1(v_floats[v_j], v_comps[v_j]);
        END IF;
    END LOOP;
    -- Evaluate the Python fraction BEFORE coercing integer totals for persistence.
    IF (v_modes[1] IN ('FI','GI') AND v_ints[1] = 0)
       OR (v_modes[1] IN ('CF','GF') AND v_floats[1] = 0) THEN
        v_fraction := 0;
    ELSIF v_modes[1] IN ('FI','GI') AND v_modes[2] IN ('FI','GI') THEN
        v_n := pg_catalog.abs(v_ints[2]);
        v_d := pg_catalog.abs(v_ints[1]);
        v_negative := (v_ints[2] < 0) <> (v_ints[1] < 0);
        v_need_ratio := true;
        v_ratio_overflow_raises := true;
    ELSE
        FOR v_j IN 1..2 LOOP
            IF v_modes[v_j] IN ('FI','GI') THEN
                -- Python int->float overflow propagates; never use the JSON helper.
                v_floats[v_j] := v_ints[v_j]::pg_catalog.float8;
            END IF;
        END LOOP;
        v_negative := (pg_catalog.get_byte(pg_catalog.float8send(v_floats[2]),0) >= 128)
                   <> (pg_catalog.get_byte(pg_catalog.float8send(v_floats[1]),0) >= 128);
        IF v_floats[1] = 'NaN'::pg_catalog.float8 OR v_floats[2] = 'NaN'::pg_catalog.float8
           OR (pg_catalog.abs(v_floats[1]) = 'Infinity'::pg_catalog.float8
               AND pg_catalog.abs(v_floats[2]) = 'Infinity'::pg_catalog.float8) THEN
            v_fraction := 'NaN'::pg_catalog.float8;
        ELSIF pg_catalog.abs(v_floats[2]) = 'Infinity'::pg_catalog.float8 THEN
            v_fraction := CASE WHEN v_negative THEN '-Infinity'::pg_catalog.float8
                               ELSE 'Infinity'::pg_catalog.float8 END;
        ELSIF pg_catalog.abs(v_floats[1]) = 'Infinity'::pg_catalog.float8 THEN
            v_fraction := CASE WHEN v_negative THEN '-0'::pg_catalog.float8 ELSE 0 END;
        ELSE
            -- Exact binary64 decode; numeric division/casts would double-round.
            FOR v_j IN 1..2 LOOP
                v_bytes := pg_catalog.float8send(v_floats[v_j]);
                v_exp := (pg_catalog.get_byte(v_bytes,0) % 128) * 16 + pg_catalog.get_byte(v_bytes,1) / 16;
                v_mantissa := pg_catalog.get_byte(v_bytes,1) % 16;
                FOR v_k IN 2..7 LOOP
                    v_mantissa := v_mantissa * 256 + pg_catalog.get_byte(v_bytes,v_k);
                END LOOP;
                IF v_exp = 0 THEN v_power := -1074;
                ELSE v_power := v_exp - 1075; v_mantissa := v_mantissa + 4503599627370496; END IF;
                v_nums[v_j] := v_mantissa;
                v_dens[v_j] := 1;
                FOR v_k IN 1..pg_catalog.abs(v_power) LOOP
                    IF v_power >= 0 THEN v_nums[v_j] := v_nums[v_j] * 2;
                    ELSE v_dens[v_j] := v_dens[v_j] * 2; END IF;
                END LOOP;
            END LOOP;
            v_n := v_nums[2] * v_dens[1];
            v_d := v_dens[2] * v_nums[1];
            v_need_ratio := true;
        END IF;
    END IF;
    IF v_need_ratio THEN
        -- Exact rational -> binary64, round-to-nearest/ties-to-even.
        -- Generate bits via div/mod without decimal approximation or giant scaling.
        v_q := pg_catalog.div(v_n,v_d);
        v_r := pg_catalog.mod(v_n,v_d);
        IF v_q >= 9007199254740992 THEN
            v_unit := 0; v_guard := 0; v_sticky := v_r <> 0;
            WHILE v_q >= 9007199254740992 LOOP
                v_sticky := v_sticky OR v_guard = 1;
                v_guard := pg_catalog.mod(v_q,2)::pg_catalog.int4;
                v_q := pg_catalog.div(v_q,2);
                v_unit := v_unit + 1;
            END LOOP;
            v_round_up := v_guard = 1 AND (v_sticky OR pg_catalog.mod(v_q,2) = 1);
        ELSE
            IF v_q > 0 THEN
                v_tmp := v_q; v_exp := -1;
                WHILE v_tmp > 0 LOOP v_exp := v_exp + 1; v_tmp := pg_catalog.div(v_tmp,2); END LOOP;
                v_bits := 52 - v_exp; v_unit := v_exp - 52;
            ELSE
                v_exp := 0;
                WHILE v_q = 0 AND v_exp > -1074 LOOP
                    v_exp := v_exp - 1;
                    IF v_r >= v_d - v_r THEN v_bit := 1; v_r := v_r - (v_d - v_r);
                    ELSE v_bit := 0; v_r := v_r + v_r; END IF;
                    v_q := v_bit;
                END LOOP;
                IF v_q = 0 THEN v_unit := -1074; v_bits := 0;
                ELSIF v_exp >= -1022 THEN v_unit := v_exp - 52; v_bits := 52;
                ELSE v_unit := -1074; v_bits := v_exp + 1074; END IF;
            END IF;
            FOR v_k IN 1..v_bits LOOP
                IF v_r >= v_d - v_r THEN v_bit := 1; v_r := v_r - (v_d - v_r);
                ELSE v_bit := 0; v_r := v_r + v_r; END IF;
                v_q := v_q * 2 + v_bit;
            END LOOP;
            v_round_up := v_r > v_d - v_r OR (v_r = v_d - v_r AND pg_catalog.mod(v_q,2) = 1);
        END IF;
        IF v_round_up THEN v_q := v_q + 1; END IF;
        IF v_q = 9007199254740992 THEN v_q := pg_catalog.div(v_q,2); v_unit := v_unit + 1; END IF;
        IF v_q = 0 THEN
            v_fraction := CASE WHEN v_negative THEN '-0'::pg_catalog.float8 ELSE 0 END;
        ELSIF v_unit + 52 > 1023 THEN
            IF v_ratio_overflow_raises THEN
                RAISE EXCEPTION USING ERRCODE = '22003', MESSAGE = 'lab check integer ratio out of range';
            END IF;
            v_fraction := CASE WHEN v_negative THEN '-Infinity'::pg_catalog.float8
                               ELSE 'Infinity'::pg_catalog.float8 END;
        ELSE
            v_exp := GREATEST(v_unit + 52,-1022);
            v_scale := 1;
            FOR v_k IN 1..pg_catalog.abs(v_exp) LOOP
                IF v_exp >= 0 THEN v_scale := v_scale * 2; ELSE v_scale := v_scale / 2; END IF;
            END LOOP;
            v_fraction := (v_q::pg_catalog.float8 / 4503599627370496.0::pg_catalog.float8) * v_scale;
            IF v_negative THEN v_fraction := -v_fraction; END IF;
        END IF;
    END IF;
    v_max_score := CASE WHEN v_modes[1] IN ('FI','GI') THEN v_ints[1]::pg_catalog.float8 ELSE v_floats[1] END;
    v_score := CASE WHEN v_modes[2] IN ('FI','GI') THEN v_ints[2]::pg_catalog.float8 ELSE v_floats[2] END;
    v_threshold_text := v_template.limits ->> 'pass_threshold';
    IF v_threshold_text IS NULL THEN
        v_threshold := v_activity.pass_threshold;
        v_threshold_is_float := true;
    ELSE
        v_threshold_num := v_threshold_text::pg_catalog.numeric;
        v_threshold_is_float := pg_catalog.strpos(v_threshold_text,'.') > 0;
        IF v_threshold_is_float THEN v_threshold := public._academy_py_json_float_v1(v_threshold_num); END IF;
    END IF;
    IF v_fraction = 'NaN'::pg_catalog.float8 OR v_max_score = 'NaN'::pg_catalog.float8 THEN
        v_above_threshold := false;
    ELSIF v_threshold_is_float THEN
        v_above_threshold := v_threshold <> 'NaN'::pg_catalog.float8 AND v_fraction >= v_threshold;
    ELSIF v_fraction = 'Infinity'::pg_catalog.float8 THEN
        v_above_threshold := true;
    ELSIF v_fraction = '-Infinity'::pg_catalog.float8 THEN
        v_above_threshold := false;
    ELSE
        -- Python compares float to integer exactly, including huge thresholds.
        v_bytes := pg_catalog.float8send(v_fraction);
        v_negative := pg_catalog.get_byte(v_bytes,0) >= 128;
        v_exp := (pg_catalog.get_byte(v_bytes,0) % 128) * 16 + pg_catalog.get_byte(v_bytes,1) / 16;
        v_mantissa := pg_catalog.get_byte(v_bytes,1) % 16;
        FOR v_k IN 2..7 LOOP v_mantissa := v_mantissa * 256 + pg_catalog.get_byte(v_bytes,v_k); END LOOP;
        IF v_exp = 0 THEN v_power := -1074;
        ELSE v_power := v_exp - 1075; v_mantissa := v_mantissa + 4503599627370496; END IF;
        v_n := v_mantissa; v_d := 1;
        FOR v_k IN 1..pg_catalog.abs(v_power) LOOP
            IF v_power >= 0 THEN v_n := v_n * 2; ELSE v_d := v_d * 2; END IF;
        END LOOP;
        IF v_negative THEN v_n := -v_n; END IF;
        v_floor := pg_catalog.div(v_n,v_d);
        IF v_n < 0 AND pg_catalog.mod(v_n,v_d) <> 0 THEN v_floor := v_floor - 1; END IF;
        v_above_threshold := v_floor >= v_threshold_num;
    END IF;
    v_passed := v_max_score <> 'NaN'::pg_catalog.float8 AND v_max_score > 0 AND v_above_threshold;
    IF EXISTS (SELECT 1 FROM public.lab_result_deliveries AS intent
               WHERE intent.tenant_id = v_tenant AND intent.operation_id = v_op.id) THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'lab check already recorded for this claim';
    END IF;
    SELECT written.* INTO v_written FROM mod_learning.record_lab_result_v1(
        v_tenant, v_instance.person_id, v_instance.activity_id, v_instance.id,
        v_instance.seed, v_score, v_max_score, v_fraction, v_passed, p_per_item
    ) AS written;
    INSERT INTO public.lab_result_deliveries (
        tenant_id, operation_id, instance_id, person_id, activity_id, course_id,
        submission_id, score_id, certificate_id, first_completion, certificate_created, first_pass,
        score, max_score, fraction, passed
    ) VALUES (
        v_tenant, v_op.id, v_instance.id, v_instance.person_id, v_instance.activity_id, v_written.course_id,
        v_written.submission_id, v_written.score_id, v_written.certificate_id,
        v_written.first_completion, v_written.certificate_created, v_passed AND NOT v_written.prior_pass_exists,
        v_score, v_max_score, v_fraction, v_passed
    ) ON CONFLICT (tenant_id, operation_id) DO NOTHING;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'lab check already recorded for this claim';
    END IF;
    INSERT INTO public.audit_events (
        id, tenant_id, actor_type, actor_id, actor_party_id, source_application, action, entity_type, entity_id, details
    ) VALUES (
        pg_catalog.gen_random_uuid(), v_tenant, 'system', 'academy_lab_worker', NULL,
        'dotmac_academy_v2', 'lab_result.record', 'lab_operation', v_op.id::pg_catalog.text,
        pg_catalog.jsonb_build_object(
            'instance_id', v_instance.id, 'submission_id', v_written.submission_id,
            'score_id', v_written.score_id, 'score', v_score, 'max_score', v_max_score, 'passed', v_passed)
    );
    submission_id := v_written.submission_id;
    score_id := v_written.score_id;
    score := v_score;
    max_score := v_max_score;
    passed := v_passed;
    RETURN NEXT;
END;
$func$;
"""


_DELIVERIES_DDL = """
CREATE TABLE public.lab_result_deliveries (
    tenant_id pg_catalog.uuid NOT NULL,
    operation_id pg_catalog.uuid NOT NULL,
    instance_id pg_catalog.uuid NOT NULL,
    person_id pg_catalog.uuid NOT NULL,
    activity_id pg_catalog.uuid NOT NULL,
    course_id pg_catalog.uuid NOT NULL,
    submission_id pg_catalog.uuid NOT NULL,
    score_id pg_catalog.uuid NOT NULL,
    certificate_id pg_catalog.uuid NULL,
    first_completion pg_catalog.bool NOT NULL,
    certificate_created pg_catalog.bool NOT NULL,
    first_pass pg_catalog.bool NOT NULL,
    score pg_catalog.float8 NOT NULL,
    max_score pg_catalog.float8 NOT NULL,
    fraction pg_catalog.float8 NOT NULL,
    passed pg_catalog.bool NOT NULL,
    created_at pg_catalog.timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    next_attempt_at pg_catalog.timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    delivered_at pg_catalog.timestamptz NULL,
    attempts pg_catalog.int4 NOT NULL DEFAULT 0,
    last_error pg_catalog.varchar(120) NULL,
    status pg_catalog.varchar(16) NOT NULL DEFAULT 'pending',
    CONSTRAINT lab_result_deliveries_pkey PRIMARY KEY (tenant_id, operation_id),
    CONSTRAINT lab_result_deliveries_tenant_id_fkey FOREIGN KEY (tenant_id)
        REFERENCES public.tenants(id) ON DELETE CASCADE,
    CONSTRAINT lab_result_deliveries_status_check CHECK (status IN ('pending','delivered','failed'))
);
CREATE INDEX ix_lab_result_deliveries_status_created
    ON public.lab_result_deliveries (status, created_at);
CREATE INDEX ix_lab_result_deliveries_due
    ON public.lab_result_deliveries (next_attempt_at, created_at)
    WHERE status = 'pending';
"""


def upgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT current_user")) != "app_admin":
        raise ValueError("Academy assembly migration requires app_admin")

    op.create_table(
        "lab_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_number", sa.Integer),
        sa.Column("activity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("topology", sa.Text, nullable=False, server_default=""),
        sa.Column("instructions_html", sa.Text, nullable=False, server_default=""),
        sa.Column("instructions_md", sa.Text),
        sa.Column(
            "checks",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "seed_spec",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "limits",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "access",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("engine", sa.String(20), nullable=False, server_default="containerlab"),
        sa.Column("source_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_lab_templates_tenant_id_id"),
        sa.UniqueConstraint("activity_id", name="uq_lab_templates_activity_id"),
    )
    op.create_index("ix_lab_templates_tenant_id", "lab_templates", ["tenant_id"])
    op.create_index("ix_lab_templates_course_id", "lab_templates", ["course_id"])
    op.create_index("ix_lab_templates_activity_id", "lab_templates", ["activity_id"])

    op.create_table(
        "lab_instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column("activity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_name", sa.String(120), nullable=False),
        sa.Column(
            "seed",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column(
            "consoles",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("error", sa.Text),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("last_active_at", sa.DateTime(timezone=True)),
        sa.Column("runtime_presence", sa.String(16), nullable=False, server_default="absent"),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_lab_instances_tenant_id_id"),
    )
    op.create_index("ix_lab_instances_tenant_id", "lab_instances", ["tenant_id"])
    op.create_index("ix_lab_instances_activity_id", "lab_instances", ["activity_id"])
    op.create_index("ix_lab_instances_person_id", "lab_instances", ["person_id"])
    op.create_index(
        "uq_lab_instances_instance_name",
        "lab_instances",
        ["instance_name"],
        unique=True,
    )

    op.create_table(
        "lab_console_activity",
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "instance_id"],
            ["lab_instances.tenant_id", "lab_instances.id"],
            name="fk_lab_console_activity_instance",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_lab_console_activity_tenant_id", "lab_console_activity", ["tenant_id"])

    op.create_table(
        "lab_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "not_before",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("claimed_by", sa.String(200)),
        sa.Column("claimed_host", sa.String(255)),
        sa.Column("claimed_epoch", sa.String(64)),
        sa.Column("claim_lease_seconds", sa.Integer),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.Column("origin", sa.String(32)),
        sa.Column("runtime_precondition", sa.String(16)),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id", "instance_id"],
            ["lab_instances.tenant_id", "lab_instances.id"],
            name="fk_lab_operations_tenant_instance",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "claim_lease_seconds IS NULL OR claim_lease_seconds > 0",
            name="ck_lab_operations_claim_lease_positive",
        ),
        sa.CheckConstraint(
            "state <> 'claimed' OR claim_lease_seconds IS NOT NULL",
            name="ck_lab_operations_claim_lease_required",
        ),
        sa.CheckConstraint(
            "(origin IS NULL AND runtime_precondition IS NULL) "
            "OR (kind = 'deploy' AND origin IS NOT NULL AND origin = 'runtime_repair' "
            "AND runtime_precondition IS NOT NULL AND runtime_precondition = 'absent') "
            "OR (kind = 'destroy' AND origin IS NOT NULL AND origin = 'runtime_cleanup' "
            "AND runtime_precondition IS NOT NULL AND runtime_precondition = 'present')",
            name="ck_lab_operations_conditional_runtime",
        ),
    )
    op.create_index("ix_lab_operations_tenant_id", "lab_operations", ["tenant_id"])
    op.create_index(
        "uq_lab_operations_open_per_instance",
        "lab_operations",
        ["instance_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('queued', 'claimed')"),
    )
    op.create_index(
        "ix_lab_operations_claim",
        "lab_operations",
        ["state", "not_before", "requested_at"],
    )

    op.execute(_DELIVERIES_DDL)

    for table in _LAB_TABLES:
        op.execute(f"ALTER TABLE public.{table} OWNER TO app_admin")
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON public.{table} "
            "USING (tenant_id = public.app_current_tenant_id()) "
            "WITH CHECK (tenant_id = public.app_current_tenant_id())"
        )
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC")

    # Web tier: observe everything, create instances/requests only.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON public.lab_templates TO app_user")
    op.execute("GRANT SELECT ON public.lab_instances TO app_user, platform_api")
    op.execute(
        "GRANT INSERT (id, tenant_id, activity_id, person_id, instance_name, seed) ON public.lab_instances TO app_user"
    )
    op.execute("GRANT SELECT ON public.lab_operations TO app_user, platform_api")
    op.execute("GRANT INSERT (id, tenant_id, instance_id, kind, requested_by) ON public.lab_operations TO app_user")
    op.execute("GRANT SELECT ON public.lab_console_activity TO app_user")
    op.execute("GRANT INSERT (instance_id, tenant_id, last_seen_at) ON public.lab_console_activity TO app_user")
    op.execute("GRANT UPDATE (last_seen_at) ON public.lab_console_activity TO app_user")
    op.execute("GRANT SELECT ON public.lab_result_deliveries TO app_user")
    op.execute(
        "GRANT UPDATE (status, delivered_at, attempts, last_error, next_attempt_at) "
        "ON public.lab_result_deliveries TO app_user"
    )

    # Worker: claim/settle operations, observe templates, write only its
    # runtime columns on instances. No mod_learning access at all: graded
    # results enter solely through the definer below.
    op.execute("GRANT SELECT, UPDATE ON public.lab_operations TO academy_lab_worker")
    op.execute("GRANT SELECT ON public.lab_instances TO academy_lab_worker")
    op.execute(
        "GRANT UPDATE (status, consoles, error, started_at, last_active_at, "
        "runtime_presence, updated_at) ON public.lab_instances TO academy_lab_worker"
    )
    op.execute("GRANT SELECT ON public.lab_templates TO academy_lab_worker")

    op.execute(_FLOAT_ADD_FUNCTION)
    op.execute(f"ALTER FUNCTION {_FLOAT_ADD_SIGNATURE} OWNER TO app_admin")
    op.execute(_JSON_FLOAT_FUNCTION)
    op.execute(f"ALTER FUNCTION {_JSON_FLOAT_SIGNATURE} OWNER TO app_admin")
    for signature in (_FLOAT_ADD_SIGNATURE, _JSON_FLOAT_SIGNATURE):
        for role in (
            "PUBLIC",
            "app_user",
            "platform_api",
            "academy_lab_worker",
            "outbox_dispatcher",
            "platform_outbox_dispatcher",
        ):
            op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM {role}")
    op.execute(_FUNCTION)
    op.execute(f"ALTER FUNCTION {_SIGNATURE} OWNER TO app_admin")
    op.execute(f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO academy_lab_worker")


def downgrade() -> None:
    raise RuntimeError("Academy assembly reversal requires a reviewed new-facts disposition")
