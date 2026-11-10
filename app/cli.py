"""Command-line interface for platform-level management operations.

Usage
-----
    python -m app.cli <subcommand> [options]

Offline tenant bootstrap requires the configured login/effective database-role
pair and the actual canonical/Academy privileges and tenant policies. Configure
BOOTSTRAP_ALLOWED_DATABASE_ROLE_PAIRS deliberately; its empty default denies.
"""

# Ported at new-composition genesis (operator CLI).

from __future__ import annotations

import argparse
import logging
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

from dotmac_kernel.exceptions import NotFoundError
from dotmac_kernel.models import Tenant as KernelTenant
from sqlalchemy.orm import Session

from app.database_runtime import bind_academy_database_runtime

_DEFAULT_CHAPTERS_DIR = Path("/home/dotmac/projects/dotmac-academy/manuals/00-foundation/chapters")
_DEFAULT_FIGURES_DIR = Path("/home/dotmac/projects/dotmac-academy/figures/final")
_DEFAULT_BANKS_DIR = Path("/home/dotmac/projects/dotmac-academy/manuals/00-foundation/assessments/banks")
_DEFAULT_LABS_DIR = Path("/home/dotmac/projects/dotmac-academy/manuals/00-foundation/labs")


@contextmanager
def _tenant_session(slug: str) -> Iterator[tuple[Session, KernelTenant]]:
    """`tenant_session_by_slug` with the CLI's error convention.

    Yields the KERNEL's `Tenant`, not `app.models.tenant.Tenant`: the kernel
    resolves the slug against its own model. Both map the `tenants` table, and
    the CLI only reads `.id`/`.slug`, so this works — but two mapped classes for
    one table is exactly the duplication that adopting the kernel's `Tenant`
    outright would remove. Annotated honestly rather than cast to ours.

    The kernel raises `NotFoundError`; a command-line tool should exit with a
    message, not a traceback. Wrapping it here keeps that translation in one
    place instead of six.
    """
    try:
        with bind_academy_database_runtime().tenant_session_by_slug(slug) as (db, tenant):
            yield db, tenant
    except NotFoundError as exc:
        raise SystemExit(str(exc)) from exc


def _bootstrap(args: argparse.Namespace) -> None:
    from app.services.bootstrap import bootstrap_tenant

    # The one command that cannot be tenant-scoped: it is CREATING the tenant,
    # so there is nothing to scope to yet. Creating a tenant is a platform
    # operation, which is what `platform_session` is for. Its small pool is
    # irrelevant here — this is a one-shot command, not a request path.
    with bind_academy_database_runtime().platform_session() as db:
        t = bootstrap_tenant(
            db,
            slug=args.slug,
            name=args.name,
            admin_email=args.admin_email,
            admin_password=args.admin_password,
        )
    print(f"tenant {t.slug} ({t.id}) created")


def _oidc_party(db: Session, *, tenant_id, email: str):
    """Resolve the operator-named local party for an OIDC binding act."""
    from dotmac_kernel.models import Party
    from sqlalchemy import func, select

    from app.services.identity import normalize_email

    parties = db.scalars(
        select(Party)
        .where(Party.tenant_id == tenant_id, func.lower(Party.email) == normalize_email(email))
        .execution_options(populate_existing=True)
    ).all()
    if len(parties) != 1:
        raise SystemExit(f"expected exactly one party for {email!r} in this tenant, found {len(parties)}")
    return parties[0]


