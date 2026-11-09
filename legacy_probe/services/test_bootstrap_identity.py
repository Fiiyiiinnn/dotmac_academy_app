"""Genuine installed-owner PostgreSQL canaries; authored NOT RUN.

Requires canonical migrations and real configured offline role pair. No fake
privileged actor, replacement runtime, schema or owner proof is installed here.
"""

import os
from uuid import uuid4

import pytest
from dotmac_kernel.credential_lifecycle import CredentialAssignmentError
from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, Role, Tenant, UserCredential
from dotmac_kernel.security import verify_password
from dotmac_kernel.session_runtime import RuntimeBindingError
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database_runtime import bind_academy_database_runtime
from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services.bootstrap import bootstrap_tenant
from app.services.bootstrap_identity import _catalog

# The CI/test deployment's actual offline role pair, declared per fixture only (never in the job
# environment, so negative tests keep the empty deny-all default). It is hard-coded, never derived
# from the observed session_user, and is asserted against the real observed pair below.
OFFLINE_ROLE_PAIR = ("platform_api", "platform_api")

INSUFFICIENT_PRIVILEGE = "42501"
BOOTSTRAP_DATABASE = "academy_bootstrap_identity"

# Every refusal message raised inside app.services.bootstrap_identity._catalog.
_CATALOGUE_REFUSAL = (
    r"bootstrap product (catalogue|columns|constraint enforcement|foreign key actions|"
    r"identity constraints|tenant policy|tenant helper|tenant columns|policy dependencies) refused"
)


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


def _assert_privilege_refused(caught) -> None:
    """The refusal must be PostgreSQL insufficient_privilege, not any error."""
    assert isinstance(caught.value, DBAPIError), type(caught.value).__name__
    assert _sqlstate(caught.value) == INSUFFICIENT_PRIVILEGE


@pytest.fixture
def offline_runtime(monkeypatch):
    monkeypatch.setattr(settings, "bootstrap_allowed_database_role_pairs", (OFFLINE_ROLE_PAIR,))
    runtime = bind_academy_database_runtime()
    with runtime.platform_session() as db:
        assert db.get_bind().dialect.name == "postgresql"
        # These tests commit tenants. They run only against their dedicated
        # database (own pytest process in CI), never the shared application one.
        assert db.scalar(text("SELECT current_database()")) == BOOTSTRAP_DATABASE
        pair = db.execute(text("SELECT session_user, current_user")).one()
        # Deployment must deliberately supply this actual role pair. Tests do
        # not invent a privileged boolean or infer authority from a role name.
        from app.services.settings_store import effective

        configured = effective(db, require_store=True)
        assert tuple(pair) in configured.bootstrap_allowed_database_role_pairs
    return runtime


def test_bootstrap_real_canonical_extension_and_password(offline_runtime):
    slug = "bootstrap-" + uuid4().hex
    with offline_runtime.platform_session() as db:
        result = bootstrap_tenant(db, slug=slug, name="Bootstrap Tenant", admin_email=slug + "@example.test",
                                  admin_password="hosted-bootstrap-secret-923!")
        tid, pid = result.id, result.owner_party_id
    with offline_runtime.platform_session() as db:
        offline_runtime.set_tenant(db, tid, transaction_local=True)
        from app.services.settings_store import effective

        policy = effective(db, require_store=True)
        tenant = db.get(Tenant, tid)
        party = db.get(Party, pid)
        person = db.get(PartyPerson, pid)
        profile = db.get(AcademyAccountProfile, pid)
        credential = db.scalars(select(UserCredential).where(UserCredential.party_id == pid)).one()
        extension = db.get(AcademyCredentialPolicy, credential.id)
        grant = db.scalars(select(PartyRoleGrant).where(PartyRoleGrant.party_id == pid)).one()
        role = db.get(Role, grant.role_id)
        assert tenant.slug == slug
        assert tenant.is_active is policy.bootstrap_tenant_is_active
        assert party.is_active is policy.account_initial_is_active
        assert profile.status == policy.account_initial_profile_status
        assert person.first_name == policy.bootstrap_owner_first_name
        assert person.last_name == policy.bootstrap_owner_last_name_template.format(tenant_name=tenant.name)
        assert role.slug == policy.bootstrap_initial_role_slug
        assert verify_password("hosted-bootstrap-secret-923!", credential.password_hash)
        assert extension.tenant_id == tenant.id
        assert extension.email == party.email
        assert (extension.reset_required, extension.lifecycle_version, extension.failed_login_attempts) == (False, 0, 0)
        assert extension.locked_until is None
        events = db.execute(text("SELECT actor_type, actor_id, actor_party_id, source_application FROM public.audit_events "
                                 "WHERE entity_id = :id"), {"id": str(credential.id)}).all()
        assert events
        assert all(row.actor_type == "system" and row.actor_party_id is None for row in events)
        assert all(row.source_application == "dotmac_academy_app" for row in events)
        assert all(row.actor_id and row.actor_id != str(pid) for row in events)


