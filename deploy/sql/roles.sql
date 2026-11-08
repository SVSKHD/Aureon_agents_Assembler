-- Scoped database roles for Aureon (docs/24-production-integration.md, Phase 22).
--
-- Run by the database OWNER / a superuser, against the app database (`spasta`), with psql:
--
--     deploy/sql/apply-roles.sh            # wrapper: passwords from env/prompt, via stdin
--
-- or by hand (passwords as psql variables, never in this file):
--
--     psql -X -v ON_ERROR_STOP=1 -d spasta \
--          -v app_password=... -v migrator_password=... -v readonly_password=... \
--          -f deploy/sql/roles.sql
--
-- Idempotent: safe to run again after every migration (it re-grants on new tables).
-- It NEVER drops anything: no DROP, no data change. It creates what is missing, transfers
-- ownership of the app schemas to the migrator, and (re)applies grants.
--
-- Roles (names overridable with -v app_role=... etc.; the tests use unique names):
--   aureon_app           LOGIN. DML on the app schemas + the public finance tables. No DDL,
--                        no TRUNCATE. Append-only on the audit trail (see below).
--   aureon_migrator      LOGIN. Owner of the app schemas and their tables. Used ONLY by the
--                        `migrate` service (MIGRATIONS_DATABASE_URL).
--   aureon_readonly      LOGIN. SELECT only (backups, analytics).
--   aureon_audit_writer  NOLOGIN group. SELECT + INSERT on the append-only audit tables;
--                        granted to aureon_app. Nobody but the owner may UPDATE/DELETE them.
--
-- Optional variables: app_password, migrator_password, readonly_password (a role's password
-- is only set when its variable is given, so a re-run without them keeps the passwords).

\set ON_ERROR_STOP on
\if :{?app_role}
\else
  \set app_role aureon_app
\endif
\if :{?migrator_role}
\else
  \set migrator_role aureon_migrator
\endif
\if :{?readonly_role}
\else
  \set readonly_role aureon_readonly
\endif
\if :{?audit_writer_role}
\else
  \set audit_writer_role aureon_audit_writer
\endif

-- Role names travel into the DO blocks below as session settings (psql variables are not
-- expanded inside dollar-quoted bodies).
SELECT set_config('aureon.app_role', :'app_role', false),
       set_config('aureon.migrator_role', :'migrator_role', false),
       set_config('aureon.readonly_role', :'readonly_role', false),
       set_config('aureon.audit_writer_role', :'audit_writer_role', false)
\gset _cfg_

BEGIN;

-- ---------------------------------------------------------------------------------------
-- 0. Sanity: plain lower-case identifiers, four distinct names, run by a superuser.
-- ---------------------------------------------------------------------------------------
DO $$
DECLARE
  names text[] := ARRAY[current_setting('aureon.app_role'),
                        current_setting('aureon.migrator_role'),
                        current_setting('aureon.readonly_role'),
                        current_setting('aureon.audit_writer_role')];
  n text;
BEGIN
  FOREACH n IN ARRAY names LOOP
    IF n !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
      RAISE EXCEPTION 'invalid role name: %', n;
    END IF;
  END LOOP;
  IF (SELECT count(DISTINCT x) FROM unnest(names) x) <> 4 THEN
    RAISE EXCEPTION 'role names must be distinct';
  END IF;
  IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
    RAISE EXCEPTION 'run roles.sql as a superuser (it creates roles and changes owners)';
  END IF;
END
$$;

-- ---------------------------------------------------------------------------------------
-- 1. Schemas. `spasta` is created EMPTY: it is reserved for future Spasta-owned app tables
--    (decision D-22-2). The other app schemas are created by the migrations; they are listed
--    here so ownership and grants cover them once they exist.
-- ---------------------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS spasta;
COMMENT ON SCHEMA spasta IS
  'Reserved for future Spasta-owned app tables (Aureon Phase 22, D-22-2). Empty by design.';

