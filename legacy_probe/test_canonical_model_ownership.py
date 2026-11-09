"""Canonical ownership checks authored for hosted CI; unexecuted locally."""

import ast
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEARNING_FACADES = {
    "attempt.py": {"attempt_models": ["ActivityAttempt", "AttemptGrant"]},
    "assessment.py": {
        "curriculum_models": ["QuestionBank", "Question"],
        "models": ["Activity", "Submission", "Score", "ASSESSMENT_MODES"],
    },
    "certificate.py": {"models": ["Certificate"]},
    "cohort.py": {"participation_models": ["Cohort", "Enrollment"]},
    "course.py": {"models": ["Course"], "curriculum_models": ["Chapter"]},
    "completion.py": {"models": ["CourseCompletion"]},
    "track.py": {"participation_models": ["Track", "TrackCourse", "CohortTrack"]},
    "reading.py": {"curriculum_models": ["ChapterRead", "SubtopicRead"]},
    "success_queue.py": {
        "delivery_models": [
            "SuccessQueueEntry",
            "REASON_INACTIVITY",
            "REASON_OVERDUE_WORK",
            "REASON_BELOW_PASSING",
            "REASON_FAILED_FINAL",
            "REASON_CERTIFICATE_BLOCKED",
            "REASON_KINDS",
            "STATUS_OPEN",
            "STATUS_ACKNOWLEDGED",
            "STATUS_RESOLVED",
        ]
    },
    "bookmark.py": {"curriculum_models": ["ChapterBookmark", "ChapterNote"]},
    "learning_event.py": {
        "models": [
            "LearningEvent",
            "KIND_COURSE_VIEWED",
            "KIND_CHAPTER_VIEWED",
            "KIND_CHAPTER_COMPLETED",
            "KIND_ACTIVITY_STARTED",
            "KIND_SUBMISSION_MADE",
            "KIND_WORK_GRADED",
            "KIND_LAB_LAUNCHED",
            "KIND_LAB_CHECK_PASSED",
            "KIND_LAB_CHECK_FAILED",
            "KIND_SESSION_ATTENDED",
            "KIND_CERTIFICATE_EARNED",
            "EVENT_KINDS",
        ]
    },
    "class_session.py": {"delivery_models": ["ClassSession", "SESSION_TYPES", "SESSION_STATUSES"]},
    "prerequisite.py": {"participation_models": ["CoursePrerequisite"]},
    "pacing.py": {"delivery_models": ["OfferingActivity"]},
    "announcement.py": {"delivery_models": ["Announcement"]},
    "offering.py": {"participation_models": ["CourseOffering"]},
}
RETAINED = {
    "admissions": ("Applicant",),
    "email_outbox": ("EmailOutbox",),
    "entrance_defaults": ("TenantEntranceDefaults",),
    "facilitator": ("FacilitatorNomination", "FacilitatorRequirement"),
    "lab": ("LabTemplate", "LabInstance", "LabOperation", "LabConsoleActivity"),
    "notification": ("Notification",),
    "onboarding": ("OnboardingTask",),
    "platform_settings": ("PlatformSetting",),
    "reminder": ("ReminderLog", "ReminderPreference"),
}
HISTORICAL_KERNEL_TABLES = {
    "tenants",
    "tenant_domains",
    "people",
    "roles",
    "person_roles",
    "user_credentials",
    "auth_sessions",
    "audit_events",
}
HISTORICAL_RETAINED_TABLES = {
    "account_tokens",
    "applicants",
    "email_outbox",
    "lab_templates",
    "lab_instances",
    "lab_operations",
    "notifications",
    "onboarding_tasks",
    "platform_settings",
    "tenant_entrance_defaults",
    "reminder_log",
    "reminder_preferences",
}
# Installed Learning tables with no counterpart in the historical (0061-era)
# catalog inventoried by app/maintenance/learning_history._TABLES (27 specs).
# Handled exactly like post-historical retained tables: owned today, absent
# from the 47-partition.
POST_HISTORICAL_LEARNING_TABLES = {"question_bank_versions"}


