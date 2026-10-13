-- ═══════════════════════════════════════════════════════════════
-- openSME — PostgreSQL Database Initialization
-- ═══════════════════════════════════════════════════════════════
-- Auto-executed on first container start (mounted to /docker-entrypoint-initdb.d/).
-- Creates additional databases required by services.
--
-- The main opensme database is created by POSTGRES_DB in docker-compose.yml.
-- Per-service users with passwords are created by 01-create-users.sh.
-- ═══════════════════════════════════════════════════════════════

-- ── Zitadel (IAM/SSO) ──
CREATE DATABASE zitadel;

-- ── SOGo (groupware) ──
CREATE DATABASE sogo;

-- ── Optional databases (only used if the corresponding service is enabled) ──
-- These are created unconditionally; harmless if unused.

-- ── Casdoor (lightweight IAM alternative) ──
CREATE DATABASE casdoor_db;

-- ── Synapse (Matrix chat, --profile chat) ──
CREATE DATABASE synapse_db;

-- ── Paperless-ngx (document management, --profile paperless) ──
CREATE DATABASE paperless_db;

-- ── Invoice Ninja (invoicing, --profile invoice) ──
CREATE DATABASE invoiceninja_db;

-- ── LimeSurvey (surveys, --profile surveys) ──
CREATE DATABASE limesurvey_db;

-- ── Notes/Impress (collaborative notes, --profile notes) ──
CREATE DATABASE notes_db;

-- ── Nosdesk (ticketing/helpdesk, --profile ticketing) ──
CREATE DATABASE nosdesk_db;