-- ---------------------------------------------------------------------------------------
-- 2. Roles. Created when missing; attributes always reset to the least privilege.
--    BYPASSRLS on the app/readonly roles keeps decision D-A7: the legacy Supabase RLS
--    policies on public.trades etc. stay, and Aureon enforces ownership in the service layer.
-- ---------------------------------------------------------------------------------------
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT * FROM (VALUES
      (current_setting('aureon.app_role'), 'LOGIN', 'BYPASSRLS'),
      (current_setting('aureon.migrator_role'), 'LOGIN', 'NOBYPASSRLS'),
      (current_setting('aureon.readonly_role'), 'LOGIN', 'BYPASSRLS'),
      (current_setting('aureon.audit_writer_role'), 'NOLOGIN', 'NOBYPASSRLS')
    ) AS v(name, login, rls)
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r.name) THEN
      EXECUTE format('CREATE ROLE %I %s', r.name, r.login);
    END IF;
    EXECUTE format(
      'ALTER ROLE %I %s %s NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT',
      r.name, r.login, r.rls);
  END LOOP;
  -- The app inherits the audit writer's grants.
  EXECUTE format('ALTER ROLE %I INHERIT', current_setting('aureon.app_role'));
END
$$;

\if :{?app_password}
ALTER ROLE :"app_role" WITH PASSWORD :'app_password';
\endif
\if :{?migrator_password}
ALTER ROLE :"migrator_role" WITH PASSWORD :'migrator_password';
\endif
\if :{?readonly_password}
ALTER ROLE :"readonly_role" WITH PASSWORD :'readonly_password';
\endif

GRANT :"audit_writer_role" TO :"app_role";

-- ---------------------------------------------------------------------------------------
-- 3. Database level: everyone connects; only the migrator may CREATE (new schemas).
--    CREATE on schema public is taken from PUBLIC (the Postgres 15+ default, made explicit).
-- ---------------------------------------------------------------------------------------
DO $$
DECLARE
  db text := current_database();
BEGIN
  EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I',
                 db, current_setting('aureon.app_role'));
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', db, current_setting('aureon.readonly_role'));
  EXECUTE format('GRANT CONNECT, CREATE, TEMPORARY ON DATABASE %I TO %I',
                 db, current_setting('aureon.migrator_role'));
  EXECUTE format('REVOKE CREATE ON DATABASE %I FROM %I', db, current_setting('aureon.app_role'));
  EXECUTE format('REVOKE CREATE ON DATABASE %I FROM %I',
                 db, current_setting('aureon.readonly_role'));
END
$$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- ---------------------------------------------------------------------------------------
-- 4. App schemas: owned by the migrator, every table/view/sequence in them too.
--    Grants on what exists now, and default privileges for what the migrator creates later.
-- ---------------------------------------------------------------------------------------
DO $$
DECLARE
  app text := current_setting('aureon.app_role');
  mig text := current_setting('aureon.migrator_role');
  ro text := current_setting('aureon.readonly_role');
  s text;
  r record;
  -- `audit` is handled separately below (append-only by default).
  dml_schemas text[] := ARRAY['core', 'ai', 'agents', 'trading', 'dhan', 'mt5', 'github', 'devops',
                              'spasta'];
BEGIN
  FOREACH s IN ARRAY dml_schemas || ARRAY['audit'] LOOP
    CONTINUE WHEN NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = s);
    EXECUTE format('ALTER SCHEMA %I OWNER TO %I', s, mig);
    -- Tables, partitioned tables, views, materialized views, foreign tables (their owned
    -- sequences and indexes follow the table).
    FOR r IN
      SELECT c.relname, c.relkind FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = s AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
    LOOP
      EXECUTE format('ALTER %s %I.%I OWNER TO %I',
                     CASE r.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                                    WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END,
                     s, r.relname, mig);
    END LOOP;
    -- Stand-alone sequences (not owned by a column).
    FOR r IN
      SELECT c.relname FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = s AND c.relkind = 'S'
        AND NOT EXISTS (SELECT 1 FROM pg_depend d
                        WHERE d.objid = c.oid AND d.deptype IN ('a', 'i'))
    LOOP
      EXECUTE format('ALTER SEQUENCE %I.%I OWNER TO %I', s, r.relname, mig);
    END LOOP;

    EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM %I', s, app);
    EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM %I', s, ro);
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I, %I', s, app, ro);
    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO %I', s, ro);
    EXECUTE format('GRANT SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I', s, ro);
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT ON TABLES TO %I',
                   mig, s, ro);
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT ON SEQUENCES TO %I',
      mig, s, ro);
    EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I', s, app);
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT USAGE, SELECT ON SEQUENCES TO %I',
      mig, s, app);
    -- Never TRUNCATE / REFERENCES / TRIGGER for the app, anywhere.
    EXECUTE format('REVOKE TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA %I FROM %I',
                   s, app);
  END LOOP;

  FOREACH s IN ARRAY dml_schemas LOOP
    CONTINUE WHEN NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = s);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO %I',
                   s, app);
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
      'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', mig, s, app);
  END LOOP;
