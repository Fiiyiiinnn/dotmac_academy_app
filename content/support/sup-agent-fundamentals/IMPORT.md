# Importing "Support Agent Fundamentals: Working in dotmac_sub"

This course is live in production on `academy.dotmac.io` (currently at
`v4`) and this file is the living import/re-import record for it, not a
pre-launch checklist. It follows the same content pipeline as the
management courses — see `docs/bank-lint-in-the-content-repo.md` and
`content/management/IMPORT.md` for the general pattern this mirrors.

**These are the exact commands to (re-)run on the `academy` host, as user
`dotmac`, from `/home/dotmac/projects/dotmac_academy_app`, any time this
course's content changes.** Re-importing is the normal way a content fix
reaches production — it is not a one-time, not-yet-happened event.

## Provenance

- Content authored: 2026-09-10.
- Screenshots (`figures/SUP-*.png`) were captured against **dotmac_sub commit
  `ce1662947f99d008b6a7faaf365a0ef51d58b7ce`** (branch `main`), via a
  throwaway, since-deactivated `support`-role account on the `seabone`
  staging instance. No production or real-customer data was captured — the
  seabone staging database is QA/test fixture data (account names such as
  "PR1747 Unsuspend Test", `@example.com` addresses, `e2e.agent@example.com`).
- Sub's field names, status enums, and RBAC permission set (ticket
  status/priority/channel, account vs. subscription status, the `support`
  role's permission list, and the absence of `monitoring:read`) were read
  directly from `dotmac_sub` source at the same commit — not inferred or
  assumed. If Sub's ticket/subscriber/RBAC model changes materially, this
  course's chapters 2–5 should be re-verified against the new model before
  re-importing, not assumed still accurate.
- **This is the reason for the `--source-ref` value below** — it pins the
  course's `Course.source_ref` field to the exact dotmac_sub commit its
  content and screenshots describe, so a future drift check has something
  concrete to diff against.
- Chapter 6 ("Understanding Customer Behavior and Professional
  Communication") was added 2026-09-11. Unlike chapters 2–5, it is not
  drawn from `dotmac_sub` source or screenshots — it is grounded in a real,
  aggregate-only (no PII, no free text) 90-day production performance pull
  recorded in Knowledge as `dotmac-sub-support-performance-aggregate-2026-09-11`.
  It carries no figure callouts (no screenshot was captured for it).

## 1. Validate locally first (already done, re-run to confirm)

From this repo's root:

```bash
python3 app/services/bank_lint.py content/support/sup-agent-fundamentals/banks/
# Expect: 7/7 bank(s) pass
```

## 2. Import the course content

```bash
set -a; . ./.env; set +a
export DATABASE_URL="$MIGRATION_DATABASE_URL"   # never echo this

.venv/bin/python -m app.cli import-manual \
  --tenant-slug dotmac \
  --slug sup-agent-fundamentals \
  --title "Support Agent Fundamentals: Working in dotmac_sub" \
  --discipline support \
  --source-ref "sup-agent-fundamentals@dotmac_sub:ce16629" \
  --chapters-dir content/support/sup-agent-fundamentals \
  --figures-dir content/support/sup-agent-fundamentals/figures

.venv/bin/python -m app.cli load-banks \
  --tenant-slug dotmac \
  --banks-dir content/support/sup-agent-fundamentals/banks

.venv/bin/python -m app.cli load-curriculum \
  --tenant-slug dotmac \
  --file content/support/CURRICULUM.yaml
```

(`--file` and `--tenant-slug` are confirmed against `app/cli.py`; unlike
`import-manual`/`load-banks`, this command was not run end-to-end in this
authoring session, since it requires a live tenant database.)

## 3. Verify the import

```bash
docker exec -i academy-db psql -U postgres -d academy -c \
  "select slug, title, discipline, status, listed, source_ref from courses where slug = 'sup-agent-fundamentals';"
```

Expect `status = published` (the `import-manual` default) and
`listed = false` — that default is almost certainly correct for this course:
it is internal training for support staff, not public-catalog material, the
same way the entrance-exam and instructor-guide courses stay unlisted.
Leave `listed` false unless there's a specific reason to change it.

## 4. Make it reachable — this is the step the management-course IMPORT.md
   undersells

Importing the course does **not** put it in front of any learner. Sub's own
access model is `Enrollment → Cohort → CourseOffering → Course`
(`app/models/offering.py`), and `Course.listed` only controls the anonymous
public-catalog page — it is unrelated to whether an enrolled learner can
reach the course. There is no CLI command for offering/cohort/enrollment
creation in this codebase; it is done through the instructor/admin web UI
(`app/web/instructor.py`). After the import above:

1. Create a **Cohort** for support agents (or reuse an existing one) —
   `discipline: support`.
2. Create a **CourseOffering** for `sup-agent-fundamentals` against that
   cohort.
3. **Enroll** the support agents who should take it.

Until all three exist, the course sits imported but invisible to every
learner, exactly like the six management courses noted in
`content/management/IMPORT.md` as "sit[ting] at 0 offerings."

## 5. Known content limitation to flag, not silently work around

Chapter 4 teaches that the support role cannot reach `/admin/network/outages`
(confirmed empirically: a support-role account gets an HTTP 403 from that
route). That boundary is real and by design. It does **not** mean frontline
agents cannot self-serve "is there a known outage here" — an earlier version
of this course said exactly that, and it was wrong: a customer's own Network
tab (reachable via `customer:read`, which support already has) shows a live
"Known incident" / "Known area outage" panel with an "Open the covering
ticket" link, or an explicit "No known outage · Last checked <time>" line
when nothing is known. That per-customer check is real, accessible
self-service; only the general, all-incidents console stays with NOC. The
remaining, genuine gap is narrower than previously stated: a "No known
outage" reading on one customer's record does not rule out an outage that
has not yet been confirmed against that specific customer, so a clustered,
same-symptom pattern across several customers still has to go to NOC rather
than being self-serviceable. That narrower gap is worth a product decision
at some point (e.g. a lightweight, support-visible signal for suspected-but-
unconfirmed incidents); this course teaches the current, real mechanism, it
does not resolve that residual gap.
