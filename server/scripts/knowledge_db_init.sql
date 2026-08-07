-- Provision the company-knowledge corpus database and its two Postgres roles.
--
-- Run this against the "postgres" maintenance database, as a superuser, on the
-- same Postgres instance that holds the control plane:
--
--   psql -v ON_ERROR_STOP=1 -h <host> -U <superuser> -d postgres \
--        -v knowledge_sync_password='<password>' \
--        -v knowledge_read_password='<password>' \
--        -f knowledge_db_init.sql
--
-- Optional overrides: -v knowledge_sync_role=knowledge_sync
--                     -v knowledge_read_role=knowledge_read
--                     -v knowledge_db=agent_knowledge
--                     -v control_db=agent_control
--
-- It closes one control-plane database per run. Where an instance holds more
-- than one - a local agent_control_test alongside agent_control, say - run it
-- again with a different -v control_db. A control database that does not exist
-- is reported and skipped, so the extra run is safe to wire in unconditionally,
-- which is what docker-compose.dev.yml does.
--
-- The one-shot `knowledge-db-init` service in docker-compose.dev.yml runs
-- exactly this file, right after `adk-db-init`. It is idempotent, so re-running
-- it on every `docker compose up` is the intended usage.
--
-- WHY THIS IS NOT AN INIT SCRIPT AND NOT AN ALEMBIC MIGRATION
--
-- The same three reasons adk_db_init.sql gives, unchanged. docker-compose.yml
-- mounts a named `pgdata` volume and the Postgres image runs
-- /docker-entrypoint-initdb.d only against an empty data directory, so every
-- existing volume would silently never get the database. CREATE DATABASE and
-- CREATE ROLE cannot run in Alembic's transactional context, and pg_dump does
-- not carry database-level privileges, so a restored dump arrives without the
-- roles exactly as a fresh volume does. This is deployment provisioning, not
-- schema. The schema inside agent_knowledge is migrations' business
-- (server/knowledge_alembic), and this script never creates a table.
--
-- WHY A SEPARATE DATABASE
--
-- server/tests/conftest.py truncates every table returned by
-- inspect(conn).get_table_names("public") between tests, so a corpus living in
-- agent_control would be wiped mid-suite. Alembic owns the control plane's
-- schema and would try to drop a corpus it did not declare.
--
-- WHY TWO SEPARATE ROLES
--
-- The sync process parses hostile bytes (any document in the mirror can be
-- crafted by whoever can write to a shared folder) and it owns the corpus, so
-- it needs write. The control plane only ever reads, so it gets a role that
-- holds SELECT and nothing else: a prompt-injected retrieval path cannot
-- rewrite the snippets a later turn will read back. Neither role can reach
-- `agent_control`, so neither can touch `controls`, `policies` or
-- `control_bindings` - the guardrails that govern the agents doing the asking.
--
-- WHY THE FINAL BLOCK ASSERTS A *POSITIVE* AS WELL AS THE REVOKES
--
-- The tables arrive later, created by migrations running as the sync role, and
-- a table's creator grants nothing to anyone implicitly. Without the reader's
-- GRANT, `knowledge_read` connects and sees nothing, every search refuses
-- `knowledge_unavailable` forever, and the failure reads as an empty corpus
-- rather than as a missing privilege. A check that only proved the negative
-- would pass in exactly that broken state, so section 5 below connects to
-- agent_knowledge and asserts the reader really can SELECT once `schema_meta`
-- exists.
--
-- Note on the passwords: they are passed as psql variables and interpolated
-- with %L, so they are quoted correctly, and \gexec does not echo the generated
-- statement. They will still appear in the server log if log_statement=all.

\set ON_ERROR_STOP on

\if :{?knowledge_sync_role}
\else
  \set knowledge_sync_role knowledge_sync
\endif

\if :{?knowledge_read_role}
\else
  \set knowledge_read_role knowledge_read
\endif

\if :{?knowledge_db}
\else
  \set knowledge_db agent_knowledge
\endif

\if :{?control_db}
\else
  \set control_db agent_control
\endif

\if :{?knowledge_sync_password}
\else
  \set knowledge_sync_password ''
\endif

\if :{?knowledge_read_password}
\else
  \set knowledge_read_password ''
\endif