END
$$;

-- ---------------------------------------------------------------------------------------
-- 5. The audit schema is APPEND-ONLY by default: a table the migrator adds there later gets
--    SELECT + INSERT only (fail closed). The tables the approval engine must update are
--    listed explicitly. tests/test_db_roles.py checks every audit table is in one list.
--
--    AUDIT_APPEND_ONLY: audit_events approval_events approval_artifacts
--    AUDIT_MUTABLE:     approval_requests approval_policies
-- ---------------------------------------------------------------------------------------
DO $$
DECLARE
  app text := current_setting('aureon.app_role');
  mig text := current_setting('aureon.migrator_role');
  aw text := current_setting('aureon.audit_writer_role');
  t text;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'audit') THEN
    RETURN;
  END IF;
  EXECUTE format('GRANT USAGE ON SCHEMA audit TO %I', aw);
  EXECUTE format('GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA audit TO %I', app);
  EXECUTE format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA audit GRANT SELECT, INSERT ON TABLES TO %I',
    mig, app);
  FOREACH t IN ARRAY ARRAY['approval_requests', 'approval_policies'] LOOP
    IF to_regclass(format('audit.%I', t)) IS NOT NULL THEN
      EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON audit.%I TO %I', t, app);
    END IF;
  END LOOP;
  FOREACH t IN ARRAY ARRAY['audit_events', 'approval_events', 'approval_artifacts'] LOOP
    IF to_regclass(format('audit.%I', t)) IS NOT NULL THEN
      -- Explicit REVOKE, from the app, the writer group and PUBLIC.
      EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON audit.%I FROM %I, %I, PUBLIC',
                     t, app, aw);
      EXECUTE format('GRANT SELECT, INSERT ON audit.%I TO %I', t, aw);
    END IF;
  END LOOP;
  EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA audit TO %I', aw);
END
$$;

-- ---------------------------------------------------------------------------------------
-- 6. public: the legacy Spasta finance tables (owned by the legacy app; ownership is NOT
--    changed) and this app's version table (owned by the migrator).
-- ---------------------------------------------------------------------------------------
DO $$
DECLARE
  app text := current_setting('aureon.app_role');
  mig text := current_setting('aureon.migrator_role');
  ro text := current_setting('aureon.readonly_role');
  t text;
BEGIN
  EXECUTE format('GRANT USAGE ON SCHEMA public TO %I, %I, %I', app, ro, mig);
  -- The migrator creates aureon_alembic_version on a fresh database.
  EXECUTE format('GRANT CREATE ON SCHEMA public TO %I', mig);
  FOREACH t IN ARRAY ARRAY['trades', 'expenses', 'secured', 'astra_documents',
                           'portfolio_profiles'] LOOP
    IF to_regclass(format('public.%I', t)) IS NOT NULL THEN
      EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I TO %I', t, app);
      EXECUTE format('REVOKE TRUNCATE, REFERENCES, TRIGGER ON public.%I FROM %I', t, app);
      EXECUTE format('GRANT SELECT ON public.%I TO %I', t, ro);
      -- Migrations only ADD columns/indexes here (0010); they need the legacy owner's rights
      -- for that, so a future migration touching these tables runs as the owner (manual).
    END IF;
  END LOOP;
  IF to_regclass('public.aureon_alembic_version') IS NOT NULL THEN
    EXECUTE format('ALTER TABLE public.aureon_alembic_version OWNER TO %I', mig);
    EXECUTE format('GRANT SELECT ON public.aureon_alembic_version TO %I, %I', app, ro);
  END IF;
END
$$;

COMMIT;

\echo 'roles.sql: done (nothing dropped).'
