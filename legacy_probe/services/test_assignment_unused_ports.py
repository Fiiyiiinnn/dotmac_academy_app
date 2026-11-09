"""No-DB port isolation checks; these do not qualify persisted assignment authority."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from dotmac_kernel.credential_lifecycle import (
    CredentialAssignmentAuthorityEvidenceV1,
    CredentialAssignmentError,
    CredentialCreateAbsentFromProofV1,
    CredentialCreateAbsentV1,
    CredentialLifecycle,
    CredentialSnapshot,
    CredentialSubjectRef,
)

from app.services.credential_assignment import _WebAssignmentPorts
from app.services.invitation_claim import _InvitationClaimPorts


class _SessionObservation:
    def __init__(self):
        self.calls = []

    def get_transaction(self):
        self.calls.append("get_transaction")
        return object()

    def __getattr__(self, name):
        self.calls.append(name)
        raise AssertionError("unsupported ports must not inspect the Session")


@pytest.fixture(params=["claim", "web"])
def legacy_ports(request):
    db = _SessionObservation()
    kwargs = dict(tenant_id=uuid4(), digest="private-digest", policy=object())
    if request.param == "claim":
        ports = _InvitationClaimPorts(db, **kwargs, clock=lambda: datetime.now(UTC))
    else:
        ports = _WebAssignmentPorts(db, **kwargs)
    return db, ports


@pytest.mark.parametrize("method", ["lock_for_update", "create", "write_hash", "record_receipt", "find_receipt"])
def test_unsupported_legacy_ports_refuse_before_session_access(legacy_ports, method):
    db, ports = legacy_ports
    before = tuple(db.calls)
    kwargs = {}
    if method in {"create", "write_hash"}:
        kwargs = dict(password_hash="private-material", reset_required=False)
    if method == "write_hash":
        kwargs["credential_version"] = 1
    with pytest.raises(CredentialAssignmentError) as refused:
        getattr(ports, method)(object(), **kwargs)
    assert "private-material" not in str(refused.value)
    assert tuple(db.calls) == before


def test_kernel_assignment_dispatch_uses_only_assignment_ports(legacy_ports):
    db, ports = legacy_ports
    before = tuple(db.calls)
    subject = CredentialSubjectRef("dotmac_academy_app", "person", str(uuid4()), str(uuid4()))
    if isinstance(ports, _InvitationClaimPorts):
        command = CredentialCreateAbsentFromProofV1(subject, str(uuid4()), "invitation", "proof", "claim", "trace")
    else:
        command = CredentialCreateAbsentV1(subject, str(uuid4()), "creation", ("role",), "create", "trace")
    calls = []

    # This deliberately isolated dispatch collaborator proves the Kernel's
    # selected port family; it is not product authorization or database proof.
    class AssignmentDispatch:
        def authorize_and_lock(self, actual):
            assert actual is command
            calls.append("authority")
            return CredentialAssignmentAuthorityEvidenceV1(
                actual, "user", "actor",
                actual.proof_ref if isinstance(actual, CredentialCreateAbsentFromProofV1) else "evidence",
            )

        def lock_current(self, actual):
            assert actual is command
            calls.append("absence")
            return None

        def validate(self, actual, secret):
            assert actual is subject and secret == "chosen-password"
            calls.append("policy")

        def create_absent(self, actual, *, password_hash, reset_required):
            assert actual is command and reset_required is False
            calls.append("creation")
            return CredentialSnapshot(subject, password_hash, 0, reset_required=False)

        def record_assignment(self, entry):
            assert entry.command is command and entry.credential_version == 0
            calls.append("audit")
            return "assignment-audit"

    dispatch = AssignmentDispatch()
    owner = CredentialLifecycle(
        store=ports,
        audit=ports,
        sessions=ports,
        recovery=ports,
        policy=dispatch,
        assignment_authority=dispatch,
        assignment_store=dispatch,
        assignment_audit=dispatch,
    )
    receipt = owner.assign_authorized_password(command, "chosen-password")
    assert receipt.audit_ref == "assignment-audit"
    assert calls == ["authority", "absence", "policy", "creation", "audit"]
    assert tuple(db.calls) == before