def test_learning_facades_reexport_actual_owner_objects_without_mappings():
    classes = set()
    for filename, groups in LEARNING_FACADES.items():
        tree = ast.parse((ROOT / "app/models" / filename).read_text())
        assert not any(isinstance(n, (ast.ClassDef, ast.FunctionDef)) for n in ast.walk(tree))
        facade = import_module("app.models." + filename.removesuffix(".py"))
        for owner, names in groups.items():
            module = import_module("dotmac_learning." + owner)
            for name in names:
                assert getattr(facade, name) is getattr(module, name)
                if isinstance(getattr(module, name), type):
                    classes.add(name)
    assert len(classes) == 27


def test_historical_partition_and_new_candidates_are_distinct():
    from dotmac_learning.manifest import module

    assert POST_HISTORICAL_LEARNING_TABLES <= set(module.tables)
    historical_learning = set(module.tables) - POST_HISTORICAL_LEARNING_TABLES
    assert len(historical_learning) == 27
    assert len(HISTORICAL_KERNEL_TABLES) == 8
    assert len(HISTORICAL_RETAINED_TABLES) == 12
    assert len(historical_learning) + len(HISTORICAL_KERNEL_TABLES) + len(HISTORICAL_RETAINED_TABLES) == 47
    from app.models.erp_completion_checkpoint import ERPCompletionCheckpoint
    from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy

    assert ERPCompletionCheckpoint.__tablename__ not in HISTORICAL_RETAINED_TABLES
    assert AcademyAccountProfile.__tablename__ == "academy_account_profiles"
    assert AcademyCredentialPolicy.__tablename__ == "academy_credential_policies"
    from app.models.facilitator import FacilitatorNomination, FacilitatorRequirement
    from app.models.lab import LabConsoleActivity

    # Post-historical retained tables (migrations 0063/0064), exactly like the
    # ERP checkpoint precedent: owned today, absent from the 47-partition.
    assert FacilitatorNomination.__tablename__ not in HISTORICAL_RETAINED_TABLES
    assert FacilitatorRequirement.__tablename__ not in HISTORICAL_RETAINED_TABLES
    assert LabConsoleActivity.__tablename__ not in HISTORICAL_RETAINED_TABLES


def test_legacy_identity_mappings_and_constructor_defaults_are_retired():
    from dotmac_kernel.models import Base as KernelBase

    from app.models.base import Base

    assert Base is not KernelBase
    for filename in (*LEARNING_FACADES, "auth.py", "rbac.py", "tenant.py", "person.py"):
        tree = ast.parse((ROOT / "app/models" / filename).read_text())
        assert not any(isinstance(n, (ast.ClassDef, ast.FunctionDef)) for n in ast.walk(tree))
    person = import_module("app.models.person")
    rbac = import_module("app.models.rbac")
    assert not hasattr(person, "Person")
    assert not hasattr(rbac, "PersonRole")


