# Legacy-compat qualification harness

Runs the legacy Academy test suite (copied from the D8 worktree with
`tests.` imports repointed to `legacy_probe.`) against this composition
on a shared v2 genesis database. This is qualification collateral, not
shipped application code.

## Run (test server)

```
TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55432/postgres \
  .venv/bin/python -m legacy_probe.genesis academy_v2_legacy_probe
export CI=true
export TEST_DATABASE_URL=postgresql+psycopg://app_user@127.0.0.1:55432/academy_v2_legacy_probe
export TEST_MIGRATION_DATABASE_URL=postgresql+psycopg://app_admin@127.0.0.1:55432/academy_v2_legacy_probe
export TEST_LAB_WORKER_DATABASE_URL=postgresql+psycopg://academy_lab_worker@127.0.0.1:55432/academy_v2_legacy_probe
export DATABASE_URL=$TEST_DATABASE_URL
export PLATFORM_DATABASE_URL=postgresql+psycopg://platform_api@127.0.0.1:55432/academy_v2_legacy_probe
export MIGRATION_DATABASE_URL=$TEST_MIGRATION_DATABASE_URL
IGNORES=$(sed "s|^|--ignore=legacy_probe/|" legacy_probe/EXCLUDED.txt | tr "\n" " ")
DESELECTS=$(sed "s|^|--deselect |" legacy_probe/DESELECT.txt | tr "\n" " ")
.venv/bin/python -m pytest legacy_probe/ -p no:cacheprovider -o addopts= $IGNORES $DESELECTS -q
```

## Exclusions (EXCLUDED.txt, 57 files)

- Old retained-schema route machinery (retained/adoption/history/
  hosted-genesis/migration-graph tests) — superseded by genesis
  lineages a0001-a0007 + the V6 transfer.
- Tests bound to the retired `platform_settings` table API
  (`app.models.platform_settings`) — settings live in kernel
  `domain_settings` here; the behavioral contracts are covered by the
  policy probe files that do run plus `tests/test_settings_store.py`.
- `test_admission_enrollment_policy_settings.py` remains wholly excluded; its
  PlatformSetting round-trip case is therefore not a separate deselection.
  Current NULL refusal is covered by `tests/test_settings_store.py`, while
  broader policy parity remains open below.
- CI/bootstrap script tests for the legacy repo layout.
- `test_ci_workflow_contract.py`, `test_test_dispositions.py`: assert
  legacy repo files (`.github/workflows/ci.yml`, disposition inventory)
  that arrive with the repo/hosting decision.
- `test_kernel_identity_entry.py`: old-route kernel entry replay.
- `services/test_bootstrap.py`: dedicated-database canary requiring its
  own `academy_bootstrap_identity` database/process.

## Deselects (DESELECT.txt)

- `test_first_completion_with_certificate_keeps_historical_delivery`:
  the legacy stub lambda lacks the `raise_on_failure` kwarg that both
  trees' current `deliver_completion_notifications` passes (stale stub).
- `test_valid_policy_write_flushes_without_committing`: asserts the
  legacy `PlatformSetting.value` storage shape via `db.get`.

## Qualification gaps (release blocking)

The exclusion list is an initial compatibility selection, not a claim that
excluded behavior is obsolete or equivalent. In particular these active
contracts still need ported, exact-revision hosted evidence:

- Worker claim/lease/recovery/capacity behavior and the dedicated worker ACL
  matrix (`services/test_lab_operations_worker.py`, `test_lab_worker_lifecycle.py`,
  `test_lab_worker_privileges.py`, and `test_lab_result_capability.py`).
- Bootstrap/canonical identity, bearer assessment/onboarding, invitation/reset
  transaction atomicity and cross-tenant authorization (the excluded canonical
  identity, onboarding, assessment, public claim and reset test files).
- Live policy freshness/revocation and settings/reports/teaching/admin access
  (excluded owner/access-policy tests and `test_settings_store_fresh_policy.py`).
- Reminder resend transaction/SQL authority, idempotency and audit/outbox effects,
  calendar-token lifecycle, notification isolation, CSV safety and report parity.
- Admission/enrollment/invitation policy refusal, ERP import/backfill and
  canonical model/FK ownership. Retired settings fixtures must be ported to
  kernel `domain_settings`; a fixture incompatibility does not retire behavior.

Other excluded tests assert legacy-only migration, image/catalog, CI wrapper,
shard or disposition machinery that has not been ported. Evaluate their controls
against this candidate's actual topology before marking them superseded.
The skip-evidence guard now has maintained tests in `tests/test_ci_evidence.py`.
The legacy workflow/shard assertions remain excluded because the v2 workflow
has a different unsharded shape; their exclusion does not approve release.

`ci-skips.txt` allows only the two `test_end_to_end.py` cases that require the
author-local manuals directory. They are missing content-flow evidence, not
passing tests; replace that path with a repository-owned fixture before release.
CI rejects every additional skip and every unused allowance. The legacy
selection runner also rejects stale deselections using actual hosted collection.