def test_ordinary_session_cannot_bootstrap(app_user_session):
    with pytest.raises((ValueError, RuntimeBindingError, CredentialAssignmentError)):
        bootstrap_tenant(app_user_session, slug="deny-" + uuid4().hex, name="Denied",
                         admin_email="denied@example.test", admin_password="hosted-secret-923!")


def test_post_body_settings_change_poisoned_at_commit(offline_runtime):
    slug = "stale-" + uuid4().hex
    with pytest.raises((ValueError, RuntimeBindingError, CredentialAssignmentError)):
        with offline_runtime.platform_session() as db:
            bootstrap_tenant(db, slug=slug, name="Late Denial", admin_email=slug + "@example.test",
                             admin_password="hosted-bootstrap-secret-923!")
            db.execute(text("INSERT INTO public.platform_settings (key,value) VALUES "
                            "('bootstrap_allowed_database_role_pairs','[]') ON CONFLICT (key) DO UPDATE SET value='[]'"))
    with offline_runtime.platform_session() as db:
        assert db.scalar(select(Tenant.id).where(Tenant.slug == slug)) is None


def test_physical_precommit_product_change_is_core_revalidated(offline_runtime, monkeypatch):
    from sqlalchemy import event

    slug = "precommit-" + uuid4().hex
    changed = []
    prepared = []
    session = []

    def alter(connection):
        # Actual Connection.commit boundary, while the Session is PREPARED.
        assert session[0].get_transaction()._state.name == 'PREPARED'
        prepared.append(True)
        connection.execute(text("UPDATE public.academy_account_profiles SET status='changed' WHERE party_id=:pid"),
                           {"pid": changed[0]})

    with pytest.raises((ValueError, RuntimeBindingError, CredentialAssignmentError)):
        with offline_runtime.platform_session() as db:
            session.append(db)
            execute = db.execute

            def no_prepared_orm(*args, **kwargs):
                assert db.get_transaction()._state.name != 'PREPARED'
                return execute(*args, **kwargs)

            monkeypatch.setattr(db, 'execute', no_prepared_orm)
            event.listen(db.connection(), "commit", alter, once=True)
            result = bootstrap_tenant(db, slug=slug, name="Precommit", admin_email=slug + "@example.test",
                                      admin_password="hosted-bootstrap-secret-923!")
            changed.append(result.owner_party_id)
    assert changed
    assert prepared == [True]


@pytest.mark.parametrize('statement', [
    "UPDATE public.roles SET name='drifted' WHERE tenant_id=:tid",
    "UPDATE public.parties SET display_name='drifted' WHERE id=:pid",
    "UPDATE public.party_persons SET first_name='drifted' WHERE party_id=:pid",
    "UPDATE public.academy_account_profiles SET prefs='{\"drifted\"\\:true}'::jsonb WHERE party_id=:pid",
    "UPDATE public.academy_credential_policies SET reset_required=true WHERE tenant_id=:tid",
    "UPDATE public.academy_credential_policies SET lifecycle_version=1 WHERE tenant_id=:tid",
    "DELETE FROM public.party_role_grants WHERE party_id=:pid",
])
def test_post_body_actual_row_drift_refuses(offline_runtime, statement):
    slug = 'row-drift-' + uuid4().hex
    with pytest.raises((ValueError, RuntimeBindingError, CredentialAssignmentError)):
        with offline_runtime.platform_session() as db:
            result = bootstrap_tenant(db, slug=slug, name='Row drift', admin_email=slug+'@example.test',
                                      admin_password='hosted-bootstrap-secret-923!')
            db.connection().execute(text(statement), {'tid':result.id, 'pid':result.owner_party_id})
    with offline_runtime.platform_session() as db:
        assert db.scalar(select(Tenant.id).where(Tenant.slug == slug)) is None


