"""Connection-only genuine product entry; hosted effects AUTHORED NOT RUN.

The caller owns the resource and sole root. No engine, transaction, registry
installation, or owner replacement is provided by this preparation helper.
"""

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from dotmac_kernel import audit_actions, security
from dotmac_kernel.migrations.identity_history import (
    IdentityHistoryCohortV1,
    IdentityHistoryImportOperationV1,
    IdentityHistoryManifestV1,
    IdentityHistoryRelationsV1,
)
from sqlalchemy.sql.elements import quoted_name

from alembic import command
from tests.fixtures.kernel_identity_entry.rehearsal import (
    COLLISIONS,
    COMPATIBILITY_SCHEMA,
    composed_config_for,
    installed_kernel,
    relocate,
)

ASSEMBLY = Path(__file__).resolve().parents[2] / "alembic/identity_assembly_versions"
KINDS = ("password_reset", "invite", "email_verify", "ical_feed")


def _synthetic_source_setup(connection):
    # Synthetic setup precedes locked copy. Only genuine owner verification
    # authorizes fixture token minting; no real password/session is collected.
    password = "hosted-academy-identity-canary-only"
    material = security.hash_password(password)
    people = connection.execute(sa.text("SELECT id,tenant_id FROM public.people ORDER BY id")).all()
    for person, tenant in people:
        assert security.verify_password(password, material) is True
        token, expiry = security.issue_access_token(person, tenant)
        connection.execute(
            sa.text("UPDATE public.auth_sessions SET token_hash=:token,expires_at=:expiry WHERE person_id=:person"),
            {"token": security.hash_token(token), "expiry": expiry, "person": person},
        )
    connection.execute(
        sa.text(
            "UPDATE public.user_credentials SET "
            "password_hash=:material,failed_login_attempts=-3,locked_until=now()+interval '1 hour'"
        ),
        {"material": material},
    )
    connection.execute(
        sa.text("UPDATE public.people SET status='suspended',avatar_path='synthetic/avatar',prefs='null'::jsonb")
    )
    connection.execute(sa.text("UPDATE public.audit_events SET details='null'::jsonb"))
    connection.execute(
        sa.text("UPDATE public.user_credentials SET email='Alias@EXAMPLE.invalid' WHERE email='alias@example.invalid'")
    )


def _synthetic_tokens(connection):
    for person, tenant in connection.execute(sa.text("SELECT id,tenant_id FROM public.people ORDER BY id")):
        for kind in KINDS:
            for state in ("future", "expired", "used"):
                identifier = uuid4()
                connection.execute(
                    sa.text(
                        "INSERT INTO public.account_tokens(id,tenant_id,person_id,kind,token_hash,expires_at,used_at) "
                        "VALUES (:id,:tenant,:person,:kind,:material, "
                        "CASE WHEN :state='expired' THEN now()-interval '1 hour' ELSE now()+interval '1 hour' END, "
                        "CASE WHEN :state='used' THEN now()-interval '2 hours' ELSE NULL END)"
                    ),
                    {
                        "id": identifier,
                        "tenant": tenant,
                        "person": person,
                        "kind": kind,
                        "material": security.hash_token("synthetic-hosted-account-token-" + str(identifier)),
                        "state": state,
                    },
                )


# Test expectations are private to the caller-owned Connection and never SQL
# relations: creating any TEMP relation invalidates the genuine Kernel gate.
_ORACLE_KEY = object()


@dataclass(frozen=True, slots=True, repr=False)
class _ExpectedValues:
    token_times: tuple[tuple[object, str | None], ...]
    applicant_values: tuple[tuple[object, str | None, str | None, str | None, str | None], ...]


def create_token_oracle(connection):
    # Capture BEFORE relocation/import. Only immutable server text enters the
    # buffer; SQL NULL stays None, JSON null stays "null", numeric/timestamp
    # spellings (including -0 and infinity) never pass through float/JSON decode.
    assert _ORACLE_KEY not in connection.info
    connection.info[_ORACLE_KEY] = _ExpectedValues(
        token_times=tuple(
            tuple(row)
            for row in connection.execute(sa.text("SELECT id,updated_at::text FROM public.account_tokens ORDER BY id"))
        ),
        applicant_values=tuple(
            tuple(row)
            for row in connection.execute(
                sa.text(
                    "SELECT id,notes,assessment_profile::text,assessment_score::text,assessment_taken_at::text "
                    "FROM public.applicants ORDER BY id"
                )
            )
        ),
    )


def restore_token_oracle(connection):
    expected = connection.info[_ORACLE_KEY]
    for identifier, updated_at in expected.token_times:
        connection.execute(
            sa.text("UPDATE public.account_tokens SET updated_at=CAST(:value AS timestamptz) WHERE id=:id"),
            {"id": identifier, "value": updated_at},
        )


def token_rows_match(connection, issued):
    # Return only a boolean so pytest cannot render token fingerprints in a diff.
    from app.maintenance import account_token_identity as tokens

    return tokens._row_fingerprints(connection) == issued.row_fingerprints


