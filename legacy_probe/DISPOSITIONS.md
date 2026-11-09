# Excluded-test disposition inventory (issue #2)

Audit date: 2026-11-07. Scope: all 57 `EXCLUDED.txt` entries plus the 2
`ci-skips.txt` author-local-content skips. Each entry was classified from
its test body against the v2 owner code.

Classes: **ACTIVE** = live contract, needs a maintained test against the
named v2 owner before flip. **SUPERSEDED** = owner replaced by a named
kernel/Learning/genesis mechanism; mapping + evidence recorded, no new
test except flagged gaps. **RETIRED** = legacy tooling/old route only;
remove from probe with reason.

Totals: 42 ACTIVE, 7 SUPERSEDED, 8 RETIRED (57 excluded); 2 ci-skips
ACTIVE. Several ACTIVE entries share one maintained test.

Port order is security-first: worker ACL -> identity atomicity/revocation
-> authorization -> integrity.

## Rulings (resolved 2026-11-07 during the port; legacy tests are the spec)

1. Worker INSERT on `lab_operations` — GRANTED in `a0008` (reconcile
   paths enqueue repair rows from worker context).
2. Worker settings read — `a0008` grants worker SELECT on kernel
   `domain_settings` (verified absent on genesis by hosted probe).
3. Facilitator activation EXECUTE — REVOKED from worker in `a0008`;
   the only caller runs on the app_admin sweep session.
4. Capability proacl — CONFIRMED on hosted genesis: outer
   `record_claimed_lab_check` {app_admin, academy_lab_worker}, inner
   `record_lab_result_v1` {app_admin, app_user}; worker has no inner
   access. Pinned by tests/test_issue2_wave0.py ACL test.
5. `smtp_password` — BUG, fixed: settings form no longer passes the
   env-only secret to `set_many` (it was refused, breaking every save
   that included it).
6. Settings CAS — IMPLEMENTED in settings_store (`_lab_proposal`,
   `_lab_cas_guard`, `LabExecutionPolicyConflict`); whole-object
   write/delete races yield exactly one winner.
7. Bulk backfill — IMPLEMENTED as scripts/backfill_applicants.py over
   the intake owner; counts + skip semantics pinned by wave-0 test.
