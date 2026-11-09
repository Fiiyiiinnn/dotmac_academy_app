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
