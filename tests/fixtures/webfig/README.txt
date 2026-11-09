Private RouterOS compatibility corpus

MikroTik retains rights to its assets. Vendor entity bytes are not included in
this public source tree and are not relicensed as Academy code. The public
manifest contains integrity metadata only.

CI installs locked dependencies and checks source formatting first, then runs
synthetic privacy reporting canaries without reading vendor bytes. Only after
those canaries and their execution evidence pass does CI acquire the corpus via
python -I -S scripts/materialize_webfig_fixtures.py. Isolated Python disables
site initialization, installed .pth files, and PYTHONPATH for the secret step.
A read-only deploy key scoped to the private fixture repository is supplied only
to that step as ACADEMY_WEBFIG_FIXTURE_SSH_KEY.
Missing access, including unqualified fork runs, fails qualification explicitly.
No test partition is skipped as a substitute for acquiring the corpus.

The descriptor in .github/private-fixtures/webfig.json pins the private commit,
manifest digest, and GitHub host trust file. Acquisition validates the exact tree,
compressed and decoded bounds, hashes, asset count and schema count without
executing provider JavaScript. Only validated manifest-listed gzip files are
written here; these files remain ignored and must never be committed or uploaded.
The temporary key is deleted before fixture materialization and qualification
tests. Public CI evidence is limited to count/byte/integrity metadata and
privacy-safe JUnit reports, including the separate synthetic privacy report.
