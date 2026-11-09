# Academy v2 candidate and cutover gates

## Current evidence and limits

The 2026-11-07 production-snapshot rehearsal and previous hosted qualification
are historical supporting evidence. The cutover-safety corrections require a
fresh exact-revision CI result and a new isolated rehearsal before a flip.

`scripts/v6_import.run_import` now owns one destination root transaction for
identity, Learning V6, product, ERP history and settings. It refuses nonfresh
or RLS-filtered extents and aborts on any exception. Product mappings reject
unreviewed schema changes and compare all mapped values. ERP ACKs and immutable
imported memberships have a dedicated verified transfer. Settings are one
validated batch, with no per-key commit fallback.

Use [the downtime runbook](flip-runbook.md) for writer fencing, source-bound
quiet checks, smoke isolation and rollback. A green CI result does not prove
live writer shutdown or physical runtime absence. Source/destination identity,
archive digest, exact source schema, provider inventory and operational receipts
remain gates for the approved execution window.

Detailed mapping review: [migration preflight findings](migration-preflight.md).

## Gate 1: reproducible candidate

- Private GitHub repository `michaelayoade/dotmac_academy_v2`.
- Poetry 2.4.1, Python 3.12, committed dependency lock, exact platform pins.
- Repository Actions secret `FORGEJO_READ_TOKEN`, sourced from OpenBao
  `secret/dotmac/forgejo/read-token#value`; never put its value in source or logs.
- CI lint/format, maintained PostgreSQL tests and declared legacy qualification
  all pass at the candidate revision. Keep JUnit and legacy-selection artifacts.
- Review every legacy exclusion; copied tests of retired internals do not
  establish parity for the active replacement. Open parity gaps block release.
- CI uses a disposable loopback-only PostgreSQL service. Trust authentication
  and `deploy/initdb-roles.sql` sample passwords are CI-only, not prod assets.

Detailed source review: [migration preflight findings](migration-preflight.md).

## Gate 2: complete the migration caller before a production-data rehearsal

The implemented Academy operator pipeline must be qualified for these steps at the exact candidate revision:

1. Bind the candidate revision, restored snapshot digest, exact source and
   destination database identities, and the explicit tenant extent.
2. Import tenant and identity records (people to genuine kernel Party,
   PartyPerson and credentials/roles) preserving UUIDs; reconcile every mapping.
3. Build Learning SnapshotV6 from the restored legacy snapshot. Retain a private
   exceptions queue and refuse import when any exception remains unresolved.
   Never silently discard rows or substitute semantic defaults after refusal.
4. Import and independently verify V6 within one destination root transaction
   per declared tenant extent, as required by the released Learning contract.
5. Transform-copy Academy product tables in dependency order, including ERP ACK
   facts; reconcile keys, row counts and values against the declared mapping.
6. Import platform settings through validated `set_many`; report rejected keys,
   invalid values and NULL policy decisions. SMTP credentials stay env-only.
7. Produce a private receipt: source/destination identities, dump checksum,
   candidate SHA and release pins, tenant extent, start/end times, expected and
   observed per-table counts, identity equivalence, settings outcomes, exception
   decisions, verification and smoke outcomes. Do not put row data, credentials,
   password hashes or snapshots in CI artifacts or Git.

The caller must never import a partial snapshot merely because `build_snapshot`
returned both a snapshot and exceptions. Any refusal leaves the destination unaccepted; abandon it or diagnose within
the approved isolated rehearsal. Do not promote a partially verified database.

## Gate 3: separately authorized production snapshot rehearsal

Approval must name the production source and the dedicated test-server target.
Only a read-only `pg_dump` is authorized at this gate; no traffic switch,
production writes or writer stoppage. Stream a custom-format dump using the
approved secret loader, protect it at rest (0600/private directory), record its
checksum, and restore only into a uniquely named isolated test database.

Use the same candidate pipeline qualified by CI. Disable outbound SMTP/ERP and
lab provisioning on the restored environment. Review all exceptions explicitly,
reconcile data and exercise health, login, lab-result consequences and worker
ACL/RLS. Record duration so the downtime estimate includes dump, restore,
transformation, verification and smoke. Remove only this rehearsal's resources
under the agreed retention policy. Never alter shared host services/firewalls.

## Gate 4: deployment and recovery packet

Before requesting the downtime window, record the target host, immutable image
or release identity, runtime roles and least-privilege grants, web/worker
entrypoints, SMTP env secret, ERP/lab-host settings, writer inventory, health
checks, traffic switch and recovery commands. Production roles need separately
provisioned secrets, never the CI passwords. Prove that all web/CLI/scheduler,
lab-worker and inbound integration writers can be fenced.

Recovery has a precise boundary: before v2 accepts writes, return traffic only
after verifying the legacy database is unchanged and all v2 writers are stopped.
After v2 accepts writes, returning to a stale legacy database loses those writes;
recovery needs an explicitly approved reconciliation/restore decision. Retaining
the legacy app alone does not prove rollback safety.

## Gate 5: separately authorized downtime flip

After Gates 1-4 pass and Michael approves a window: fence every writer, verify
quiescence, capture the final dump, run the identical rehearsed pipeline, check
receipt and smoke outcomes, then switch traffic and enable the intended writers.
Abort before traffic if any reconciliation, exception or smoke gate fails.
Retain legacy read-only through acceptance; archive and close superseded D8
items only after acceptance. Snapshot permission does not authorize this gate.
