"""Trusted route-free Academy bootstrap composition over the actual Connection.

Policy is process composition, never caller-supplied authority. Product-owned
extension outcomes and their real PostgreSQL enforcement are pinned here;
canonical identity, transaction custody, role admission and audit are Kernel-owned.
"""

# Ported at new-composition genesis (accounts-core slice).

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from dotmac_kernel.credential_lifecycle import (
    CredentialAssignmentError,
    CredentialCreateAbsentV1,
    CredentialPolicyViolation,
    CredentialSnapshot,
)
from dotmac_kernel.identity_provisioning import BootstrapPolicyV1, BootstrapRoleV1
from dotmac_kernel.identity_storage import NewPersonValues
from sqlalchemy import select, text

from app.admissions_settings import BOOTSTRAP_POLICY_KEYS
from app.models.identity_extensions import AcademyAccountProfile
from app.services.canonical_identity_store import NewPersonValues as AcademyPersonValues
from app.services.canonical_identity_store import create_person
from app.services.credential_storage import create_absent as _create_absent_rows
from app.services.identity import normalize_email
from app.services.initial_identity_policy import InitialIdentityPolicy
from app.services.settings_store import effective


def _require_material_logging_floor(connection):
    """Fixed material-safety floor at trusted product callback boundaries."""
    engine = connection.engine
    if engine.hide_parameters is not True or engine.echo is not False or engine.logger.isEnabledFor(logging.DEBUG):
        raise CredentialAssignmentError("bootstrap credential logging configuration refused")


@dataclass(frozen=True, slots=True)
class _Composition:
    values: tuple
    policy: BootstrapPolicyV1
    initial: InitialIdentityPolicy
    minimum_length: int
    first_name: str
    last_name_template: str


def _composition(connection):
    configured = effective(connection, require_store=True)
    roles = configured.bootstrap_role_provisioning
    if configured.bootstrap_initial_role_slug not in roles:
        raise CredentialAssignmentError("bootstrap initial role is not provisionable")
    # Include every relevant setting in late equality; no retained Settings ORM
    # instances, caller pending objects, or settings rows authorize admission.
    values = tuple(
        (key, tuple(sorted(value.items())) if isinstance(value, dict) else value)
        for key in (*BOOTSTRAP_POLICY_KEYS, "account_initial_is_active", "account_initial_profile_status")
        for value in (getattr(configured, key),)
    )
    return _Composition(
        values,
        BootstrapPolicyV1(
            allowed_database_role_pairs=configured.bootstrap_allowed_database_role_pairs,
            roles=tuple(BootstrapRoleV1(slug, name) for slug, name in sorted(roles.items())),
            initial_role_slug=configured.bootstrap_initial_role_slug,
            tenant_is_active=configured.bootstrap_tenant_is_active,
        ),
        InitialIdentityPolicy(configured.account_initial_is_active, configured.account_initial_profile_status),
        configured.bootstrap_password_min_length,
        configured.bootstrap_owner_first_name,
        configured.bootstrap_owner_last_name_template,
    )


def _expression(value):
    return "".join((value or "").replace("public.", "").split()).replace("(", "").replace(")", "")


