"""Register genuine canonical account fixtures for hosted consumers.

These are the original fixture functions, not replacement principals or proof.
Their canonical installed-owner and real app_user prerequisites still apply.
"""

from legacy_probe.services.test_canonical_current_user import current_account
from legacy_probe.services.test_canonical_sign_in import sign_in_account

__all__ = ["current_account", "sign_in_account"]
