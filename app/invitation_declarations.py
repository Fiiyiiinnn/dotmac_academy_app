"""Prepared product audit vocabulary; NOT installed/enrolled by this capsule.

Future assembly composition must include this real FeatureManifest declaration
in its audit registry. Importing it does not install a registry or claim that the
identity/token storage owner is a new stateful feature.
"""

# Ported at new-composition genesis (presentation-coupled wave).

from dotmac_kernel.features import FeatureManifest

INVITATION_COMPLETED_ACTION = "account.invitation.completed"
INVITATION_AUDIT_DECLARATION = FeatureManifest(name="academy_invitations", audit_actions=(INVITATION_COMPLETED_ACTION,))
