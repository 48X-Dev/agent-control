-- Provision the agent executor's database and its dedicated Postgres role.
--
-- Run this against the "postgres" maintenance database, as a superuser, on the
-- same Postgres instance that holds the control plane:
--
--   psql -v ON_ERROR_STOP=1 -h <host> -U <superuser> -d postgres \
--        -v adk_password='<password>' -f adk_db_init.sql
--
-- Optional overrides: -v adk_role=adk -v adk_db=adk_runtime -v control_db=agent_control
--
-- It closes one control-plane database per run. Where an instance holds more
-- than one - a local agent_control_test alongside agent_control, say - run it
-- again with a different -v control_db. A control database that does not exist
-- is reported and skipped, so the extra run is safe to wire in unconditionally,
-- which is what docker-compose.dev.yml does.
--
-- The one-shot `adk-db-init` service in docker-compose.dev.yml runs exactly
-- this file. It is idempotent, so re-running it on every `docker compose up` is
-- the intended usage.
--
-- WHY THIS IS NOT AN INIT SCRIPT AND NOT AN ALEMBIC MIGRATION
--
-- docker-compose.yml mounts a named `pgdata` volume, and the Postgres image
-- runs /docker-entrypoint-initdb.d only against an empty data directory. Every
-- developer and deployment that already has a volume would silently never get
-- the database. Alembic is also wrong: it owns the schema *inside*
-- agent_control, CREATE DATABASE cannot run in its transactional context, and
-- a downgrade would have to DROP DATABASE and take every agent conversation
-- with it. This is deployment provisioning, not schema.
--
-- WHY A SEPARATE DATABASE
--
-- server/tests/conftest.py truncates every table returned by
-- inspect(conn).get_table_names("public") between tests, so executor session
-- tables living in agent_control would be wiped mid-suite, and Alembic
-- autogenerate would try to drop them.
--
-- WHY A SEPARATE ROLE
--
-- POSTGRES_USER is `agent_control`, which owns the control-plane database. An
-- executor connecting as that role could read and rewrite `controls`,
-- `policies` and `control_bindings` - that is, an agent editing the guardrails
-- that govern it, reachable through ordinary prompt injection into a tool
-- result. The role created here owns adk_runtime and can reach nothing else.
--
-- Note on the password: it is passed as a psql variable and interpolated with
-- %L, so it is quoted correctly, and \gexec does not echo the generated
-- statement. It will still appear in the server log if log_statement=all.

\set ON_ERROR_STOP on

\if :{?adk_role}
\else
  \set adk_role adk
\endif

\if :{?adk_db}
\else
  \set adk_db adk_runtime
\endif

\if :{?control_db}
\else
  \set control_db agent_control
\endif

\if :{?adk_password}
\else
  \set adk_password ''
\endif

-- Carried as GUCs because psql does not interpolate its variables inside the
-- dollar-quoted bodies of the DO blocks below.
SELECT set_config('adk_init.adk_role', :'adk_role', false) AS _r,
       set_config('adk_init.control_db', :'control_db', false) AS _c,
       set_config('adk_init.password', :'adk_password', false) AS _p
\gset

DO $$
BEGIN
    IF current_setting('adk_init.password') = '' THEN
        RAISE EXCEPTION
            'adk_password is required: psql -v adk_password=''...'' -f adk_db_init.sql';
    END IF;
END
$$;


-- 1. The role the executor connects as ---------------------------------------
--
-- Created without login first, then given its attributes in one ALTER, so that
-- an existing role provisioned by hand converges on the same attribute floor
-- instead of being left however it was made. NOINHERIT means that if this role
-- is ever granted into a group with control-plane access, it does not silently
-- acquire it.

SELECT format('CREATE ROLE %I NOLOGIN', :'adk_role')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'adk_role')
\gexec

SELECT format(
           'ALTER ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE '
           'NOREPLICATION NOBYPASSRLS NOINHERIT',
           :'adk_role', :'adk_password'
       )
\gexec


-- 2. The executor's own database ---------------------------------------------
--
-- Owned by the executor role, so it can create its session tables without any
-- further grant. On Postgres 15+ the `public` schema of a database is owned by
-- pg_database_owner, so ALTER DATABASE ... OWNER TO also repairs a database
-- someone created by hand with `createdb adk_runtime` as the wrong role.

SELECT format('CREATE DATABASE %I OWNER %I', :'adk_db', :'adk_role')
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'adk_db')
\gexec

SELECT format('ALTER DATABASE %I OWNER TO %I', :'adk_db', :'adk_role')
  FROM pg_database d
  JOIN pg_roles r ON r.oid = d.datdba
 WHERE d.datname = :'adk_db' AND r.rolname <> :'adk_role'
\gexec

-- Agent transcripts are not public reading either. The owner keeps implicit
-- access, so this only removes the default PUBLIC grant.
SELECT format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', :'adk_db')
\gexec


-- 3. Close the control-plane database to the executor role -------------------
--
-- REVOKE CONNECT ... FROM adk on its own does nothing. Postgres grants are
-- additive and a REVOKE of a privilege that was never granted directly to the
-- role is a no-op; it does not create a negative grant. CONNECT is granted to
-- PUBLIC by default on every database, and PUBLIC is what the executor role
-- would be using. Verified empirically against this repo's Postgres: after
-- CREATE ROLE plus REVOKE CONNECT FROM that role, the role still connected to
-- agent_control and listed every control-plane table.
--
-- So the PUBLIC grant is the one that has to go. Because that grant is what
-- every other non-superuser login role is also relying on today, each of them
-- is given an explicit CONNECT first, which preserves the status quo exactly
-- on an existing install rather than locking anyone out. Roles created *after*
-- this runs are a different matter: they will not inherit CONNECT from PUBLIC
-- any more, so a new non-superuser application role needs an explicit
-- GRANT CONNECT ON DATABASE <control_db> TO <role>.

SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'control_db') AS control_db_exists \gset

\if :control_db_exists

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'control_db', rolname)
  FROM pg_roles
 WHERE rolcanlogin
   AND NOT rolsuper
   AND rolname <> :'adk_role'
   AND has_database_privilege(rolname, :'control_db', 'CONNECT')
\gexec

-- CONNECT only, not ALL: PUBLIC also holds TEMP by default and nothing about
-- this threat requires taking that away from every other role on the instance.
SELECT format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', :'control_db')
\gexec

SELECT format('REVOKE ALL ON DATABASE %I FROM %I', :'control_db', :'adk_role')
\gexec

-- Fail loudly rather than reporting success on a half-applied lockdown.
DO $$
BEGIN
    IF has_database_privilege(
           current_setting('adk_init.adk_role'),
           current_setting('adk_init.control_db'),
           'CONNECT'
       ) THEN
        RAISE EXCEPTION
            'role % can still CONNECT to %; the control-plane database is not isolated',
            current_setting('adk_init.adk_role'), current_setting('adk_init.control_db');
    END IF;
END
$$;

\else
\echo 'NOTE: control database not found on this instance; skipped the REVOKE step.'
\endif


-- 4. Report ------------------------------------------------------------------

SELECT :'adk_role' AS role,
       has_database_privilege(:'adk_role', :'adk_db', 'CONNECT') AS can_connect_adk_runtime,
       CASE WHEN :'control_db_exists'::boolean
            THEN has_database_privilege(:'adk_role', :'control_db', 'CONNECT')
       END AS can_connect_control_plane;