def test_retained_cross_metadata_targets_and_actions():
    from dotmac_kernel.models import Party, PartyPerson, Tenant
    from dotmac_learning.curriculum_models import QuestionBank
    from dotmac_learning.participation_models import CohortTrack

    tenant_fks = 0
    for module_name, classes in RETAINED.items():
        module = import_module("app.models." + module_name)
        for name in classes:
            table = getattr(module, name).__table__
            if "tenant_id" in table.c:
                # A retained tenant_id usually also leads composite FKs, so
                # select the standalone tenant object-FK by identity rather
                # than by set order. LabConsoleActivity has none (composite
                # to its instance only) and therefore does not count.
                tenant_fk = next(
                    (fk for fk in table.c.tenant_id.foreign_keys if fk.column is Tenant.__table__.c.id),
                    None,
                )
                if tenant_fk is not None:
                    assert tenant_fk.ondelete == "CASCADE"
                    tenant_fks += 1
            if table.name in {"notifications", "reminder_log", "reminder_preferences"}:
                fks = {fk.name: fk for fk in table.foreign_key_constraints}
                composite = next(fk for fk in fks.values() if len(fk.elements) == 2)
                assert [fk.column for fk in composite.elements] == [Party.__table__.c.tenant_id, Party.__table__.c.id]
                subtype = next(
                    fk for fk in fks.values() if any(e.column is PartyPerson.__table__.c.party_id for e in fk.elements)
                )
                assert composite.ondelete == subtype.ondelete == "CASCADE"
    assert tenant_fks == 12
    from app.models.admissions import Applicant

    constraints = {fk.name: fk for fk in Applicant.__table__.foreign_key_constraints}
    cohort = constraints["fk_applicants_tenant_cohort_track"]
    bank = constraints["fk_applicants_tenant_assessment_bank"]
    assert [e.column for e in cohort.elements] == [
        CohortTrack.__table__.c.tenant_id,
        CohortTrack.__table__.c.cohort_id,
        CohortTrack.__table__.c.track_id,
    ]
    assert [e.column for e in bank.elements] == [QuestionBank.__table__.c.tenant_id, QuestionBank.__table__.c.id]
    assert cohort.ondelete is bank.ondelete is None

    # Post-historical facilitator/activity composites, per migrations
    # 0063/0064: nominee cascades with the party, the nominator does not;
    # requirements cascade with their nomination; console activity cascades
    # with its lab instance.
    from typing import cast

    import sqlalchemy as sa
    from dotmac_learning.models import Course
    from dotmac_learning.participation_models import Cohort, CourseOffering

    from app.models.facilitator import FacilitatorNomination, FacilitatorRequirement
    from app.models.lab import LabConsoleActivity, LabInstance

    nomination_table = cast("sa.Table", FacilitatorNomination.__table__)
    requirement_table = cast("sa.Table", FacilitatorRequirement.__table__)
    activity_table = cast("sa.Table", LabConsoleActivity.__table__)
    nomination_fks = {fk.name: fk for fk in nomination_table.foreign_key_constraints}
    nominee = nomination_fks["fk_facilitator_nominations_person"]
    assert [e.column for e in nominee.elements] == [Party.__table__.c.tenant_id, Party.__table__.c.id]
    assert nominee.ondelete == "CASCADE"
    nominee_subtype = nomination_fks["fk_facilitator_nominations_person_subtype"]
    assert [e.column for e in nominee_subtype.elements] == [PartyPerson.__table__.c.party_id]
    assert nominee_subtype.ondelete == "CASCADE"
    nominator = nomination_fks["fk_facilitator_nominations_nominator"]
    assert [e.column for e in nominator.elements] == [Party.__table__.c.tenant_id, Party.__table__.c.id]
    assert nominator.ondelete is None
    nom_cohort = nomination_fks["fk_facilitator_nominations_cohort"]
    assert [e.column for e in nom_cohort.elements] == [Cohort.__table__.c.tenant_id, Cohort.__table__.c.id]
    assert nom_cohort.ondelete == "CASCADE"
    nom_offering = nomination_fks["fk_facilitator_nominations_offering"]
    assert [e.column for e in nom_offering.elements] == [
        CourseOffering.__table__.c.tenant_id,
        CourseOffering.__table__.c.id,
    ]
    assert nom_offering.ondelete == "CASCADE"
    requirement_fks = {fk.name: fk for fk in requirement_table.foreign_key_constraints}
    req_nomination = requirement_fks["fk_facilitator_requirements_nomination"]
    assert [e.column for e in req_nomination.elements] == [
        FacilitatorNomination.__table__.c.tenant_id,
        FacilitatorNomination.__table__.c.id,
    ]
    assert req_nomination.ondelete == "CASCADE"
    req_course = requirement_fks["fk_facilitator_requirements_course"]
    assert [e.column for e in req_course.elements] == [Course.__table__.c.tenant_id, Course.__table__.c.id]
    assert req_course.ondelete is None
    activity_fk = next(iter(activity_table.foreign_key_constraints))
    assert activity_fk.name == "fk_lab_console_activity_instance"
    assert [e.column for e in activity_fk.elements] == [LabInstance.__table__.c.tenant_id, LabInstance.__table__.c.id]
    assert activity_fk.ondelete == "CASCADE"


