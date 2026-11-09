"""Fixed API audit vocabulary; future root assembly must enroll this manifest."""

# Ported at new-composition genesis (composition wave).

from dotmac_kernel.features import FeatureManifest

API_IDENTITY_AUDIT_ACTIONS = ("person.create", "person.delete", "role.create", "role.grant")
API_IDENTITY_AUDIT_DECLARATION = FeatureManifest(name="academy_api_identity", audit_actions=API_IDENTITY_AUDIT_ACTIONS)
