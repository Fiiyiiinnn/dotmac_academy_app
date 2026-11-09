"""Academy trims input; the published Kernel owns identity lowercasing."""

import pytest
from dotmac_kernel.identity import normalize_email as kernel_normalize_email

from app.services import identity


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        (" \t\n", ""),
        ("  Student@Example.COM \t", "student@example.com"),
        ("USER+Tag@Example.COM", "user+tag@example.com"),
        ("  ÉLÈVE@EXAMPLE.COM  ", "élève@example.com"),
    ],
)
def test_product_optional_and_trim_semantics_are_preserved(raw, expected):
    assert identity.normalize_email(raw) == expected


def test_normalization_delegates_trimmed_input_to_actual_public_owner(monkeypatch):
    assert identity.normalize_identity_email is kernel_normalize_email
    received = []

    def owner(value):
        received.append(value)
        return "OwnerResult"

    monkeypatch.setattr(identity, "normalize_identity_email", owner)
    assert identity.normalize_email("  Mixed@Example.COM  ") == "OwnerResult"
    assert received == ["Mixed@Example.COM"]


def test_owner_refusal_is_not_replaced_by_local_normalization(monkeypatch):
    def refuse(value):
        raise ValueError("canonical identity owner refused")

    monkeypatch.setattr(identity, "normalize_identity_email", refuse)
    with pytest.raises(ValueError, match="canonical identity owner refused"):
        identity.normalize_email("USER@EXAMPLE.COM")
