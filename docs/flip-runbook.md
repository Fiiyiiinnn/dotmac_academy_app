# Academy historical downtime flip runbook

> **Historical procedure — updated 2026-11-10.** The initial flip completed on
> 2026-11-08 and the new application has accepted production writes. Recovery
> is **fix forward only**: never restore legacy traffic or the legacy database
> as the production recovery target. This document does not authorize another
> import, flip, rollback, retirement or deletion. See [current cutover status](cutover.md).

The sequence below preserves the original initial-flip procedure and acceptance
evidence requirements. Deployment packet issue 3 supplied the concrete host
commands, service names and target details; this document does not invent
host-specific names or commands. Its original pre-write recovery procedure
applied only before the completed flip and is not current authorization.

The candidate must be one exact immutable commit whose hosted checks are
green. Pin its full commit ID and CI run in the private receipt before the
window. Do not substitute a later green commit during execution.

## Historical decisions

- People without credentials (108 in the 2026-11-07 inventory) import as
  parties with no login; they claim later via invite/reset. Excluding them
  would orphan their enrollments, applications and history.
- Email outbox: drain pending mail before the snapshot; failed rows are not
  carried (the importer skips `status='failed'`). Record failed-row counts
  and identifiers in the private receipt. Sent and cancelled history carries.
- Lab rows (templates, instances, operations, console activity) carry as-is,
  but only after reaching a quiet state. Learner lab history and grading
  records depend on them.
- Auth sessions expire at the flip by design; password hashes carry verbatim
  and upgrade to Argon2id on first login.

## Historical initial downtime sequence

1. **Bind the source first.** Using deployment packet issue 3, record and
   independently verify the source host, database name, PostgreSQL cluster
   system identifier, server/version, tenant scope, and current migration
   head. Confirm every source DSN resolves to that same database and cluster.
   These read-only identity facts anchor every later guard, inventory, and
   dump. Stop if any identity value is missing or inconsistent.
2. **Fence every writer and new producer.** Put legacy Academy into downtime
   with no logins and prevent new work through existing sessions, web/API
   requests, inbound integrations (including ERP), CLI/operator entrypoints,
   and scheduled jobs, timers, or schedulers. Inventory each producer and
   record how it is fenced and how restart is prevented. Keep these fences in
   place through final snapshot capture. Do not silently expire or ignore
   already-running sessions; establish that they cannot continue writes.
3. **Drain controlled work while its consumers remain available.** Keep only
   the explicitly controlled consumers needed to finish legacy email-outbox
   delivery, delayed lab-result delivery intents, and lab teardown. Let
   pending email drain to zero; failed rows remain excluded as decided above.
   Complete or tear down all running lab instances and let controlled
   consumers finish pending result-delivery intents. Record remaining failed
   delivery/outbox items for the private receipt.
4. **Stop lab creation and teardown work.** With other producers still
   fenced, finish teardown with the controlled lab worker still available.
   Then stop the lab worker and prevent its restart. If more teardown is
   required, return to step 3; do not enqueue work behind a stopped consumer. Confirm the database `runtime_presence` inventory is
   absent, and independently inspect the actual lab provider for orphan
   containers and consoles; database absence alone is not proof of provider
   cleanup. Resolve any remaining runtime or console before continuing.
5. **Stop consumers and check quiescence.** Stop the controlled outbox,
   delayed-result, and lab consumers; prevent their restart. Inspect live
   database transactions and confirm none can still write or hold unfinished
   flip work. Keep all producer fences and restart blocks active. Run the
   read-only quiet-state preflight, bound to the step 1 source identity, with
   its source DSN supplied through an environment variable, never a
   password-bearing command-line argument. It must report QUIET and exit 0.
   The check must prove the expected source identity and full visibility; an
   RLS-filtered or otherwise partial inventory is a stop condition. Refer to
   deployment packet issue 3 for the exact invocation and environment setup.
   The guard refuses queued/claimed or unknown operations, running or unknown
   lifecycle states, any runtime presence other than explicit `absent`, pending
   email, and pending/delayed or unknown lab-result intents. Example invocation
   (all non-secret expected identities come from the approved private receipt):

   ```sh
   python -m scripts.flip_preflight --dsn-env ACADEMY_SOURCE_DATABASE_URL \
     --expected-database "$ACADEMY_APPROVED_DATABASE" \
     --expected-system-identifier "$ACADEMY_APPROVED_SYSTEM_IDENTIFIER" \
     --expected-head "$ACADEMY_APPROVED_SOURCE_HEAD" \
     --approved-role "$ACADEMY_APPROVED_SOURCE_ROLE"
   ```

   Repeat `--expected-head` for a multi-head source. An actually absent legacy
   `lab_result_deliveries` table refuses by default. Only when the pinned source
   catalog and approved receipt explicitly confirm that the legacy deployment
   has no durable-intent table may the operator add
   `--allow-absent-legacy-delivery-table`. This flag never skips an existing
   table or its pending work; a missing inventory entry is not proof of absence.
