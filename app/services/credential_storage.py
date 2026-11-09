"""Private shared absent-credential construction; no authorization or hashing.

Callers must prove absence, authorization and reset state, and retain their own
readbacks. Exactly the historical two flushes; the caller owns the root.
"""

# Ported at new-composition genesis (accounts-core slice).

from dotmac_kernel.models import UserCredential

from app.models.identity_extensions import AcademyCredentialPolicy


def create_absent(db, *, credential_id, tenant_id, party_id, email, password_hash, occurred_at):
    credential = UserCredential(
        id=credential_id,
        tenant_id=tenant_id,
        party_id=party_id,
        password_hash=password_hash,
        created_at=occurred_at,
        updated_at=occurred_at,
    )
    db.add(credential)
    db.flush()
    policy = AcademyCredentialPolicy(
        credential_id=credential.id,
        tenant_id=tenant_id,
        email=email,
        failed_login_attempts=0,
        locked_until=None,
        lifecycle_version=0,
        reset_required=False,
        created_at=occurred_at,
        updated_at=occurred_at,
    )
    db.add(policy)
    db.flush()
    return credential, policy
