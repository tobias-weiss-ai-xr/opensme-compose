#!/bin/bash
set -e

# ═══════════════════════════════════════════════════════════════
# openSME — PostgreSQL User Creation
# ═══════════════════════════════════════════════════════════════
# Creates per-service database users with passwords.
# Runs after 00-create-databases.sql.
#
# Databases (created by 00-create-databases.sql):
#   zitadel, sogo, casdoor_db, synapse_db, paperless_db,
#   invoiceninja_db, notes_db, limesurvey_db
# ═══════════════════════════════════════════════════════════════

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-EOSQL
    -- ── Casdoor (lightweight IAM alternative) ──
    DO \$\$
    BEGIN
      CREATE USER casdoor_user WITH PASSWORD '${CASDOOR_DB_PASSWORD:-changeme}';
    EXCEPTION WHEN duplicate_object THEN
      ALTER USER casdoor_user WITH PASSWORD '${CASDOOR_DB_PASSWORD:-changeme}';
    END \$\$;
    GRANT ALL PRIVILEGES ON DATABASE casdoor_db TO casdoor_user;
    ALTER DATABASE casdoor_db OWNER TO casdoor_user;

    -- ── Synapse (Matrix chat) ──
    DO \$\$
    BEGIN
      CREATE USER synapse_user WITH PASSWORD '${SYNAPSE_DB_PASSWORD:-changeme}';
    EXCEPTION WHEN duplicate_object THEN
      ALTER USER synapse_user WITH PASSWORD '${SYNAPSE_DB_PASSWORD:-changeme}';
    END \$\$;
    GRANT ALL PRIVILEGES ON DATABASE synapse_db TO synapse_user;
    ALTER DATABASE synapse_db OWNER TO synapse_user;

    -- ── Paperless-ngx (document management) ──
    DO \$\$
    BEGIN
      CREATE USER paperless_user WITH PASSWORD '${PAPERLESS_DB_PASSWORD:-changeme}';
    EXCEPTION WHEN duplicate_object THEN
      ALTER USER paperless_user WITH PASSWORD '${PAPERLESS_DB_PASSWORD:-changeme}';
    END \$\$;
    GRANT ALL PRIVILEGES ON DATABASE paperless_db TO paperless_user;
    ALTER DATABASE paperless_db OWNER TO paperless_user;

    -- ── Invoice Ninja (invoicing) ──
    DO \$\$
    BEGIN
      CREATE USER invoiceninja_user WITH PASSWORD '${INVOICENINJA_DB_PASSWORD:-changeme}';
    EXCEPTION WHEN duplicate_object THEN
      ALTER USER invoiceninja_user WITH PASSWORD '${INVOICENINJA_DB_PASSWORD:-changeme}';
    END \$\$;
    GRANT ALL PRIVILEGES ON DATABASE invoiceninja_db TO invoiceninja_user;
    ALTER DATABASE invoiceninja_db OWNER TO invoiceninja_user;

    -- ── Notes/Impress (collaborative notes) ──
    DO \$\$
    BEGIN
      CREATE USER notes_user WITH PASSWORD '${NOTES_DB_PASSWORD:-changeme}';
    EXCEPTION WHEN duplicate_object THEN
      ALTER USER notes_user WITH PASSWORD '${NOTES_DB_PASSWORD:-changeme}';
    END \$\$;
    GRANT ALL PRIVILEGES ON DATABASE notes_db TO notes_user;
    ALTER DATABASE notes_db OWNER TO notes_user;

    -- ── LimeSurvey (surveys) — runs its own schema migrations on
    -- first boot, so it needs database ownership (PG15+ public schema)
    DO \$\$
    BEGIN
      CREATE USER limesurvey_user WITH PASSWORD '${SURVEYS_DB_PASSWORD:-changeme}';
    EXCEPTION WHEN duplicate_object THEN
      ALTER USER limesurvey_user WITH PASSWORD '${SURVEYS_DB_PASSWORD:-changeme}';
    END \$\$;
    GRANT ALL PRIVILEGES ON DATABASE limesurvey_db TO limesurvey_user;
    ALTER DATABASE limesurvey_db OWNER TO limesurvey_user;

    -- ── Nosdesk (ticketing/helpdesk) ──
    DO \$\$
    BEGIN
      CREATE USER nosdesk_user WITH PASSWORD '${NOSDESK_DB_PASSWORD:-changeme}';
    EXCEPTION WHEN duplicate_object THEN
      ALTER USER nosdesk_user WITH PASSWORD '${NOSDESK_DB_PASSWORD:-changeme}';
    END \$\$;
    GRANT ALL PRIVILEGES ON DATABASE nosdesk_db TO nosdesk_user;
    -- PG15+: schema public is owned by pg_database_owner — services that
    -- run their own migrations need the database (not just privileges)
    ALTER DATABASE nosdesk_db OWNER TO nosdesk_user;
    -- Nosdesk runtime connects as nosdesk_user and SET ROLEs into the
    -- RLS roles its migrations create/expect (nosdesk_app = tenant-
    -- isolated DML, nosdesk_admin = BYPASSRLS for cross-tenant ops).
    -- Pre-create them here (migrations are role-agnostic/idempotent) so
    -- membership grants exist before the first app boot.
    DO \$\$
    BEGIN
      CREATE ROLE nosdesk_app NOLOGIN;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END \$\$;
    DO \$\$
    BEGIN
      CREATE ROLE nosdesk_admin NOLOGIN;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END \$\$;
    GRANT nosdesk_app TO nosdesk_user;
    GRANT nosdesk_admin TO nosdesk_user;
EOSQL

echo "✅ Per-service database users created (casdoor, synapse, paperless, invoiceninja, notes, nosdesk, limesurvey)"
