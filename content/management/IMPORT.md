# Importing the management courses

Nine courses, 46 chapters, 55 banks. Chapters and banks live here; **figures and
covers do not** — see "Figures" below.

## The two things that go wrong

**1. `import-manual` rewrites the course title on every run.** It is not
create-only. Pass a wrong or empty `--title` and the course is renamed, publicly,
on `/courses`. This happened on 2026-08-06 and put slugs in place of all nine
titles. Use the table below verbatim.

**2. Assessment policy is applied at load time, not at read time.** A `policy:`
block only takes effect when `load-banks` runs against a build that understands
it. Banks loaded by an older revision keep whatever the activity had before —
silently, because the old parser ignores the key. If you deploy a release that
changes policy handling, **re-run `load-banks`** or nothing changes.

## Canonical slugs and titles

| slug | title |
| --- | --- |
| `mgmt-team-leadership` | Leading a Team: First-Time Manager Essentials |
| `mgmt-project-management` | Project Management Essentials |
| `mgmt-finance-fundamentals` | Finance for Non-Financial Managers |
| `mgmt-customer-experience` | Customer Experience & Service Management |
| `mgmt-vendor-procurement` | Vendor & Procurement Management |
| `mgmt-decision-making` | Decision Making & Problem Solving |
| `mgmt-communication` | Communication & Reporting for Managers |
| `mgmt-data-kpis` | Managing with Data & KPIs |
| `mgmt-sales-fundamentals` | Sales Essentials |

## Procedure (proven on academy.dotmac.io)

The app DB uses RLS and the app role fails closed — 0 rows — without a tenant
GUC, so the CLI runs with the migration role. Tenant slug is `dotmac`. As user
`dotmac`, from `/home/dotmac/projects/dotmac_academy_app`:

```bash
set -a; . ./.env; set +a
export DATABASE_URL="$MIGRATION_DATABASE_URL"   # never echo this

CONTENT=/home/dotmac/projects/academy-management-courses

.venv/bin/python -m app.cli import-manual \
  --tenant-slug dotmac \
  --slug mgmt-team-leadership \
  --title "Leading a Team: First-Time Manager Essentials" \
  --discipline management \
  --chapters-dir "$CONTENT/mgmt-team-leadership" \
  --figures-dir  "$CONTENT/figures"

.venv/bin/python -m app.cli load-banks \
  --tenant-slug dotmac \
  --banks-dir "$CONTENT/mgmt-team-leadership/banks"
```

**Always pass `--figures-dir` explicitly.** Its default is the *fiber* figures
tree (`/home/dotmac/projects/dotmac-academy/figures/final`), and `import-manual`
runs `strict_figures=True`, so an omitted flag aborts on the first `MGT-*`
reference rather than doing something quietly wrong.

**Read the `load-banks` output.** A bank failing lint is **skipped with a printed
reason** — the command does not abort and exits 0. "Done — 6/6" is the line that
means success; "5/6" means one bank did not load and the old questions are still
live.

Run these from a script file, not a nested-quoted `ssh 'su - dotmac -c "..."'`
one-liner. The title incident above was a shell-quoting failure.

## Figures

`figures/` (37M) and `covers/` (19M) are **not in this repo**. They live on the
academy host at `/home/dotmac/projects/academy-management-courses/`. A fresh
clone therefore cannot import chapters without either fetching them or passing
`--allow-missing-figures`, which substitutes placeholder blocks — acceptable for
a local smoke test, never for a real import.

`figures-src/` *is* committed: HTML sources for the explanatory figures, rendered
to PNG with headless Chrome at 1344x768:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless \
  --disable-gpu --screenshot=figures/MGT-FN-02.png --window-size=1344,768 \
  --hide-scrollbars "file://$PWD/figures-src/MGT-FN-02.html"
```

Illustrative chapter-opener figures are nano-banana output and are not
reproducible; those PNGs are the artefact, not a derivative.

## Assessment policy

Each bank declares its own policy, applied by `load-banks`:

```yaml
bank:
  policy: {mode: exam, max_attempts: 5, pool: 20}
```

Chapter banks are `mode: practice`. Finals are `mode: exam` with
`max_attempts: 5`, and carry `pool: 20` where the bank holds 30 questions —
`lint_bank` rejects a pool that is not smaller than its bank, since that holds
nothing back.

`mgmt-team-leadership`, `mgmt-decision-making`, `mgmt-communication` and
`mgmt-customer-experience` still have 20-question finals and therefore no pool;
they need +10 questions each before pooling is meaningful.

## After importing

Courses are only visible to learners once they have an **offering** and an
**entitlement**. Import alone changes nothing a learner can see.