8. Identity proofs — 23502 (reset_required no-default) and
   cross-tenant 42501/RLS proofs added in tests/test_issue2_wave0.py.
   Populated-import continuity itself is proven by the rehearsed
   importer (issue #1) on the production snapshot.
9. ERP ACK conservation — PROVEN in tests/test_issue2_erp_ack.py:
   imported key without checkpoint refuses, NULL-ACK membership +
   recorded stamp, raw ACK suppression without new receipt or HTTP,
   UNMATCHED never stamps, cross-tenant refused.

Additional findings fixed during the port (not in the original nine):
lab overrides were stored but never applied (expansion + budget
validation + split-row refusal now in settings_store); canonical
settings could be shadowed via search_path/schema translation (store
now uses schema-qualified literal SQL); bootstrap refused every
genesis (platform_api grants a0009 + assembly
`source_application` aligned to the legacy `dotmac_academy_app`
convention every writer uses; a0010 aligns the lab definer); reminder resend refused all
callers under the locked SQLAlchemy 2.1.8 — a requalification attempt
produced 54 native-flush failures, so the dependency is pinned back to
the qualified 2.0.51 exactly (latent `_clause` typo fixed en route);
test processes lacked the audit-action registry (conftest installs it).

## ACTIVE — worker ACL (4)

| Excluded file | v2 owner | Maintained test to write |
|---|---|---|
| services/test_lab_worker_privileges.py | alembic a0001:756-791 grants/RLS | test_lab_worker_acl_exact_grants |
| test_lab_result_capability.py | app/services/lab_result_capability.py + a0001 definer | test_lab_result_capability_claim_fence_and_atomic_receipt |
| services/test_lab_operations_worker.py | app/services/lab_operations.py, lab_lifecycle.py, lab_jobs.py | test_lab_operations_claim_once_lease_and_console_fence |
| services/test_lab_worker_lifecycle.py | app/cli.py _lab_worker/_lab_reconcile, host_lock, lab_jobs | test_lab_worker_singleton_single_settings_snapshot |

## ACTIVE — identity atomicity and revocation (8)

| Excluded file | v2 owner | Maintained test to write |
|---|---|---|
| services/test_bootstrap.py | app/services/bootstrap.py, bootstrap_identity.py | test_bootstrap_default_roles_single_admin_grant |
| services/test_bootstrap_identity.py | app/services/bootstrap_identity.py, database_runtime.py | test_bootstrap_identity_drift_poisons_zero_tenant |
| services/test_canonical_onboarding.py | app/services/invitation_issuance.py, canonical_identity_store | test_onboarding_bearer_atomic_zero_effect_on_failure |
| test_canonical_api_identity.py | app/services/api_identity_operations.py, app/api/deps.py | test_api_identity_revoked_session_and_role_loss_reauthorize_live |
| web/test_canonical_public_invitation_claim.py | app/services/invitation_claim.py | test_invitation_claim_spends_token_and_sibling_atomically |
| web/test_canonical_public_reset.py | app/services/canonical_lifecycle.py, credential_lifecycle.py | test_public_reset_anti_enumeration_and_token_spend |
| web/test_instructor_canonical_identity.py | app/web/instructor.py over kernel Party/PartyPerson | test_instructor_roster_canonical_identity_only |
| services/test_genuine_assessment_submit_context.py | app/admission_assessment_context.py, entrance_exam.py | test_assessment_submit_bearer_replay_deadline_no_consequence |

## ACTIVE — authorization / fresh purpose-role policy (17)

Common composition: `require_web_user` fresh session +
`effective(require_store=True)` + tenant-local `role_slugs`.
`PlatformSetting` fixtures port to kernel `DomainSetting` in every row.

| Excluded file | v2 owner | Maintained test to write |
|---|---|---|
| web/test_settings_access_policy.py | app/services/settings_access.py | test_settings_access_fresh_role_every_route |
| web/test_settings.py | app/web/settings.py | test_settings_admin_gate_and_save_validation |
| web/test_reports_access_policy.py | app/services/reports_access.py | test_reports_access_fresh_on_all_11_routes |
| web/test_product_admin_routes_owner.py | app/services/product_admin_access.py | test_product_admin_purpose_roles_independent |
| web/test_teaching_home_owner.py | app/services/teaching_access.py | test_teaching_access_separate_from_settings_policy |
| web/test_catalog_gradebook_owner.py | app/services/gradebook_access.py, app/web/catalog.py | test_gradebook_catalog_purpose_roles_separate |
| services/test_search_route_policy.py | app/web/search.py, app/services/roles.py | test_search_staff_visibility_fresh_policy_only |
| web/test_reports_owner.py | app/web/reports.py, app/services/reports.py | test_reports_owner_instructor_admin_tenant_scope |
| services/test_instructor_read_facades.py | app/services/dashboards.py, analytics.py, lookups.py | test_instructor_facades_tenant_isolated_fresh_thresholds |
| services/test_settings_store_fresh_policy.py | app/services/settings_store.py, app/lab_policy.py | test_settings_store_fresh_read_never_stale_or_fallback (CAS part: ruling 6) |
| services/test_learning_policy_settings.py | app/services/learning_policy.py, app/admissions_settings.py | test_learning_policy_malformed_refuses_never_defaults |
| test_admission_enrollment_policy_settings.py | app/services/admission_enrollment_policy.py, app/config.py | test_admission_enrollment_policy_store_refusals |
| test_bootstrap_policy_settings.py | app/config.py validate_bootstrap_policy_value, settings_store | test_bootstrap_policy_deny_pair_and_stored_null |
| test_enrollment_initial_state_policy_settings.py | app/services/invitation_policy.py, roster_policy.py | test_enrollment_initial_state_policy_snapshot_pinning |
| test_entrance_invitation_policy_settings.py | app/services/entrance_invitation_policy.py | test_entrance_invitation_deadline_policy_strict |
| services/test_reports_csv_insights_owner.py | app/services/csv_reports.py, insights.py | test_csv_reports_formula_neutralized_best_grade |
| web/test_gradebook_csv_safety_owner.py | app/web/gradebook.py, csv_reports.sanitize_cell | test_gradebook_csv_formula_prefixes_neutralized |

## ACTIVE — integrity (13)

| Excluded file | v2 owner | Maintained test to write |
|---|---|---|
| services/test_ical_owner.py | app/services/ical.py | test_ical_token_rotation_and_rfc5545_safety |
| services/test_ical_v2_owner.py | app/services/ical.py, canonical_identity_store.lock_parties | test_ical_rotation_canonical_lock_order |
| services/test_notifications_owner.py | app/services/notifications.py | test_notifications_isolation_and_caller_rollback |
| services/test_reminder_resend_request_owner.py | app/services/reminder_resend.py | test_reminder_resend_genuine_cookie_only_atomic_receipt |
| services/test_reminder_resend_v2_guards.py | app/services/reminder_resend_unit.py | test_reminder_resend_swallowed_commit_latches_error |
| services/test_reminder_resend_v3_caller_transactions.py | app/services/reminder_resend_unit.py | test_reminder_resend_caller_transaction_entries |
| services/test_reminder_resend_v4_sql_authority.py | app/services/reminder_resend_unit.py | test_reminder_resend_raw_sql_control_refused_pre_dispatch (drop V3-manifest sub-case; logical-flush [policy] superseded by kernel platform-scope RLS — app_user can never flush platform settings, pending-detection still proven by preflight [policy]) |
| services/test_reminders.py | app/services/reminders.py | test_reminders_sweep_digest_quiet_hours_idempotent |
| web/test_todo_calendar_owner.py | app/web/todo.py, calendar_feed.py, app/services/ical.py | test_todo_calendar_token_shown_once_feed_token_only |
| web/test_bookmarks_owner.py | app/services/bookmarks.py (Learning delegates) | test_bookmarks_guard_precedes_lookup_and_rollback |
| test_canonical_model_ownership.py | app/canonical_models.py | test_canonical_model_exactly_one_owner_per_table (re-derive table census) |
| test_ordinary_owner_composition.py | app/assembly.py, kernel_runtime.py, owner_composition.py | test_owner_composition_exact_declared_owners |
| test_backfill.py | app/services/admissions.py (per-row only) | test_backfill_per_row_idempotent_lowercase_skip_empty (bulk wrapper: ruling 7) |

## ACTIVE — ci-skips fixture replacements (2)

| Skip | Replacement |
|---|---|
| legacy_probe.test_end_to_end::test_banks_lint_clean | repo-owned tests/fixtures/foundation/assessments/banks/ with two minimal lint-clean BankDoc YAMLs; test_banks_lint_clean_fixture_banks |
| legacy_probe.test_end_to_end::test_full_flow | repo-owned tests/fixtures/foundation/ (3 chapters, empty figures/, foundation-ch03 bank); ACADEMY/FIGS point there; test_full_flow_fixture_foundation |

## SUPERSEDED (7)

| Excluded file | Superseding mechanism | Existing evidence / gap |
|---|---|---|
| test_academy_credential_reset_state.py | a0003 born-in-genesis `reset_required` NOT NULL no-default | tests/test_identity_domain.py (column/RLS only; 23502 proof owed, ruling 8) |
| test_kernel_identity_entry.py | fresh composed genesis a0001-a0007 + V6 transfer | tests/test_v6_roundtrip.py; 42501 proof owed (ruling 8) |
| services/test_settings_store.py | kernel `domain_settings` via settings_store | tests/test_settings_store.py (partial: non-strict fallback, get_all shape, KNOWN_KEYS uncovered) |
| test_catalog_json_codec.py | a0001..a0007 genesis + V6 replaces retained adoption codec | genesis lineages; no live codec contract |
| test_ci_test_skip_evidence.py | scripts/check_ci_test_skips.py | tests/test_ci_evidence.py (already ported, stricter) |
| test_erp_progress_transition.py | a0006 ERP genesis + erp_sync.push_completion | tests/test_issue2_erp_ack.py (ruling 9 closed) |
| test_erp_progress_transition_hosted.py | same a0006 mechanism | tests/test_issue2_erp_ack.py (ruling 9 closed) |

Sub-mechanism (not a file class): `platform_settings` storage ->
kernel `domain_settings` in fixtures across worker/identity/policy/
integrity rows; the behaviors stay ACTIVE.

## RETIRED (8)

| Excluded file | Reason |
|---|---|
| test_ci_composed_bootstrap.py | legacy disposable-DB declaration wrapper absent; v2 CI hard-codes loopback URLs |
| test_ci_test_shards.py | legacy sharded-CI tooling absent; v2 runs one unsharded qualify job |
| test_ci_workflow_contract.py | legacy retained-image guard and step names only; v2 uses plain postgres:16 |
| test_genesis_derived_static.py | legacy derived Postgres image recipe absent; no v2 owner |
| test_genesis_image_declaration.py | legacy genesis-image pinning/materialize absent; no v2 owner |
| test_hosted_container_inspect_identity.py | Docker-inspect projection for retired retained hosted genesis; not auth identity |
| test_hosted_context_refusal_diagnostics.py | poison/diagnostic machinery of the retired hosted-genesis context only |
| test_test_dispositions.py | legacy retirement-inventory enforcement (toml absent); this file is its v2 replacement |

## Acceptance tracking

Flip approval requires: every ACTIVE row covered by a passing hosted
maintained test at the exact candidate revision; rulings 1-9 resolved or
explicitly waived by Michael; SUPERSEDED evidence gaps closed; RETIRED
rows deleted from the probe. Until then, portable-suite green is
candidate evidence only.
