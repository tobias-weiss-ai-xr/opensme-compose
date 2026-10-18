<!--
SPDX-FileCopyrightText: 2026 Tobias Weiss
SPDX-License-Identifier: Apache-2.0
-->

# E2E Journeys — Epics, User Stories & Tests

The user-facing truth of openSME is not the compose files — it is whether a
person can log in once and get their work done. Layer 5
([tests/05-e2e/](../tests/05-e2e/run.py)) verifies exactly those journeys
over real HTTP against a running stack: no mocks, no form scraping — logins
go through the Zitadel v2 Session API, the same API the hosted Login UI uses.

Every story below maps 1:1 to a check in the runner. Checks **skip** (not
fail) when the participating app isn't running, so the suite is meaningful
for any overlay subset.

---

## Epic A — Identity: one login for everything

> *Als Mitarbeiter möchte ich mich einmal anmelden und alle Apps nutzen
> können — ohne zweite Passwörter, ohne Second-Screen-Raterei.*

| # | User story | Journey test |
|---|------------|--------------|
| A1 | Als Betreiber sehe ich, dass der IdP seine OIDC-Metadaten korrekt veröffentlicht (endpoints, issuer, JWKS) | `section_discovery` — discovery document + JWKS keys |
| A2 | Als Mitarbeiter melde ich mich per Browser-Flow an (authorization-code + PKCE) und erhalte Tokens | `run_auth_code_flow` — real code exchange via v2 Session API |
| A3 | Als integrierende App vertraue ich dem ID-Token: RS256-Signatur gegen den live JWKS geprüft, iss/aud/exp/iat/nonce/sub stimmen | `verify_id_token` — pure-stdlib RSA verify, openssl-verified |
| A4 | Als Mitarbeiter lande ich in der zweiten App ohne erneute Passworteingabe | `section_sso_reuse` — second authorize finalized from the existing IdP session |
| A5 | Als Mitarbeiter beende ich meine Sitzung — und die tote Session wird überall abgewiesen | `section_logout` — DELETE session, then reject reuse; `logout.py` (A5/LO-1) — dieselbe Sitzung stirbt über das Portal-Logout, inklusive Sign-out bei der zweiten App |

**Security invariants covered by A2–A5:** state binding, PKCE S256,
audience binding, replay rejection after logout.

### Dedizierte SSO-Journeys (laufen in CI)

Zwei Journeys prüfen das Identitätsverhalten über `run.py` hinaus — beide
brauchen nur den Demo-Stack (Portal + IdP), laufen also im SSO-Flows-Job:

| Journey | Prüft | Checks |
|---------|-------|--------|
| `logout.py` | Portal-Logout & Single Log-out: `POST /api/logout` löscht das Session-Cookie und antwortet mit 303 auf den `end_session`-Endpoint (mit `id_token_hint` + Post-Logout-URI), der Browser landet auf `/logged-out`, die geteilte IdP-Session ist tot (Replay abgewiesen), und ein zweiter Relying-Party-Client (Stellvertreter für OpenCloud) ist danach ebenfalls abgemeldet | K1–K4 |
| `session_lifecycle.py` | Sitzungs-Lebenszyklus: S1 alle Credentials sind zeitlich begrenzt und `expires_in`/`exp`/`iat` stimmen Ende-zu-Ende überein, S2 `offline_access` wird geehrt (Refresh-Grant liefert NEUES Access-Token mit rotiertem Refresh-Token, ohne erneuten Login, live an Userinfo), S3 EINE IdP-Session trägt ZWEI Relying Parties (Portal-App + OpenCloud-Client) | S1–S3 |

Ohne Browser, ohne Wartezeiten auf echte Timeouts: geprüft wird der
Timeout-*Vertrag* des Stacks (endlich, konsistent, aktuell gültig) — das
ist, was ein E2E-Test ohne Stunden Schlaf ehrlich zusichern kann.

## Epic B — Portal: the single entry point

> *Als Mitarbeiter möchte ich einen Ort, der mir zeigt, was es gibt und ob
> es läuft — und der nichts verspricht, was nicht konfiguriert ist.*

| # | User story | Journey test |
|---|------------|--------------|
| B1 | Als Mitarbeiter öffne ich das Portal und sehe eine funktionierende Seite | `section_portal` — landing page 200, mentions openSME |
| B2 | Als Sicherheitsbeauftragter erwarte ich harte Browser-Header auf jeder Antwort (CSP, nosniff, frame-deny, referrer-policy) | header contract — mirrors the middleware in `portal/src/main.rs` |
| B3 | Als Mitarbeiter sehe ich, welche Dienste für mich freigeschaltet sind | `/api/services` returns the gated service list |
| B4 | Als Mitarbeiter lese ich Betreiber-Ankündigungen — wohlgeformt und ohne XSS-Risiko | `/api/announcements` schema contract (level ∈ info\|warn, non-empty text, ≤ 8) |
| B5 | Als Betreiber will ich, dass die AI-Karte (und ihr Endpoint) unsichtbar bleibt, solange keine KI konfiguriert ist | AI card gating — card visibility must match `AI_API_URL` |

## Epic C — Groupware & Chat: SSO reaches every app

> *Als Mitarbeiter möchte ich Mail, Webmail und Chat über denselben Login
> erreichen wie Dateien und Dokumente.*

| # | User story | Journey test |
|---|------------|--------------|
| C1 | Als Mitarbeiter erreiche ich OpenCloud, Notes, Paperless, Ticketing, Website und Store | `section_apps` — reachability (skips per app) |
| C2 | Als Chat-Nutzer beginnt der Matrix-Login beim zentralen IdP, nicht bei Synapse | `section_synapse_sso` — SSO redirect targets the IdP |
| C3 | Als Webmail-Nutzer lande ich beim zentralen IdP, nicht bei einer SOGo-Own-Login-Seite | `section_sogo_sso` — same contract for the groupware; `webmail.py` prüft zusätzlich die Seite selbst (Login-Markup + jedes referenzierte Asset), wenn ein SOGo-Container läuft |