def test_whole_credential_cohort_growth_refused(offline_runtime):
    slug = 'cohort-' + uuid4().hex
    with pytest.raises((ValueError, RuntimeBindingError, CredentialAssignmentError)):
        with offline_runtime.platform_session() as db:
            result = bootstrap_tenant(db, slug=slug, name='Cohort', admin_email=slug+'@example.test',
                                      admin_password='hosted-bootstrap-secret-923!')
            # Real second canonical row, retaining the existing valid hash
            # wholly inside PostgreSQL; no hash enters test receipts/output.
            db.connection().execute(text("""
                INSERT INTO public.user_credentials (id,tenant_id,party_id,password_hash,created_at,updated_at)
                SELECT :id,tenant_id,party_id,password_hash,created_at,updated_at
                FROM public.user_credentials WHERE party_id=:party
            """), {'id':uuid4(), 'party':result.owner_party_id})
    with offline_runtime.platform_session() as db:
        assert db.scalar(select(Tenant.id).where(Tenant.slug == slug)) is None


@pytest.mark.parametrize('phase', ['tenant', 'party', 'person', 'profile', 'role', 'grant', 'credential', 'policy', 'audit'])
def test_actual_write_cancellation_recovers_savepoint_then_poisoned_commit(offline_runtime, monkeypatch, phase):
    from dotmac_kernel.audit import AuditEvent

    from app.models.platform_settings import PlatformSetting

    types = {'tenant':Tenant, 'party':Party, 'person':PartyPerson, 'profile':AcademyAccountProfile,
             'role':Role, 'grant':PartyRoleGrant, 'credential':UserCredential, 'policy':AcademyCredentialPolicy,
             'audit':AuditEvent}
    marker = 'bootstrap-prior-' + uuid4().hex
    slug = 'cancel-' + uuid4().hex

    class Cancel(BaseException):
        pass

    cancellation = Cancel('cancelled bootstrap test')
    recovered = []
    with pytest.raises(RuntimeBindingError):
        with offline_runtime.platform_session() as db:
            db.add(PlatformSetting(key=marker,value='prior flushed work'))
            db.flush()
            root = db.get_transaction()
            flush = db.flush

            def cancel(*args, **kwargs):
                selected = any(isinstance(row,types[phase]) for row in db.new)
                result = flush(*args, **kwargs)
                if selected:
                    raise cancellation
                return result

            monkeypatch.setattr(db, 'flush', cancel)
            with pytest.raises(Cancel) as caught:
                bootstrap_tenant(db, slug=slug, name='Cancellation', admin_email=slug+'@example.test',
                                 admin_password='hosted-bootstrap-secret-923!')
            monkeypatch.setattr(db, 'flush', flush)
            assert caught.value is cancellation
            assert db.get_transaction() is root and root.is_active
            assert db.get_nested_transaction() is None
            assert db.scalar(select(PlatformSetting.value).where(PlatformSetting.key == marker)) == 'prior flushed work'
            assert db.scalar(select(Tenant.id).where(Tenant.slug == slug)) is None
            recovered.append(True)
    assert recovered == [True]


def test_duplicate_slug_uses_native_collision_and_rolls_back_new_owner(offline_runtime):
    from sqlalchemy.exc import IntegrityError

    slug = 'duplicate-' + uuid4().hex
    with offline_runtime.platform_session() as db:
        result = bootstrap_tenant(db, slug=slug, name='Original', admin_email=slug+'@example.test',
                                  admin_password='hosted-bootstrap-secret-923!')
        original = result.id
    with pytest.raises((IntegrityError, RuntimeBindingError)):
        with offline_runtime.platform_session() as db:
            bootstrap_tenant(db, slug=slug, name='Duplicate', admin_email=slug+'2@example.test',
                             admin_password='hosted-bootstrap-secret-923!')
    with offline_runtime.platform_session() as db:
        assert db.scalar(select(Tenant.id).where(Tenant.slug == slug)) == original


def test_cli_announces_only_committed_bootstrap(offline_runtime, capsys):
    from argparse import Namespace

    from app.cli import _bootstrap

    slug = 'cli-' + uuid4().hex
    _bootstrap(Namespace(slug=slug,name='CLI',admin_email=slug+'@example.test',
                         admin_password='hosted-bootstrap-secret-923!'))
    message = capsys.readouterr().out
    with offline_runtime.platform_session() as db:
        tenant = db.scalar(select(Tenant).where(Tenant.slug == slug))
        assert tenant is not None
        assert message == f'tenant {slug} ({tenant.id}) created\n'


