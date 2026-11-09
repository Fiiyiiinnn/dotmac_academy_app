"""Unused reset-receipt ports must refuse without database or account state."""

import pytest
from dotmac_kernel.credential_lifecycle import CredentialAuthenticatedAccountPasswordChangeError

from app.services.password_change import _ChangePorts


@pytest.mark.parametrize("method", ["record_receipt", "find_receipt"])
def test_unused_reset_receipt_port_refuses_before_database(method):
    ports = object.__new__(_ChangePorts)

    with pytest.raises(CredentialAuthenticatedAccountPasswordChangeError, match="password change context refused"):
        getattr(ports, method)(object())

    assert ports.__dict__ == {}