## Epic D — Local AI: there when configured, invisible when not

> *Als Mitarbeiter frage ich den lokalen Assistenten im Portal — als
> Betreiber stelle ich sicher, dass ohne `--profile ai` keine Spur davon
> sichtbar ist.*

| # | User story | Journey test |
|---|------------|--------------|
| D1 | (siehe B5) — UI/Endpoint gating | `section_portal` AI card gating |
| D2 | Als Mitarbeiter bekommt meine Frage eine Antwort über den Portal-Proxy (Retries überbrücken die Modell-Ladezeit) | Layer 4 `ai-proxy` round-trip — deployed twin of the portal's in-process contract tests |

## Epic E — Trust & transport: every response is defensible

> *Als Nutzer und als Auditor vertraue ich dem Portal auch dann, wenn ich
> auf eine falsche URL klicke oder bewusst HTTP eintippe.*

| # | User story | Journey test |
|---|------------|--------------|
| E1 | Als Nutzer, der `http://` eintippt, lande ich sofort verschlüsselt | plain-HTTP request → redirect to HTTPS |
| E2 | Als Auditor erwarte ich die harten Header auf JEDER Antwort — auch auf 404 | security headers survive a 404 response |

## Epic F — Truthful catalog: the portal never lies

> *Als Mitarbeiter klicke ich auf eine Kachel und erwarte, dass dahinter
> wirklich ein Dienst steht — keine Leichen im Katalog.*

| # | User story | Journey test |
|---|------------|--------------|
| F1 | Als Mitarbeiter kann ich jeder beworbenen Kachel trauen: advertised ⇔ reachable | every `/api/services` entry answers with < 500 |

## Epic G — Identity lifecycle: identities are born, work, and die

> *Als Betreiber lege ich einen Mitarbeiter über die API an; er meldet
> sich an; nach Kündigung ist die Identität sofort tot — überall.*

| # | User story | Journey test |
|---|------------|--------------|
| G1 | Als Betreiber provisions ich eine Identität per IdP-API (inkl. Initialpasswort) | `section_user_lifecycle` — create-with-password |
| G2 | Als frisch angelegter Mitarbeiter melde ich mich über den kompletten SSO-Flow an | full authorization-code flow as the new user |
| G3 | Als Betreiber lösche ich die Identität — und der tote Login wird abgewiesen, solange die App existiert | deletion → login attempt → rejection |

The journey is idempotent: leftovers from crashed runs are purged before
provisioning, and the identity is cleaned up even on failure.

## Epic H — Broadcast: the operator talks to every user at once

> *Als Betreiber verkünde ich eine Ankündigung an alle Nutzer — und ein
> Angreifer kann über diesen Kanal kein JavaScript einschleusen.*

| # | User story | Journey test |
|---|------------|--------------|
| H1 | Als Nutzer sehe ich die Betreiber-Ankündigung auf der Landing Page | `tests/05-e2e/broadcast.py` — env-driven publish, portal recreate |
| H2 | Als Angreifer erreiche ich über den Announcements-Kanal keine Skript-Ausführung | `<script>` payload arrives HTML-escaped on page and API |
| H3 | Als Nutzer erlebe ich Stille, wenn nichts zu sagen ist | withdraw → zero announcements |

Unlike the read-only journeys, Epic H **recreates the portal container**
with a `PORTAL_ANNOUNCEMENTS` override and restores it afterwards — the
only journey that drives the stack's lifecycle, because that is exactly
what an operator broadcast does.

## Epic I — Files & collaboration: the cloud is walled

> *Als Nutzer lege ich Dateien in der Cloud ab — und als Auditor stelle
> ich fest, dass niemand ohne Login an sie herankommt.*

| # | User story | Journey test |
|---|------------|--------------|
| I1 | Als Nutzer lädt die Cloud-Weboberfläche | `section_opencloud` — UI 200 with product marker |
| I2 | Als Angreifer komme ich ohne Credentials nicht an Dateien | unauthenticated WebDAV → 401/403 (auth wall holds) |
| I3 | Als Betreiber sehe ich ehrlich, ob Cloud-Login ans IdP angebunden ist | OIDC wiring probe — **warn** when the client isn't registered (the demo subset currently ships without it — web UI loads, login would fail; register the `opencloud` app for the full demo) |

## Epic J — Resilience: the stack heals itself

> *Als Betreiber übersteht mein Stack einen DB-Neustart und einen
> IdP-Ausfall, ohne dass Nutzer etwas davon merken.*

| # | User story | Journey test |
|---|------------|--------------|
| J1 | Als Nutzer merke ich nichts, wenn die Datenbank bounced | `tests/05-e2e/resilience.py` — `restart postgres` → IdP, portal reconnect and get healthy |
| J2 | Als Nutzer arbeite ich weiter, wenn der IdP ausfällt | `stop zitadel` → portal + cloud STAY up (degradation, not outage) → `start zitadel` → discovery serves again |

This is the second lifecycle-driving journey — it bounces real containers
and restores the stack in `finally`, so a crash can't leave it broken.

## Epic K — Backup & recovery: provable restore points

> *Als Betreiber kann ich nachweisen, dass mein Backup ein echter
> Restore-Punkt ist — kein Placebo.*

| # | User story | Journey test |
|---|------------|--------------|
| K1 | Als Betreiber erzeugt `scripts/backup.sh` nicht-leere, gültige Artefakte | `tests/05-e2e/backup.py` — runs the operator's own tooling, audits output |
| K2 | Der SQL-Dump enthält echte Tabellen — inklusive der IdP-Datenbank | gzip'd dump: size, `CREATE TABLE` count, zitadel db present |
| K3 | Das Traefik-Archiv (ACME/TLS-Material) ist ein gültiges tar | tar listing non-empty; full gzip CRC verified |

