# dotmac_academy_v2

Dotmac Academy, composed thin from Starter. Ruled 2026-11-06 (Michael):
Academy moves to this new composed app and migrates to it; the legacy
`dotmac_academy_app` remains untouched as rollback until the flip.
Ruling and scope: Knowledge `dotmac-academy-new-composition-20261106`;
service classification: `.cutover-private/academy-new-composition-20261106/
classification.md` in the projects workspace.

Composition:

- `dotmac-kernel` 0.1.0a107 — tenancy, identity, authorization, sessions,
  settings, audit, application construction (`create_app`).
- `dotmac-learning` 0.1.0a6 — the learning domain and its `mod_learning`
  storage (`lr` lineage through `lr_0012`); SQL is the sole
  completion/certificate writer.
- `dotmac-ui` 0.1.0a9 — shared design system.
- `app/` — Academy-unique domain only (labs, admissions, ERP adapters,
  delivery policy, presentation), ported slice by slice per the
  classification. Anything the kernel or a module owns is consumed,
  never reimplemented.

Genesis is the composed lineage: kernel head + learning head. Learning
data arrives by the module's V6 storage transfer from the legacy app
(transform-to-fit; exceptions to manual review), rehearsed on a
production snapshot before one downtime flip.

Local migration smoke (dedicated test server, disposable PG16):

    MIGRATION_DATABASE_URL=postgresql+psycopg://app_admin@HOST/DB \
        alembic upgrade heads
    psql "$ADMIN_URL" -f deploy/grants.sql

Candidate qualification runs in GitHub Actions using Poetry 2.4.1 and the
committed lock. See [cutover gates](docs/cutover.md) for current migration gaps,
snapshot-rehearsal approval and the separate downtime-flip approval.

## Public source and private qualification data

This repository has a fresh source history. The original private Academy history
is retained separately because it contains vendor-owned compatibility fixtures.
Its source anchor is commit `42fee6c5da8e238231bd2ff594acb5ad40d88aab`
in GitHub repository ID `1449046316`; repository names are not the identity proof.
Application, template, static, migration, and dependency files are unchanged from
that source anchor. Publication changes concern qualification and documentation.

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
