# Migration caller preflight findings

Source-only review on 2026-11-07. These findings refine the work still needed
before a complete production-data rehearsal; they are not production findings
or a claim that data has been transferred. Exact source: initial candidate
`4beca0b` plus this document revision.

## Refresh the source catalog

`legacy_probe/prod_columns.txt` contains names, coarse types and nullability.
It omits `lab_result_deliveries` and `lab_operations.claim_lease_seconds`, both
present in the destination. Obtain the actual snapshot catalog, including
constraints, lengths, defaults, indexes and triggers. Missing inventory is not
evidence of an empty table or a NULL value. Queue unexpected schema rather than
silently selecting the intersection of source and destination columns.

## Identity mapping and decisions

No existing Academy bulk identity importer was found. The old canonical-entry
fixture relocates legacy relations and replays kernel migrations; it explicitly
does not import people. Never run Kernel `0003_party_identity` directly over
populated legacy tables: it drops the legacy identity relations. Start with
fresh composed genesis, then import and verify explicitly.

The intended UUID-preserving map is:

| Legacy source | Destination |
| --- | --- |
| `people.id`, tenant, name/email/timestamps | `parties.id` unchanged, person type; `party_persons.party_id` unchanged, first/last name |
| Person status/avatar/preferences | `academy_account_profiles`, not kernel custom fields |
| Credential ID, password hash, timestamps | Kernel `user_credentials`, with `party_id=person_id`; never expose credential values in reports |
| Credential email/lockout policy | `academy_credential_policies` keyed by preserved credential UUID |
| Roles | Kernel roles with preserved UUIDs |
| `person_roles` | Kernel `party_role_grants`, preserving grant IDs and substituting `party_id=person_id` |

The local Starter checkout is a105 while this app locks a107. Bind every target
column/API claim to the exact locked a107 artifact and live genesis catalog;
old ADR references to `party_roles` or `academy_person_profiles` are not the
current target names.

Party activation is independent of Academy account status: the old ADR's direct
status-to-`is_active` mapping was superseded. Obtain an explicit activation
provenance/policy. Normalize and collision-check primary Party email and every
credential alias before writes. New credential policy `lifecycle_version` and
`reset_required` require explicit initial-state policy; do not silently invent
missing historical facts. Inventory auth sessions, account tokens and audit
actor references and define continuity/invalidation before the flip.

`canonical_identity_store.create_person` and `bootstrap_identity` are provision
writers, not qualified bulk importers. The Sub audit/adjudication/backfill code
provides a reusable process pattern: PII-free source fingerprints, exact
snapshot/plan digests, explicit bounded decisions, caller-owned transaction,
collision checks, conservation verification and a receipt. Reuse that pattern,
not Sub business decisions or an unconditional upsert. Refuse unadjudicated
identity differences; preserve source values until a ruling exists.

## Explicit product mapping after identity and Learning

| Extent | Import rule / refusal |
| --- | --- |
| `lab_templates`, `lab_instances` | Preserve UUIDs, all source values and timestamps; verify person/course/activity references independently, including references without FKs. |
| `lab_console_activity` | Preserve history. The a0007 `lab_console_activity_stamp` BEFORE INSERT/UPDATE trigger overwrites `last_seen_at` with now(). A controlled destination-only import must preserve timestamps and restore the trigger before commit; ordinary INSERT is insufficient. |
| `lab_operations` | Preserve terminal history. Queue claimed work lacking an evidenced lease duration; do not derive historical leases from current settings. Drain/fence writers and reconcile provider state before the actual flip. |
| `lab_result_deliveries` | Refresh inventory; preserve pending/failed intents and idempotency history. Never assume absent inventory means no rows. |
| `applicants` | Preserve recorded columns; `assessment_bank_version_id=NULL` explicitly represents a legacy sitting. Do not invent a historical question-bank version. |
| `onboarding_tasks` | Copy after applicants. |
| `facilitator_nominations`, `facilitator_requirements` | Copy in parent-first order; do not invoke activation services during historical transfer. |
| `tenant_entrance_defaults`, `notifications`, `account_tokens`, `reminder_preferences` | Preserve source state and reference closure. Token continuation/revocation and old-link handling need an explicit operating policy. |
| `email_outbox`, `reminder_log` | Copy outbox before reminder history. Preserve IDs, sent facts, attempts, schedules and deduplication keys. Suppress all delivery in rehearsal. Queue pending messages whose links or bearer semantics would change. |
| ERP checkpoints/imported keys | Apply the conservation rules below, not a blind checkpoint table copy. |

Keep kernel identity-extension tables in the identity owner's mapping. Preserve
`class_sessions` through Learning V6, not a duplicate product-table copy.

## ERP ACK conservation

Read both `course_completions.(erp_synced_at, erp_synced_pct)` and the separate
legacy `academy_erp_completion_checkpoints` timestamp. Preserve raw timestamps,
NULLs and float bits. Queue orphans and conflicting facts for a precedence
ruling; neither migration time nor current completion percent is an ACK.

The destination adds `erp_synced_pct` and
`academy_erp_imported_completion_keys`. Populate immutable imported-key
membership for every imported completion, including NULL-ACK rows; it prevents
silent historical replay if a checkpoint is lost. Verify sender behavior and
membership conservation in hosted tests before allowing outbound ERP traffic.

## Strict settings migration

`app/services/settings_store.py:set_many` validates before writing and flushes
without committing, so the caller can own the transaction. Runtime coercion is
not sufficient migration validation: generic NULL becomes a default, unknown
boolean strings become false, and generic int/float conversions can accept
coercions or nonfinite numbers.

The caller must:

1. Require an empty destination Academy settings extent, or explicitly validate
   the entire merged destination state.
2. Classify every source key. Refuse unknown keys and environment-only secrets;
   SMTP password remains outside the settings store.
3. Queue NULL values for explicit per-key disposition. Strictly validate generic
   boolean/int/finite-float representations before calling runtime coercion.
4. Preserve raw JSON text for per-key strict validators and duplicate-key
   detection. Do not blanket-decode all scalar strings as JSON.
5. Validate one complete batch, call `set_many` in the caller root transaction,
   verify normalized persisted values and `effective(require_store=True)`.
6. Report only key + fixed error category in public diagnostics. Raw conversion
   errors and SQL bound parameters can expose values. Keep source row data,
   hashes, exceptions and detailed receipts in protected rehearsal storage.

## Release evidence still owed

The legacy exclusion register contains active worker ACL/lease/recovery,
bearer authority, live policy revocation, reset/invitation atomicity, resend
transaction and route-isolation checks. The README records these as open parity
gaps. Green portable tests do not clear them. Each needs maintained hosted proof
or a reviewed, specific supersession mapping before flip approval.