-- Carried as GUCs because psql does not interpolate its variables inside the
-- dollar-quoted bodies of the DO blocks below.
SELECT set_config('knowledge_init.sync_role', :'knowledge_sync_role', false) AS _s,
       set_config('knowledge_init.read_role', :'knowledge_read_role', false) AS _r,
       set_config('knowledge_init.knowledge_db', :'knowledge_db', false) AS _k,
       set_config('knowledge_init.control_db', :'control_db', false) AS _c,
       set_config('knowledge_init.sync_password', :'knowledge_sync_password', false) AS _sp,
       set_config('knowledge_init.read_password', :'knowledge_read_password', false) AS _rp
\gset

DO $$
BEGIN
    IF current_setting('knowledge_init.sync_password') = '' THEN
        RAISE EXCEPTION
            'knowledge_sync_password is required: psql -v knowledge_sync_password=''...'' -f knowledge_db_init.sql';
    END IF;
    IF current_setting('knowledge_init.read_password') = '' THEN
        RAISE EXCEPTION
            'knowledge_read_password is required: psql -v knowledge_read_password=''...'' -f knowledge_db_init.sql';
    END IF;
END
$$;


-- 1. The two roles -----------------------------------------------------------
--
-- Created without login first, then given their attributes in one ALTER, so
-- that a role provisioned by hand converges on the same attribute floor
-- instead of being left however it was made. NOINHERIT means that if either is
-- ever granted into a group with control-plane access, it does not silently
-- acquire it.

SELECT format('CREATE ROLE %I NOLOGIN', :'knowledge_sync_role')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'knowledge_sync_role')
\gexec

SELECT format('CREATE ROLE %I NOLOGIN', :'knowledge_read_role')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'knowledge_read_role')
\gexec

SELECT format(
           'ALTER ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE '
           'NOREPLICATION NOBYPASSRLS NOINHERIT',
           :'knowledge_sync_role', :'knowledge_sync_password'
       )
\gexec

SELECT format(
           'ALTER ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE '
           'NOREPLICATION NOBYPASSRLS NOINHERIT',
           :'knowledge_read_role', :'knowledge_read_password'
       )
\gexec


-- 2. The corpus database -----------------------------------------------------
--
-- Owned by the sync role, so migrations run as that role can create tables
-- without any further grant. On Postgres 15+ the `public` schema of a database
-- is owned by pg_database_owner, so ALTER DATABASE ... OWNER TO also repairs a
-- database someone created by hand with `createdb agent_knowledge` as the
-- wrong role.

SELECT format('CREATE DATABASE %I OWNER %I', :'knowledge_db', :'knowledge_sync_role')
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'knowledge_db')
\gexec

SELECT format('ALTER DATABASE %I OWNER TO %I', :'knowledge_db', :'knowledge_sync_role')
  FROM pg_database d
  JOIN pg_roles r ON r.oid = d.datdba
 WHERE d.datname = :'knowledge_db' AND r.rolname <> :'knowledge_sync_role'
\gexec

-- The company's documents are not public reading. The owner keeps implicit
-- access, so this only removes the default PUBLIC grant; the reader then needs
-- its CONNECT back explicitly, which is the next statement.
SELECT format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', :'knowledge_db')
\gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'knowledge_db', :'knowledge_read_role')
\gexec


-- 3. Close the control-plane database to both knowledge roles ----------------
--
-- REVOKE CONNECT ... FROM <role> on its own does nothing. Postgres grants are
-- additive and a REVOKE of a privilege that was never granted directly to the
-- role is a no-op; it does not create a negative grant. CONNECT is granted to
-- PUBLIC by default on every database, and PUBLIC is what these roles would be
-- using. adk_db_init.sql proved that empirically against this repo's Postgres
-- and the same holds here, which is why this script repeats the work rather
-- than assuming the ADK script ran first: an instance provisioned for
-- knowledge alone must still be isolated.
--
-- Because the PUBLIC grant is what every other non-superuser login role is
-- relying on today, each of them is given an explicit CONNECT first, which
-- preserves the status quo exactly on an existing install rather than locking
-- anyone out. Roles created *after* this runs need an explicit
-- GRANT CONNECT ON DATABASE <control_db> TO <role>.

SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'control_db') AS control_db_exists \gset

