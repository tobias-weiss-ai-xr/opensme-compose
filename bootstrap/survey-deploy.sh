#!/usr/bin/env bash
# bootstrap/survey-deploy.sh — provision the example LimeSurvey survey.
#
# Creates (idempotently) the seeded example survey "Beispiel: Datei-Upload"
# with a short-text question and a file-upload question, activates it and
# prints the public URL. Uploaded files are mirrored to OpenCloud by the
# limesurvey-cloud-sync sidecar (services/limesurvey.yml, profile surveys).
#
# Usage:
#   ./bootstrap/survey-deploy.sh                     # via docker exec
#   SURVEYS_URL=https://surveys.example.org ./bootstrap/survey-deploy.sh
#
# Requires the opensme-postgres container (or set PG_CONTAINER/PG_USER).
# Provisioning runs at the database level, mirroring LimeSurvey's own
# activation schema — no admin credentials needed.
set -euo pipefail

TITLE="Beispiel: Datei-Upload (openSME)"

# ── Resolve containers ────────────────────────────
PG_CONTAINER="${PG_CONTAINER:-opensme-postgres}"
PG_USER="${PG_USER:-$(docker exec "$PG_CONTAINER" env | grep '^POSTGRES_USER=' | cut -d= -f2)}"
DB="limesurvey_db"

if [ -z "${PG_USER:-}" ]; then
  echo "error: cannot resolve postgres user from $PG_CONTAINER" >&2
  exit 1
fi

psql() {
  docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$DB" "$@"
}

# ── Idempotency: does the example survey already exist? ──
SID="$(psql -tAc "SELECT surveyls_survey_id FROM lime_surveys_languagesettings WHERE surveyls_title = '$TITLE' LIMIT 1" | tr -d '[:space:]')"
if [ -n "$SID" ]; then
  echo "✓ example survey already exists (sid=$SID) — nothing to do"
else
  # LimeSurvey has no sid sequence (sids are generated app-side) — use
  # max+1 with a 6-digit floor, deterministic and collision-free
  SID="$(psql -tAc "SELECT GREATEST(100000, COALESCE((SELECT max(sid) FROM lime_surveys),99999)+1)" | tr -d '[:space:]')"

  # ── Survey (active, all-in-one, german) + language settings ──
  psql -1 -v ON_ERROR_STOP=1 <<SQL
INSERT INTO lime_surveys (sid, owner_id, admin, adminemail, active, format,
  language, additional_languages, datecreated, lastmodified)
VALUES ($SID, 1, 'openSME Administrator', 'hello@opensme.org', 'Y', 'A',
  'de', '', now(), now());

INSERT INTO lime_surveys_languagesettings (surveyls_survey_id, surveyls_language,
  surveyls_title, surveyls_description, surveyls_welcometext, surveyls_endtext,
  surveyls_dateformat)
VALUES ($SID, 'de', '$TITLE',
  'Demo-Umfrage der openSME-Suite: eine Textfrage und eine Datei-Upload-Frage. Hochgeladene Dateien werden automatisch in die OpenCloud gespiegelt (Ordner "Umfragen/LimeSurvey").',
  'Danke fürs Ausprobieren!', 'Deine Antwort wurde gespeichert.', 1);

-- ── Question group ──
INSERT INTO lime_groups (gid, sid, group_order, randomization_group, grelevance)
VALUES (nextval('lime_groups_gid_seq'), $SID, 1, '', '1');

INSERT INTO lime_group_l10ns (id, gid, group_name, description, language)
VALUES (nextval('lime_group_l10ns_id_seq'),
  (SELECT gid FROM lime_groups WHERE sid = $SID AND group_order = 1),
  'Angaben', '', 'de');

-- ── Q1: short free text ──
INSERT INTO lime_questions (qid, parent_qid, sid, gid, type, title, other,
  mandatory, encrypted, question_order, scale_id, same_default, relevance,
  question_theme_name, same_script)
VALUES (nextval('lime_questions_qid_seq'), 0, $SID,
  (SELECT gid FROM lime_groups WHERE sid = $SID AND group_order = 1),
  'S', 'name', 'N', 'N', 'N', 1, 0, 0, '1', 'shortfreetext', 0);

INSERT INTO lime_question_l10ns (id, qid, question, help, script, language)
VALUES (nextval('lime_question_l10ns_id_seq'),
  (SELECT qid FROM lime_questions WHERE sid = $SID AND title = 'name'),
  'Wie heißt du? (Vor- und Nachname)', NULL, NULL, 'de');

-- ── Q2: file upload ──
INSERT INTO lime_questions (qid, parent_qid, sid, gid, type, title, other,
  mandatory, encrypted, question_order, scale_id, same_default, relevance,
  question_theme_name, same_script)
VALUES (nextval('lime_questions_qid_seq'), 0, $SID,
  (SELECT gid FROM lime_groups WHERE sid = $SID AND group_order = 1),
  '|', 'upload', 'N', 'N', 'N', 2, 0, 0, '1', 'upload', 0);

INSERT INTO lime_question_l10ns (id, qid, question, help, script, language)
VALUES (nextval('lime_question_l10ns_id_seq'),
  (SELECT qid FROM lime_questions WHERE sid = $SID AND title = 'upload'),
  'Bitte lade eine Datei hoch (z. B. Foto oder PDF)', NULL, NULL, 'de');
SQL

  # ── Activation: response table, mirroring LimeSurvey's own schema ──
  GID="$(psql -tAc "SELECT gid FROM lime_groups WHERE sid=$SID AND group_order=1" | tr -d '[:space:]')"
  QID1="$(psql -tAc "SELECT qid FROM lime_questions WHERE sid=$SID AND title='name'" | tr -d '[:space:]')"
  QID2="$(psql -tAc "SELECT qid FROM lime_questions WHERE sid=$SID AND title='upload'" | tr -d '[:space:]')"
  psql -1 -v ON_ERROR_STOP=1 -c "CREATE TABLE lime_survey_${SID} (
  id serial PRIMARY KEY,
  submitdate timestamp,
  lastpage integer,
  startlanguage character varying(20) NOT NULL DEFAULT 'en',
  seed character varying(31) NOT NULL DEFAULT '',
  \"${SID}X${GID}X${QID1}\" text,
  \"${SID}X${GID}X${QID2}\" text,
  \"${SID}X${GID}X${QID2}_filecount\" integer
);"

  echo "✓ example survey created and activated (sid=$SID)"
fi

URL="${SURVEYS_URL:-http://localhost:8080}"
echo "  public URL: $URL/index.php/$SID"
echo "  admin:      $URL/admin"
