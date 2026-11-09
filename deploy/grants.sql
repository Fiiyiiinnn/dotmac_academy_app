-- Composition EXECUTE grants for the Learning SQL capabilities.
--
-- lr_0011/lr_0012 install owner-only: EXECUTE is revoked from PUBLIC and
-- granted to nobody. The composing application grants it to the roles
-- that call the Python result/completion/certificate/lab APIs — never
-- to the lab worker, which reaches the ledger only through the product
-- definer (a012). Run as the migration owner (app_admin) after
-- `alembic upgrade heads`, once per database.
GRANT EXECUTE ON FUNCTION
    mod_learning.record_lab_result_v1(uuid, uuid, uuid, uuid, jsonb,
        double precision, double precision, double precision, boolean, jsonb),
    mod_learning.recompute_completion_v1(uuid, uuid, uuid, timestamptz),
    mod_learning.issue_certificate_v1(uuid, uuid, uuid, timestamptz)
TO app_user;
