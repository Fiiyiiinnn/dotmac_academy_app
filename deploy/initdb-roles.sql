-- CI/test bootstrap only. Never use these sample passwords in production.
-- Production roles/secrets require the separately reviewed deployment packet.
CREATE ROLE app_admin LOGIN BYPASSRLS PASSWORD 'app_admin';
CREATE ROLE academy_lab_worker LOGIN NOINHERIT BYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD 'academy_lab_worker';
CREATE ROLE app_user LOGIN PASSWORD 'app_user';
CREATE ROLE platform_api LOGIN PASSWORD 'platform_api';
-- Precreate kernel dispatcher roles as the cluster bootstrap authority.
-- The migrator deliberately has neither CREATEROLE nor SUPERUSER.
CREATE ROLE outbox_dispatcher LOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE platform_outbox_dispatcher LOGIN NOSUPERUSER NOBYPASSRLS;
GRANT ALL ON SCHEMA public TO app_admin;