\if :control_db_exists

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'control_db', rolname)
  FROM pg_roles
 WHERE rolcanlogin
   AND NOT rolsuper
   AND rolname <> :'knowledge_sync_role'
   AND rolname <> :'knowledge_read_role'
   AND has_database_privilege(rolname, :'control_db', 'CONNECT')
\gexec

-- CONNECT only, not ALL: PUBLIC also holds TEMP by default and nothing about
-- this threat requires taking that away from every other role on the instance.
SELECT format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', :'control_db')
\gexec

SELECT format('REVOKE ALL ON DATABASE %I FROM %I', :'control_db', :'knowledge_sync_role')
\gexec

SELECT format('REVOKE ALL ON DATABASE %I FROM %I', :'control_db', :'knowledge_read_role')
\gexec

-- Fail loudly rather than reporting success on a half-applied lockdown.
DO $$
DECLARE
    offender text;
BEGIN
    FOREACH offender IN ARRAY ARRAY[
        current_setting('knowledge_init.sync_role'),
        current_setting('knowledge_init.read_role')
    ] LOOP
        IF has_database_privilege(
               offender,
               current_setting('knowledge_init.control_db'),
               'CONNECT'
           ) THEN
            RAISE EXCEPTION
                'role % can still CONNECT to %; the control-plane database is not isolated',
                offender, current_setting('knowledge_init.control_db');
        END IF;
    END LOOP;
END
$$;

\else
\echo 'NOTE: control database not found on this instance; skipped the REVOKE step.'
\endif


-- 4. The reader's floor, inside the corpus database --------------------------
--
-- Connecting is not reading. Everything below has to happen inside
-- agent_knowledge, so this reconnects; psql carries the same host, port and
-- user across \connect, and the GUCs above are session-local, so they are set
-- again on the other side.
--
-- The default privileges are recorded here as well as in migration 001. Here
-- they are declared FOR ROLE <sync>, which is what makes them apply to tables
-- that role creates later even when the migration ran before this script did -
-- the order the two are applied in is not something a deployment should have
-- to get right.

\connect :"knowledge_db"

SELECT set_config('knowledge_init.sync_role', :'knowledge_sync_role', false) AS _s,
       set_config('knowledge_init.read_role', :'knowledge_read_role', false) AS _r,
       set_config('knowledge_init.knowledge_db', :'knowledge_db', false) AS _k
\gset

SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'knowledge_read_role')
\gexec

SELECT format(
           'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT ON TABLES TO %I',
           :'knowledge_sync_role', :'knowledge_read_role'
       )
\gexec

-- Catch-up for tables that already exist, because ALTER DEFAULT PRIVILEGES is
-- forward-looking only.
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA public TO %I', :'knowledge_read_role')
\gexec


-- 5. Assert the reader can actually read -------------------------------------
--
-- `schema_meta` is migration 001's marker table and the one row the server
-- reads at startup to decide whether it understands this corpus. Its presence
-- means migrations have run; its readability is the privilege that a search
-- path silently depends on.

DO $$
DECLARE
    marker text := 'public.schema_meta';
BEGIN
    IF to_regclass(marker) IS NULL THEN
        RAISE NOTICE
            'schema_meta does not exist yet in %; migrations have not run, so the '
            'reader''s SELECT is asserted on the next provisioning run instead',
            current_setting('knowledge_init.knowledge_db');
        RETURN;
    END IF;
    IF NOT has_table_privilege(current_setting('knowledge_init.read_role'), marker, 'SELECT') THEN
        RAISE EXCEPTION
            'role % cannot SELECT from %; every knowledge search would refuse as if the '
            'corpus were empty. Re-run this script after the migrations.',
            current_setting('knowledge_init.read_role'), marker;
    END IF;
END
$$;


-- 6. Report ------------------------------------------------------------------

SELECT :'knowledge_sync_role' AS sync_role,
       :'knowledge_read_role' AS read_role,
       has_database_privilege(:'knowledge_sync_role', :'knowledge_db', 'CONNECT') AS sync_can_connect_corpus,
       has_database_privilege(:'knowledge_read_role', :'knowledge_db', 'CONNECT') AS read_can_connect_corpus,
       CASE WHEN to_regclass('public.schema_meta') IS NOT NULL
            THEN has_table_privilege(:'knowledge_read_role', 'public.schema_meta', 'SELECT')
       END AS read_can_select_schema_meta;
