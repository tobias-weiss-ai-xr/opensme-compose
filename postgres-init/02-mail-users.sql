-- ═══════════════════════════════════════════════════════════════
-- openSME — SOGo mail user source (database sogo, table mail_users)
-- ═══════════════════════════════════════════════════════════════
-- Auto-executed on first container start (mounted to /docker-entrypoint-initdb.d/).
-- Runs after 00-create-databases.sql (creates the sogo database) and
-- 01-create-users.sh (creates sogo_user, which owns that database).
--
-- WHY A SEPARATE TABLE: SOGo authenticates webmail logins against its OWN
-- store, Stalwart authenticates IMAP/SMTP against ITS store. This table is
-- the half SOGo reads (sogo.conf.template → SOGoUserSources → viewURL
-- postgresql://…/sogo/mail_users); Stalwart's half is provisioned through its
-- admin API. Both halves are written in one call by
--   scripts/provision-demo-users.sh --component stalwart
-- so the two stores never drift. No LDAP: this stack serves <= 500 users and
-- a directory daemon would be one more moving part for one table.
--
-- COLUMN CONTRACT — names are fixed, sogo.conf.template maps them directly
-- (the LDAP keys CNFieldName/UIDFieldName do not apply to SQL sources):
--   c_uid       login name, == IMAPLoginFieldName
--   c_name      login name again (SQL sources read c_name as the account name)
--   c_cn        display name (certificate/common-name style, shown in the UI)
--   c_password  {SCHEME}hash — Dovecot scheme name first, e.g.
--               {SHA512-CRYPT}$6$salt$hash. prependPasswordScheme = YES, so a
--               bare hash is read as sha512-crypt.
--   mail        primary address, matched against MailFieldNames = (mail)
--
-- The table is created EMPTY on purpose: passwords — even demo ones — do not
-- belong in version control, and a committed hash would silently become the
-- stack's credential. Provision users with the script above (a fresh demo
-- stack answers the webmail login page either way, but no login succeeds
-- until then — tests/05-e2e/webmail.py skips its login leg in that state).
\connect sogo

CREATE TABLE IF NOT EXISTS mail_users (
    c_uid      text NOT NULL,
    c_name     text,
    c_cn       text,
    c_password text NOT NULL,
    mail       text NOT NULL,
    CONSTRAINT mail_users_pkey PRIMARY KEY (c_uid)
);

-- SOGo owns the sogo database (01-create-users.sh) and needs SELECT here;
-- the file above runs as the superuser, so hand the table over explicitly.
ALTER TABLE mail_users OWNER TO sogo_user;
GRANT SELECT ON mail_users TO sogo_user;
