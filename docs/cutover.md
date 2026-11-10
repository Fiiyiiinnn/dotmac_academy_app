# Academy cutover status and historical evidence

## Current status — 2026-11-10

The production data cutover completed on 2026-11-08. The new application has
accepted live writes since that flip. Michael subsequently approved exact
public source revision `d12260b6996b18b27e04a784042f764bd583952d`, tree
`d273399b7ccaa812208a242dc7794f1289873819`, deployed to both web and lab
hosts on 2026-11-10. No second import or traffic flip is pending.

The canonical public repository is
[michaelayoade/dotmac_academy_app](https://github.com/michaelayoade/dotmac_academy_app),
stable GitHub ID `1451805046`. Repository naming is complete. See the
[repository identity crosswalk](../README.md#public-source-and-private-qualification-data)
for the separate public legacy archive, private composed-app archive and private
fixture repository. Names may change; stable repository IDs and exact revisions
bind the evidence.

**Recovery is fix forward only.** The legacy database is not a rollback target.
Retain the legacy database, final dump, source and receipts as archive/evidence
until Michael explicitly approves deletion. Earlier instructions to return
traffic to legacy, or to seek a reconciliation decision before doing so, are
superseded by this ruling. Acceptance does not itself authorize retirement or
deletion; legacy retirement and D8 residual authority remain separate work.

## Qualification and live acceptance

[Public CI run 42687722118](https://github.com/michaelayoade/dotmac_academy_app/actions/runs/42687722118)
qualified the exact deployed revision: **4,903 passed**, zero failures/errors,
and two declared legacy skips (`test_banks_lint_clean` and `test_full_flow`).
The run includes synthetic reporting-privacy checks before pinned private
fixture acquisition. Missing fixture access, changed bytes or incomplete
execution fail qualification. See the [README](../README.md) for the trusted
CI and private fixture requirements.

The deployed composition uses Kernel `0.1.0a108`, Learning `0.1.0a6`, UI
`0.1.0a9`, delivery-resend `0.1.0a1` and app-sync source
`e1bfa54e23bd8184c20beb2dc1d9f0938fa336f6`. All three approved acceptance
areas passed:

| Area | Evidence and limit |
| --- | --- |
| Delivery-resend adoption | Installed module and actual caller wiring verified, backed by 670 hosted authority cases. No real SMTP send probe was performed. |
| Linux ttyd | Scoped CSP, real WebSocket keyboard input and terminal output, isolation, saved grade/reload and logout passed. A separate authenticated wrong-origin Upgrade request returned 403; browser cookie transmission was not established by the browser probe. |
| RouterOS/WebFig | Ready before first console 200, normal device login and Quick Set-to-WebFig navigation, all nine schemas, confined device POSTs, Interfaces/group behavior, denial checks and logout passed, with zero CSP script errors. |

Owned fixture cleanup completed: zero owned runtimes, active fixture sessions
or synthetic outbound mail remained. Seven original timers were restored and
active. Historical reaped records remain evidence, not live runtimes.

The final `AcademyLiveAcceptanceReceipt.v1` has `overall_acceptance=true` and
SHA256 `77a1c92de14230717b3f31986ae0772eb5b373294684c1fc5dac2955045e75dd`.
The full receipt and operational evidence are retained privately; public docs
do not contain host details, private operational paths or fixture payloads.
These results establish the named acceptance scopes, not closure of all
migration, integration or module-adoption debt.

## Remaining work and authority boundaries

| Work item | Current status / acceptance still needed |
| --- | --- |
| Admissions business journey | Live apply-to-enrolled proof is not recorded. Do not infer it from login, lab acceptance or the older D1 route. |
| Thin HTTP regression coverage | `tests/test_http_journeys.py` exercises the full application assembly for admissions/manual review, reset/invite and lab intent/ownership journeys. These run in the first CI partition; retain exact-revision JUnit evidence. Aggregate test or file counts do not establish complete route coverage. |
| ERP app-sync | An ERP v2 application skeleton is recorded; receiver qualification and live availability remain unproven here. Closure requires a qualified authenticated receiver route, idempotency and HMAC behavior, followed by authorized shadow enablement and readback. The old ERP webhook remains authoritative. |
| Settings | The historical 2026-11-08 production comparison passed: 159 keys, zero divergence. Legacy settings remain authoritative pending a deliberate Kernel strict-read/relations-layer cutover and fresh proof; the historical comparison alone does not establish that cutover. |
| Credential operations | Current login, reset, invitation assignment and authenticated password change are wired to released Kernel owners. `a0003` persists lifecycle/reset policy and `a0004` binds tokens to Party/PartyPerson; generic future API/migration and SOURCE ONLY headers were stale and are corrected here. The new HTTP journeys cover these active user flows; exact-revision hosted qualification remains required. Cohort force-reset and generated-material provisioning have no Academy caller; they are separate use cases, not prerequisites for current flows. |
| Weekly learner digest | Off by default, matching the existing parity gate. Enabling it needs an explicit product decision. The separate instructor email digest defaults on; its policy does not establish learner digest activation. |
| D1 retained-schema admission | Historical platform-route work for the old application, distinct from the Admissions apply-to-enrolled business journey. Older records conflict on disposition/proof; reconcile them against the new-composition ruling before claiming closure. |
| Legacy retirement and D8 residual authority | Separate follow-ups; retain archive/evidence and review residual ownership without assuming live console acceptance closes them. |

Repository naming is completed work and is not an outstanding adoption gate.

The [seven-gap closure tracker](post-cutover-gaps.json) records evidence, limits
and closure criteria separately from implementation. Hosted HTTP qualification
and production live proof are distinct records.

## Historical migration evidence

The earlier candidate gates, production-snapshot rehearsals, V6 transfer and
one downtime flip explain how the current application was established. They
are historical evidence, not instructions to repeat a production import or
flip. Their detailed mappings, schema decisions, identities, exceptions and
receipts remain retained under the existing privacy and retention boundaries.

- [Migration preflight findings](migration-preflight.md) record mapping and
  safety review. The importer binds one destination root transaction for
  identity, Learning V6, product, ERP history and settings, refuses nonfresh or
  RLS-filtered extents, rejects unresolved exceptions and verifies mapped
  values. Those controls do not authorize running it against the live target.
- [The historical downtime runbook](flip-runbook.md) records writer fencing,
  quiescence, final dump, smoke isolation and the original flip process. Any
  pre-write rollback wording there is superseded by the current fix-forward
  ruling above. It is not a present rollback or retirement authorization.
- Private migration and acceptance receipts bind source/destination identities,
  snapshot digests, revisions, pins, tenant extent, reconciliation, exceptions
  and smoke outcomes. Keep row data, password hashes, snapshots, credentials,
  vendor bytes and unsanitized diagnostics out of public Git and CI artifacts.

Any further production operation needs its own authorized scope. Isolated
migration work belongs on the dedicated test server; do not use local Docker.