def test_retained_internal_foreign_keys_and_deliberately_unconstrained_values():
    from app.models.admissions import Applicant
    from app.models.entrance_defaults import TenantEntranceDefaults
    from app.models.lab import LabInstance, LabOperation, LabTemplate
    from app.models.onboarding import OnboardingTask

    for table, constraint_name, target in (
        (OnboardingTask.__table__, "fk_onboarding_tasks_tenant_applicant", Applicant.__table__),
        (LabOperation.__table__, "fk_lab_operations_tenant_instance", LabInstance.__table__),
    ):
        fk = next(fk for fk in table.foreign_key_constraints if fk.name == constraint_name)
        assert {e.column.table for e in fk.elements} == {target}
        assert fk.ondelete == "CASCADE"
    for table, names in (
        (Applicant.__table__, ("person_id",)),
        (TenantEntranceDefaults.__table__, ("default_bank_id",)),
        (LabTemplate.__table__, ("activity_id", "course_id")),
        (LabInstance.__table__, ("activity_id", "person_id")),
        (LabOperation.__table__, ("requested_by",)),
    ):
        assert all(not table.c[name].foreign_keys for name in names)


def test_checkpoint_owns_erp_markers_and_references_canonical_completion():
    from dotmac_kernel.models import Tenant
    from dotmac_learning.models import CourseCompletion

    from app.models.erp_completion_checkpoint import ERPCompletionCheckpoint

    table = ERPCompletionCheckpoint.__table__
    assert set(table.c.keys()) == {"tenant_id", "completion_id", "erp_synced_at", "erp_synced_pct"}
    assert table.c.erp_synced_at.nullable
    assert table.c.erp_synced_pct.nullable
    assert not {"erp_synced_at", "erp_synced_pct"} & set(CourseCompletion.__table__.c.keys())
    # tenant_id also leads the composite completion FK; pick the standalone
    # tenant object-FK by identity, not by set order.
    assert any(fk.column is Tenant.__table__.c.id for fk in table.c.tenant_id.foreign_keys)
    fk = next(fk for fk in table.foreign_key_constraints if len(fk.elements) == 2)
    assert [e.column for e in fk.elements] == [CourseCompletion.__table__.c.tenant_id, CourseCompletion.__table__.c.id]
    assert fk.ondelete == "CASCADE"


def test_imported_membership_references_completion_independently_of_checkpoint():
    from dotmac_kernel.models import Tenant
    from dotmac_learning.models import CourseCompletion

    from app.models.erp_completion_checkpoint import ERPImportedCompletionKey

    table = ERPImportedCompletionKey.__table__
    assert set(table.c.keys()) == {"tenant_id", "completion_id"}
    assert set(table.primary_key.columns.keys()) == {"tenant_id", "completion_id"}
    assert any(fk.column is Tenant.__table__.c.id for fk in table.c.tenant_id.foreign_keys)
    relation = next(fk for fk in table.foreign_key_constraints if len(fk.elements) == 2)
    assert [e.column for e in relation.elements] == [CourseCompletion.__table__.c.tenant_id, CourseCompletion.__table__.c.id]
    assert relation.ondelete == "CASCADE"
    assert all(fk.column.table.name != "academy_erp_completion_checkpoints" for fk in table.foreign_keys)


