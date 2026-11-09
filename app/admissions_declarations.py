"""Prepared admissions audit declaration; assembly enrollment remains required."""

# Ported at new-composition genesis (composition wave).

from dotmac_kernel.features import FeatureManifest

APPLICANT_TRANSITION_ACTION = "applicant.transition"
ADMISSIONS_AUDIT_DECLARATION = FeatureManifest(
    name="academy_admissions",
    audit_actions=(
        APPLICANT_TRANSITION_ACTION,
        "applicant.intake_assigned",
        "applicant.invitation_resent",
        "applicant.access_extended",
        "applicant.assessment_reset",
        "applicant.assessment_reinvite",
    ),
)