def test_cli_does_not_announce_on_real_commit_denial(offline_runtime, capsys, monkeypatch):
    from argparse import Namespace

    from app import cli
    from app.services import bootstrap as facade

    actual = facade.bootstrap_tenant

    def changed(db, **kwargs):
        result = actual(db, **kwargs)
        db.connection().execute(text("UPDATE public.academy_account_profiles SET status='late' WHERE party_id=:pid"),
                                {'pid':result.owner_party_id})
        return result

    monkeypatch.setattr(facade, 'bootstrap_tenant', changed)
    slug = 'cli-denied-' + uuid4().hex
    with pytest.raises((ValueError, RuntimeBindingError, CredentialAssignmentError)):
        cli._bootstrap(Namespace(slug=slug,name='CLI denial',admin_email=slug+'@example.test',
                                 admin_password='hosted-bootstrap-secret-923!'))
    assert capsys.readouterr().out == ''


def test_configured_custom_roles_status_activation_name_password_commit(offline_runtime):
    from app.models.platform_settings import PlatformSetting
    from app.services.settings_store import set_many

    values = {'bootstrap_role_provisioning':'{"founder":"Founding Owner","member":"Member"}',
              'bootstrap_initial_role_slug':'founder', 'bootstrap_tenant_is_active':'false',
              'account_initial_is_active':'false', 'account_initial_profile_status':'custom_review',
              'bootstrap_owner_first_name':'Founder', 'bootstrap_owner_last_name_template':'Team {tenant_name}',
              'bootstrap_password_min_length':'23'}
    slug = 'custom-' + uuid4().hex
    with offline_runtime.platform_session() as db:
        original = dict(db.execute(select(PlatformSetting.key,PlatformSetting.value)
                                   .where(PlatformSetting.key.in_(tuple(values)))).all())
    try:
        with offline_runtime.platform_session() as db:
            set_many(db, values)
            result = bootstrap_tenant(db,slug=slug,name='Configured',admin_email=slug+'@example.test',
                                      admin_password='configured-hosted-secret-923!')
            tid,pid = result.id,result.owner_party_id
        with offline_runtime.platform_session() as db:
            offline_runtime.set_tenant(db,tid,transaction_local=True)
            assert db.get(Tenant,tid).is_active is False
            assert db.get(Party,pid).is_active is False
            assert db.get(PartyPerson,pid).first_name == 'Founder'
            assert db.get(PartyPerson,pid).last_name == 'Team Configured'
            assert db.get(AcademyAccountProfile,pid).status == 'custom_review'
            assert set(db.execute(select(Role.slug,Role.name).where(Role.tenant_id == tid)).all()) == {
                ('founder','Founding Owner'),('member','Member')}
            grant = db.scalar(select(PartyRoleGrant).where(PartyRoleGrant.party_id == pid))
            assert db.get(Role,grant.role_id).slug == 'founder'
            credential = db.scalar(select(UserCredential).where(UserCredential.party_id == pid))
            assert verify_password('configured-hosted-secret-923!',credential.password_hash)
        with pytest.raises((ValueError,CredentialAssignmentError)):
            with offline_runtime.platform_session() as db:
                bootstrap_tenant(db,slug='short-'+uuid4().hex,name='Short',admin_email=uuid4().hex+'@example.test',
                                 admin_password='short')
    finally:
        with offline_runtime.platform_session() as db:
            table = PlatformSetting.__table__
            db.execute(table.delete().where(table.c.key.in_(tuple(values))))
            if original:
                db.execute(table.insert(),[{'key':key,'value':value} for key,value in original.items()])