This journey caught a real bug: `backup.sh` used `compose run` (a second,
serverless container) for the SQL dump — it silently produced **empty**
dumps, and its default mode downed the stack *before* dumping. Both are
fixed; the journey keeps them fixed.

## Epic L — Session isolation: coworkers don't share keys

> *Als zwei Mitarbeiter angemeldet sind, darf das Abmelden des einen den
> anderen nie rauswerfen.*

| # | User story | Journey test |
|---|------------|--------------|
| L1 | Als Mitarbeiterin habe ich meine EIGENE IdP-Sitzung | `section_session_isolation` — two provisioned coworkers, distinct session ids |
| L2 | Als Mitarbeiter bleibt meine Sitzung bestehen, wenn ein Kollege sich abmeldet | A's logout sticks AND B's session still finalizes auth requests |

## Epic M — Exposure: management planes stay closed

> *Als Betreiber ist die Angriffsfläche dokumentiert — und nichts
> Statefulliches oder Verwaltbares hängt am Host.*

| # | User story | Journey test |
|---|------------|--------------|
| M1 | Der Host exponiert nur die dokumentierten Ports | `tests/05-e2e/exposure.py` — published ports == {80, 443, 8080} |
| M2 | Als Angreifer erreiche ich postgres/redis/memcached nicht vom Host | no host bindings + live port probes refuse |
| M3 | Traefiks Verwaltungs-API ist nicht öffentlich geroutet | `/api/http/routers`, `/dashboard/`, `/api/overview` → not 200 on any public host |

## Epic N — Runtime truth: what runs is what's declared

> *Als Betreiber kann ich beweisen, dass der Stack genau die Bits
> fährt, die deklariert sind — und keine `:latest`-Lotterie.*

| # | User story | Journey test |
|---|------------|--------------|
| N1 | Das Laufende entspricht der Deklaration — kein Drift | `tests/05-e2e/runtime_truth.py` — `compose config` vs. running images (build services are their own truth) |
| N2 | Kein Container läuft auf einem veränderlichen `:latest`-Tag | every running image is version-pinned |

This journey forced real pinning fixes: traefik `:latest` → `v3.7.13`,
zitadel → `v4.19.4`, opencloud → `8.0.1` — exactly what was running, now
immune to silent upstream upgrades.

## Epic O — Local AI, for real: the flagship path, live

> *Als Nutzer stelle ich dem Portal eine Frage und bekomme eine Antwort
> vom lokalen KI-Backend — mit sauberem Vertrag und abgesicherten
> Eingaben.*

| # | User story | Journey test |
|---|------------|--------------|
| O1 | Mit konfigurierter KI WIRD die Karte sichtbar (Umkehrung von D2/B5) | `tests/05-e2e/ai_journey.py` — portal recreated with `AI_API_URL` → `id="ai-card"` present |
| O2 | Frage rein, Antwort raus — der Proxy-Roundtrip funktioniert | POST `/api/ai/chat` → answer from the OpenAI-compatible mock |
| O3 | Das Portal spricht den korrekten Upstream-Vertrag | mock logs prove: bearer auth, `model` from `AI_MODEL`, user message == question, `max_tokens` bounded |
| O4 | Leere Fragen werden abgewiesen, nicht weitergeleitet | empty question → 400 AND nothing reaches the backend |
| O5 | Nach dem Test ist der Stack wieder im Auslieferungszustand | teardown: card hidden again |

The backend is a pure-stdlib mock (`tests/05-e2e/ai_mock.py`) that the
portal reaches over the docker bridge gateway — the journey tests the
PORTAL's AI surface and its contract, not a real LLM.

## Epic P — Persistence: the data is still there

> *Als Betreiber überlebt mein Dienst einen kompletten Stack-Restart —
> ohne Datenverlust und ohne disruption beim normalen Redeploy.*

| # | User story | Journey test |
|---|------------|--------------|
| P1 | Ein erneutes `up -d` stört den laufenden Betrieb nicht | `tests/05-e2e/persistence.py` — container start-times unchanged (no recreation churn) |
| P2 | Daten überleben einen vollen Stack-Restart (Volumes halten) | marker row in a journey-owned DB → `down` (volumes kept) → `up -d` → row still there |
| P3 | Nach dem Restart ist der Stack nicht nur "up", sondern brauchbar | IdP discovery + portal health answer again |

The journey owns its database (`e2e_persist`, dropped afterwards); the
stack's own volumes are never touched.

## Epic Q — Burst: a small office at 09:00

> *Um 9 Uhr öffnet die ganze Belegschaft den Arbeitsplatz — der Portal
> bleibt ruhig.*

| # | User story | Journey test |
|---|------------|--------------|
| Q1 | Parallelzugriff erzeugt keine Serverfehler | `tests/05-e2e/burst.py` — 40 workers × 8 requests across `/`, `/health`, `/api/services`: zero 5xx, zero conn errors |
| Q2 | Die Antwortzeit bleibt im Rahmen | p95 within a generous budget (CI runners are slow; the assertion is regression protection, not a benchmark) |
| Q3 | Der Katalog bleibt unter Last korrekt | every `/api/services` response under burst equals the calm one (no truncation, no mixed bodies) |

## Epic S — Hygiene: no defaults, no open writes

> *Als Auditor finde ich keinen Platzhalter-Credential im Laufzeit-Stack
> und keine anonyme Schreiboperation.*

