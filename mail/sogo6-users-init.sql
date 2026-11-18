-- openSME — SOGo6 webmail SQL user source (single relational DB, no LDAP)
--
-- The SOGo6-server authenticates webmail logins against this small `users`
-- table living in the SAME relational database the server already uses
-- (opensme-sogo6-postgres, db `sogo`; empty US_SQL_USER_URL falls back to
-- the application DB). Demo rows are written at provisioning time by
-- openSME-compose `scripts/provision-demo-users.sh --component sogo6`
-- (pbkdf2-sha256 hashes; identities <user>@<DOMAIN> = uid = mail).
--
-- Mounted into the sogo6-postgres container as an initdb script
-- (/docker-entrypoint-initdb.d/*.sql), i.e. it runs on a fresh data volume.
-- On an existing volume apply it manually:
--   docker exec opensme-sogo6-postgres psql -U sogo -d sogo -f \
--       /docker-entrypoint-initdb.d/10-users.sql

CREATE TABLE IF NOT EXISTS users (
    uid         VARCHAR(255) PRIMARY KEY,          -- login / IdP email claim must be unique
    cn          VARCHAR(255) NOT NULL,             -- display name
    mail        VARCHAR(255) NOT NULL,             -- mailbox / routing address
    domain      VARCHAR(255) NOT NULL DEFAULT '',  -- US_SQL_DOMAIN_FIELD
    password    VARCHAR(255) NOT NULL,             -- {bcrypt}... or {pbkdf2-sha256}$iter$salt$hash
    enabled     BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS users_domain_idx ON users (domain);
