"""Natural hosted independent-connection races; authored NOT RUN."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from dotmac_kernel.models import Party
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.services import admissions, onboarding
from app.services import invitation_issuance as issuance
from app.services.security import hash_password
from legacy_probe.services.test_canonical_invitation_issuance import _credential, _person
from legacy_probe.services.test_canonical_onboarding import onboarding_case
from legacy_probe.services.test_canonical_self_profile_locking import _bind, _wait_for_lock

assert onboarding_case


@pytest.mark.parametrize("mutation", ["rotation", "rejection", "intake", "reset", "seed"])
def test_applicant_and_task_writers_serialize_with_bearer_completion(onboarding_case, mutation):
    c = onboarding_case
    c.a.db.commit()
    SessionLocal = sessionmaker(bind=c.a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []

    def holder():
        with SessionLocal() as db:
            _bind(db, c.a.tid)
            applicant = onboarding._lock_applicant(db, tenant_id=c.a.tid, applicant_id=c.id)
            if mutation == "rotation":
                admissions.mint_onboarding_token(db, applicant=applicant)
            elif mutation == "rejection":
                # The actual rejection writer's Applicant-first storage phase;
                # its admin audit attribution remains separately owed.
                applicant.status = "rejected"
                db.flush()
            elif mutation == "intake":
                applicant.cohort_id = None
                db.flush()
            elif mutation == "reset":
                task = next(
                    t
                    for t in onboarding.list_tasks(db, tenant_id=c.a.tid, applicant_id=c.id)
                    if t.key == "confirm_details"
                )
                onboarding.set_task_status(db, task_id=task.id, status="pending")
            else:
                onboarding.seed_tasks(db, tenant_id=c.a.tid, applicant_id=c.id)
            locked.set()
            assert release.wait(10)
            db.commit()

    def writer():
        with SessionLocal() as db:
            _bind(db, c.a.tid)
            backend.append(db.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            try:
                with issuance.finish_onboarding_step(db, tenant_id=c.a.tid, raw=c.raw, key="orientation") as result:
                    enrolled = result is not None and result.enrolled_now
                db.commit()
                return enrolled
            except issuance.InvitationIssuanceError:
                db.commit()  # recovered savepoint is safe for the outer caller
                return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(holder)
        assert locked.wait(10)
        follower = pool.submit(writer)
        assert started.wait(10)
        try:
            _bind(c.a.db, c.a.tid)
            _wait_for_lock(c.a.db, backend[0])
        finally:
            release.set()
        leader.result(timeout=20)
        outcome = follower.result(timeout=20)
    assert (
        outcome
        == ({"rotation": "refused", "intake": "refused", "rejection": False, "reset": False, "seed": True}[mutation])
    )


def test_party_first_credential_creation_suppresses_first_password_invitation(onboarding_case):
    c = onboarding_case
    person = _person(c.a.db, tenant_id=c.a.tid, email=c.email, first_name="Existing", last_name="Learner")
    c.a.db.commit()
    SessionLocal = sessionmaker(bind=c.a.db.get_bind(), autoflush=False)
    locked, release, started = Event(), Event(), Event()
    backend = []

    def holder():
        with SessionLocal() as db:
            _bind(db, c.a.tid)
            db.scalars(select(Party).where(Party.id == person.id, Party.tenant_id == c.a.tid).with_for_update()).one()
            _credential(
                db,
                tenant_id=c.a.tid,
                person_id=person.id,
                email="different-alias@a.edu",
                password_hash=hash_password("chosen-password9"),
            )
            locked.set()
            assert release.wait(10)
            db.commit()

    def writer():
        with SessionLocal() as db:
            _bind(db, c.a.tid)
            backend.append(db.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            with issuance.finish_onboarding_step(db, tenant_id=c.a.tid, raw=c.raw, key="orientation") as result:
                assert result.enrolled_now and result.invite_raw is None
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(holder)
        assert locked.wait(10)
        follower = pool.submit(writer)
        assert started.wait(10)
        try:
            _bind(c.a.db, c.a.tid)
            _wait_for_lock(c.a.db, backend[0])
        finally:
            release.set()
        leader.result(timeout=20)
        follower.result(timeout=20)


def test_duplicate_completion_enrolls_once(onboarding_case):
    c = onboarding_case
    c.a.db.commit()
    SessionLocal = sessionmaker(bind=c.a.db.get_bind(), autoflush=False)
    entered, release, started = Event(), Event(), Event()
    backend = []

    def writer(first):
        with SessionLocal() as db:
            _bind(db, c.a.tid)
            if not first:
                backend.append(db.scalar(text("SELECT pg_backend_pid()")))
                started.set()
            try:
                with issuance.finish_onboarding_step(db, tenant_id=c.a.tid, raw=c.raw, key="orientation") as result:
                    enrolled = result.enrolled_now
                    if first:
                        entered.set()
                        assert release.wait(10)
                db.commit()
                return enrolled
            except issuance.InvitationIssuanceError:
                db.commit()
                return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(writer, True)
        assert entered.wait(10)
        second = pool.submit(writer, False)
        assert started.wait(10)
        try:
            _bind(c.a.db, c.a.tid)
            _wait_for_lock(c.a.db, backend[0])
        finally:
            release.set()
        assert first.result(timeout=20) is True
        assert second.result(timeout=20) == "refused"
    _bind(c.a.db, c.a.tid)
    with issuance.finish_onboarding_step(c.a.db, tenant_id=c.a.tid, raw=c.raw, key="orientation") as result:
        assert not result.enrolled_now