| # | User story | Journey test |
|---|------------|--------------|
| S1 | Kein CHANGEME/Beispiel-Passwort erreicht den laufenden Stack | `tests/05-e2e/hygiene.py` — inspect every container env (documented minio pair excepted, full profile only) |
| S2 | Der IdP lehnt anonyme Management-Writes ab | `POST /v2/users/human` / `DELETE /v2/users/…` ohne Token → 401/403 |
| S3 | Die Cloud lehnt gefälschte Credentials ab | forged bearer on WebDAV + capabilities → 401/403 |

S1 caught a real one: the SMTP password placeholder (`CHANGEME_smtp`)
sat in opencloud's live env of EVERY SMTP-less deployment — the default
is now empty, mail stays opt-in.

## Epic T — Intercom: internal short messages

> *Als Mitarbeiter schicke ich dem Team eine kurze Notiz direkt über das
> Portal — ohne Kanal-Wechsel.*

| # | User story | Test |
|---|------------|------|
| T1 | Als Nutzer sehe ich das Intercom-Panel auf dem Portal | landing page contains `id="intercom-card"`; `GET /api/intercom` lists messages |
| T2 | Als Nutzer sende ich eine Nachricht; sie erscheint in der Liste | `POST /api/intercom` with text → 2xx, message retrievable, rendered on the page |
| T3 | Als Nutzer wird mein Text XSS-sicher gerendert | `<script>` in text lands escaped in the page, raw in the JSON API |
| T4 | Als Betreiber sind Nachrichten begrenzt | empty text and >2000 chars rejected with 400; store capped at 50 (FIFO) |

The store is in-memory by design (v1): messages are conversational
ephemera, not records. The journey never assumes persistence across
restarts — that is Epic P's territory for DATA, not chat.

## Epic U — Attachments from the cloud service

> *Als Nutzer hänge ich einer Nachricht eine Datei aus der Cloud an —
> mit echten Metadaten, ohne den Inhalt durch das Portal zu schleusen.*

| # | User story | Test |
|---|------------|------|
| U1 | Als Nutzer füge ich eine Cloud-Datei per Link hinzu | `POST` with `attachment_url` → attachment metadata (name, size, type) attached to the message |
| U2 | Die Metadaten kommen zur Sendezeit vom Cloud-Server (Snapshot) | portal fetches HEAD at send time — name from Content-Disposition, size from Content-Length, type from Content-Type |
| U3 | Als Betreiber akzeptiert das Portal nur die eigene Cloud als Quelle | host allowlist (`INTERCOM_ATTACHMENT_HOSTS`, default `cloud.<domain>`) — foreign hosts and ports rejected with 400 |
| U4 | Als Angreifer kann ich SSRF nicht nutzen | IP-literal hosts, link-local targets and non-allowlisted internal services rejected; http refused unless `INTERCOM_ALLOW_HTTP=1` (dev) |
| U5 | Dateinamen mit Schadcode werden XSS-sicher gerendert | `<script>` in Content-Disposition filename lands escaped on the page |

The portal stores a metadata SNAPSHOT at send time (the attachment stays
in the cloud — the portal never proxies file bodies).

## Epic V — Portability: bring your own domain

> *Als SME-Betreiber will ich den Stack auf meine eigene Domain bringen,
> ohne den Code anzufassen — nur .env-Werte ändern.*

| # | User story | Test |
|---|------------|------|
| V1 | Mein Portal läuft unter meiner Domain | portal recreated with `PORTAL_DOMAIN=portal.meine-firma.test` answers 200 on the NEW host (DNS fallback maps `*.test` to loopback) and the footer shows the new domain |
| V2 | Die Migration ersetzt die alte Adresse — sie ist keine Kopie | `https://portal.opensme.local` stops routing (traefik 404) after the recreate |
| V3 | Abgeleitete Defaults folgen meiner Domain | the intercom default allowlist is `cloud.<domain>`: a cloud stand-in aliased `cloud.meine-firma.test` on the compose network is ACCEPTED as attachment source |
| V4 | Der Rollback ist derselbe Weg zurück | recreate with the original env → old domain answers again, new domain is gone |

The journey migrates the PORTAL (compose labels re-interpolate at
recreate time, so the traefik router follows). A full-stack domain move
(IdP issuer, cloud, mail) is a `.env` + fresh-boot affair — out of scope
for a CI-priced journey, documented honestly here.

## Epic W — Observability & log hygiene

> *Als Operator will ich sehen, was der Stack tut — und ich will
> gleichzeitig wissen, dass die Logs keine Secrets ausspucken.*

| # | User story | Test |
|---|------------|------|
| W1 | Jeder Dienst verrät seinen Zustand | every running `opensme-*` container with a healthcheck reports `healthy` (containers younger than their start period are skipped, not failed) |
| W2 | Secrets landen nicht in den Logs | every `*PASS*/*SECRET*/*TOKEN*/*KEY*/*SALT*` value from `.env` (≥10 chars) is absent from the last 4000 log lines of EVERY opensme container |
| W3 | Das Portal loggt strukturiert (RFC 3339, Level) | the portal's recent log lines carry a RFC-3339 timestamp + level prefix (tracing format), so aggregation works out of the box |

Read-only journey — recreates nothing, safe to run anywhere.

## Epic X — Idempotency: the second boot is a no-op

> *Als Betreiber will ich `scripts/demo.sh` (oder den Deploy-Weg) einfach
> nochmal fahren können — ohne Duplikate, ohne kaputte Secrets, ohne
> Überraschungen.*