def _catalog(connection):
    """Closed Academy extension census; actual catalogue, never copied metadata."""
    tables = connection.execute(
        text("""
        SELECT c.relname,c.oid,c.relowner,c.relkind,c.relrowsecurity,c.relforcerowsecurity,
               (pg_catalog.has_table_privilege(c.oid,'SELECT')
                AND pg_catalog.has_table_privilege(c.oid,'INSERT')) AS admitted,
               EXISTS(SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid AND NOT t.tgisinternal) AS triggers,
               EXISTS(SELECT 1 FROM pg_catalog.pg_rewrite r WHERE r.ev_class=c.oid) AS rules
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname IN ('academy_account_profiles','academy_credential_policies')
        ORDER BY c.relname
    """)
    ).all()
    if len(tables) != 2 or any(
        row.relkind != "r"
        or not row.relrowsecurity
        or not row.relforcerowsecurity
        or not row.admitted
        or row.triggers
        or row.rules
        for row in tables
    ):
        raise CredentialAssignmentError("bootstrap product catalogue refused")
    columns = connection.execute(
        text("""
        SELECT c.relname,a.attname,pg_catalog.format_type(a.atttypid,a.atttypmod),a.attnotnull,
               a.attidentity,a.attgenerated,pg_catalog.pg_get_expr(d.adbin,d.adrelid)
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
        LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        WHERE n.nspname='public' AND c.relname IN ('academy_account_profiles','academy_credential_policies')
        ORDER BY c.relname,a.attnum
    """)
    ).all()
    actual = {(row[0], row[1]): tuple(row[2:]) for row in columns}
    expected = {
        ("academy_account_profiles", "party_id"): ("uuid", True, "", "", None),
        ("academy_account_profiles", "tenant_id"): ("uuid", True, "", "", None),
        ("academy_account_profiles", "status"): ("character varying(20)", True, "", "", None),
        ("academy_account_profiles", "avatar_path"): ("character varying(255)", False, "", "", None),
        ("academy_account_profiles", "prefs"): ("jsonb", True, "", "", None),
        ("academy_credential_policies", "credential_id"): ("uuid", True, "", "", None),
        ("academy_credential_policies", "tenant_id"): ("uuid", True, "", "", None),
        ("academy_credential_policies", "email"): ("character varying(254)", True, "", "", None),
        ("academy_credential_policies", "failed_login_attempts"): ("integer", True, "", "", None),
        ("academy_credential_policies", "lifecycle_version"): ("bigint", True, "", "", None),
        ("academy_credential_policies", "locked_until"): ("timestamp with time zone", False, "", "", None),
        ("academy_credential_policies", "reset_required"): ("boolean", True, "", "", None),
    }
    for table in ("academy_account_profiles", "academy_credential_policies"):
        for column in ("created_at", "updated_at"):
            expected[table, column] = ("timestamp with time zone", True, "", "", None)
    if actual != expected:
        raise CredentialAssignmentError("bootstrap product columns refused")
    constraints = connection.execute(
        text("""
        SELECT c.relname,k.contype,k.convalidated,k.condeferrable,k.condeferred,
               ARRAY(SELECT a.attname::pg_catalog.text FROM pg_catalog.unnest(k.conkey) WITH ORDINALITY x(num,ord)
                     JOIN pg_catalog.pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=x.num ORDER BY x.ord),
               rn.nspname,rc.relname,
               ARRAY(SELECT a.attname::pg_catalog.text FROM pg_catalog.unnest(k.confkey) WITH ORDINALITY x(num,ord)
                     JOIN pg_catalog.pg_attribute a ON a.attrelid=k.confrelid AND a.attnum=x.num ORDER BY x.ord),
               k.confdeltype,k.confupdtype,k.confmatchtype,pg_catalog.pg_get_constraintdef(k.oid),
               CASE WHEN k.conindid=0 THEN true ELSE
                   (SELECT i.indisvalid AND i.indisready AND i.indislive AND i.indisunique
                    AND i.indpred IS NULL AND i.indexprs IS NULL
                    FROM pg_catalog.pg_index i WHERE i.indexrelid=k.conindid) END
        FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_class c ON c.oid=k.conrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_catalog.pg_class rc ON rc.oid=k.confrelid
        LEFT JOIN pg_catalog.pg_namespace rn ON rn.oid=rc.relnamespace
        WHERE n.nspname='public' AND c.relname IN ('academy_account_profiles','academy_credential_policies')
        ORDER BY c.relname,k.oid
    """)
    ).all()
    wanted = {
        ("academy_account_profiles", "p", ("party_id",), None, None, ()),
        ("academy_account_profiles", "f", ("tenant_id", "party_id"), "public", "parties", ("tenant_id", "id")),
        ("academy_account_profiles", "f", ("party_id",), "public", "party_persons", ("party_id",)),
        ("academy_credential_policies", "p", ("credential_id",), None, None, ()),
        (
            "academy_credential_policies",
            "f",
            ("tenant_id", "credential_id"),
            "public",
            "user_credentials",
            ("tenant_id", "id"),
        ),
        ("academy_credential_policies", "u", ("tenant_id", "email"), None, None, ()),
    }
    observed = set()
    checks = []
    for row in constraints:
        if not row[2] or row[3] or row[4] or not row[13]:
            raise CredentialAssignmentError("bootstrap product constraint enforcement refused")
        if row[1] == "c":
            checks.append((row[0], _expression(row[12])))
        else:
            observed.add((row[0], row[1], tuple(row[5]), row[6], row[7], tuple(row[8])))
        if row[1] == "f" and (row[9], row[10], row[11]) != ("c", "a", "s"):
            raise CredentialAssignmentError("bootstrap product foreign key actions refused")
    if observed != wanted or checks != [("academy_credential_policies", "CHECKlifecycle_version>=0")]:
        raise CredentialAssignmentError("bootstrap product identity constraints refused")
    policies = connection.execute(
        text("""
        SELECT c.relname,p.polname,p.polcmd,p.polpermissive,p.polroles::pg_catalog.text,
               pg_catalog.pg_get_expr(p.polqual,p.polrelid),pg_catalog.pg_get_expr(p.polwithcheck,p.polrelid),p.oid
        FROM pg_catalog.pg_policy p JOIN pg_catalog.pg_class c ON c.oid=p.polrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname IN ('academy_account_profiles','academy_credential_policies')
        ORDER BY c.relname,p.polname
    """)
    ).all()
    if len(policies) != 2 or any(
        row[1] != row[0] + "_tenant_isolation"
        or row[2] != "*"
        or not row[3]
        or row[4] != "{0}"
        or _expression(row[5]) != "tenant_id=app_current_tenant_id"
        or _expression(row[6]) != "tenant_id=app_current_tenant_id"
        for row in policies
    ):
        raise CredentialAssignmentError("bootstrap product tenant policy refused")
    helper = connection.execute(
        text("""
        SELECT p.oid,p.proowner,p.prosecdef,p.provolatile,p.prorettype::pg_catalog.regtype::pg_catalog.text,l.lanname,
               p.prosrc,p.proconfig,pg_catalog.has_function_privilege(p.oid,'EXECUTE')
        FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_catalog.pg_language l ON l.oid=p.prolang
        WHERE n.nspname='public' AND p.proname='app_current_tenant_id' AND p.pronargs=0
    """)
    ).one()
    expected_body = (
        "BEGIN RETURN NULLIF(current_setting('app.current_tenant', true), '')::uuid; "
        "EXCEPTION WHEN invalid_text_representation THEN RETURN NULL; END;"
    )
    if (helper[2], helper[3], helper[4], helper[5], helper[7], helper[8]) != (
        False,
        "s",
        "uuid",
        "plpgsql",
        None,
        True,
    ) or _expression(helper[6]) != _expression(expected_body):
        raise CredentialAssignmentError("bootstrap product tenant helper refused")
    # Deparsed expression shape alone cannot distinguish identically named
    # shadow helpers, custom operators, or references to other columns/tables.
    # Resolve the live canonical tenant column; never assume an attnum.
    tenant_columns = connection.execute(
        text("""
        SELECT c.oid,a.attnum
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid
        WHERE n.nspname='public'
          AND c.relname IN ('academy_account_profiles','academy_credential_policies')
          AND a.attname='tenant_id' AND a.attnum>0 AND NOT a.attisdropped
    """)
    ).all()
    tenant_attnums = dict(tenant_columns)
    if len(tenant_attnums) != 2:
        raise CredentialAssignmentError("bootstrap product tenant columns refused")
    table_oids = {row.relname: row.oid for row in tables}
    dependencies = []
    for policy in policies:
        oid = table_oids[policy[0]]
        observed_dependencies = connection.execute(
            text("""
            SELECT DISTINCT d.refclassid,d.refobjid,d.refobjsubid,d.deptype
            FROM pg_catalog.pg_depend d
            WHERE d.classid='pg_catalog.pg_policy'::pg_catalog.regclass
              AND d.objid=:policy AND d.objsubid=0
        """),
            {"policy": policy[7]},
        ).all()
        catalog_oids = connection.execute(
            text("""
            SELECT 'pg_catalog.pg_class'::pg_catalog.regclass::pg_catalog.oid,
                   'pg_catalog.pg_proc'::pg_catalog.regclass::pg_catalog.oid
        """)
        ).one()
        expected_dependencies = {
            (catalog_oids[0], oid, 0, "a"),
            (catalog_oids[0], oid, tenant_attnums[oid], "n"),
            (catalog_oids[1], helper[0], 0, "n"),
        }
        actual_dependencies = {tuple(row) for row in observed_dependencies}
        if actual_dependencies != expected_dependencies:
            raise CredentialAssignmentError("bootstrap product policy dependencies refused")
        dependencies.append(tuple(sorted(actual_dependencies)))
    return (
        tuple(tuple(row) for row in tables),
        tuple(tuple(row) for row in columns),
        tuple(tuple(tuple(value) if isinstance(value, list) else value for value in row) for row in constraints),
        tuple(tuple(row) for row in policies),
        tuple(helper),
        tuple(dependencies),
    )