@pytest.mark.parametrize('when', ['before','after'])
@pytest.mark.parametrize('statement', [
    "ALTER TABLE public.academy_account_profiles NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE public.academy_credential_policies DROP CONSTRAINT uq_academy_policy_tenant_email",
    "ALTER TABLE public.academy_credential_policies DROP CONSTRAINT fk_academy_policy_tenant_credential",
    "ALTER TABLE public.academy_account_profiles DROP CONSTRAINT fk_academy_account_person",
    "ALTER POLICY academy_account_profiles_tenant_isolation ON public.academy_account_profiles USING (true)",
])
def test_offline_role_cannot_mutate_product_catalogue(offline_runtime, statement, when):
    """The offline role does not own the product catalogue: DDL on it is refused.

    This proves the offline role cannot perform the mutation at all. Detection
    of injected drift by the bootstrap catalogue preflight is covered separately
    by ``test_owner_injected_catalogue_drift_is_refused_by_the_preflight`` below,
    which injects the drift as the catalogue owner.
    """
    slug = 'catalogue-' + uuid4().hex
    reached = False
    with pytest.raises(DBAPIError) as caught:
        with offline_runtime.platform_session() as db:
            if when == 'before':
                db.connection().execute(text(statement))
            bootstrap_tenant(db,slug=slug,name='Catalogue drift',admin_email=slug+'@example.test',
                             admin_password='hosted-bootstrap-secret-923!')
            if when == 'after':
                reached = True
                db.connection().execute(text(statement))
    if when == 'after':
        assert reached, 'bootstrap itself failed before the refused statement'
    _assert_privilege_refused(caught)
    with offline_runtime.platform_session() as db:
        assert db.scalar(select(Tenant.id).where(Tenant.slug == slug)) is None


@pytest.fixture
def owner_catalogue_connection():
    """Catalogue-owner connection whose every change is rolled back.

    Uses the migration authority (the owner of the product catalogue in the
    composed CI) on an engine that satisfies the bootstrap logging floor, inside
    an outer transaction with a savepoint. ``bootstrap_tenant`` itself is not
    run here: it refuses any session not issued by ``platform_session``, so the
    catalogue preflight ``_catalog`` is exercised directly.
    """
    url = os.getenv("TEST_MIGRATION_DATABASE_URL") or os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL not set — these tests require a real Postgres")
    engine = create_engine(url, hide_parameters=True, echo=False, poolclass=NullPool)
    connection = engine.connect()
    outer = connection.begin()
    try:
        connection.begin_nested()
        yield connection
    finally:
        outer.rollback()
        connection.close()
        engine.dispose()


def test_owner_catalogue_preflight_accepts_the_undrifted_catalogue(owner_catalogue_connection):
    """Control: the harness sees the real catalogue, so the refusals below are the drift's."""
    _catalog(owner_catalogue_connection)


@pytest.mark.parametrize('statement', [
    "ALTER TABLE public.academy_account_profiles NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE public.academy_credential_policies DROP CONSTRAINT uq_academy_policy_tenant_email",
    "ALTER TABLE public.academy_credential_policies DROP CONSTRAINT fk_academy_policy_tenant_credential",
    "ALTER TABLE public.academy_account_profiles DROP CONSTRAINT fk_academy_account_person",
    "ALTER POLICY academy_account_profiles_tenant_isolation ON public.academy_account_profiles USING (true)",
])
def test_owner_injected_catalogue_drift_is_refused_by_the_preflight(owner_catalogue_connection, statement):
    """Drift injected by the catalogue owner (rolled back) is refused by ``_catalog``."""
    owner_catalogue_connection.execute(text(statement))
    with pytest.raises(CredentialAssignmentError, match=_CATALOGUE_REFUSAL):
        _catalog(owner_catalogue_connection)


def test_offline_role_cannot_update_audit_events(offline_runtime):
    slug = 'audit-immutable-' + uuid4().hex
    reached = False
    with pytest.raises(DBAPIError) as caught:
        with offline_runtime.platform_session() as db:
            result = bootstrap_tenant(db, slug=slug, name='Audit immutable', admin_email=slug+'@example.test',
                                      admin_password='hosted-bootstrap-secret-923!')
            reached = True
            db.connection().execute(text("UPDATE public.audit_events SET actor_id='drifted' WHERE tenant_id=:tid"),
                                    {'tid': result.id})
    assert reached, 'bootstrap itself failed before the refused UPDATE'
    _assert_privilege_refused(caught)
    with offline_runtime.platform_session() as db:
        assert db.scalar(select(Tenant.id).where(Tenant.slug == slug)) is None


