\set ON_ERROR_STOP on
BEGIN;
DO $$ BEGIN
  IF current_database() <> 'antiscam_v2' OR current_user <> 'postgres'
     OR NOT EXISTS (SELECT 1 FROM migration_runs WHERE state='verified' AND manifest->>'purpose'='production') THEN
    RAISE EXCEPTION 'Unexpected target or missing verified production import';
  END IF;
END $$;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO antiscam_runtime, antiscam_backup;
SELECT format('GRANT INSERT, UPDATE, DELETE ON TABLE public.%I TO antiscam_runtime;', tablename)
FROM pg_tables WHERE schemaname='public' AND tablename NOT IN ('alembic_version','migration_runs')
\gexec
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO antiscam_runtime;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO antiscam_backup;
GRANT SELECT ON system_state, jobs TO antiscam_health;
COMMIT;