| # | User story | Test |
|---|------------|------|
| X1 | Ein Zweitboot mit bestehendem `.env` ist ungeschrieben | the existing `.env` is byte-identical after a second `scripts/demo.sh` run (no silent re-rotation, "using existing .env" path) |
| X2 | Der Zweitboot erzeugt keine Duplikate in Zitadel | an OIDC app created BEFORE the second boot still exists EXACTLY ONCE after it (same id, no copy) |
| X3 | Der Automation-Machineuser arbeitet weiter | the seeded PAT (refreshed by the second boot's `zitadel setup`) still authenticates: `POST /auth/v1/users/me` → 200 |
| X4 | Der Stack ist danach gesund | portal, IdP and cloud answer 200 on their health/public routes after the second boot |

This is the boot-strapping promise as a test: setup steps and seeding
are idempotent, volumes hold state, and a re-run converges instead of
diverging.

## Epic Y — Disaster recovery: a backup that has survived its drill

> *Als Betreiber will ich aus einem echten Total-Verlust (Volumen weg,
> Datenbank weg) mit dem Bordmittel wiederherstellen können — und zwar
> so, dass es einer vorher geprüft hat.*

| # | User story | Test |
|---|------------|------|
| Y1 | Mein Restore-Punkt ist vollständig | `backup.sh` produces the SQL dump (with the drill marker database in it) and non-empty volume archives |
| Y2 | Ich sehe vorher, was passieren würde | `restore.sh --dry-run <prefix>` exits 0 and names the artifacts |
| Y3 | Der Ernstfall läuft durch | the marker database is DESTROYED (`DROP DATABASE`, verified gone), then the interactive `restore.sh` is answered "yes" — it takes the stack down, restores PostgreSQL + volumes and boots it back |
| Y4 | Die Daten sind wirklich zurück | the marker row resurrects byte-for-byte from the dump (`SELECT` returns the exact buried value) |
| Y5 | Danach ist der Stack wieder sauber online | the portal answers 200 on the public route after the drill |

The drill drives the stack lifecycle like persistence does, but goes
further: it destroys state for real and puts it back with the
operator's own tooling.

## Epic Z — Trust & transport at the edge: the contract of the front door

> *Als Nutzer will ich, dass der Transport mirror-sicher ist: Klartext
> landet nirgends, keine Antwort verrät Interna, Management-Flächen
> verlangen Beweise.*

| # | User story | Test |
|---|------------|------|
| Z1 | Klartext-HTTP gibt es nicht | every public hostname (portal/auth/cloud) answers plain http with a 3xx redirect to the https URL |
| Z2 | Jede Antwort trägt die Sicherheits-Header | page AND API responses carry `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin` and a CSP |
| Z3 | Fehlerseiten verraten nichts | unknown paths answer 404 with a body containing no framework names, no panics, no stack traces |
| Z4 | Das Traefik-Dashboard bleibt hinter Basic-Auth | the dashboard router answers 401 without credentials |
| Z5 | Fremde Hostnamen bekommen nichts | an unknown vhost gets traefik's default 404 — no other component answers for it |

## Epic AB — Document hygiene: the SSR markup is the interface

> *Als Nutzer mit Screenreader (oder altem Browser) will ich, dass die
> server-rendered Seite alleine schon sauber strukturiert ist — ohne
> dass JavaScript nötig wäre.*

| # | User story | Test |
|---|------------|------|
| AB1 | Das Dokument erklärt sich | `<html lang>` set, viewport meta present, non-empty `<title>` |
| AB2 | Bilder haben Alternativtexte | every `<img>` carries an alt attribute (decorative `alt=""` counts — it must EXIST) |
| AB3 | Jedes Formularfeld ist beschriftet | every input/textarea has aria-label, aria-labelledby or an associated `<label for>` |
| AB4 | Keine Inline-Event-Handler | no `onclick=`-style attributes — behaviour lives in `/app.js` (CSP `script-src 'self'` without unsafe-inline) |
| AB5 | Die Überschriften-Gliederung stimmt | exactly one `<h1>`, no heading level is skipped on the way down |
| AB6 | Interaktive Elemente sind benannt | every link/button has visible text or an aria-label |

Journeys here parse the landing page with the stdlib HTML parser —
no browser, no JS, pure document truth.

## Epic AC — Hostile input: nothing breaks out of the renderer

> *Als Angreifer will ich Skripte und Tricks durch Notizen und
> Anhänge in die Seite schmuggeln — als Betreiber will ich, dass der
> Renderer jede Attacke zu inertem Text degradiert.*

| # | User story | Test |
|---|------------|------|
| AC1 | Event-Handler-Injection scheitert | `<img onerror>`, `<svg onload>`, script tags and broken-tag smuggles reach the intercom list ONLY html-escaped — no raw handler attribute in the served segment (the page's own script tag is out of scope) |
| AC2 | Feindliche Attachment-URLs werden abgewiesen | `javascript:`, `data:`, credential-bearing and explicit-port URLs are rejected or refused storage — no 5xx, never a live link |
| AC3 | Unicode überlebt den Round-Trip | umlauts, emoji and RTL text survive POST → API → page byte-identically (raw or html-escaped spelling) |
| AC4 | API und Seite erzählen absichtlich verschiedene Wahrheiten | the API serves the raw text (machine truth), the page serves the escaped form (human safety) — the contrast holds for a probe payload |
| AC5 | Die Liste bleibt begrenzt | the 51st note is answered honestly (201 or 4xx) and the served list never exceeds MAX_MESSAGES (50) — no unbounded growth |

## Epic AD — Rolling recreate: deploy without turbulence

> *Als Betreiber will ich das Portal unter laufendem Verkehr neu
> erstellen können (Upgrade, Konfig-Änderung), ohne dass Nutzer einen
> Hänger sehen.*

| # | User story | Test |
|---|------------|------|
| AD1 | Fehler bleiben im Fenster | a traffic poller (≈8 rps across / and /health) runs through a forced portal recreate — every failure (5xx or connection-level) falls INSIDE the recreate window, the in-window 5xx budget is small, and nothing errors after the window closes |
| AD2 | Der Verkehr erholt sich vollständig | the tail of the run is 100% ok — full success rate after the recreate, conn-level failures bounded |
| AD3 | Der Inhalt bleibt unberührt | stack healthy and the intercom API answers after the recreate — the recreation was transparent to content |

## Epic AE — Abuse resistance: the edge limiter actually limits

> *Als Betreiber will ich, dass die zugesagte Ratenbegrenzung (100
> req/s, burst 200) eine gefeuerte Zusage ist — nicht Dekoration.*

| # | User story | Test |
|---|------------|------|
| AE1 | Die Middleware ist verkabelt | the ratelimit middleware is declared in `traefik/dynamic.yml` AND mounted into the running traefik container |
| AE2 | Normaler Verkehr wird nie bestraft | 100 sequential requests: zero 429, zero 5xx |
| AE3 | Der Flood trifft die Grenze — und der Eddge überlebt | a 1000-request concurrent flood trips the limiter (429s appear, no 5xx), and after a short cool-down ordinary traffic flows freely again (bucket refills) |

## Epic AF — Secret surface: least privilege, measured

> *Als Angreifer will ich aus irgendeinem Container passende Schlüssel
> mitnehmen — als Betreiber will ich, dass jedes Secret genau so viel
> Fläche hat, wie sein Dienst braucht.*

| # | User story | Test |
|---|------------|------|
| AF1 | Der Edge trägt keine Geheimnisse | traefik's REAL environment (docker inspect) holds no secret-shaped env vars — the most exposed surface has nothing worth stealing |
| AF2 | Das Portal sieht nie das DB-Passwort | the portal talks HTTP to the world, not SQL to postgres — no POSTGRES/DB password in its env |
| AF3 | Der Machinekey hat einen Halter | the zitadel machinekey volume is mounted by exactly one container — zitadel itself |
| AF4 | `.env` wird nie whole-mount | no container mounts the repo's `.env` — host config must not become a wholesale secret dump |
| AF5 | Dashboard-Credentials sind gehasht | the traefik basic-auth users label is an apr1/bcrypt-shaped hash, never plaintext |

Generic by design: a new container that starts consuming the DB
password or mounting `.env` trips this journey without list upkeep.

## Epic AG — TLS contract at the crypto edge

> *Als Betreiber will ich, dass „nur TLS" eine gemessene Zusicherung
> ist: alte Protokolle abgelehnt, moderne akzeptiert, HSTS gesetzt,
> Zertifikat im Fenster.*

| # | User story | Test |
|---|------------|------|
| AG1 | Legacy TLS wird abgewiesen | handshakes capped at TLS 1.0/1.1 are refused at the edge (stdlib ssl) |
| AG2 | Modernes TLS verbindet | TLS 1.2 and 1.3 handshakes succeed |
| AG3 | HSTS ist gesetzt | every response carries `strict-transport-security: max-age≥31536000; includeSubDomains` — browsers never try plain http again (pinned by a portal unit test too) |
| AG4 | Das Zertifikat ist im Fenster | the served cert's validity covers now (parsed with openssl like an operator would) — catches stale or swapped certs |

## Epic AH — Concurrent writers: the store holds under contention

> *Als Team wollen wir zwanzig Notizen im selben Augenblick schreiben
> können, ohne dass eine verloren geht oder die Seite bricht.*

| # | User story | Test |
|---|------------|------|
| AH1 | Parallele Schreiber kommen alle durch | 20 concurrent intercom POSTs all answer 201 — no deadlock, no dropped writers |
| AH2 | Keine Note geht verloren | every posted note is in the store afterwards — no lost update, no interleaving corruption |
| AH3 | Leser sehen nie Halbfertiges | 10 readers DURING the storm all see valid HTTP-200 JSON |
| AH4 | Die Seite trägt den Sturm | the rendered page carries the notes that survived the contention |

This journey exists because the wiring was found DEAD: the
name-suffixed key form of the entrypoint middleware flags was silently
dropped by traefik's slice parser — 1875 rps, zero 429s. The fix
(repeated-flag form) is pinned by measuring, not by reading config.

## Epic MA — Mail, for real: mailcow delivers actual mail

> *Als Betreiber will ich die volle Mail-Server-Option (mailcow-dockerized,
> als Git-Submodule gepinnt) wie ein Nutzer UND wie ein Auditor fahren:
> echtes SMTP/IMAP, echte Zustellung, abgeschottete Verwaltung.*

| # | User story | Journey test |
|---|------------|--------------|
| MA1 | Die Admin-UI lädt über den openSME-Traefik | `tests/05-e2e/mailcow_journey.py` — `https://mail.<domain>/` serves the mailcow UI |
| MA2 | SMTP kündigt sich sauber an :25 an | EHLO → 250 with the mailcow banner |
| MA3 | Submission auf :587 verhandelt STARTTLS | smtplib STARTTLS handshake succeeds |
| MA4 | IMAPS auf :993 verhandelt TLS | stdlib TLS handshake succeeds |
| MA5 | **Zustell-Roundtrip**: Mailbox rein, Post an sich selbst, INBOX raus | REST API provisions domain+mailbox (deterministic password via `edit/mailbox`) → authenticated SMTP submission → message found in INBOX over IMAPS — pure stdlib (`smtplib`/`imaplib`) |
| MA6 | SOGo-Webmail antwortet | `/SOGo/` reachable through the same edge |
| MA7 | Die REST-API wird NIEMALS an der Öffentlichkeit serviert | `/api/v1/...` via Traefik → 401/403/404 (the allowlist covers localhost only) |

Integration facts (deliberately honest):

- **Submodule, kein Fork**: `mail/mailcow-dockerized` ist auf ein
  Upstream-Release gepinnt (`git submodule status` zeigt den Commit, Tag
  `2026-09` zum Zeitpunkt der Integration). GPL-3.0 bleibt in seinem
  eigenen Baum — kein Code wird in dieses Apache-2.0-Repo kopiert.
- **`scripts/mailcow.sh` ist der einzige Entrypoint**: rendert
  `mail/mailcow.conf` (gitignored, Secrets) aus `.env`, bootet das
  mailcow-Compose-Projekt, hängt Traefik ans mailcow-Netz und verwaltet
  den dynamischen Router als Marker-Block in `traefik/dynamic.yml`.
- **TLS gehört Traefik**: mailcows eigener ACME/HTTP-Weg ist aus, die
  Weboberfläche ist nur über die OpenSME-Edge erreichbar. Ein
  self-signed-Zertifikat (plus DH-Params) wird vom Wrapper in
  `data/assets/ssl` gelegt, damit die internen Listener (nginx/dovecot)
  starten.
- **IPv6**: mailcow bindet dual-stack. Auf Hosts, die mit
  `ipv6.disable=1` booten, patcht der Wrapper die offiziellen
  user-editable-Konfigpunkte (`data/conf/*`, update-fest) auf IPv4 und
  erzeugt die Bridge mit `enable_ipv6=false`. Dual-Stack-Hosts brauchen
  nichts.
- **Demo-leane Defaults**: ClamAV und Full-Text-Search sind aus
  (`MAILCOW_SKIP_CLAMD`/`MAILCOW_SKIP_FTS`), damit der Stack neben dem
  OpenSME-Kern in ~2 GB RAM passt. Für Produktion: beide auf `n`.
- **Kein OIDC in der gepinnten Version**: mailcow 2026-09 authentifiziert
  gegen seine eigene Mailbox-DB. IdP-gebundenes SSO für die Admin-UI ist
  Follow-up, kein brav - die Journeys dokumentieren den Ist-Zustand.

---

## SME Sagas — cross-service journeys (S1–S6)

> *Als Mitarbeitende,r Gov-Führungen,r Backoffice oder Supporter erreiche
> ich jedes Geschäftsziel über genau einen Einstieg: das Portal. Jede
> Saga reitet auf bestehenden Service-Contracts — das Portal navigiert,
> es mediiert keine Geschäftsdaten.*

Spec: openspec capability `sme-function-catalog` (portal-catalog +
sme-interoperability). Contracts: `contracts/tiles.yml`,
`contracts/interop.yml`; static layer: `tests/00-static/check_tiles.py`,
`tests/00-static/check_interop.py`.

| Saga | Journey (user story) | Navigation edges | Deep tests |
|------|----------------------|------------------|------------|
| S1 Daily work (onboarding) | MA öffnet Portal → Cloud/Webmail/Notes | tiles → service edges answer | `saga_onboarding.py` (OIDC discovery → first tile → SSO reuse), `run.py` (webmail, cloud), `webmail.py` |
| S2 Quote-to-cash | BO erstellt Angebot → Rechnung → Freigabe → Ablage | invoice → workflow → archive | `saga_quote_to_cash.py` (invoice export + DMS ingest APIs), `saga_smoke.py` P5, `check_bootstrap.py` (BPMN) |
| S3 Approvals (leave request) | GF genehmigt Urlaub/Rechnung im Cockpit | workflow tile → engine | `saga_leave_request.py` (engine API + vacation-request BPMN), `run.py` SSO, `bootstrap/bpmn-deploy.sh` |
| S4 Customer service (support ticket) | SUP löst Ticket im Helpdesk | support tile → ticketing | `saga_support_ticket.py` (ticketing API), `run.py` apps |
| S5 External visibility (publishing) | MA publiziert News/Shop; Portal liest Feeds | website/shop tiles, `/api/feeds` degrade | `saga_publishing.py` (CMS publish API + public URL), `saga_smoke.py` P2, `feeds.rs` unit tests |
| S6 Knowledge (team knowledge) | Team verlinkt Notes-Snippets in Intercom | notes tile, share-link shape | `saga_team_knowledge.py` (Notes API), `check_interop.py` (share links), `intercom.py` |

### Journey blueprints

Each catalog saga has a dedicated journey blueprint that validates the
participating services' **public contracts** (not just navigation edges).
All blueprints skip cleanly when a participating service is not deployed.

| Blueprint | Participating services | Public contract validated | Skip when |
|----------|------------------------|---------------------------|-----------|
| `saga_onboarding.py` | IdP, portal, OpenCloud, SOGo | OIDC discovery → portal `/health` → first tile opens → OpenCloud + SOGo SSO reuse (auth-code redirect) | OpenCloud or SOGo not deployed |
| `saga_quote_to_cash.py` | Invoice Ninja, Paperless-ngx | Invoice export endpoint (401/403, not 404) + DMS ingest endpoint (401/403, not 404) | Invoice Ninja or Paperless-ngx not deployed |
| `saga_leave_request.py` | Operaton (BPMN), mail | Engine API (`/engine-rest/engine`) reachable + vacation-request process definition deployed | Operaton not deployed |
| `saga_support_ticket.py` | Ticketing (Nosdesk) | Ticketing service answers at the edge (HTTP < 400) | Ticketing not deployed |
| `saga_publishing.py` | CMS (website) | CMS publish API (`/admin`, auth-walled) + published site reachable (200) | CMS not deployed |
| `saga_team_knowledge.py` | Notes (Impress) | Notes API (`/api/v1.0/documents/`, auth-walled, not 404) | Notes not deployed |

```bash
# saga navigation smoke — works against demo stack OR live deployment:
python3 tests/05-e2e/saga_smoke.py opensme.local
python3 tests/05-e2e/saga_smoke.py home.opensme.graphwiz.ai

# individual journey blueprints (each skips cleanly on partial deployments):
python3 tests/05-e2e/saga_onboarding.py opensme.local
python3 tests/05-e2e/saga_quote_to_cash.py opensme.local
python3 tests/05-e2e/saga_leave_request.py opensme.local
python3 tests/05-e2e/saga_support_ticket.py opensme.local
python3 tests/05-e2e/saga_publishing.py opensme.local
python3 tests/05-e2e/saga_team_knowledge.py opensme.local

# all six saga blueprints via make:
make saga DOMAIN=opensme.local
```

Skips are saga preconditions, not failures: an overlay that is not
deployed skips its saga step visibly. A rendered tile whose edge does
not answer is always a failure — "no tile to nowhere".

## Agentic journeys — agent actions are decisions (AS1–AS6)

> *Der Agent darf nichts alleine: jeder Schreibzugriff ist ein Draft mit
> menschlicher Entscheidung, jede Remidiation braucht Consent, und jede
> Ablehnung wird zum Feedback, aus dem der Agent lernt.*

Specs: openspec `agent-saga`, `agent-feedback`, `agent-self-maintenance`,
`dev-agent-extensions`. Contract: `contracts/agents.yml` (write_mode
consent). Bots: serve-mode dev-maintenance-bot (eigenes Isolated-Container-
Setup je Journey — kein Live-Stack nötig).

| Journey | Datei | Beweis |
|---------|-------|--------|
| AS1 Saga: krank → diagnose → consent → heal → audit | `tests/05-e2e/agent_saga.py` | 11 Checks: Consent-Gate, Heal-Receipt, Evidence-Kette |
| AS2/3/5 Write-Gate + HITL + Feedback-Loop | `tests/05-e2e/agent_draft_saga.py` | B1–B6 (Draft inert bis Approve, Commit auditiert) + C1–C7 (`/drafts`-Sichtbarkeit, Major-Flag, Reject mit Pflicht-Grund, finale Ablehnung, `/feedback`-Aggregat, Restart-Dauerhaftigkeit) — 14 Checks |
| AS6 Merge-Gate = Testpyramide | `make agentic-gate` | Opt-in (`AGENTIC_STRICT=1` erzwingt; default nie rot) |

Operator-Oberfläche (pi-Extension): `/drafts` (Sichtbarkeit), `/approve <id>`
(mit Confirm — der Commit), `/reject <id> <grund>` (Pflicht-Grund → Feedback),
`/feedback` (Aggregat). Die Loop schließt: die letzten Rejections fließen
anonymisiert in den LLM-Kontext des Bots.

```bash
python3 tests/05-e2e/agent_saga.py
python3 tests/05-e2e/agent_draft_saga.py
AGENTIC_STRICT=1 python3 tests/00-static/check_agentic.py
```

## Running

```bash
# demo stack (bootstrap + full flows):
./scripts/demo.sh
python3 tests/run.py --e2e --domain opensme.local

# operator broadcast journey (recreates the portal — CI runs it after the suite):
python3 tests/05-e2e/broadcast.py opensme.local

# resilience journey (bounces postgres + zitadel — CI runs it after the suite):
python3 tests/05-e2e/resilience.py opensme.local

# backup journey (runs scripts/backup.sh, audits the artifacts):
python3 tests/05-e2e/backup.py

# exposure + runtime truth (read-only operator audits):
python3 tests/05-e2e/exposure.py opensme.local
python3 tests/05-e2e/runtime_truth.py

# local AI journey (recreates the portal against a stdlib AI mock):
python3 tests/05-e2e/ai_journey.py opensme.local

# burst + hygiene (read-only quality/credential audits):
python3 tests/05-e2e/burst.py opensme.local
python3 tests/05-e2e/hygiene.py opensme.local

# transport + accessibility (read-only edge/document audits):
python3 tests/05-e2e/transport.py opensme.local
python3 tests/05-e2e/accessibility.py opensme.local

# rate limit journey (fires a flood at the edge limiter, measures 429s):
python3 tests/05-e2e/rate_limit.py opensme.local

# tls contract + secret surface journeys (crypto edge + least privilege):
python3 tests/05-e2e/tls_contract.py opensme.local
python3 tests/05-e2e/secret_surface.py opensme.local

# concurrent writers journey (20 writers + 10 readers at once):
python3 tests/05-e2e/concurrent_writers.py opensme.local

# hostile input journey (feeds XSS payloads through the intercom renderer):
python3 tests/05-e2e/hostile_input.py opensme.local

# rolling recreate journey (recreates the portal under synthetic traffic):
python3 tests/05-e2e/rolling_recreate.py opensme.local

# persistence journey (restarts the whole stack — run it last):
python3 tests/05-e2e/persistence.py opensme.local

# restore drill (runs backup.sh + restore.sh, drops/restores state — run LAST):
python3 tests/05-e2e/restore_drill.py opensme.local

# intercom journey (recreates the portal with an attachment-source mock):
python3 tests/05-e2e/intercom.py opensme.local

# portability journey (recreates the portal under another domain):
python3 tests/05-e2e/portability.py opensme.local

# observability journey (read-only health + log-secrets audit):
python3 tests/05-e2e/observability.py opensme.local

# idempotency journey (runs scripts/demo.sh a second time — slow):
python3 tests/05-e2e/idempotency.py opensme.local

# mailcow journey (requires the submodule + `scripts/mailcow.sh up`):
git submodule update --init mail/mailcow-dockerized
scripts/mailcow.sh up mail.opensme.local
python3 tests/05-e2e/mailcow_journey.py opensme.local
scripts/mailcow.sh down

# static-ish subset (no credentials needed):
python3 tests/05-e2e/run.py opensme.local --skip-flows

# CI runs the full suite against a fresh demo stack on every push to main.
```

Credentials: `ZITADEL_ADMIN_PASSWORD` from `.env`; the runner bootstraps a
throw-away OIDC app (`e2e-sso`) via the Management API and removes it
afterwards. See [tests/README.md](../tests/README.md#e2e-tests-05-e2e--sso--friends)
for all configuration knobs.