6. **Capture the final source snapshot.** Without releasing any fences, take
   the final custom-format dump using the approved deployment packet path.
   Record source identity, migration head, dump size and SHA-256 in the private
   receipt. Restore it in isolation and verify catalog identity and expected
   visibility. If any source writer ran after the quiet-state proof, discard
   the dump and repeat the drain, quiescence, preflight, and capture sequence.
7. **Prepare the exact v2 candidate.** Provision the fresh destination from
   the genesis lineages (a0001–a0010 plus kernel/Learning lineages) with the
   deployment packet's roles and grants. Record destination identity and
   verify it is fresh and isolated. Deploy only the pinned immutable candidate
   from the pre-window receipt.
8. **Import atomically and verify parity.** Run the complete V6 transfer and
   product-table/settings import from the restored snapshot in the required
   transaction boundaries. Require zero importer exceptions. Verify value
   and key parity, not just row counts, across identities, learning extents,
   product tables, settings, and historical ERP checkpoint/ACK records.
   Account explicitly for the approved failed-outbox exclusion; all other
   differences block the flip and go to manual review.
9. **Smoke before traffic.** Keep production traffic and external effects
   suppressed. Use the controlled v2 worker path with SMTP and ERP delivery
   suppressed, the designated smoke identity, and the deployment packet's
   isolated smoke data. Verify health/login and the agreed role sign-in,
   lab launch/result, and reminder flow. Tear down the smoke runtime and
   confirm no smoke containers, consoles, or pending external effects remain.
   Record the smoke identity, checks, and cleanup evidence privately.
10. **Switch traffic and start production consumers.** Only after all gates
    pass, switch traffic to v2 and start the approved v2 workers and timers.
    Keep the legacy producer fences in place. Record the switch time and
    destination identity.
11. **Revoke snapshot access.** Revoke the temporary snapshot key from
    production `authorized_keys` after successful completion. Also revoke it
    on abort whenever it is no longer needed to perform the approved recovery
    or evidence handling.
12. **Retain evidence and retire deliberately.** Keep the legacy database,
    final dump, source and private receipts as archive/evidence until Michael
    explicitly approves deletion. Legacy retirement remains a separate
    authorized follow-up; acceptance alone does not authorize deletion.

## Current recovery and retention

The new application has accepted production writes since 2026-11-08.
Michael's fix-forward-only ruling supersedes the original rollback procedure
and the later suggestion to await a reconciliation/restore decision before
returning to legacy. Resolve defects in the current application; never restore
legacy traffic or the legacy database as the production recovery target.
Preserve evidence and obtain the appropriate authorization for any further
production operation.

Retain the legacy database, final dump, source and receipts as archive/evidence
until Michael explicitly approves deletion. Legacy retirement and residual
D8 authority remain separate work; completed live acceptance does not close
those items or authorize removal of potentially shared resources.

### Historical pre-write recovery boundary

Before the initial flip accepted any production writes, the original procedure
allowed stopping every new-application writer, cleaning up its smoke runtime,
abandoning the unaccepted destination and reinstating verified legacy services.
That boundary has passed. This paragraph preserves the historical decision;
it must not be used to authorize a present rollback.

## Historical private receipt requirements

Keep retained receipts access-restricted and out of Git. The initial-flip
receipt requirements included:

- exact candidate commit and hosted green CI run;
- source host/database/cluster identity, tenant scope, migration head, and
  source dump path, size, and SHA-256;
- destination identity and genesis/migration heads;
- per-phase gate results, timestamps, and operator evidence references;
- producer inventory and fence/restart-prevention evidence;
- pending/failed outbox and delayed lab-result delivery counts, with the
  approved failed-outbox exclusion stated explicitly;
- runtime-presence query result and actual provider orphan-container/console
  inventory and cleanup result;
- importer exception count and value/key parity evidence, including ERP
  history;
- smoke identity, suppressed external effects, smoke results, and runtime
  teardown evidence;
- restart policy, historical recovery disposition, snapshot-key revocation on completion
  or abort, and remaining cleanup/retention actions.
