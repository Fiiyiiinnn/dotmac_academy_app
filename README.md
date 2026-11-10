# Dotmac Academy

Dotmac Academy is a thin Starter-composed application. The canonical public
source is [michaelayoade/dotmac_academy_app](https://github.com/michaelayoade/dotmac_academy_app)
(repository ID `1451805046`). Internal runtime and package identifiers retain
their existing names.

The data cutover completed on 2026-11-08 and the new application has accepted
live writes. Michael approved exact source revision
`d12260b6996b18b27e04a784042f764bd583952d`, deployed to both web and lab
hosts on 2026-11-10. Recovery is **fix forward only**. Legacy data, source and
receipts remain archive/evidence until Michael explicitly approves deletion.
See [cutover status and remaining work](docs/cutover.md).

Composition:

- `dotmac-kernel` 0.1.0a108 — tenancy, identity, authorization, sessions,
  settings, audit, application construction (`create_app`).
- `dotmac-learning` 0.1.0a6 — the learning domain and its `mod_learning`
  storage (`lr` lineage through `lr_0012`); SQL is the sole
  completion/certificate writer.
- `dotmac-ui` 0.1.0a9 — shared design system.
- `dotmac-delivery-resend` 0.1.0a1 — released delivery authority; installed
  module and caller wiring verified.
- App-sync source `e1bfa54e23bd8184c20beb2dc1d9f0938fa336f6` — ERP adapter
  composition; receiver qualification, authorized shadow enablement and
  readback remain separate work.
- `app/` — Academy-unique domain only (labs, admissions, ERP adapters,
  delivery policy, presentation), ported slice by slice per the
  service ownership classification. Anything the kernel or a module owns
  is consumed, never reimplemented.

Genesis uses the kernel and Learning module lineages. The historical V6
transform-to-fit transfer and downtime flip are complete; a second import or
traffic flip is not a current task.

[Exact-revision GitHub Actions qualification](https://github.com/michaelayoade/dotmac_academy_app/actions/runs/42687722118)
passed **4,903 tests**, with zero failures/errors and two declared legacy skips.
Qualification uses Poetry 2.4.1 and the committed lock. Live acceptance passed
for delivery module adoption, Linux ttyd and RouterOS/WebFig. Delivery adoption
verified installed wiring and retained hosted authority cases; it did not send
a real SMTP probe. These results do not close every migration or adoption gap;
see [remaining work](docs/cutover.md#remaining-work-and-authority-boundaries).

Migration tooling is retained for reviewed historical evidence and separately
authorized isolated work on the dedicated test server, not a new production
import. Do not use a local Docker daemon.

## Public source and private qualification data

This repository has a fresh source history. The completed repository naming
change preserves the following stable identity crosswalk:

| Repository | Stable GitHub ID | Status and purpose |
| --- | --- | --- |
| [dotmac_academy_app](https://github.com/michaelayoade/dotmac_academy_app) | `1451805046` | Public canonical source; formerly `dotmac_academy_v2_public`. |
| [dotmac_academy_app_legacy_archive_20261110](https://github.com/michaelayoade/dotmac_academy_app_legacy_archive_20261110) | `1284678375` | Public, archived legacy application. |
| `dotmac_academy_v2_private_archive_20261110` | `1449046316` | Private, archived original composed-app history, including vendor fixtures. |
| `dotmac_academy_console_fixtures` | `1451803855` | Private fixture-only source; unchanged by repository naming. |

Repository names are not identity proof. The public source's private-history
anchor is commit `42fee6c5da8e238231bd2ff594acb5ad40d88aab` in repository
ID `1449046316`. Application, template, static, migration and dependency files
at deployed public revision `d12260b6996b18b27e04a784042f764bd583952d` match
that anchor; publication changes concern qualification and documentation.
Private history and vendor assets remain private. Repository naming completion
does not authorize legacy data deletion or close residual adoption debt.

The WebFig payloads are not distributed here. Their public manifest and hashes
remain in `tests/fixtures/webfig`, and the immutable private fixture source is
recorded in `.github/private-fixtures/webfig.json`. Full CI first proves reporting
privacy using synthetic data, then retrieves and verifies all 42 assets and nine
schemas. Missing access, changed bytes, or incomplete execution fail qualification.

Trusted CI needs `FORGEJO_READ_TOKEN` for the pinned platform packages and
`ACADEMY_WEBFIG_FIXTURE_SSH_KEY` for the fixture-only repository. The latter must
be a dedicated read-only deploy key. Credentials are unavailable to fork pull
requests; full qualification therefore requires a maintainer-reviewed trusted
branch. Do not use `pull_request_target` to execute contributor code with secrets.
Vendor bytes and unsanitized diagnostics must never be committed or uploaded.