def _oidc_bind(args: argparse.Namespace) -> None:
    from app.services import oidc_login

    with _tenant_session(args.tenant_slug) as (db, tenant):
        party = _oidc_party(db, tenant_id=tenant.id, email=args.email)
        try:
            binding = oidc_login.bind_staff_identity(
                db,
                tenant=tenant,
                party=party,
                subject=args.subject,
                bound_by=args.bound_by,
                reason=args.reason,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        party_id, binding_id = party.id, binding.id
    print(f"bound party {party_id} binding {binding_id}")


def _oidc_disable(args: argparse.Namespace) -> None:
    from app.services import oidc_login

    with _tenant_session(args.tenant_slug) as (db, tenant):
        party = _oidc_party(db, tenant_id=tenant.id, email=args.email)
        party_id = party.id
        count = oidc_login.disable_staff_identities(db, tenant=tenant, party_id=party_id)
    print(f"disabled {count} binding(s) for party {party_id}")


def _settings_shadow_sweep(args: argparse.Namespace) -> None:
    """ADR-0003 shadow gate: kernel resolution vs the legacy store, read-only.

    Compares every registered Academy spec at platform scope. The report
    names domains/keys/types only — never values — and the exit code is
    the gate: 0 clean, 2 diverged. Nothing served changes.
    """
    from dotmac_kernel.settings_shadow import sweep

    from app import settings_store
    from app.settings_specs import ACADEMY_SETTING_SPECS, install_academy_setting_registries

    install_academy_setting_registries()
    with bind_academy_database_runtime().platform_session() as db:
        legacy = settings_store.effective(db, require_store=True)

        def _legacy_reader(domain, key):
            value = legacy[key]
            # Legacy role lists are tuples; the kernel list type resolves
            # JSON arrays to lists. That is representation, not drift.
            return list(value) if isinstance(value, tuple) else value

        report = sweep(db, _legacy_reader, specs=list(ACADEMY_SETTING_SPECS))
    print(f"settings-shadow-sweep: {report.describe()}")
    if not report.clean:
        raise SystemExit(2)


def _email_outbox(args: argparse.Namespace) -> None:
    """Deliver/reconcile committed email intents without exposing payloads."""
    from app.services.email_outbox import deliver_pending, requeue_failed
    from app.services.lab_jobs import admin_session
    from app.services.lab_result_delivery import materialize_pending, purge_delivered
    from app.services.lab_result_delivery import requeue_failed as requeue_failed_labs

    # Commit and release lab locks before SMTP. A failed materializer must not
    # prevent password resets or invitations from reaching deliver_pending.
    with admin_session() as db:
        try:
            lab_requeued = (
                requeue_failed_labs(db, limit=args.limit)
                if getattr(args, "requeue_failed_lab_deliveries", False)
                else 0
            )
            labs = materialize_pending(db, limit=args.limit)
            purged = purge_delivered(db, limit=args.limit)
            db.commit()
        except Exception as exc:
            db.rollback()
            logging.getLogger(__name__).warning("lab delivery job failed: %s:materialize", type(exc).__name__)
        else:
            print(
                f"lab-deliveries: delivered={labs.delivered} retried={labs.retried} "
                f"failed_total={labs.failed_total} oldest_pending_age_s={labs.oldest_pending_age_s:.0f} "
                f"requeued={lab_requeued} purged={purged}"
            )
            if labs.failed_total or labs.oldest_pending_age_s > 300:
                logging.getLogger(__name__).warning(
                    "lab delivery backlog: failed_total=%d oldest_pending_age_s=%.0f",
                    labs.failed_total,
                    labs.oldest_pending_age_s,
                )

    with admin_session() as db:
        requeued = requeue_failed(db) if args.requeue_failed else 0
        counts = deliver_pending(db, limit=args.limit)
        db.commit()
    print(
        f"email-outbox: sent={counts['sent']} retried={counts['retried']} failed={counts['failed']} requeued={requeued}"
    )


def _import_foundation(args: argparse.Namespace) -> None:
    from app.services.content_import import import_foundation, sync_figures

    with _tenant_session(args.tenant_slug) as (db, tenant):
        course = import_foundation(
            db,
            tenant_id=tenant.id,
            chapters_dir=args.chapters_dir,
            figures_dir=args.figures_dir,
            strict_figures=not args.allow_missing_figures,
        )
        db.commit()
        # Copy produced figures into the served static tree so chapter <img> tags resolve.
        static_figures = Path(__file__).resolve().parent.parent / "static" / "figures"
        copied = sync_figures(args.figures_dir, static_figures)
        print(
            f"foundation course '{course.slug}' ({course.id}) v{course.version} imported; "
            f"{copied} figure(s) synced to static/figures/"
        )


def _import_manual(args: argparse.Namespace) -> None:
    from app.services.content_import import import_manual, sync_figures

    with _tenant_session(args.tenant_slug) as (db, tenant):
        course = import_manual(
            db,
            tenant_id=tenant.id,
            slug=args.slug,
            title=args.title,
            discipline=args.discipline,
            source_ref=args.source_ref or f"{args.slug}@0.1.0",
            chapters_dir=args.chapters_dir,
            figures_dir=args.figures_dir,
            strict_figures=not args.allow_missing_figures,
        )
        db.commit()
        static_figures = Path(__file__).resolve().parent.parent / "static" / "figures"
        copied = sync_figures(args.figures_dir, static_figures)
        print(
            f"course '{course.slug}' ({course.id}) v{course.version} imported; "
            f"{copied} figure(s) synced to static/figures/"
        )


def _audit_banks(args: argparse.Namespace) -> None:
    """Check every bank already live in the database against lint_bank.

    load-banks enforces the rules on the way in, which leaves a gap: a bank
    loaded before a rule existed stays live and non-compliant, and nothing
    says so. This closes it — the same linter, pointed at the projection.
    """
    from dotmac_learning.curriculum_models import QuestionBank
    from dotmac_learning.models import Course

    from app.services.bank_loader import doc_from_db, lint_bank

    with _tenant_session(args.tenant_slug) as (db, tenant):
        q = (
            db.query(QuestionBank, Course)
            .join(Course, Course.id == QuestionBank.course_id)
            .filter(QuestionBank.tenant_id == tenant.id)
        )
        if args.discipline:
            q = q.filter(Course.discipline == args.discipline)
        if args.course:
            q = q.filter(Course.slug == args.course)
        rows = q.order_by(Course.discipline, Course.slug, QuestionBank.chapter_number).all()

        banks = failing = 0
        per_discipline: dict[str, list[int]] = {}
        for bank, course in rows:
            banks += 1
            doc = doc_from_db(db, bank=bank, course_slug=course.slug)
            violations = lint_bank(doc)
            tally = per_discipline.setdefault(course.discipline, [0, 0])
            tally[0] += 1
            if violations:
                failing += 1
                tally[1] += 1
                label = f"ch{bank.chapter_number}" if bank.chapter_number else bank.kind
                print(f"{course.slug} [{label}] {len(doc.questions)}q")
                for v in violations:
                    print(f"    {v}")

        print()
        print(f"{'discipline':32} {'banks':>6} {'failing':>8}")
        for discipline, (total, bad) in sorted(per_discipline.items()):
            print(f"{discipline:32} {total:>6} {bad:>8}")
        print(f"{'TOTAL':32} {banks:>6} {failing:>8}")

        if failing and args.fail_on_violations:
            raise SystemExit(1)


def _load_banks(args: argparse.Namespace) -> None:
    """Import YAML banks as Learning DRAFTS (never publishes).

    Retires the destructive writer: import never edits a published
    version, never overwrites a UI draft, never repoints activities or
    entrance defaults, and never touches Activity title/pass threshold.
    The YAML policy block is reported as unapplied proposals. A
    lint-failing bank still becomes a draft for repair in the UI.
    Publishing is an explicit admin action in the instructor UI.
    """
    import dotmac_learning as learning
    from dotmac_learning.models import Course
    from sqlalchemy import select

    from app.services.bank_authoring import cli_bank_audit_context
    from app.services.bank_loader import BankDocumentConversionError, import_bank_as_draft, parse_bank

    with _tenant_session(args.tenant_slug) as (db, tenant):
        banks_dir = Path(args.banks_dir)
        if not banks_dir.is_dir():
            raise SystemExit(f"Banks directory not found: {banks_dir}")

        yaml_files = sorted(banks_dir.glob("*.yaml"))
        if not yaml_files:
            print(f"No *.yaml files found in {banks_dir}")
            return

        context = cli_bank_audit_context(tenant_id=tenant.id)
        imported = 0
        for yaml_path in yaml_files:
            try:
                doc = parse_bank(yaml_path)
            except Exception:  # unparseable file: report, keep going
                print(f"SKIP {yaml_path.name}: could not parse bank document")
                continue
            course = db.scalars(
                select(Course).where(Course.tenant_id == tenant.id).where(Course.slug == doc.course)
            ).first()
            if course is None:
                print(f"SKIP {yaml_path.name}: course '{doc.course}' not found for tenant '{args.tenant_slug}'")
                continue
            try:
                report = import_bank_as_draft(
                    db,
                    tenant_id=tenant.id,
                    course_id=course.id,
                    doc=doc,
                    audit_context=context,
                    replace_imported_draft=bool(getattr(args, "replace_imported_draft", False)),
                )
            except learning.BankImportRefused as exc:
                # Learning stages the refusal audit in this transaction;
                # commit so the refusal evidence persists.
                db.commit()
                print(f"REFUSED {yaml_path.name}: {exc}")
                continue
            except BankDocumentConversionError as exc:
                db.rollback()
                print(f"SKIP {yaml_path.name}: {exc}")
                continue
            except learning.BankLifecycleError as exc:
                db.rollback()
                print(f"ERROR {yaml_path.name}: {exc}")
                continue
            db.commit()
            proposals = " ".join(f"{key}={value}" for key, value in report.policy_proposals)
            suffix = f", policy proposals NOT applied: {proposals}" if proposals else ""
            print(
                f"Imported {yaml_path.name} as draft: outcome={report.outcome} bank={report.bank_id} "
                f"version={report.version_id} questions={len(doc.questions)} "
                f"lint_violations={len(report.lint_violations)}{suffix}"
            )
            for violation in report.lint_violations:
                print(f"  - {violation}")
            imported += 1

        print(f"Done — {imported}/{len(yaml_files)} bank(s) imported as drafts (none published).")


def _export_bank(args: argparse.Namespace) -> None:
    """Export one bank version to YAML (audited; full answer keys).

    Writes only to the explicit --output file; keys are never printed.
    """
    from dotmac_learning.models import Course
    from sqlalchemy import select

    from app.services import bank_authoring

    with _tenant_session(args.tenant_slug) as (db, tenant):
        course = db.scalars(
            select(Course).where(Course.tenant_id == tenant.id).where(Course.slug == args.course)
        ).first()
        if course is None:
            raise SystemExit(f"Course '{args.course}' not found for tenant '{args.tenant_slug}'")
        bank = bank_authoring.find_bank(
            db, tenant_id=tenant.id, course_id=course.id, chapter_number=args.chapter, kind=args.kind
        )
        if bank is None:
            raise SystemExit(f"No bank for course '{args.course}' chapter={args.chapter} kind={args.kind}")
        version_id = args.version_id or bank.current_version_id
        if version_id is None:
            raise SystemExit("Bank has no published version to export; pass --version-id for a specific version.")
        text = bank_authoring.export_yaml(
            db,
            tenant_id=tenant.id,
            version_id=version_id,
            course_slug=course.slug,
            context=bank_authoring.cli_bank_audit_context(tenant_id=tenant.id),
        )
        output = Path(args.output)
        output.write_text(text, encoding="utf-8")
        db.commit()
        print(f"Exported bank {bank.id} version {version_id} to {output} ({len(text)} bytes, audited).")


def _import_labs(args: argparse.Namespace) -> None:
    from dotmac_learning.models import Course

    from app.services.lab_content import import_labs

    with _tenant_session(args.tenant_slug) as (db, tenant):
        course = db.query(Course).filter(Course.tenant_id == tenant.id, Course.slug == args.course_slug).first()
        if course is None:
            raise SystemExit(
                f"Course '{args.course_slug}' not found for tenant '{args.tenant_slug}'. "
                "Import the course chapters before importing its labs."
            )

        labs_dir = Path(args.labs_dir)
        if not labs_dir.is_dir():
            raise SystemExit(f"Labs directory not found: {labs_dir}")

        templates = import_labs(
            db,
            tenant_id=tenant.id,
            course_id=course.id,
            labs_dir=labs_dir,
            chapters_dir=args.chapters_dir,
        )
        db.commit()
        for t in templates:
            print(f"lab '{t.slug}' -> activity {t.activity_id} v{t.version}")
        print(f"Done — {len(templates)} lab(s) imported for course '{course.slug}' and tenant '{args.tenant_slug}'.")


def _reminders_sweep(args: argparse.Namespace) -> None:
    """Run one reminder policy sweep per tenant (timer entrypoint).

    Uses an offline BYPASSRLS session because scheduled jobs have no request
    tenant context. Decisions are owned by services.reminders.sweep; email
    delivery stays with the outbox worker.
    """
    from dotmac_kernel.models import Tenant
    from sqlalchemy import select

    from app.services import lab_jobs
    from app.services.facilitator_nominations import SweepFacilitatorsCommand, sweep_pending
    from app.services.reminders import sweep
    from app.services.success_queue import sweep as queue_sweep

    with lab_jobs.admin_session() as db:
        tenants = db.scalars(select(Tenant)).all()
        for tenant in tenants:
            facilitator_counts = sweep_pending(
                db,
                command=SweepFacilitatorsCommand(tenant_id=tenant.id),
            )
            db.commit()
            print(f"facilitator-sweep[{tenant.slug}]: {facilitator_counts}")
            counts = sweep(db, tenant_id=tenant.id)
            db.commit()
            print(f"reminders-sweep[{tenant.slug}]: {counts}")
            qcounts = queue_sweep(db, tenant_id=tenant.id)
            db.commit()
            print(f"success-queue[{tenant.slug}]: {qcounts}")


def _learner_digest(args: argparse.Namespace) -> None:
    """Queue each learner's weekly progress email.

    Distinct from ``email-digest``, which is the *instructor* cohort matrix.
    Gated by its own ``learner_digest_enabled`` Academy setting, off by default:
    this mails every active learner, so it must be switched on deliberately
    rather than start sending the moment it deploys.
    """
    from dotmac_kernel.models import Tenant
    from sqlalchemy import select

    from app.services import lab_jobs, learner_digest
    from app.services.settings_store import effective

    with lab_jobs.admin_session() as db:
        cfg = effective(db)
        if not bool(cfg.get("learner_digest_enabled", False)) and not args.force:
            print("learner-digest: disabled via Academy settings; skipping (use --force to override)")
            return
        base_url = str(cfg.get("academy_base_url", args.base_url)).rstrip("/")
        branding = str(cfg.get("branding_name", "Dotmac Academy"))
        totals = {"queued": 0, "skipped_optout": 0, "skipped_no_courses": 0}
        for tenant in db.scalars(select(Tenant)).all():
            counts = learner_digest.send_weekly_digests(db, tenant_id=tenant.id, base_url=base_url, branding=branding)
            for key, n in counts.items():
                totals[key] += n
        if args.dry_run:
            db.rollback()
            print(f"DRY RUN — nothing queued. Would be: {totals}")
            return
        db.commit()
    print(
        f"learner-digest: queued={totals['queued']} "
        f"opted-out={totals['skipped_optout']} no-live-course={totals['skipped_no_courses']}"
    )


def _classify_audience(args: argparse.Namespace) -> None:
    """Mark enrolments staff/external from an ERP roster export (ADR 0004).

    The roster is CSV ``work_email,employee_ref`` — ERP's answer, not a guess.
    Anything absent stays unclassified unless --assume-roster-complete says the
    export covers every employee, which is a claim only an operator can make.
    """
    import csv

    from dotmac_kernel.models import Tenant
    from sqlalchemy import select

    from app.services import audience, lab_jobs

    roster: dict[str, str] = {}
    with open(args.roster_file, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) >= 2 and row[0].strip() and not row[0].lstrip().startswith("#"):
                roster[row[0].strip()] = row[1].strip()
    if not roster:
        raise SystemExit(f"No usable rows in {args.roster_file} (expected: work_email,employee_ref)")

    with lab_jobs.admin_session() as db:
        tenant = db.scalars(select(Tenant).where(Tenant.slug == args.tenant_slug)).first()
        if tenant is None:
            raise SystemExit(f"Tenant '{args.tenant_slug}' not found.")
        counts = audience.classify_from_roster(
            db,
            tenant_id=tenant.id,
            roster=roster,
            mark_rest_external=args.assume_roster_complete,
        )
        split = audience.counts_by_audience(db, tenant_id=tenant.id)
        pending = audience.unclassified(db, tenant_id=tenant.id)
        if args.dry_run:
            db.rollback()
            print(f"DRY RUN — nothing written. Would be: {counts}")
        else:
            db.commit()
            print(f"classify-audience: {counts}")
        print(f"roster now: {split}")
        if pending:
            print(f"\n{len(pending)} learner(s) still unclassified — review rather than guess:")
            for email, name in pending[:20]:
                print(f"  {email}  {name}")
            if len(pending) > 20:
                print(f"  ... and {len(pending) - 20} more")


def _hr_report(args: argparse.Namespace) -> None:
    """Email HR the staff-only training roll-up."""
    from dotmac_kernel.models import Tenant
    from sqlalchemy import select

    from app.services import hr_digest, lab_jobs
    from app.services.settings_store import effective

    recipients = [r for r in (args.to or "").split(",") if r.strip()]
    if not recipients:
        raise SystemExit("No recipients: pass --to hr@dotmac.ng")

    queued = 0
    with lab_jobs.admin_session() as db:
        branding = str(effective(db).get("branding_name", "Dotmac Academy"))
        for tenant in db.scalars(select(Tenant)).all():
            if args.dry_run:
                from datetime import UTC, datetime, timedelta

                snap = hr_digest.snapshot(db, tenant_id=tenant.id, since=datetime.now(UTC) - timedelta(days=args.days))
                print(f"DRY RUN [{tenant.slug}] {hr_digest.render(snap, branding=branding)[1]}")
                continue
            queued += hr_digest.send_hr_report(
                db, tenant_id=tenant.id, recipients=recipients, days=args.days, branding=branding
            )
        if args.dry_run:
            db.rollback()
            return
        db.commit()
    print(f"hr-report: queued {queued} message(s) to {', '.join(recipients)}")


def _email_digest(args: argparse.Namespace) -> None:
    """Queue each cohort instructor's weekly Academy summary.

    Uses an offline BYPASSRLS session because scheduled jobs do not have request
    tenant context. Delivery is owned by the outbox worker.
    """
    from datetime import UTC, datetime

    from dotmac_kernel.models import Party, PartyPerson, PartyType, Tenant
    from dotmac_learning.participation_models import Cohort, Enrollment
    from sqlalchemy import select

    from app.models.identity_extensions import AcademyAccountProfile
    from app.services import lab_jobs
    from app.services.email import recipient_allows, render_cohort_html
    from app.services.email_outbox import enqueue_email
    from app.services.reports import cohort_matrix
    from app.services.settings_store import effective

    queued = 0
    period = datetime.now(UTC).strftime("%G-W%V")
    with lab_jobs.admin_session() as db:
        if not effective(db).email_digest_enabled:
            print("email-digest: disabled via Academy settings; skipping")
            return
        tenants = db.scalars(select(Tenant)).all()
        for tenant in tenants:
            cohorts = db.scalars(select(Cohort).where(Cohort.tenant_id == tenant.id)).all()
            for cohort in cohorts:
                matrix = cohort_matrix(db, tenant_id=tenant.id, cohort_id=cohort.id)
                instructors = db.execute(
                    select(
                        Party.id,
                        Party.email,
                        PartyPerson.first_name,
                        PartyPerson.last_name,
                        AcademyAccountProfile.prefs,
                    )
                    .join(PartyPerson, PartyPerson.party_id == Party.id)
                    .join(
                        AcademyAccountProfile,
                        (AcademyAccountProfile.party_id == Party.id)
                        & (AcademyAccountProfile.tenant_id == Party.tenant_id),
                    )
                    .join(
                        Enrollment,
                        (Enrollment.person_id == Party.id) & (Enrollment.tenant_id == Party.tenant_id),
                    )
                    .where(Party.tenant_id == tenant.id, Party.party_type == PartyType.person)
                    .where(Enrollment.tenant_id == tenant.id)
                    .where(Enrollment.cohort_id == cohort.id)
                    .where(Enrollment.role_in_cohort == "instructor")
                    .where(Enrollment.status == "active")
                ).all()
                for instructor in instructors:
                    if not recipient_allows(instructor, "email_digest"):
                        continue
                    if enqueue_email(
                        db,
                        tenant_id=tenant.id,
                        idempotency_key=(f"weekly-digest:{period}:{cohort.id}:{instructor.id}"),
                        kind="weekly_digest",
                        recipient=instructor.email,
                        subject=f"Weekly progress digest — {cohort.name}",
                        html_body=render_cohort_html(matrix),
                        payload={"cohort_id": str(cohort.id)},
                    ):
                        queued += 1
        db.commit()
    print(f"email-digest: queued {queued} message(s)")


def _at_risk_sweep(args: argparse.Namespace) -> None:
    """Cross-tenant: nudge students who are behind/overdue (in-app notification).

    BYPASSRLS admin session like the digest; deduped so re-runs don't spam.
    """
    from dotmac_kernel.models import Tenant
    from dotmac_learning.participation_models import Enrollment
    from sqlalchemy import select

    from app.services import at_risk, lab_jobs

    print("at-risk-sweep: DEPRECATED — the Success Queue (reminders-sweep) is the intervention owner")
    sent = 0
    with lab_jobs.admin_session() as db:
        for tenant in db.scalars(select(Tenant)).all():
            student_ids = set(
                db.scalars(
                    select(Enrollment.person_id)
                    .where(Enrollment.tenant_id == tenant.id)
                    .where(Enrollment.role_in_cohort == "student")
                    .where(Enrollment.status == "active")
                ).all()
            )
            for pid in student_ids:
                sent += at_risk.notify_person_if_at_risk(db, tenant_id=tenant.id, person_id=pid)
        db.commit()
    print(f"at-risk-sweep: sent {sent} nudge(s)")


def _erp_training_sync(args: argparse.Namespace) -> None:
    """Cross-tenant: push staff progress and completions to ERP with v2 events.

    The existing offline BYPASSRLS admin session owns checkpoint updates.
    Stable enrollment employee_ref supplies identity; accepted percentage and
    raw completion ACK markers suppress acknowledged snapshots. Delivery stays
    at least once across HTTP acceptance and a local rollback. Inert unless
    ERP_WEBHOOK_URL is configured and the guarded ERP successor is installed.
    """
    from dotmac_kernel.models import Tenant
    from sqlalchemy import select

    from app.services import erp_sync, lab_jobs

    totals = {erp_sync.SYNCED: 0, erp_sync.UNMATCHED: 0, erp_sync.FAILED: 0}
    with lab_jobs.admin_session() as db:
        for tenant in db.scalars(select(Tenant)).all():
            for outcome, n in erp_sync.sync_pending(db, tenant_id=tenant.id).items():
                totals[outcome] += n
        db.commit()
    print(
        f"erp-training-sync: synced={totals[erp_sync.SYNCED]} "
        f"unmatched={totals[erp_sync.UNMATCHED]} failed={totals[erp_sync.FAILED]}"
    )
    if totals[erp_sync.UNMATCHED]:
        # Unmatched requires the enrollment's stable ERP employee_ref to be
        # corrected; retries alone cannot repair the identity link.
        print(
            f"NOTE: {totals[erp_sync.UNMATCHED]} training event(s) reached ERP but were not recorded "
            "(no matching employee). These stay unsynced until the identity link is fixed."
        )


def _set_entrance_bank(args: argparse.Namespace) -> None:
    """Designate a cohort's entrance-assessment question bank (opens it for intake)."""
    import uuid

    from dotmac_learning.participation_models import Cohort

    from app.services import lab_jobs

    with lab_jobs.admin_session() as db:
        cohort = db.get(Cohort, uuid.UUID(args.cohort_id))
        if cohort is None:
            raise SystemExit(f"Cohort {args.cohort_id} not found.")
        cohort.entrance_bank_id = uuid.UUID(args.bank_id)
        if args.time_limit_minutes is not None:
            cohort.entrance_time_limit_minutes = args.time_limit_minutes or None
        db.commit()
        limit = cohort.entrance_time_limit_minutes
        print(
            f"cohort '{cohort.name}' entrance bank set to {args.bank_id}"
            + (f" (time limit {limit} min)" if limit else " (untimed)")
        )


def _admin_report(args: argparse.Namespace) -> None:
    """Cross-tenant: email each tenant's admins the activity report."""
    from dotmac_kernel.models import Tenant
    from sqlalchemy import select

    from app.services import admin_reports, lab_jobs

    print("at-risk-sweep: DEPRECATED — the Success Queue (reminders-sweep) is the intervention owner")
    sent = 0
    with lab_jobs.admin_session() as db:
        for tenant in db.scalars(select(Tenant)).all():
            sent += admin_reports.send_activity_report(db, tenant_id=tenant.id, hours=args.hours)
        db.commit()
    print(f"admin-report: sent {sent} email(s)")


def _reinvite_stranded(args: argparse.Namespace) -> None:
    """Re-issue activation links to enrolled learners who never set a password.

    These learners cannot act on any other message we send them, so this is the
    one wave that has to precede a re-engagement campaign, not follow it.
    """
    import uuid

    from dotmac_kernel.models import Tenant
    from sqlalchemy import select

    from app.services import lab_jobs, reengagement

    cohort_id = uuid.UUID(args.cohort_id) if args.cohort_id else None
    with lab_jobs.admin_session() as db:
        tenant = db.scalars(select(Tenant).where(Tenant.slug == args.tenant_slug)).first()
        if tenant is None:
            raise SystemExit(f"Tenant '{args.tenant_slug}' not found.")

        targets = reengagement.stranded_learners(db, tenant_id=tenant.id, cohort_id=cohort_id)
        print(f"{len(targets)} stranded learner(s) — enrolled, no password, never signed in (base {args.base_url})")
        if args.dry_run:
            for p in targets[:20]:
                print(f"  DRY-RUN would email {p.email}")
            if len(targets) > 20:
                print(f"  ... and {len(targets) - 20} more")
            print("\nDRY RUN — no tokens minted and no email queued.")
            return

        res = reengagement.reinvite_stranded(db, tenant_id=tenant.id, base_url=args.base_url, cohort_id=cohort_id)
        db.commit()
        print(f"\nqueued: {res['queued']}   queue failures: {res['failed']}")
        if res["failed"]:
            print("NOTE: queue failures did not expose or print activation tokens.")


def _set_auto_accept(args: argparse.Namespace) -> None:
    """Set (or clear) a cohort's auto-accept threshold for entrance sittings."""
    import uuid

    from dotmac_learning.participation_models import Cohort

    from app.services import lab_jobs

    with lab_jobs.admin_session() as db:
        cohort = db.get(Cohort, uuid.UUID(args.cohort_id))
        if cohort is None:
            raise SystemExit(f"Cohort {args.cohort_id} not found.")
        if args.threshold is not None and not 0.0 <= args.threshold <= 1.0:
            raise SystemExit("--threshold must be a fraction between 0 and 1 (e.g. 0.6)")
        cohort.auto_accept_threshold = args.threshold
        db.commit()
        if args.threshold is None:
            print(f"cohort '{cohort.name}': auto-accept OFF (human decisions only)")
        else:
            print(f"cohort '{cohort.name}': valid sittings scoring >= {args.threshold:.0%} auto-accept")


def _set_default_entrance_bank(args: argparse.Namespace) -> None:
    """Set the academy-wide default entrance bank — every applicant sits it."""
    import uuid

    from dotmac_kernel.models import Tenant
    from sqlalchemy import select

    from app.services import lab_jobs
    from app.services.entrance_exam import set_academy_defaults

    with lab_jobs.admin_session() as db:
        tenant = db.scalars(select(Tenant).where(Tenant.slug == args.tenant_slug)).first()
        if tenant is None:
            raise SystemExit(f"Tenant '{args.tenant_slug}' not found.")
        row = set_academy_defaults(
            db,
            tenant_id=tenant.id,
            bank_id=uuid.UUID(args.bank_id),
            time_limit_minutes=(args.time_limit_minutes or None),
        )
        db.commit()
        limit = row.default_time_limit_minutes
        print(
            f"academy '{tenant.slug}' default entrance bank set to {args.bank_id}"
            + (f" (time limit {limit} min)" if limit else " (untimed)")
        )


def _invite_applicants(args: argparse.Namespace) -> None:
    """Email the entrance-assessment invitation to applicants who haven't sat it.

    This is the backfill for every applicant who applied before the invitation
    email existed — they were shown the link once on-screen (or never), so they
    have no way to reach the exam. Also the recovery path for a bounced email.

    Idempotent-ish: re-inviting mints a fresh token and a fresh deadline, which
    invalidates any older link for that applicant.
    """
    from datetime import UTC, datetime

    from dotmac_kernel.models import Tenant
    from sqlalchemy import select

    from app.models.admissions import Applicant
    from app.services import entrance_exam, lab_jobs
    from app.services.entrance_invitation_policy import entrance_invitation_policy

    with lab_jobs.admin_session() as db:
        tenant = db.scalars(select(Tenant).where(Tenant.slug == args.tenant_slug)).first()
        if tenant is None:
            raise SystemExit(f"Tenant '{args.tenant_slug}' not found.")

        policy = entrance_invitation_policy(db, deadline_days=args.deadline_days)
        policy.deadline(datetime.now(UTC))  # Validate representability before token/outbox effects.
        deadline_days = policy.deadline_days

        stmt = (
            select(Applicant)
            .where(Applicant.tenant_id == tenant.id)
            .where(Applicant.assessment_taken_at.is_(None))  # never sat it
        )
        if args.cohort_id:
            import uuid

            stmt = stmt.where(Applicant.cohort_id == uuid.UUID(args.cohort_id))
        if not args.resend:
            stmt = stmt.where(Applicant.invite_sent_at.is_(None))  # not already invited
        if args.email:
            stmt = stmt.where(Applicant.email == args.email)
        targets = list(db.scalars(stmt).all())

        print(f"{len(targets)} applicant(s) to invite (deadline {deadline_days} days, base {args.base_url})")
        if args.dry_run:
            for a in targets[:20]:
                print(f"  DRY-RUN would email {a.email}")
            if len(targets) > 20:
                print(f"  ... and {len(targets) - 20} more")
            print("\nDRY RUN — no tokens minted and no email queued.")
            return

        queued = failed = skipped = 0
        for a in targets:
            if not entrance_exam.has_entrance_exam(db, applicant=a):
                skipped += 1
                continue
            res = entrance_exam.invite(db, applicant=a, base_url=args.base_url, deadline_days=deadline_days)
            if res["emailed"]:
                queued += 1
            else:
                failed += 1
                print(f"  !! invitation could not be queued for {a.email}")
        db.commit()
        print(f"\nqueued: {queued}   queue failures: {failed}   no exam configured: {skipped}")
        if failed:
            print("NOTE: queue failures did not expose or print invitation tokens.")


def _reset_entrance_exam(args: argparse.Namespace) -> None:
    """Reopen an applicant's entrance sitting and mint a fresh exam link.

    The recovery path when a candidate loses their one attempt to a dropped
    connection, a dead battery, or a clock that ran down while they were offline.
    Without this, a network blip permanently locks a good candidate out.
    """
    from dotmac_kernel.models import Tenant
    from sqlalchemy import select

    from app.models.admissions import Applicant
    from app.services import entrance_exam, lab_jobs

    with lab_jobs.admin_session() as db:
        tenant = db.scalars(select(Tenant).where(Tenant.slug == args.tenant_slug)).first()
        if tenant is None:
            raise SystemExit(f"Tenant '{args.tenant_slug}' not found.")
        applicant = db.scalars(
            select(Applicant).where(Applicant.tenant_id == tenant.id).where(Applicant.email == args.email)
        ).first()
        if applicant is None:
            raise SystemExit(f"No applicant with email '{args.email}' in '{args.tenant_slug}'.")

        had = applicant.assessment_taken_at is not None
        queued = entrance_exam.reset_and_invite(
            db,
            applicant=applicant,
            base_url=args.base_url,
        )
        db.commit()
        print(f"reset entrance sitting for {applicant.email} (reset #{applicant.assessment_reset_count})")
        if had:
            print("  note: a completed result was discarded — they now re-sit from scratch.")
        print(f"  replacement invitation queued={queued}")


def _recompute_entrance_levels(args: argparse.Namespace) -> None:
    """Re-derive level bands from a cohort's ACTUAL score distribution.

    The built-in floors (beginner/intermediate/advanced) are a *prediction* of item
    difficulty, not a measurement of it. If the real cohort clusters differently,
    absolute cut-offs mis-stream everyone — e.g. an empty "advanced" band.

    Percentile banding is self-calibrating: bottom 25% -> beginner, top 25% ->
    advanced. Computed over VALID sittings only (a near-chance or click-through
    result is an absence of data and must not drag the distribution).
    """
    import uuid

    from dotmac_kernel.models import Tenant
    from sqlalchemy import select

    from app.models.admissions import Applicant
    from app.services import lab_jobs

    with lab_jobs.admin_session() as db:
        tenant = db.scalars(select(Tenant).where(Tenant.slug == args.tenant_slug)).first()
        if tenant is None:
            raise SystemExit(f"Tenant '{args.tenant_slug}' not found.")

        stmt = (
            select(Applicant)
            .where(Applicant.tenant_id == tenant.id)
            .where(Applicant.assessment_score.is_not(None))
            .where(Applicant.assessment_valid.is_not(False))
        )
        if args.cohort_id:
            stmt = stmt.where(Applicant.cohort_id == uuid.UUID(args.cohort_id))
        # The query already excludes NULL scores; pair each applicant with its score
        # so the band comparisons below are over plain floats.
        rows = [(a, a.assessment_score) for a in db.scalars(stmt).all() if a.assessment_score is not None]

        if len(rows) < args.min_cohort:
            raise SystemExit(
                f"only {len(rows)} valid sitting(s) — below --min-cohort {args.min_cohort}. "
                "Percentiles on a handful of scores are noise; leaving bands as they are."
            )

        scores = sorted(score for _, score in rows)
        n = len(scores)
        p25 = scores[int(0.25 * (n - 1))]
        p75 = scores[int(0.75 * (n - 1))]
        print(f"valid sittings: {n}   p25={p25:.3f}  p75={p75:.3f}")

        if args.dry_run:
            print("DRY RUN — no levels written.")
            return

        changed = 0
        for a, score in rows:
            band = "beginner" if score <= p25 else "advanced" if score >= p75 else "intermediate"
            if a.assessment_level != band:
                a.assessment_level = band
                a.assessment_result_version = (a.assessment_result_version or 0) + 1
                a.assessment_erp_synced_at = None
                changed += 1
        db.commit()
        print(f"re-banded {changed} applicant(s) by percentile (bottom 25% / middle / top 25%).")


class _WorkerShutdownRequested(BaseException):
    """Raised by the lab worker's own SIGTERM handler (see
    :func:`_sigterm_raises_shutdown_requested`).

    Subclasses ``BaseException``, not ``Exception`` — mirroring
    ``KeyboardInterrupt``/``SystemExit``'s own convention — for two reasons:
    it must never be accidentally swallowed by a bare ``except Exception:``
    anywhere in the call stack (e.g. a containerlab call site's
    ``RuntimeError`` handling), and it must be caught and re-raised by
    ``_run_contained``'s existing ``except BaseException:`` cleanup path
    exactly like it already handles ``KeyboardInterrupt`` today.
    """


@contextmanager
def _sigterm_raises_shutdown_requested() -> Iterator[None]:
    """Convert a received SIGTERM into a Python-level exception.

    Python's default SIGTERM disposition terminates the process immediately
    with no exception raised anywhere — unlike SIGINT, which Python's own
    default handling turns into ``KeyboardInterrupt``. Without this, a bare
    ``kill <pid>`` against a manually-launched worker bypasses every cleanup
    path in this process (the singleton lock's own ``with``-block teardown,
    ``_run_contained``'s process-group cleanup) because the process simply
    dies mid-instruction, leaking a detached ``start_new_session=True``
    subprocess tree that the systemd-managed worker's cgroup-wide
    ``KillMode=control-group`` backstop never covers for a manual invocation.

    Restores whatever SIGTERM handler was previously installed on exit, for
    any reason — including when :class:`_WorkerShutdownRequested` itself is
    the reason for exiting. This matters both for real-world correctness
    (repeat calls to ``_lab_worker`` in the same process must not stack
    handlers) and because tests call ``_lab_worker`` in-process: leaving a
    real SIGTERM handler installed in the pytest process itself would be a
    hazard for the rest of that run.
    """

    def _handler(signum: int, frame: object) -> None:
        raise _WorkerShutdownRequested()

    previous = signal.signal(signal.SIGTERM, _handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _lab_worker(args: argparse.Namespace) -> None:
    import shutil

    from app.config import settings, validate_settings
    from app.services import lab_jobs, lab_operations
    from app.services.host_lock import HostLockUnavailable, worker_singleton_lock
    from app.services.labengine.containerlab import ContainerlabEngine
    from app.services.settings_store import execution_policy

    if settings.lab_host_role.lower() != "lab":
        raise SystemExit("lab-worker requires LAB_HOST_ROLE=lab")
    if shutil.which("containerlab") is None:
        raise SystemExit("lab-worker requires containerlab on PATH")
    errors = validate_settings(settings)
    if errors:
        raise SystemExit("; ".join(errors))
    # The singleton lock is held for this whole process's lifetime (acquired
    # before any database session or reclaim/drain work), not per-iteration
    # like host_lock — see worker_singleton_lock's docstring. A second worker
    # process racing this one (manual invocation, or two systemd instances)
    # fails fast here instead of both later contending on every containerlab
    # call. The SIGTERM handler is installed around it (early, before the
    # lock is even attempted) so that a bare `kill` against a manually
    # launched worker unwinds through the lock's own `with`-block teardown
    # instead of killing the process with no cleanup running at all.
    try:
        with _sigterm_raises_shutdown_requested(), worker_singleton_lock():
            # Startup restart-reclaim: exactly once, immediately after the
            # singleton lock is held and before any ordinary poll iteration.
            # Computed once here and passed explicitly into every later
            # drain_once() call below, so the identity used to reclaim this
            # host's own previous-epoch claims is the exact same value later
            # used to make this process's own claims — never recomputed.
            identity = lab_operations.worker_identity_parts()
            with lab_jobs.lab_worker_session() as db:
                try:
                    startup_policy = execution_policy(db)
                    reclaimed = lab_operations.reclaim_previous_epoch(
                        db, host=identity.host, epoch=identity.epoch, policy=startup_policy
                    )
                    db.commit()
                except BaseException:
                    # Includes _WorkerShutdownRequested (a SIGTERM arriving
                    # mid-reclaim), which deliberately subclasses
                    # BaseException rather than Exception. Postgres
                    # transactions are atomic, so an interrupted commit
                    # either fully lands or not at all — either outcome here
                    # is safe. Re-raised so the outer
                    # except _WorkerShutdownRequested: handler below still
                    # catches it with no further changes needed there.
                    db.rollback()
                    raise
            if reclaimed:
                print(f"reclaimed {reclaimed} operation(s) from a previous worker epoch on this host")
            print("lab-worker started; draining durable operations")
            while True:
                with lab_jobs.lab_worker_session() as db:
                    policy = execution_policy(db)
                    engine = ContainerlabEngine(
                        settings.lab_workdir, execution_policy=policy, lock_label="academy-lab-worker"
                    )
                    repaired = lab_operations.reconcile_stuck(db, policy=policy)
                    db.commit()
                    recovered = lab_jobs.recover_missing_consoles(db, engine, policy=policy)
                    db.commit()
                    n = lab_jobs.drain_once(
                        db,
                        engine,
                        policy=policy,
                        claimed_by=identity.claimed_by,
                        claimed_host=identity.host,
                        claimed_epoch=identity.epoch,
                    )
                if repaired:
                    print(f"reconciled {repaired} lab operation(s)")
                if recovered:
                    print(f"recovered {recovered} missing lab console(s)")
                if n:
                    print(f"provisioned {n} lab(s)")
                time.sleep(policy.lab_worker_poll_interval_seconds)
    except HostLockUnavailable as exc:
        raise SystemExit(f"lab-worker: {exc}") from exc
    except _WorkerShutdownRequested:
        print("lab-worker: SIGTERM received; shutting down")


def _reap_labs(args: argparse.Namespace) -> None:
    from app.services import lab_jobs

    with lab_jobs.admin_session() as db:
        requested = lab_jobs.request_idle_reaps(db)
    print(f"enqueued {requested} idle lab destroy operation(s)")


def _lab_reconcile(args: argparse.Namespace) -> None:
    import shutil

    from app.config import settings, validate_settings
    from app.services import lab_jobs, lab_operations
    from app.services.host_lock import HostLockUnavailable
    from app.services.labengine.containerlab import ContainerlabEngine
    from app.services.settings_store import execution_policy

    if settings.lab_host_role.lower() != "lab":
        raise SystemExit("lab-reconcile requires LAB_HOST_ROLE=lab")
    if shutil.which("containerlab") is None:
        raise SystemExit("lab-reconcile requires containerlab on PATH")
    errors = validate_settings(settings)
    if errors:
        raise SystemExit("; ".join(errors))
    with lab_jobs.lab_worker_session() as db:
        policy = execution_policy(db)
        engine = ContainerlabEngine(settings.lab_workdir, execution_policy=policy, lock_label="academy-lab-reconcile")
        requeued = lab_operations.reconcile_stuck(db, policy=policy)
        # Commit the stuck-claim reconciliation in its own short transaction —
        # its FOR UPDATE SKIP LOCKED scan takes a row lock on every currently
        # claimed operation (not just the stuck ones; "stuck" is decided in
        # Python after the rows are already locked), so holding it open through
        # reconcile_runtime's live containerlab inspect/destroy calls would
        # block a healthy worker's heartbeat()/_settle() UPDATE on those same
        # rows for the duration of that external work. _lab_worker's loop
        # already commits between reconcile_stuck and drain_once for the same
        # reason.
        db.commit()
        try:
            runtime_repairs, destroyed_orphans = lab_jobs.reconcile_runtime(db, engine, policy=policy)
        except HostLockUnavailable as exc:
            # reconcile_runtime makes no database writes before it can raise
            # this (see its phase breakdown), so there is nothing this pass
            # wrote that needs undoing — the rollback below is still explicit
            # rather than assumed. Skip the console sweep and its commit too:
            # this whole pass is abandoned, and the 1-minute timer will retry
            # it from scratch on its own.
            db.rollback()
            print(f"lab-reconcile: skipped this pass — {exc}")
            return
        orphans = lab_jobs.sweep_orphan_consoles(db)
        db.commit()
    print(
        f"reconciled {requeued} expired/cutover operation(s); "
        f"queued {runtime_repairs} runtime repair(s); "
        f"destroyed {destroyed_orphans} rowless lab(s); "
        f"killed {orphans} orphan console(s)"
    )


def _load_curriculum(args: argparse.Namespace) -> None:
    """Parse product YAML and delegate prerequisite reconciliation to Learning.

    The tenant session remains the transaction authority. Learning stages graph
    changes and counts; this CLI retains dry-run rollback and ordinary commit.
    Typed commands preserve supported string slugs, not arbitrary malformed
    Python/YAML error ordering.

    HM-01 creation defaults are materialized here, never in Learning: an
    entry carrying explicit ``conditions`` (or ``composition``) is
    authoritative for its course, so an omitted composition falls back to
    the composed creation default. A pure legacy ``requires`` entry stays
    on the legacy path at the historical ``course_completed`` default, so
    re-running an old file never restates authored conditions or
    composition; a non-default creation kind is materialized into typed
    conditions only when it needs no parameters a slug cannot supply.
    """
    import dotmac_learning as learning
    import yaml

    from app.services.learning_policy import creation_defaults

    doc = yaml.safe_load(args.file.read_text())["curriculum"]
    declared = doc.get("prerequisites") or []

    with _tenant_session(args.tenant_slug) as (db, tenant):
        defaults = creation_defaults(db)
        declarations = []
        for entry in declared:
            course = entry["course"]
            raw_conditions = entry.get("conditions") or []
            composition = entry.get("composition")
            requires = entry.get("requires") or []
            if raw_conditions:
                declarations.append(
                    learning.PrerequisiteDeclaration(
                        course=course,
                        requires=cast("tuple[str, ...]", requires),
                        conditions=tuple(
                            learning.PrerequisiteConditionDeclaration(**condition) for condition in raw_conditions
                        ),
                        composition=cast(
                            "Literal['all', 'any'] | None",
                            composition or defaults.prerequisite_default_composition,
                        ),
                    )
                )
                continue
            if requires and defaults.prerequisite_default_kind != "course_completed":
                if defaults.prerequisite_default_kind != "certificate_held":
                    raise SystemExit(
                        f"learning_prerequisite_default_kind {defaults.prerequisite_default_kind!r} "
                        "needs typed conditions in the curriculum file; legacy requires "
                        "entries cannot supply its parameters"
                    )
                declarations.append(
                    learning.PrerequisiteDeclaration(
                        course=course,
                        conditions=tuple(
                            learning.PrerequisiteConditionDeclaration(
                                kind=defaults.prerequisite_default_kind,
                                requires=slug,
                            )
                            for slug in requires
                        ),
                        composition=cast(
                            "Literal['all', 'any'] | None",
                            composition or defaults.prerequisite_default_composition,
                        ),
                    )
                )
                continue
            declarations.append(
                learning.PrerequisiteDeclaration(
                    course=course,
                    requires=cast("tuple[str, ...]", requires),
                    composition=composition,
                )
            )
        command = learning.ReconcilePrerequisitesCommand(
            tenant_id=tenant.id,
            discipline=doc.get("discipline"),
            declarations=tuple(declarations),
        )
        try:
            result = learning.reconcile_prerequisites(db, command)
        except learning.PrerequisiteReconciliationError as exc:
            raise SystemExit(str(exc)) from exc

        if args.dry_run:
            db.rollback()
            print(f"[dry-run] would add {result.added}, remove {result.removed}")
            return
        db.commit()
        print(f"prerequisites: {result.added} added, {result.removed} removed, {result.declared} declared")


def main() -> None:
    p = argparse.ArgumentParser(
        prog="app.cli",
        description="Platform management CLI for dotmac_academy_v2.",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    b = sub.add_parser(
        "bootstrap-tenant",
        help="Create a tenant and initial owner using configured bootstrap policy.",
        description=(
            "Create a tenant, configured roles, and an initial owner through the offline Kernel owner. "
            "Requires an explicitly configured login/effective database-role pair and actual write privileges."
        ),
    )
    b.add_argument("--slug", required=True, help="URL-safe tenant identifier")
    b.add_argument("--name", required=True, help="Human-readable tenant name")
    b.add_argument("--admin-email", required=True, help="Email for the initial admin user")
    b.add_argument("--admin-password", required=True, help="Password for the initial admin user")
    b.set_defaults(func=_bootstrap)

    ob = sub.add_parser(
        "oidc-bind",
        help="Bind one exact Keycloak subject to one staff party (OIDC pilot).",
        description=(
            "Explicit operator act: bind the exact Keycloak subject (sub) for "
            "a staff member to their Academy party. No email matching and no "
            "just-in-time provisioning happen at login."
        ),
    )
    ob.add_argument("--tenant-slug", required=True, help="Slug of the target tenant")
    ob.add_argument("--email", required=True, help="The staff member's Academy email (lookup only)")
    ob.add_argument("--subject", required=True, help="Exact Keycloak subject (sub) from the IdP admin console")
    ob.add_argument("--bound-by", required=True, help="Operator identity recorded on the binding")
    ob.add_argument("--reason", required=True, help="Evidence recorded on the binding")
    ob.set_defaults(func=_oidc_bind)

    od = sub.add_parser(
        "oidc-disable",
        help="Disable a party's Keycloak bindings and revoke their OIDC sessions.",
    )
    od.add_argument("--tenant-slug", required=True, help="Slug of the target tenant")
    od.add_argument("--email", required=True, help="The staff member's Academy email (lookup only)")
    od.set_defaults(func=_oidc_disable)

    imp = sub.add_parser(
        "import-foundation",
        help="Import the Foundation manual markdown files as rendered HTML chapters.",
        description=(
            "Parse chapter-*.md files from the Foundation manual directory and upsert "
            "them into the database as Course/Chapter records for the given tenant. "
            "Idempotent — re-running skips unchanged chapters and only bumps Course.version "
            "when content changed."
        ),
    )
    imp.add_argument("--tenant-slug", required=True, help="Slug of the target tenant")
    imp.add_argument(
        "--chapters-dir",
        type=Path,
        default=_DEFAULT_CHAPTERS_DIR,
        help="Directory containing chapter-*.md files (default: Foundation manual)",
    )
    imp.add_argument(
        "--figures-dir",
        type=Path,
        default=_DEFAULT_FIGURES_DIR,
        help="Directory containing produced figure PNG files (default: figures/final)",
    )
    imp.add_argument(
        "--allow-missing-figures",
        action="store_true",
        help="Import chapters with placeholder blocks for missing figures.",
    )
    imp.set_defaults(func=_import_foundation)

    im = sub.add_parser(
        "import-manual",
        help="Import any manual's chapters as a course (generic over Foundation/Fiber/etc.).",
        description=(
            "Parse chapter-*.md from --chapters-dir and upsert them as a Course "
            "(identified by --slug) plus its Chapters for the tenant. Idempotent. "
            "Use this for any manual, e.g. fiber-engineering."
        ),
    )
    im.add_argument("--tenant-slug", required=True, help="Slug of the target tenant")
    im.add_argument("--slug", required=True, help="Course slug, e.g. fiber-engineering")
    im.add_argument("--title", required=True, help="Course title, e.g. 'Fiber Engineering'")
    im.add_argument("--discipline", default="networking", help="Discipline tag (default: networking)")
    im.add_argument("--source-ref", default=None, help="Provenance string (default: <slug>@0.1.0)")
    im.add_argument(
        "--chapters-dir",
        type=Path,
        required=True,
        help="Directory containing chapter-*.md files",
    )
    im.add_argument(
        "--figures-dir",
        type=Path,
        default=_DEFAULT_FIGURES_DIR,
        help="Directory containing produced figure PNG files",
    )
    im.add_argument(
        "--allow-missing-figures",
        action="store_true",
        help="Import chapters with placeholder blocks for missing figures.",
    )
    im.set_defaults(func=_import_manual)

    lb = sub.add_parser(
        "load-banks",
        aliases=["import-banks-as-draft"],
        help="Import YAML MCQ question banks as unpublished drafts for a tenant.",
        description=(
            "For each *.yaml file in --banks-dir: parse the bank and import it as a "
            "Learning DRAFT version. Import never publishes, never overwrites a UI "
            "draft, never repoints activities/entrance defaults, and never touches "
            "Activity rows; YAML policy is reported as unapplied proposals. "
            "Lint-failing banks become unrepublishable drafts for UI repair."
        ),
    )
    lb.add_argument("--tenant-slug", required=True, help="Slug of the target tenant")
    lb.add_argument(
        "--banks-dir",
        type=Path,
        default=_DEFAULT_BANKS_DIR,
        help="Directory containing *.yaml bank files (default: Foundation assessments/banks)",
    )
    lb.add_argument(
        "--replace-imported-draft",
        action="store_true",
        help="Replace an existing untouched YAML-imported draft (UI drafts are never replaced).",
    )
    lb.set_defaults(func=_load_banks)

    eb = sub.add_parser(
        "export-bank",
        help="Export one bank version to a YAML file (audited; contains answer keys).",
        description=(
            "Render the bank's current published version (or --version-id) to the "
            "YAML transport shape with provenance comments. The export is audited "
            "and written only to --output; keys are never printed."
        ),
    )
    eb.add_argument("--tenant-slug", required=True, help="Slug of the target tenant")
    eb.add_argument("--course", required=True, help="Course slug owning the bank")
    eb.add_argument("--chapter", type=int, default=None, help="Bank chapter number (omit for non-chapter banks)")
    eb.add_argument("--kind", required=True, help="Bank kind, e.g. chapter or final")
    eb.add_argument("--version-id", type=UUID, default=None, help="Specific version UUID (default: current published)")
    eb.add_argument("--output", type=Path, required=True, help="YAML file to write (contains answer keys)")
    eb.set_defaults(func=_export_bank)

    lc = sub.add_parser(
        "load-curriculum",
        help="Apply a CURRICULUM.yaml prerequisite graph for a tenant.",
        description=(
            "The canonical writer for CoursePrerequisite, which entitlements enforces "
            "but nothing previously created. The file is authoritative: declared edges "
            "are added, and edges no longer declared are removed, so the table stays a "
            "projection of the file. Refuses to write on an unknown slug or a cycle."
        ),
    )
    lc.add_argument("--tenant-slug", required=True, help="Slug of the target tenant")
    lc.add_argument("--file", type=Path, required=True, help="Path to CURRICULUM.yaml")
    lc.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    lc.set_defaults(func=_load_curriculum)

    ab = sub.add_parser(
        "audit-banks",
        help="Lint every question bank already live in the database.",
        description=(
            "load-banks enforces the bank rules on the way in. That leaves banks "
            "loaded before a rule existed live and non-compliant with nothing to "
            "say so. This runs the same lint_bank over the database, so the state "
            "of the estate is a question anyone can answer rather than a query "
            "someone has to write."
        ),
    )
    ab.add_argument("--tenant-slug", required=True, help="Slug of the target tenant")
    ab.add_argument("--discipline", default=None, help="Restrict to one discipline")
    ab.add_argument("--course", default=None, help="Restrict to one course slug")
    ab.add_argument(
        "--fail-on-violations",
        action="store_true",
        help="Exit 1 if any bank fails, for use as a scheduled check.",
    )
    ab.set_defaults(func=_audit_banks)

    il = sub.add_parser(
        "import-labs",
        help="Load labs-as-code (lab.yaml dirs) as Activity(type='lab')+LabTemplate.",
        description=(
            "For each <labs-dir>/*/lab.yaml: parse the lab definition (topology + "
            "instructions + checks + seed_spec + limits), render instructions to HTML "
            "(resolving $include directives against --chapters-dir), and upsert a paired "
            "Activity(type='lab') and LabTemplate keyed by (course, slug). Idempotent — "
            "unchanged labs are skipped and version only bumps when content changes."
        ),
    )
    il.add_argument("--tenant-slug", required=True, help="Slug of the target tenant")
    il.add_argument(
        "--course-slug",
        default="foundation",
        help="Course slug to attach labs to (default: foundation)",
    )
    il.add_argument(
        "--labs-dir",
        type=Path,
        default=_DEFAULT_LABS_DIR,
        help="Directory containing <slug>/lab.yaml lab dirs (default: Foundation labs)",
    )
    il.add_argument(
        "--chapters-dir",
        type=Path,
        default=_DEFAULT_CHAPTERS_DIR,
        help="Directory of chapter-*.md files for $include resolution (default: Foundation chapters)",
    )
    il.set_defaults(func=_import_labs)

    lw = sub.add_parser(
        "lab-worker",
        help="Run the cross-tenant durable lab-operation worker.",
        description=(
            "Long-running background worker: every ~5s, opens an academy_lab_worker "
            "(BYPASSRLS) session and claims queued lab operations across all tenants. "
            "It must run with LAB_HOST_ROLE=lab on the containerlab host. Intended to "
            "run under systemd (academy-lab-worker.service, Restart=always)."
        ),
    )
    lw.set_defaults(func=_lab_worker)

    rl = sub.add_parser(
        "reap-labs",
        help="One-shot: enqueue destroy operations for idle lab instances.",
        description=(
            "Find active lab instances idle longer than LAB_IDLE_MINUTES and enqueue "
            "worker-owned destroy intents. This command never imports or invokes the "
            "containerlab engine and is safe on the web host."
        ),
    )
    rl.set_defaults(func=_reap_labs)

    lr = sub.add_parser(
        "lab-reconcile",
        help="One-shot lab-host recovery: repair expired claims and runtime drift.",
    )
    lr.set_defaults(func=_lab_reconcile)

    ed = sub.add_parser(
        "email-digest",
        help="One-shot: email each cohort's instructor(s) a progress digest.",
        description=(
            "Cross-tenant: open an offline app_admin (BYPASSRLS) session, build the "
            "cohort progress matrix for every cohort in every tenant, and email "
            "each cohort's enrolled instructor(s) a summary. Email failures are "
            "non-fatal. Intended to run on a timer "
            "(academy-email-digest.timer -> academy-email-digest.service oneshot)."
        ),
    )
    ed.set_defaults(func=_email_digest)

    ld = sub.add_parser(
        "learner-digest",
        help="One-shot: email each learner their weekly progress summary.",
        description=(
            "Cross-tenant: open an offline app_admin (BYPASSRLS) session and queue one "
            "weekly progress email per active student learner — what they did, "
            "where they are, what is next. Distinct from email-digest, which is "
            "the instructor cohort matrix. Idempotent per ISO week. Gated by the "
            "learner_digest_enabled Academy setting, which is OFF by default."
        ),
    )
    ld.add_argument(
        "--base-url",
        default="https://academy.dotmac.io",
        help="Fallback public base URL when academy_base_url is unset",
    )
    ld.add_argument("--dry-run", action="store_true", help="Count recipients without queueing")
    ld.add_argument(
        "--force",
        action="store_true",
        help="Run even when learner_digest_enabled is off (use with --dry-run to preview)",
    )
    ld.set_defaults(func=_learner_digest)
    ca = sub.add_parser(
        "classify-audience",
        help="Mark enrolments staff/external from an ERP roster export (ADR 0004)",
        description=(
            "Reads CSV 'work_email,employee_ref' — ERP's answer, not a guess. "
            "Matches are marked staff and carry their employee reference; "
            "anything absent stays unclassified and is listed for review, "
            "because audience is never inferred from an email domain."
        ),
    )
    ca.add_argument("--tenant-slug", required=True)
    ca.add_argument("--roster-file", required=True, help="CSV: work_email,employee_ref")
    ca.add_argument(
        "--assume-roster-complete",
        action="store_true",
        help="Mark every non-match as external (only if the export covers ALL employees)",
    )
    ca.add_argument("--dry-run", action="store_true")
    ca.set_defaults(func=_classify_audience)

    hr = sub.add_parser(
        "hr-report",
        help="Email HR the staff-only training roll-up",
        description=(
            "Counts only enrolments explicitly marked staff. Unclassified "
            "learners are excluded and the count of them is stated in the mail, "
            "so a partial roster cannot be mistaken for a complete picture."
        ),
    )
    hr.add_argument("--to", required=True, help="Comma-separated recipients, e.g. hr@dotmac.ng")
    hr.add_argument("--days", type=int, default=7, help="Reporting window (default 7)")
    hr.add_argument("--dry-run", action="store_true", help="Print the report without queueing")
    hr.set_defaults(func=_hr_report)

    eo = sub.add_parser(
        "email-outbox",
        help="Deliver committed email intents with durable retries.",
    )
    eo.add_argument("--limit", type=int, default=100, help="Maximum due messages to process")
    eo.add_argument(
        "--requeue-failed",
        action="store_true",
        help="Requeue terminal failures before attempting delivery",
    )
    eo.set_defaults(func=_email_outbox)
    eo.add_argument(
        "--requeue-failed-lab-deliveries",
        action="store_true",
        help="Requeue terminal lab delivery failures before materializing",
    )

    rsw = sub.add_parser("reminders-sweep", help="Detect due student reminders and queue delivery")
    rsw.set_defaults(func=_reminders_sweep)

    ar = sub.add_parser(
        "at-risk-sweep",
        help="DEPRECATED: superseded by the Success Queue (runs inside "
        "reminders-sweep); now only sends legacy in-app nudges",
    )
    ar.set_defaults(func=_at_risk_sweep)

    ets = sub.add_parser("erp-training-sync", help="Push staff training progress/completions using ERP v2")
    ets.set_defaults(func=_erp_training_sync)

    seb = sub.add_parser("set-entrance-bank", help="Designate a cohort's entrance-assessment bank")
    seb.add_argument("--cohort-id", required=True)
    seb.add_argument("--bank-id", required=True)
    seb.add_argument(
        "--time-limit-minutes", type=int, default=None, help="Per-sitting time limit (0 or omit = untimed)"
    )
    seb.set_defaults(func=_set_entrance_bank)

    arp = sub.add_parser("admin-report", help="Email tenant admins the admissions/learning activity report")
    arp.add_argument(
        "--hours", type=int, default=None, help="Reporting window in hours (omission uses effective Academy settings)"
    )
    arp.set_defaults(func=_admin_report)

    saa = sub.add_parser("set-auto-accept", help="Auto-accept valid entrance sittings at/above a score threshold")
    saa.add_argument("--cohort-id", required=True)
    saa.add_argument(
        "--threshold", type=float, default=None, help="Fraction 0..1 (e.g. 0.6); omit to turn auto-accept off"
    )
    saa.set_defaults(func=_set_auto_accept)

    sdb = sub.add_parser("set-default-entrance-bank", help="Academy-wide default entrance bank (all applicants sit it)")
    sdb.add_argument("--tenant-slug", required=True)
    sdb.add_argument("--bank-id", required=True)
    sdb.add_argument(
        "--time-limit-minutes", type=int, default=None, help="Per-sitting time limit (0 or omit = untimed)"
    )
    sdb.set_defaults(func=_set_default_entrance_bank)

    inv = sub.add_parser(
        "invite-applicants",
        help="Email the entrance-assessment invitation to applicants who haven't sat it",
    )
    inv.add_argument("--tenant-slug", required=True)
    inv.add_argument(
        "--base-url", default="https://academy.dotmac.io", help="Public base URL used to build the exam link"
    )
    inv.add_argument(
        "--deadline-days",
        type=int,
        default=None,
        help="Days the link stays valid (omit to use the configured Settings default)",
    )
    inv.add_argument("--cohort-id", default=None, help="Limit to one intake")
    inv.add_argument("--email", default=None, help="Just this one applicant (use to test first)")
    inv.add_argument(
        "--resend",
        action="store_true",
        help="Also re-invite applicants already emailed (mints a NEW token, killing the old link)",
    )
    inv.add_argument("--dry-run", action="store_true")
    inv.set_defaults(func=_invite_applicants)

    ris = sub.add_parser(
        "reinvite-stranded",
        help="Re-issue activation links to enrolled learners who never set a password",
    )
    ris.add_argument("--tenant-slug", required=True)
    ris.add_argument(
        "--base-url", default="https://academy.dotmac.io", help="Public base URL used to build the activation link"
    )
    ris.add_argument("--cohort-id", default=None, help="Limit to one cohort")
    ris.add_argument("--dry-run", action="store_true")
    ris.set_defaults(func=_reinvite_stranded)

    rex = sub.add_parser(
        "reset-entrance-exam",
        help="Reopen an applicant's entrance sitting (dropped connection / lost attempt)",
    )
    rex.add_argument("--tenant-slug", required=True)
    rex.add_argument("--email", required=True, help="Applicant's email")
    rex.add_argument("--base-url", default="https://academy.dotmac.io")
    rex.set_defaults(func=_reset_entrance_exam)

    rel = sub.add_parser(
        "recompute-entrance-levels",
        help="Re-derive level bands from the cohort's real score distribution (percentiles)",
    )
    rel.add_argument("--tenant-slug", required=True)
    rel.add_argument("--cohort-id", default=None, help="Limit to one intake (default: all)")
    rel.add_argument(
        "--min-cohort",
        type=int,
        default=20,
        help="Refuse below this many valid sittings — percentiles on few scores are noise",
    )
    rel.add_argument("--dry-run", action="store_true")
    rel.set_defaults(func=_recompute_entrance_levels)

    sss = sub.add_parser(
        "settings-shadow-sweep",
        help="ADR-0003 phase gate: kernel vs legacy settings resolution (read-only, prints no values)",
    )
    sss.set_defaults(func=_settings_shadow_sweep)

    args = p.parse_args()
    bind_academy_database_runtime()
    args.func(args)


if __name__ == "__main__":
    main()
