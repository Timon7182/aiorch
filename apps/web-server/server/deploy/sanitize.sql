-- sanitize.sql — run against cts_golden_a after restore, BEFORE it is cloned,
-- so no PII or bulky history ends up in any preview/static DB.
--
-- This is a generic starting point that works without knowing the exact schema:
--   * truncates tables whose name looks like logs/audit/history/events/jobs
--   * scrubs common PII columns (email/phone/password/token/iin/secret)
-- Review the NOTICE output and tighten for the real cts schema.
--
-- Usage: psql -U admin -d cts_golden_a -f sanitize.sql

\set ON_ERROR_STOP on

-- 1. Truncate big append-only tables (logs/audit/history/events/outbox/jobs).
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT format('%I.%I', schemaname, tablename) AS t
    FROM pg_tables
    WHERE schemaname NOT IN ('pg_catalog','information_schema')
      AND (
        tablename ~* '(_|^)(log|logs|audit|history|histories|event|events|outbox|inbox|job|jobs|trace|telemetry|notification|notifications)(_|$)'
      )
  LOOP
    RAISE NOTICE 'TRUNCATE %', r.t;
    EXECUTE format('TRUNCATE TABLE %s CASCADE', r.t);
  END LOOP;
END $$;

-- 2. Scrub common PII columns in-place (anonymize, keep row counts).
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT format('%I.%I', table_schema, table_name) AS t, column_name AS c, data_type
    FROM information_schema.columns
    WHERE table_schema NOT IN ('pg_catalog','information_schema')
      AND column_name ~* '(email|e_mail|phone|mobile|passwd|password|pwd|secret|token|api_key|apikey|iin|ssn|passport|card|cvv)'
      AND data_type IN ('character varying','text','character')
  LOOP
    RAISE NOTICE 'SCRUB %.%', r.t, r.c;
    BEGIN
      IF r.c ~* 'email|e_mail' THEN
        EXECUTE format('UPDATE %s SET %I = ''user'' || id || ''@example.invalid'' WHERE %I IS NOT NULL', r.t, r.c, r.c);
      ELSE
        EXECUTE format('UPDATE %s SET %I = ''REDACTED'' WHERE %I IS NOT NULL', r.t, r.c, r.c);
      END IF;
    EXCEPTION WHEN others THEN
      RAISE NOTICE '  skipped %.% (%):', r.t, r.c, SQLERRM;  -- e.g. no "id" column
    END;
  END LOOP;
END $$;

-- 3. (Optional, uncomment + edit) shrink very large business tables for fast clones:
-- DELETE FROM public.shipments WHERE created_at < now() - interval '90 days';

ANALYZE;