def test_actual_bootstrap_damaged_physical_root_retires_and_preserves_cancel(offline_runtime, monkeypatch):
    slug = 'custody-' + uuid4().hex

    class Cancel(BaseException):
        pass

    cancellation = Cancel('damaged custody cancellation')
    damaged = []
    with pytest.raises(Cancel) as caught:
        with offline_runtime.platform_session() as db:
            flush = db.flush

            def damage(*args, **kwargs):
                selected = any(isinstance(row,AcademyAccountProfile) for row in db.new)
                result = flush(*args, **kwargs)
                if selected:
                    physical = db.connection().get_transaction()
                    physical.rollback()
                    damaged.append(physical)
                    raise cancellation
                return result

            monkeypatch.setattr(db,'flush',damage)
            bootstrap_tenant(db,slug=slug,name='Damaged custody',admin_email=slug+'@example.test',
                             admin_password='hosted-bootstrap-secret-923!')
    assert caught.value is cancellation
    assert len(damaged) == 1 and damaged[0].is_active is False
    with offline_runtime.platform_session() as db:
        assert db.scalar(select(Tenant.id).where(Tenant.slug == slug)) is None


def test_offline_role_cannot_create_shadow_helper_schema(offline_runtime):
    """A shadow policy-helper schema needs CREATE on the database, which the role lacks."""
    shadow = 'bootstrap_shadow_' + uuid4().hex
    with pytest.raises(DBAPIError) as caught:
        with offline_runtime.platform_session() as db:
            db.connection().execute(text(f'CREATE SCHEMA {shadow}'))
    _assert_privilege_refused(caught)


def test_temporary_fake_catalog_is_ignored_by_canonical_bootstrap(offline_runtime):
    slug = 'fake-catalog-' + uuid4().hex
    with offline_runtime.platform_session() as db:
        db.connection().execute(text('CREATE TEMP TABLE pg_class (relname pg_catalog.text) ON COMMIT DROP'))
        db.connection().execute(text('SET LOCAL search_path=pg_temp,pg_catalog,public'))
        result = bootstrap_tenant(db, slug=slug, name='Fake catalog', admin_email=slug+'@example.test',
                                  admin_password='hosted-bootstrap-secret-923!')
        tenant_id = result.id
    with offline_runtime.platform_session() as db:
        assert db.scalar(select(Tenant.id).where(Tenant.slug == slug)) == tenant_id


@pytest.mark.parametrize('mutation', ['parameters', 'echo', 'debug'])
def test_logging_mutation_after_shared_constructor_denies_before_material_readback(offline_runtime, monkeypatch, mutation):
    import logging

    from app.services import bootstrap_identity

    actual = bootstrap_identity._create_absent_rows
    slug = 'logging-drift-' + uuid4().hex
    mutated = []
    engine = offline_runtime.platform_engine
    logger = getattr(engine.logger, 'logger', engine.logger)
    prior = engine.hide_parameters, engine.echo, logger.level

    def mutate(*args, **kwargs):
        result = actual(*args, **kwargs)
        if mutation == 'parameters':
            engine.hide_parameters = False
        elif mutation == 'echo':
            engine.echo = True
        else:
            logger.setLevel(logging.DEBUG)
        mutated.append(True)
        return result

    monkeypatch.setattr(bootstrap_identity, '_create_absent_rows', mutate)
    try:
        # The kernel re-raises the product refusal as its own wrapped message; the
        # product guard must still be the exception's context.
        with pytest.raises(CredentialAssignmentError, match='credential assignment failed; rollback required') as caught:
            with offline_runtime.platform_session() as db:
                try:
                    bootstrap_tenant(db, slug=slug, name='Logging drift', admin_email=slug+'@example.test',
                                     admin_password='hosted-bootstrap-secret-923!')
                finally:
                    # Restore before rollback/cleanup; never capture SQL or hashes.
                    engine.hide_parameters, engine.echo = prior[:2]
                    logger.setLevel(prior[2])
    finally:
        engine.hide_parameters, engine.echo = prior[:2]
        logger.setLevel(prior[2])
    assert mutated == [True]
    cause = caught.value.__context__
    assert isinstance(cause, CredentialAssignmentError)
    assert 'logging configuration refused' in str(cause)
    with offline_runtime.platform_session() as db:
        assert db.scalar(select(Tenant.id).where(Tenant.slug == slug)) is None


def test_offline_role_cannot_create_schema_for_settings_shadowing(offline_runtime):
    shadow = 'bootstrap_settings_' + uuid4().hex
    with pytest.raises(DBAPIError) as caught:
        with offline_runtime.platform_session() as db:
            db.connection().execute(text(f'CREATE SCHEMA {shadow}'))
    _assert_privilege_refused(caught)