def restore_retained_oracle(connection):
    expected = connection.info[_ORACLE_KEY]
    for identifier, notes, profile, score, taken_at in expected.applicant_values:
        connection.execute(
            sa.text(
                "UPDATE public.applicants SET notes=:notes,assessment_profile=CAST(:profile AS jsonb), "
                "assessment_score=CAST(:score AS float8),assessment_taken_at=CAST(:taken_at AS timestamptz) "
                "WHERE id=:id"
            ),
            {"id": identifier, "notes": notes, "profile": profile, "score": score, "taken_at": taken_at},
        )


def clear_value_oracle(connection):
    connection.info.pop(_ORACLE_KEY, None)


def prepare_relocated_identity(connection, plan, tmp_path):
    registry = audit_actions.active_audit_actions()
    registry.require("identity.history_imported")
    assert registry.owner("identity.history_imported") == "kernel"
    relocate(connection, plan)
    kernel = installed_kernel(connection, tmp_path / "inspect")
    assert any(r.revision == "0030_credential_tenant_key" for r in kernel.revisions)
    applied = []
    cfg = composed_config_for(connection, tmp_path / "composed", applied)

    # Select explicit stages with the COMPLETE composed graph present.
    command.upgrade(cfg, kernel.head)
    assert tuple(applied) == kernel.revisions
    composed_start = len(applied)
    command.upgrade(cfg, "a002_credential_reset_state")
    assert [r.revision for r in applied[composed_start:]] == ["a001_identity_extensions", "a002_credential_reset_state"]
    relations = {}
    cohort = {}
    for name in COLLISIONS:
        relations[name] = connection.scalar(
            sa.text("SELECT to_regclass(:relation)::oid"),
            {"relation": COMPATIBILITY_SCHEMA + "." + name},
        )
        # Closed fixture relation allowlist; no credential material fetched.
        source = sa.table(quoted_name(name, True), sa.column("id", sa.Uuid), schema=COMPATIBILITY_SCHEMA)
        cohort[name] = frozenset(connection.scalars(sa.select(source.c.id)))
    expected = IdentityHistoryManifestV1(
        relations=IdentityHistoryRelationsV1(schema=COMPATIBILITY_SCHEMA, **relations),
        cohort=IdentityHistoryCohortV1(**cohort),
        source_revision="a" * 40,
        session_issuance_review_ref="synthetic-owner-verified-population",
        password_session_ids=cohort["auth_sessions"],
        installed_kernel_head=kernel.head,
    )
    operation = IdentityHistoryImportOperationV1(
        is_active=True,
        activated_person_ids=cohort["people"],
        reason="Synthetic copy canary",
        decision_ref="synthetic-hosted-copy",
        actor_type="service",
        actor_id="academy-maintenance-canary",
        source_application="dotmac_academy_app",
    )
    return expected, operation


def _populate_retained(connection):
    # Real frozen schema, native server payloads; no credentials enter Python.
    tenant, person, course = connection.execute(
        sa.text("""
        SELECT p.tenant_id,p.id,c.id FROM public.people p
        JOIN public.courses c ON c.tenant_id=p.tenant_id ORDER BY p.id LIMIT 1
    """)
    ).one()
    params = {"tenant": tenant, "person": person, "course": course}
    for statement in (
        "INSERT INTO public.applicants(id,tenant_id,email,first_name,last_name) "
        "VALUES(gen_random_uuid(),:tenant,'digest@example.invalid','Digest','Canary')",
        "INSERT INTO public.email_outbox(id,tenant_id,idempotency_key,recipient,subject,html_body) "
        "VALUES(gen_random_uuid(),:tenant,'digest','digest@example.invalid','Digest','<p>Digest</p>')",
        "INSERT INTO public.lab_instances(id,tenant_id,activity_id,person_id,instance_name) "
        "VALUES(gen_random_uuid(),:tenant,gen_random_uuid(),:person,'digest')",
        "INSERT INTO public.lab_operations(id,tenant_id,instance_id,kind) "
        "SELECT gen_random_uuid(),tenant_id,id,'check' FROM public.lab_instances WHERE tenant_id=:tenant",
        "INSERT INTO public.lab_templates(id,tenant_id,course_id,activity_id,slug,title) "
        "VALUES(gen_random_uuid(),:tenant,:course,gen_random_uuid(),'digest','Digest')",
        "INSERT INTO public.notifications(id,tenant_id,person_id,kind,title) "
        "VALUES(gen_random_uuid(),:tenant,:person,'digest','Digest')",
        "INSERT INTO public.onboarding_tasks(id,tenant_id,applicant_id,key,label) "
        "SELECT gen_random_uuid(),tenant_id,id,'digest','Digest' FROM public.applicants WHERE tenant_id=:tenant",
        "INSERT INTO public.platform_settings(key,value) VALUES('digest-canary',NULL)",
        "INSERT INTO public.reminder_log(id,tenant_id,person_id,event_kind,occurrence_key) "
        "VALUES(gen_random_uuid(),:tenant,:person,'digest','digest')",
        "INSERT INTO public.reminder_preferences(id,tenant_id,person_id) " "VALUES(gen_random_uuid(),:tenant,:person)",
        "INSERT INTO public.tenant_entrance_defaults(tenant_id) VALUES(:tenant)",
    ):
        connection.execute(sa.text(statement), params)
