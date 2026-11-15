-- Execute once as postgres, connected to postgres, on the new isolated V2 cluster.
-- Roles remain NOLOGIN until separate credentials have been securely configured.
\set ON_ERROR_STOP on
BEGIN;
DO $$
BEGIN
  IF current_database() <> 'postgres' OR current_user <> 'postgres'
     OR current_setting('server_version_num')::integer NOT BETWEEN 170012 AND 179999 THEN
    RAISE EXCEPTION 'Unexpected bootstrap connection';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_database WHERE datname = 'antiscam_v2')
     OR EXISTS (SELECT 1 FROM pg_roles WHERE rolname IN
       ('antiscam_migrator', 'antiscam_runtime', 'antiscam_backup', 'antiscam_health')) THEN
    RAISE EXCEPTION 'STOP: V2 database or roles already exist';
  END IF;
END $$;
CREATE ROLE antiscam_migrator NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE antiscam_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE antiscam_backup NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE antiscam_health NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
COMMIT;
CREATE DATABASE antiscam_v2 OWNER antiscam_migrator TEMPLATE template0 ENCODING 'UTF8';
REVOKE ALL ON DATABASE antiscam_v2 FROM PUBLIC;
GRANT CONNECT ON DATABASE antiscam_v2 TO antiscam_migrator, antiscam_runtime, antiscam_backup, antiscam_health;
ALTER DATABASE antiscam_v2 SET timezone = 'UTC';
\connect antiscam_v2
BEGIN;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO antiscam_runtime, antiscam_backup, antiscam_health;
ALTER DEFAULT PRIVILEGES FOR ROLE antiscam_migrator IN SCHEMA public GRANT SELECT ON TABLES TO antiscam_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE antiscam_migrator IN SCHEMA public GRANT SELECT ON SEQUENCES TO antiscam_backup;
COMMIT;
SELECT current_database() AS database, pg_get_userbyid(datdba) AS owner
FROM pg_database WHERE datname = current_database();
SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
FROM pg_roles WHERE rolname LIKE 'antiscam\_%' ESCAPE '\' ORDER BY rolname;
