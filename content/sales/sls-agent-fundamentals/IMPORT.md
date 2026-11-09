# Importing "Sales Agent Fundamentals: Working in dotmac_sub"

This course was authored locally (not yet imported into the production
`academy.dotmac.io` database). It follows the same content pipeline as the
management and support courses — see `docs/bank-lint-in-the-content-repo.md`
and `content/support/sup-agent-fundamentals/IMPORT.md` for the general
pattern this mirrors.

**Nothing in this file has been run against production.** These are the
exact commands to run on the `academy` host, as user `dotmac`, from
`/home/dotmac/projects/dotmac_academy_app`.

## Provenance

- Content authored: 2026-09-11.
- Screenshots (`figures/SLS-*.png`) were captured against **dotmac_sub commit
  `ce1662947f99d008b6a7faaf365a0ef51d58b7ce`** (branch `main`), via a
  throwaway, since-deactivated custom "sales" training role on the `seabone`
  staging instance — deliberately a **custom** role for this capture, not a
  seeded one, since dotmac_sub ships no seeded sales role (see the known
  limitation below). No production or real-customer data was captured — the
  seabone staging database is QA/test fixture data (names such as "Chidinma
  Okafor" and identifiers such as `SO-000001`/`PROJ-1108` are staging fixture
  values, not real subscribers).
- Sub's lead/quote/sales-order field names, status enums, the quote
  acceptance conversion behavior, and the RBAC gap described in Chapter 1
  were read directly from `dotmac_sub` source at the same commit — not
  inferred or assumed. If Sub's sales/CRM model changes materially, this
  course's chapters should be re-verified against the new model before
  re-importing, not assumed still accurate.
- **This is the reason for the `--source-ref` value below** — it pins the
  course's `Course.source_ref` field to the exact dotmac_sub commit its
  content and screenshots describe, so a future drift check has something
  concrete to diff against.

## 1. Validate locally first (already done, re-run to confirm)

From this repo's root:

```bash
python3 app/services/bank_lint.py content/sales/sls-agent-fundamentals/banks/
# Expect: 6/6 bank(s) pass
```

## 2. Import the course content

```bash
set -a; . ./.env; set +a
export DATABASE_URL="$MIGRATION_DATABASE_URL"   # never echo this

.venv/bin/python -m app.cli import-manual \
  --tenant-slug dotmac \
  --slug sls-agent-fundamentals \
  --title "Sales Agent Fundamentals: Working in dotmac_sub" \
  --discipline sales \
  --source-ref "sls-agent-fundamentals@dotmac_sub:ce16629" \
  --chapters-dir content/sales/sls-agent-fundamentals \
  --figures-dir content/sales/sls-agent-fundamentals/figures

.venv/bin/python -m app.cli load-banks \
  --tenant-slug dotmac \
  --banks-dir content/sales/sls-agent-fundamentals/banks

.venv/bin/python -m app.cli load-curriculum \
  --tenant-slug dotmac \
  --file content/sales/CURRICULUM.yaml
```

(`--file` and `--tenant-slug` are confirmed against `app/cli.py`; unlike
`import-manual`/`load-banks`, this command was not run end-to-end in this
authoring session, since it requires a live tenant database.)

## 3. Verify the import

```bash
docker exec -i academy-db psql -U postgres -d academy -c \
  "select slug, title, discipline, status, listed, source_ref from courses where slug = 'sls-agent-fundamentals';"
```

Expect `status = published` (the `import-manual` default) and
`listed = false` — that default is almost certainly correct for this course:
it is internal training for sales staff, not public-catalog material, the
same way the support course and the entrance-exam/instructor-guide courses
stay unlisted. Leave `listed` false unless there's a specific reason to
change it.

## 4. Make it reachable — this is the step the earlier courses' IMPORT.md
   files undersell

Importing the course does **not** put it in front of any learner. Sub's own
access model is `Enrollment → Cohort → CourseOffering → Course`
(`app/models/offering.py`), and `Course.listed` only controls the anonymous
public-catalog page — it is unrelated to whether an enrolled learner can
reach the course. There is no CLI command for offering/cohort/enrollment
creation in this codebase; it is done through the instructor/admin web UI
(`app/web/instructor.py`). After the import above:

1. Create a **Cohort** for sales agents (or reuse an existing one) —
   `discipline: sales`.
2. Create a **CourseOffering** for `sls-agent-fundamentals` against that
   cohort.
3. **Enroll** the sales agents who should take it.

Until all three exist, the course sits imported but invisible to every
learner, exactly like the support course and the six management courses
noted as "sit[ting] at 0 offerings" in their own IMPORT.md files.

## 5. Known content limitation to flag, not silently work around

Chapter 1 teaches that **dotmac_sub ships no seeded "sales" role** —
`scripts/seed/seed_rbac.py` declares the `crm:lead:*`, `crm:quote:*`, and
`crm:sales_order:*` permission keys as UI-assignable but stays
admin-implicit on all of them, with an inline comment stating this
explicitly, until a dedicated role is defined. This is accurate to the
current system and is a real, current limitation of the source material
this course teaches from, not a simplification for the course's benefit:
whoever enrolls real sales staff against this course should expect that
production access today is most likely broader, admin-like access rather
than a narrowly-scoped sales role, and should treat that as a known gap to
plan around — not assume the tidy per-role access model this course
describes for the Sales Agent attribution field (governed by the separate
"Customer Experience" role) extends to the rest of sales work as well. A
real seeded sales role, scoped to exactly `crm:lead:*`/`crm:quote:*`/
`crm:sales_order:*`, is the eventual fix; this course teaches the current,
real state, it does not resolve the underlying gap.