def test_actual_installed_disabled_learning_is_in_inspection_graph():
    from dataclasses import replace

    from dotmac_kernel.modules import ModuleRegistry
    from dotmac_learning.manifest import module

    from app.canonical_models import inspect_canonical_models, kernel_owned_tables

    kernels = kernel_owned_tables()
    registry = ModuleRegistry((replace(module, enabled_by_default=False),))
    graph = inspect_canonical_models(registry, kernel_tables=kernels)
    assert not registry.enabled_codes()
    assert len([owner for owner in graph.owners.values() if owner == "learning"]) == len(module.tables)
    assert len(graph.metadata.tables) == len(graph.owners)
    assert graph.owners[("public", "tenants")] == "kernel"
    assert graph.owners[("public", "academy_erp_completion_checkpoints")] == "academy"
    assert graph.owners[("public", "academy_erp_imported_completion_keys")] == "academy"
    assert all(
        fk.column.table.metadata is graph.metadata
        for table in graph.metadata.tables.values()
        for fk in table.foreign_keys
    )


def test_identity_reference_copies_are_excluded_from_product_ownership():
    from app.canonical_models import physical_key, product_owned_tables
    from app.models.identity_extensions import IdentityExtensionBase

    tables = product_owned_tables()
    assert len(tables) == 19
    assert len({physical_key(table) for table in tables}) == 19
    assert "public.tenants" in IdentityExtensionBase.metadata.tables
    assert all(table.name not in {"tenants", "parties", "party_persons", "user_credentials"} for table in tables)
    assert {"academy_account_profiles", "academy_credential_policies", "account_tokens"} <= {
        table.name for table in tables
    }


def test_normalized_duplicate_owners_are_refused_before_copy():
    import pytest
    import sqlalchemy as sa

    from app.canonical_models import ModelOwnershipError, OwnedTable, compose_inspection_graph

    first = sa.Table("sample", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True))
    copied = first.to_metadata(sa.MetaData(), schema="public")
    with pytest.raises(ModelOwnershipError, match="duplicate physical table owner"):
        compose_inspection_graph((OwnedTable("one", first), OwnedTable("two", copied)))


def test_unresolved_and_unowned_foreign_key_targets_are_refused():
    import pytest
    import sqlalchemy as sa

    from app.canonical_models import ModelOwnershipError, OwnedTable, compose_inspection_graph

    metadata = sa.MetaData()
    source = sa.Table("sample", metadata, sa.Column("id", sa.Integer, sa.ForeignKey("missing.id")))
    with pytest.raises(ModelOwnershipError, match="unresolved foreign key"):
        compose_inspection_graph((OwnedTable("test", source),))
    sa.Table("missing", metadata, sa.Column("id", sa.Integer, primary_key=True))
    with pytest.raises(ModelOwnershipError, match="no declared owner"):
        compose_inspection_graph((OwnedTable("test", source),))


def test_legacy_identity_learning_and_module_as_kernel_ownership_are_refused():
    import pytest
    import sqlalchemy as sa

    from app.canonical_models import ModelOwnershipError, OwnedTable, compose_inspection_graph

    for owner, schema, name, message in (
        ("academy", None, "people", "legacy identity"),
        ("academy", "public", "person_roles", "legacy identity"),
        ("academy", "public", "courses", "legacy Learning"),
        ("kernel", "mod_learning", "courses", "claimed as Kernel"),
    ):
        table = sa.Table(name, sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True), schema=schema)
        with pytest.raises(ModelOwnershipError, match=message):
            compose_inspection_graph((OwnedTable(owner, table),))


def test_unclaimed_shared_base_tables_are_refused():
    import pytest
    import sqlalchemy as sa
    from dotmac_kernel.models import Base as KernelBase
    from dotmac_kernel.modules import ModuleRegistry
    from dotmac_learning.manifest import module

    from app.canonical_models import ModelOwnershipError, declared_ownership, kernel_owned_tables

    kernels = kernel_owned_tables()
    unknown = sa.Table("unclaimed_test_owner", KernelBase.metadata, sa.Column("id", sa.Integer), schema="public")
    try:
        with pytest.raises(ModelOwnershipError, match="unclaimed registered table"):
            declared_ownership(ModuleRegistry((module,)), kernel_tables=kernels)
    finally:
        KernelBase.metadata.remove(unknown)