@dataclass(frozen=True, slots=True)
class AcademyBootstrapProductPort:
    """Material-free immutable trusted process composition."""

    def open(self, connection, command):
        return _Operation(connection, command)


class _Operation:
    def __init__(self, connection, command):
        self._connection = connection
        self._command = command
        self._configured = _composition(connection)
        self._catalog = _catalog(connection)
        self._person = None
        self._person_created = False
        self._credential_command = None

    @property
    def policy(self):
        return self._configured.policy

    def person_values(self, *, tenant_id, party_id, occurred_at):
        if self._person is not None:
            raise CredentialAssignmentError("bootstrap product person already selected")
        configured = self._configured
        first = configured.first_name
        last = configured.last_name_template.format(tenant_name=self._command.name)
        values = NewPersonValues(
            id=party_id,
            tenant_id=tenant_id,
            email=normalize_email(self._command.owner_email),
            display_name=f"{first} {last}".strip(),
            first_name=first,
            last_name=last,
            is_active=configured.initial.is_active,
            custom_fields={},
            created_at=occurred_at,
            updated_at=occurred_at,
        )
        self._person = values
        return values

    def create_person(self, db, *, values: NewPersonValues):
        if values is not self._person or db.connection() is not self._connection:
            raise CredentialAssignmentError("bootstrap product person binding refused")
        create_person(
            db,
            values=AcademyPersonValues(
                id=values.id,
                tenant_id=values.tenant_id,
                email=values.email,
                display_name=values.display_name,
                first_name=values.first_name,
                last_name=values.last_name,
                is_active=values.is_active,
                custom_fields=values.custom_fields,
                created_at=values.created_at,
                updated_at=values.updated_at,
                profile_status=self._configured.initial.profile_status,
                profile_avatar_path=None,
                profile_prefs={},
            ),
        )
        self._person_created = True

    def validate(self, subject, secret):
        if (
            self._person is None
            or subject.principal_ref != str(self._person.id)
            or subject.application != "dotmac_academy_app"
        ):
            raise CredentialPolicyViolation("bootstrap password subject refused")
        if type(secret) is not str or len(secret) < self._configured.minimum_length:
            raise CredentialPolicyViolation("configured bootstrap password policy refused")

    def create_absent(self, db, command, *, password_hash, reset_required):
        values = self._person
        if (
            values is None
            or self._credential_command is not None
            or reset_required is not False
            or db.connection() is not self._connection
        ):
            raise CredentialAssignmentError("bootstrap product credential binding refused")
        self._bind_subject(command)
        from datetime import UTC, datetime

        _require_material_logging_floor(self._connection)
        _create_absent_rows(
            db,
            credential_id=UUID(command.target_subject.credential_ref),
            tenant_id=values.tenant_id,
            party_id=values.id,
            email=values.email,
            password_hash=password_hash,
            occurred_at=datetime.now(UTC),
        )
        self._credential_command = command
        _require_material_logging_floor(self._connection)
        snapshot = self.observe_credential(self._connection, command)
        if snapshot.password_hash != password_hash:
            raise CredentialAssignmentError("bootstrap product persisted credential differs")
        return snapshot

    def _bind_subject(self, command):
        if (
            self._person is None
            or command.tenant_ref != str(self._person.tenant_id)
            or command.target_subject.principal_ref != str(self._person.id)
            or command.target_subject.application != "dotmac_academy_app"
            or command.target_subject.principal_kind != "person"
        ):
            raise CredentialAssignmentError("bootstrap product credential subject refused")

    def observe_credential(self, connection, command: CredentialCreateAbsentV1):
        self._bind_subject(command)
        if connection is not self._connection or command is not self._credential_command:
            raise CredentialAssignmentError("bootstrap product credential observation refused")
        _require_material_logging_floor(connection)
        # Whole credential cohort; a selected credential cannot hide another one.
        rows = (
            connection.execute(
                text("""
            SELECT c.id,c.tenant_id,c.party_id,c.password_hash,p.credential_id,p.tenant_id AS policy_tenant,
                   p.email,p.failed_login_attempts,p.locked_until,p.lifecycle_version,p.reset_required
            FROM public.user_credentials c LEFT JOIN public.academy_credential_policies p ON p.credential_id=c.id
            WHERE c.party_id=:party OR c.id=:credential ORDER BY c.id
        """),
                {
                    "party": cast(NewPersonValues, self._person).id,
                    "credential": UUID(command.target_subject.credential_ref),
                },
            )
            .mappings()
            .all()
        )
        if len(rows) != 1:
            raise CredentialAssignmentError("bootstrap product credential cohort differs")
        row = rows[0]
        if (
            row["id"] != UUID(command.target_subject.credential_ref)
            or row["tenant_id"] != cast(NewPersonValues, self._person).tenant_id
            or row["party_id"] != cast(NewPersonValues, self._person).id
            or row["credential_id"] != row["id"]
            or row["policy_tenant"] != cast(NewPersonValues, self._person).tenant_id
            or row["email"] != cast(NewPersonValues, self._person).email
            or row["failed_login_attempts"] != 0
            or row["locked_until"] is not None
            or type(row["lifecycle_version"]) is not int
            or row["lifecycle_version"] != 0
            or row["reset_required"] is not False
        ):
            raise CredentialAssignmentError("bootstrap product credential policy differs")
        return CredentialSnapshot(
            command.target_subject,
            row["password_hash"],
            0,
            is_active=cast(NewPersonValues, self._person).is_active,
            is_locked=False,
            reset_required=False,
        )

    def revalidate(self, connection):
        if (
            connection is not self._connection
            or _composition(connection) != self._configured
            or _catalog(connection) != self._catalog
        ):
            raise CredentialAssignmentError("bootstrap product composition changed")
        values = self._person
        if not self._person_created:
            return
        if values is None:
            raise CredentialAssignmentError("bootstrap product person missing")
        table = AcademyAccountProfile.__table__
        rows = connection.execute(select(table).where(table.c.party_id == values.id)).mappings().all()
        if len(rows) != 1:
            raise CredentialAssignmentError("bootstrap product profile missing")
        row = rows[0]
        if (
            row["tenant_id"] != values.tenant_id
            or row["status"] != self._configured.initial.profile_status
            or row["avatar_path"] is not None
            or row["prefs"] != {}
            or row["created_at"] != values.created_at
            or row["updated_at"] != values.updated_at
        ):
            raise CredentialAssignmentError("bootstrap product profile changed")
