<!--
SPDX-FileCopyrightText: 2026 Tobias Weiss
SPDX-License-Identifier: Apache-2.0
-->

# SSO

Single Sign-On im openSME-Stack: **ein** zentraler Identity Provider —
[Zitadel](https://zitadel.com/) v4.19.4, Container `opensme-zitadel`,
live erreichbar unter `https://auth.home.opensme.graphwiz.ai` (management
API; `ZITADEL_EXTERNALSECURE=false`, `ZITADEL_EXTERNALPORT=443`, Hostname
`auth.home.opensme.graphwiz.ai`, TLS endet am Edge-Traefik) — und jede
einzelne Anwendung als OIDC-Relying-Party. Ein Login am IdP gilt für alle
Dienste; Abmeldung läuft als RP-initiated Logout über die geteilte IdP-Session
(`portal/src/logout.rs`).

Alle Angaben in diesem Dokument sind **live verifiziert am 2026-10-26** auf
`opensme.graphwiz.ai`, sofern nicht anders vermerkt. Das Dokument ist
erklärend — normativ sind die Compose-Dateien, [`specs/`](../specs/),
[`contracts/`](../contracts/) und [`tests/`](../tests/) (siehe
[docs/README.md](README.md)). Die ausführbaren Gegenstücke zu jedem Abschnitt:

| Abschnitt | Erzwungen durch |
|---|---|
| Client-Registrierung (R1–R3) | `tests/00-static/check_oidc.py` (Layer 0) |
| Login-UI deklariert (R7) | `tests/00-static/check_oidc.py` |
| Backchannel-Pin + Autoprovisioning (R4/R5) | `tests/00-static/check_oidc.py` |
| Redirect-Gesundheit pro Dienst | `scripts/sso-check.sh` (`make sso-check`) |
| Headless-Login-Beweis (Session-API) | `tests/05-e2e/run.py` (`make e2e`) |

## Architektur

Der Browser-Login-Durchgang ist ein OIDC Authorization-Code-Flow mit PKCE —
der Code-Austausch läuft server-seitig in der Anwendung:

```
Browser ──GET https://cloud…/──► App (z. B. OpenCloud)
   ◄─ 302 Location: https://auth…/oauth/v2/authorize?client_id=…&PKCE…
Browser ──► Zitadel /oauth/v2/authorize
   ◄─ 302 Location: /ui/v2/login/login        (loginV2 ist „required“)
Browser ──► zitadel-login UI (SEPARATER Container, /ui/v2/login)
   ── Formular: Nutzer + Passwort ──► Zitadel v2 Session API
   ◄─ Session-Cookie am IdP (diese Session trägt ALLE künftigen Logins)
Zitadel finalisiert den Auth-Request
   ◄─ 302 Location: https://cloud…/oidc/callback?code=…&state=…
App-Backend ── code ──► Zitadel /oauth/v2/token
   ◄─ access token (+ ID token)
App validiert: JWT lokal gegen JWKS — oder opaque per Introspection
App provisioniert den externen Nutzer (Autoprovisioning) → App-Session
```

Wichtige Konsequenzen:

- **Die Login-UI ist ein eigener Container** (siehe
  [Zitadel-Login-UI](#zitadel-login-ui-browser-login)) — ohne ihn endet
  *jeder* Browser-Login in einem 404, während headless Journeys (Session-API)
  weiter grün laufen.
- **Token-Typ matters**: JWKS-validierende Backends brauchen JWT-Access-Tokens
  (siehe [OIDC-Token-Typen](#oidc-token-typen)).
- **Der server-seitige Backchannel** der Apps zum IdP darf nicht durch Dockers
  embedded DNS laufen (siehe [ocis-Backchannel-Pin](#ocis-backchannel-pin)).
- **Externe IdP-Subjekte müssen autoprovisioniert** werden, sonst 401 nach
  erfolgreicher Token-Validierung (siehe
  [Externe Nutzer provisionieren](#externe-nutzer-provisionieren)).

Die OIDC-Clients registriert [`scripts/zitadel-client.sh`](../scripts/zitadel-client.sh)
(idempotent; Zitadel minted Client-ID + Secret und das Script persistiert das
Paar in `.env` unter den Schlüsseln der jeweiligen App).

## Status-Tabelle

Stand: **2026-10-27** (CI-verifiziert auf HEAD; Live-Zeile so weit wie möglich).

| Dienst | Client | Status | Anmerkung |
|---|---|---|---|
| OpenCloud (Dateien/ocis) | `opencloud` (`OC_OIDC_CLIENT_ID`) | ✅ VERIFIZIERT | Headless-Beweis: authorize → v2 Session API → JWT → OCS `/ocs/v1.php/cloud/user` 200 **+** WebDAV 200 als `zitadel-admin@opensme.auth.home.opensme.graphwiz.ai`. Braucht zwingend `OIDC_TOKEN_TYPE_JWT` + Backchannel-Pin + Autoprovisioning (Abschnitte unten). **Regression 2026-10-30: Browser-Login brach am Token-Endpoint (`invalid_client: empty client secret`) — die SPA-App war als confidential/BASIC angelegt**, ein öffentliches PKCE-SPA kann das nie nutzen. Fix: `OIDC_AUTH_METHOD_TYPE_NONE` via `PUT …/apps/{id}/oidc_config`; `scripts/zitadel-client.sh` erzwingt das dauerhaft (opencloud-Branch `USE_PUBLIC_AUTH=1` → `enforce_auth_method_none`). **Re-verifiziert 2026-10-30 im Browser: Login → `/files/spaces/personal/testuer@…` (pers. Raum, 1 Item).** |
| Chat (Element/Synapse) | `opensme-synapse` (`SYNAPSE_OIDC_CLIENT_ID`) | ✅ VERIFIZIERT | Synapse-SSO: `/login/sso/redirect` → 302 auf Zitadel-Authorize mit PKCE. Element: `base_url` **und** `server_name` sind beide `matrix.home.opensme.graphwiz.ai` — `server_name` wird im Entrypoint aus `ELEMENT_DEFAULT_SERVER_URL` abgeleitet (kein `ELEMENT_HOMESERVER_NAME`), kann also nicht desyncen. **Regression 2026-10-30: OIDC-Callback „localpart is invalid“ → „Numeric user IDs are reserved“ — Zitadel-ID-Token tragen hier keine email/name/preferred_username-Claims** (nur `sub`+Metadata; Profile-Claims kommen ausschließlich über `/oidc/v1/userinfo`). Fix in `services/synapse-setup.sh`: `localpart_template: "u_{{ user.sub }}"` (claim-unabhängig, valid). **Re-verifiziert 2026-10-30 im Browser: Element `#/home` als `@u_397215662709145620`.** |
| Notes (Impress) | `opensme-notes` (`NOTES_OIDC_CLIENT_ID`) | ✅ VERIFIZIERT | Client registriert, Redirect auf den IdP funktioniert; **Browser-SSO-Flow 2026-10-30 verifiziert: Zitadel-Login als testuer → Backend-Board gelandet.** |
| Webmail (SOGo6) | `sogo-webmail` (`SOGO_OIDC_CLIENT_ID`) | 🟢 **live: echter OIDC-SSO end-to-end verifiziert** | Client registriert via `scripts/zitadel-client.sh --app sogo` (registriert auch den sogo6-Callback `{PUBLIC_BASE}/api/user/v1/auth/callback/{domain}` als Redirect-URI). **Stand 2026-10-31 (SSO-Ticket geschlossen):** sogo6 ist ein echtes OIDC-RP — `POST /de/ → email` triggert `kind:"sso"` + authorize-URL (`/api/user/v1/auth/mode`), Browser geht automatisch zu Zitadel `/ui/v2/login`, nach Login → Callback → JWT-Voucher (`302 → /auth/callback#token=<jwt>`, uid = Zitadel-`loginName`) → Frontend-Pickup → **`/de/u/0/INBOX` (Session via `/api/sse` 200, testuer authentifiziert)**. Voraussetzungen live: (a) Zitadel-App `397489856642613268` + `SOGO_OIDC_CLIENT_ID/SECRET` in `/opt/opensme-compose/.env`; (b) DB `AUTH_SETTINGS` = `openid` + CONFIG_URL `https://auth.home…` + Client-Name/-Secret (Patch in Live-DB); (c) `extra_hosts: auth.home…:host-gateway` am sogo6-server (OIDC-Discovery/Token holt Router „Traefik offline AFAIA bastelt nicht“ — Nachbau: siehe weiter unten); (d) **UI-Proxy-Fix** (SOGo6-UI `route.ts`): `fetch(...,{redirect:'manual'})` — sonst frisst die Next-Proxy-Schicht die Backend-302 und verliert das `#token`, der Browser bleibt am Callback hängen (Live-Bundle-Hotpatch liegt ein, Image-Rebuild aus dem fix gebauten Repo ersetzt ihn). **Weiterhin offen (pre-existing Open Item):** die **Postfach-UI rendert nicht** — sogo6→Stalwart-IMAP-LOGIN wird abgelehnt (Stalwart-Directory=`oidc` = Plain nur App-Passwörter; XOAUTH2/OAUTHBEARER bräuchte das IdP-Zugriffstoken, das im `user.password` steckt, aber der Webmail-IMAP-Client sendet Plain). Nebenbefund früher behoben: Domain-Settings zeigten auf toten Hostname `sogo6-stalwart:993` → `stalwart:143`/`587`/STARTTLS (Live-DB + SOGo6-server-Repo `scripts/init/domain_settings.json`). Optionen: App-Passwörter seeden **oder** IMAP-OAUTHBEARER (siehe `docs/operaton-sso.md`-Muster). **✅ RESOLUT 2026-10-31 (sogo→stalwart-App-Passwort-Seeding):** Postfach rendert end-to-end — (e) `SOGO_D_{IMAP,SMTP,SIEVE}_AUTH_MECH=xoauth2` (Access-Token als XOAUTH2 statt Plain; Live-DB + SOGo6-server-Repo `scripts/init/domain_settings.json`), (f) UI-Image `sogo6-ui:latest` aus Main `31538d2b` neu gebaut (im alten 10-26-Build fehlte der `memo`-Fix `8ae412be` → Client-Crash `memo is not defined`), (g) App-Passwörter via Stalwart-0.15-REST `POST/GET /api/account/auth` (self-service, Bearer = OIDC-Token) — Skript `scripts/stalwart-app-password.sh`. Verifikation: Browser-E2E → INBOX „1 Nachricht“, SMTP-XOAUTH2 `235`, IMAP-LOGIN mit App-Passwort OK (Details: Abschnitt „Stalwart App-Passwörter (sogo→stalwart)“). |
| Paperless | `opensme-paperless` (`PAPERLESS_OIDC_CLIENT_ID`) | 🟡 verdrahtet | Client registrierbar via `scripts/zitadel-client.sh --app paperless`; `services/paperless.yml` aktiviert django-allauth generisches OIDC (PKCE, Auto-Signup, Issuer `https://auth.<domain>`). Positiver Login-Flow-Test steht noch aus — lokal an `/tmp/reprodemo` nachholbar (Live-Container 🟥 steht hinter dem runc-EOF-Keil). |
| Invoice (Invoice-Ninja) | — | ❌ nicht nativ OIDC-fähig | Selfhosted Invoice-Ninja hat kein generisches OIDC (nur Google/Microsoft-Socialite; Feature-Request [#10839](https://github.com/invoiceninja/invoiceninja/issues/10839) offen). SSO bedarf eines eigenen Patches oder Reverse-Proxy-Auth — bewusst noch kein Client registriert. |
| Operaton (Camunda-Nachfolger) | `opensme-camunda` (`operaton.bpm.oauth2.*` + Plugin) | 🟢 **live: SSO-Login + Admin-Cockpit verifiziert** | Operaton 2.x CE ist **nativ OIDC-fähig** (spring-security-oauth2); eigener ZITADEL-`ReadOnlyIdentityProvider`-Plugin (Repo `operaton-zitadel`, 34 Tests grün, Gruppen = ZITADEL-Project-Rollen aus User-Grants, Admin via `operaton-admin`-Grant → `camunda-admin`). Live: OIDC-Client `397414680370348052`, Cockpit-Login als `tweiss` → Cockpit mit Admin-Rechten (12 Instanzen, 9 Tasks, 4 Defs); `--oauth2 --rest --webapps`-Launcher, Issuer via `host-gateway`-extra_hosts, `default.yml`-Override ohne `admin-user`; Details + Verifikation in `docs/operaton-sso.md`. |
| Surveys (LimeSurvey) | — | ❌ nicht nativ OIDC-fähig | LimeSurvey Community braucht ein Dritt-Plugin (AuthOAuth2/AuthOpenIDConnect), Konfig in Plugin-Einstellungen/`config.php`, nicht per Compose-Env — bewusst noch kein Client registriert. |
| Portal | `opensme-portal` (`PORTAL_OIDC_CLIENT_ID`) | ✅ SSO aktiv | Das Portal ist seit dem Auth-Gate ein richtiger OIDC-RP: Authorize-Code-Flow hinter `/api/auth/login` + `/api/auth/callback` (Redirection gegen `PORTAL_DOMAIN`), `/userinfo`-Bestätigung, dann eine **eigene stateless HMAC-Session** in `OPENSMEPORTALSESSION` (getrennt vom IdP-Token-Cookie `OPENSMESESSION`, das die RP-initiated-Logout-Hint trägt). **Intercom + Umfragen antworten nur mit gültiger Portal-Session (sonst 401, fail-closed bei fehlendem/`CHANGEME_`-Secret).** Client registriert via `scripts/zitadel-client.sh --app portal`; SLO-URI `/logged-out` ist registriert. Verifiziert via Rust-/Vitest-Tests (unitaire + Mock-IdP-Flow); Live-Journeys (`intercom.py`, `concurrent_writers.py`, `rolling_recreate.py`) fahren den echten Login und prüfen zuerst, dass anonyme Zugriffe 401 sind. **Live round-trip am 2026-10-28**: anonym `GET /api/intercom` → 401 (fail-closed); voller OIDC-Flow via `portal_login` (v2 Session API, `zitadel-admin@…`) → `OPENSMEPORTALSESSION`; eingeloggt `GET /api/intercom` → 200, `POST` → 201, Note in Liste sichtbar. |

## Zitadel-Login-UI (Browser-Login)

Zitadel v4 trennt die Login-UI vom Core: Die Seite, auf die `authorize`
menschen schickt, ist eine **eigene Next.js-Applikation** (`/ui/v2/login`),
geliefert vom Container **`opensme-zitadel-login`**
(`ghcr.io/zitadel/zitadel-login:v4.19.3`), nicht vom `opensme-zitadel`-Core.
In [`idm/zitadel.yml`](../idm/zitadel.yml) ist der Service hinter dem
`donotstart`-Profil deklariert — ein normales `up -d` startet ihn nie
halbfalsch; `scripts/demo.sh` startet ihn explizit, **nachdem**
`zitadel setup --steps /steps.yaml` das Login-Client-PAT (`/machinekey/
login-client.pat`) geseedet hat.

**Repo-Variante** (Demo-Stack): Container im `opensme-net`, Traefik-Labels
routen `Host(auth…) && PathPrefix(/ui/v2/login)` mit Priority 250 (schlägt den
Host-only-Zitadel-Router) auf Port 3000.

**Live-Variante** (2026-10-26, Fleet auf opensme.graphwiz.ai): Der VM war der
Network-Namespace-Pool ausgegangen — der Container läuft deshalb
**Host-Network** als `opensme-zitadel-login` auf `127.0.0.1:3000`, und ein
Fleet-Traefik-Standin (`/root/git/docker-traefik/dynamic/zitadel-login-standin.yml`)
routet `Host(auth.home.opensme.graphwiz.ai) && PathPrefix(/ui/v2/login)` mit
Priority 1000 auf `http://127.0.0.1:3000`. Relevante Env (live):

- `NEXT_PUBLIC_BASE_PATH=/ui/v2/login`
- `ZITADEL_API_URL=https://auth.home.opensme.graphwiz.ai` (Host-Network kann
  den Compose-Service-Namen nicht auflösen — im Repo-Setup ist es
  `http://zitadel:8080`)
- `ZITADEL_SERVICE_USER_TOKEN_FILE=/machinekey/login-client.pat`
  (Volume `compose_zitadel-machinekey`, read-only; im Repo `zitadel-machinekey`)
- `ZITADEL_SESSION_COOKIE_SECRET`
- `CUSTOM_REQUEST_HEADERS: Host:auth.home.opensme.graphwiz.ai,X-Forwarded-Proto:https`
  (Zitadel routet Instanzen nach Host — ohne die Header 404t der API-Call)

**Failure-Mode:** Fehlt die UI, folgt auf jeden `authorize` ein
`302 → /ui/v2/login/login → 404`. Headless Journeys bleiben grün (sie fahren
die Session-API direkt) — der Ausfall ist **nur im Browser sichtbar**.
Layer-0-Wächter: `tests/00-static/check_oidc.py` (R7 — Service muss in
`idm/zitadel.yml` deklariert bleiben).

**Verifizieren:**

```sh
# (1) Ready-Probe des Login-Apps — muss 200 sein
curl -sk -o /dev/null -w '%{http_code}\n' \
  https://auth.home.opensme.graphwiz.ai/ui/v2/login/ready
# → 200

# (2) Ein echter Authorize muss das Login-FORMULAR (200) liefern, nicht 404:
curl -skL -o /dev/null -w '%{url_effective} %{http_code}\n' \
  'https://auth.home.opensme.graphwiz.ai/oauth/v2/authorize?client_id=…&response_type=code&redirect_uri=…&scope=openid&code_challenge=…&code_challenge_method=S256'
# → …/ui/v2/login/login 200
```

## OIDC-Token-Typen

Zitadel minted Access-Tokens in zwei Formen:

- **Opaque** (Zitadel-Default): nur per Introspection-Endpunkt validierbar
  (Server-zu-Server-Roundtrip beim IdP).
- **JWT** (`OIDC_TOKEN_TYPE_JWT`): selbsttragend, signiert, lokal gegen die
  Issuer-JWKS (`/.well-known/openid-configuration/jwks`) validierbar.

Backends, die Access-Tokens **lokal per JWKS** validieren — im Stack v. a.
**OpenCloud/ocis** — MÜSSEN den Client auf JWT umstellen. Ein opaque Token hat
keine drei Segmente; ocis weist es mit
`token contains an invalid number of segments` ab, obwohl Issuer, Client und
Secret korrekt sind — der Fehler sieht aus wie ein Netzwerk-/Konfig-Problem
und ist es nicht.

**Stolperfalle:** Der Token-Typ ist nur über den spezialisierten Endpunkt
änderbar:

```sh
# RICHTIG: oidc_config-Endpunkt
curl -X PUT "https://auth.home.opensme.graphwiz.ai/management/v1/projects/${PID}/apps/${APPID}/oidc_config" \
  -H "Authorization: Bearer ${ZITADEL_PAT}" \
  -H 'Content-Type: application/json' \
  -d '{"accessTokenType": "OIDC_TOKEN_TYPE_JWT"}'
```

Das generische `PUT /management/v1/projects/{pid}/apps/{appid}` antwortet
höflich `No changes` und ändert den Token-Typ **nicht** — der Client bleibt
opaque und ocis bleibt kaputt. Nach dem Umstellen Client-Secret nicht
ändern müssen; ein Re-Login erzwingen (alte Tokens bleiben bis Ablauf opaque).

Die Clients selbst registriert [`scripts/zitadel-client.sh`](../scripts/zitadel-client.sh);
der Token-Typ-Switch ist eine einmalige Management-API-Aktion danach.

## ocis-Backchannel-Pin

ocis validiert Tokens server-seitig gegen `OC_OIDC_ISSUER`
(= `https://auth.home.opensme.graphwiz.ai`) **über HTTPS**. Dockers embedded
DNS löst den öffentlichen IdP-Hostnamen aber zur **Zitadel-Container-IP**
auf — dort lauscht nur plain-HTTP `:8080`, TLS lebt am
Edge-Traefik. Ergebnis: `connection refused` auf `:443` bei Discovery/JWKS,
und **jeder API-Call 401t still** — trotz perfekt registriertem Client.

Deshalb pinnt [`opencloud/opencloud.yml`](../opencloud/opencloud.yml) den
Hostnamen auf das Docker-Host-Gateway:

```yaml
extra_hosts:
  - "${ZITADEL_DOMAIN:-auth.opensme.graphwiz.ai}:host-gateway"
```

`host-gateway` (die docker0-Bridge des Docker-Hosts) führt zum Fleet-Traefik,
der auf `:443` TLS terminiert und den IdP-vHost per Host/SNI bedient.
(RFC1918-Adressen werden hier bewusst nicht genannt — die Compose-Quelle
in `opencloud/opencloud.yml` ist der einzige normative Referenzpunkt.)

**Probe im Container** — ocis' Go-HTTP-Client ignoriert Dockers injiziertes
`http_proxy`; ein Proxey-nutzender Test lügt also. Busybox-wget hat kein
`--no-proxy`, der Schalter ist `-Y off`:

```sh
docker exec opensme-opencloud wget -Y off -q -O- \
  https://auth.home.opensme.graphwiz.ai/.well-known/openid-configuration | head -c 200

# Auflösung prüfen: muss host-gateway sein, NICHT die Zitadel-Container-IP
docker exec opensme-opencloud getent hosts auth.home.opensme.graphwiz.ai
```

Layer-0-Wächter: `tests/00-static/check_oidc.py` (R4 — Pin muss in
`extra_hosts` stehen).

## Externe Nutzer provisionieren

Zitadel-Subjekte existieren nicht im ocis-IDM. Ohne Autoprovisioning 401t
ocis **jeden** SSO-Nutzer direkt nach erfolgreicher JWT-Validierung —
Log-Meldung: `Autoprovisioning disabled` (`proxy/account_resolver`).
Konfiguration in [`opencloud/opencloud.yml`](../opencloud/opencloud.yml):

```yaml
PROXY_AUTOPROVISION_ACCOUNTS: "true"          # externen Nutzer beim ersten Login anlegen
PROXY_ROLE_ASSIGNMENT_DRIVER: default          # NICHT 'oidc' — der braucht einen
                                               # groups-Claim, den der IdP nicht emittiert
PROXY_USER_OIDC_CLAIM: preferred_username      # Zitadel-Claim → IDM-Feld
PROXY_USER_CS3_CLAIM: username
```

Der `default`-Driver gibt jedem authentifizierten Subjekt die eingebaute
Nutzerrolle — robust, solange der IdP keine Gruppen-Rollen mapped.
Layer-0-Wächter: `tests/00-static/check_oidc.py` (R5).

## Ops-Runbook

### Stalwart-Webmail-Backchannel (OIDC-Directory → idpplain)

Stalwart validiert XOAUTH2-Zugriffstokens über `[directory.oidc]` an Zitadels
Userinfo-Endpoint. Der Aufruf läuft über den **nur-internen** `idpplain`-
Entrypoint (`:8081`, nie publiziert) mit einer Host-Rewrite-Middleware
(`zitadel-idp-host`). Zwei Dinge sind nicht verhandelbar:

1. **Kein hartkodierter IP-Pin im Shared-Compose** — die Demo/CI-Stacks
   lösen `${ZITADEL_DOMAIN}` über den Traefik-Netzwerk-Alias auf; der
   Fleet-Deploy (VM) bekommt seinen `host-gateway`-Pin aus dem Ansible-
   Overlay (`stalwart-idpplain.yml`, Rolle `opensme_suite`).
2. **Issuer-Override**: Dieselbe Middleware muss zusäztlich
   `X-Forwarded-Host`/`Proto`/`Port` auf die externe HTTPS-Origin setzen.
   Sonst leitet Zitadel aus dem proxied Request (`http://host:8081`) einen
   falschen Issuer ab und lehnt das Token mit `PermissionDenied "issuer
   does not match"` ab → SMTP 535 **und** SOGo-INBOX 500.

Wächter: `tests/00-static/check_webmail_sso.py` (W1–W5) + Mutationen
(`webmail-sso-*`).

### Schnell-Check: Redirect-Gesundheit pro Dienst

```sh
make sso-check          # = scripts/sso-check.sh
```

Folgt pro konfiguriertem Dienst dem Login-Redirect bis zum IdP und prüft,
dass der finale Redirect den IdP-Host **und die `client_id`** trägt
(OpenCloud, Notes, Synapse, Ticketing, Webmail). Dazu `make e2e` für die
vollen Journeys (siehe unten).

### Login-UI

Siehe [Zitadel-Login-UI](#zitadel-login-ui-browser-login): `/ui/v2/login/ready`
muss 200 liefern und ein durchgespielter `authorize` muss auf dem
Login-Formular (`/ui/v2/login/login`, HTTP 200) landen — ein 404 heißt:
Login-Container tot oder Standin/Router-Priority falsch.

### Headless-Beweis (ohne Browser)

Das ist der Durchgang, mit dem OpenCloud am 2026-10-26 verifiziert wurde —
automatisiert fährt ihn `tests/05-e2e/run.py` (`make e2e`):

1. **Discovery**: `GET /.well-known/openid-configuration` (issuer,
   authorization/token/jwks-Endpunkte) + JWKS abrufbar.
2. **Authorize mit PKCE**: Authorize-URL mit `code_challenge` anstoßen →
   Zitadel händigt eine Auth-Request-ID aus (Redirect auf die Login-UI —
   die wird hier nicht geladen).
3. **v2 Session API** (autorisiert durch das IAM_LOGIN_CLIENT-PAT
   `/machinekey/login-client.pat`): Session anlegen (`POST /v2/sessions`) →
   User-Check → Passwort-Check (`/v2/sessions/{id}`) →
   **Auth-Request finalisieren** → Callback-URL mit `code`.
4. **Token**: `code` + `code_verifier` gegen `/oauth/v2/token` tauschen →
   JWT-Access-Token + ID-Token.
5. **Beweis am Dienst**: Mit dem JWT `GET /ocs/v1.php/cloud/user` (OCS)
   → **200** mit der Nutzersicht, und ein WebDAV-`PROPFIND` → **200**.
   Live-Beweis 2026-10-26: als
   `zitadel-admin@opensme.auth.home.opensme.graphwiz.ai`.

Dazu `tests/05-e2e/logout.py` (Single Sign-Out) und
`tests/05-e2e/session_lifecycle.py` (Timeout, Refresh-Rotation,
Cross-Service-SSO).

### Container-Neustart (Reihenfolge)

```sh
docker compose ps postgres                 # (1) Postgres muss healthy sein
# Erst-Boot: zitadel init + setup --masterkeyFile … --steps /steps.yaml
#            (seeds Automation-PAT UND login-client.pat — siehe scripts/demo.sh)
docker compose up -d traefik zitadel       # (2) IdP zuerst, bis healthy
docker compose --profile donotstart up -d zitadel-login
                                           # (3) Login-UI — erst NACH setup
                                           #     (braucht login-client.pat);
                                           #     Live-Fleet: Host-Network +
                                           #     Traefik-Standin neu laden
scripts/zitadel-client.sh --app opencloud  # (4) Clients (idempotent, optional)
docker compose up -d                       # (5) Rest des Stacks
```

Statisch gegen Drift gesichert (frischer Checkout, ohne laufenden Stack):

```sh
python3 tests/00-static/check_oidc.py      # R1–R7: Registrierung, Login-UI,
                                           # Backchannel-Pin, Autoprovisioning,
                                           # Element server_name
```

### Webmail (SOGo6) SSO — Live-Fixes (2026-10-31)

Ticket „sogo sso not working“: sogo6 war zwar als RP kodiert, aber live fehlten
Client/Secret, der Domain-Eintrag stand auf `plain`, und die OIDC-Discovery
erreichte den IdP nicht. Der Flow ist jetzt end-to-end grün
(Status-Tabelle, Zeile Webmail). Reproduktionschronik:

1. **Zitadel-Client** — `./scripts/zitadel-client.sh --app sogo` registriert
   eine App (live: Client `397489856642613268`) und schreibt
   `SOGO_OIDC_CLIENT_ID`/`SOGO_OIDC_CLIENT_SECRET` in die `.env`, die der
   Aufrufer nutzt (live: `/opt/opensme-compose/.env`, **nicht** `compose/.env`).
   Der sogo-Branch registriert die sogo6-Callback-URI
   `{SOGO6_PUBLIC_URL}/api/user/v1/auth/callback/{OPENSME_DOMAIN}` (auch http).
2. **Domain-Einstellungen (DB)** — `opensme-sogo6-postgres`/db `sogo`,
   `sogo6_sogo_settings.settings_domain_default` → `AUTH_SETTINGS`
   (Patch im Live-SQL, Credentials aus der richtigen `.env`):
   - `SOGO_D_AUTH_TYPE=openid`, `SOGO_D_OPENID_CONFIG_URL=
     https://auth.<domain>`, `SOGO_D_OPENID_CLIENT_NAME/SECRET` = Zitadel-Client,
     `SOGO_D_OPENID_ALLOW_REDIRECT` = beide Callback-URIs.
3. **IdP-Erreichbarkeit** — DNS im Compose-Netz zeigt für `auth.<domain>` auf
   einen toten Bridge-Alias (Traefik läuft host-net). Fix wie Camunda:
   `extra_hosts: ["auth.<domain>:host-gateway"]` am Service `sogo6-server`
   (live: `compose/mail/sogo6.yml`; kein Recreate nötig wenn `/etc/hosts`
   ephemere pin nur beim nächsten Image-Rebuild wirkt — tatsächlich zwingend
   Recreate, da `/etc/hosts` read-only ist).
4. **UI-Proxy `redirect:'manual'`** — der Next-Proxy
   (`SOGo6-UI src/app/api/[[...path]]/route.ts`) folgt der Backend-302 selbst
   und verliert das `#token=`-Fragment → Browser bleibt am Callback hängen.
   Fix: `fetch(...,{redirect:'manual'})` + rohe Location-Weitergabe. Live ist
   der Hotpatch in den gebauten Chunks eingespielt
   (`_0ub05ha._.js`, `[root-of-the-server]__1up58lg._.js`); ein Image-Rebuild
   aus dem gefixten Repo ersetzt den Hotpatch dauerhaft.
5. **Verifikation** — `/api/user/v1/auth/mode?username=…` liefert `kind:"sso"`
   + authorize-URL; kompletter Browser-Flow (email → auto-redirect IdP →
   Login → Callback → `/de/u/0/INBOX`, `/api/sse` 200). **Seit 2026-10-31
   rendert die Postfach-UI vollständig** (IMAP/SMTP/Sieve-Mechs auf `xoauth2`
   gedreht, UI-Image aus Main neu gebaut, App-Passwort-Seeding etabliert —
   siehe nächster Abschnitt).

## Stalwart App-Passwörter (sogo→stalwart) — Live-Fixes (2026-10-31)

Ticket „app-passwort-seeding sogo->stalwart”: Webmail-Postfach crashte mit
„This page couldn't load”, Root-Cause war zweigeteilt — (1) falscher
Auth-Mechanismus, (2) veralteter UI-Build. Beide behoben + Seeding-Mechanik
etabliert.

**Stalwart-0.15-Fakt:** App-Passwörter werden NICHT über JMAP
(`x:AppPassword` = 0.16+), sondern über den **REST-Endpunkt
`GET/POST /api/account/auth`** verwaltet (self-service). Authentifizierung mit
`Authorization: Bearer <OIDC-Access-Token>` des Nutzers; Body ist ein flaches
Ops-Array:

```json
[{"type":"addAppPassword", "name":"<base64(label$ISO8601)>", "password":"<sha512-crypt-Hash>"}]
[{"type":"removeAppPassword", "name":"<base64(...)>"}]
```

Das Secret wird clientseitig erzeugt und als `$6$…`-Hash übergeben; `GET`
listet Namen (base64) unter `data.appPasswords[]`.

**Fixes live umgesetzt (alle 2026-10-31):**
1. **Mechanismen auf `xoauth2`** (Access-Token als XOAUTH2 statt Plain):
   Live-DB `sogo6_sogo_settings.settings_domain_default.MAIL_SETTINGS` →
   `SOGO_D_IMAP_AUTH_MECH`/`SOGO_D_SMTP_AUTH_MECH`/`SOGO_D_SIEVE_AUTH_MECH`
   alle `xoauth2`; gespiegelt in SOGo6-server-Repo
   `scripts/init/domain_settings.json`. Die Clients (`ClientImap`,
   `ClientSmtp`, `ClientSieve`) unterstützen `xoauth2` (Login als
   `user=<email>\x01auth=Bearer <token>`); Stalwart (`directory=oidc`)
   validiert das Token via Zitadel-userinfo.
2. **UI-Image `sogo6-ui:latest` neu gebaut** aus SOGo6-UI `main` (`31538d2b`):
   behebt den **`memo is not defined`-Client-Crash** (`ReferenceError` im
   Notes-Modul des Mail-Panels; Fix stammt aus origin/`8ae412be` „add missing
   memo import to NotesList and NotesEditor“) im alten 2026-10-26-Build und
   ersetzt die früheren Bundle-Hotpatches. Bau: lokal `docker build -f
   Dockerfile.prod` → `docker save|gzip` → Host → `docker load` → Compose
   `up -d --no-deps --force-recreate sogo6-ui` (netns blieb 29).
3. **Seeding-Skript** `scripts/stalwart-app-password.sh <email> [label]` (siehe
   Skript-Header): liest den OIDC-Token des Nutzers aus der SOGo6-Redis-Session
   (`user_oidc_session:<email>`), mintet Secret + `$6$`-Hash, POST
   `addAppPassword`, verifiziert `IMAP LOGIN`, gibt das Secret einmal aus.

**Live verifiziert (testuer@opensme.auth.home.opensme.graphwiz.ai):**
- `AUTH XOAUTH2` stalwart:143 → **OK**; SMTP `AUTH XOAUTH2` (587, STARTTLS) →
  `235 Authentication succeeded`; Test-Mail versendet → **Postfach zeigt
  „1 Nachricht“** (Browser-E2E, `PAGEERRORS: []`).
- App-Passwort `sogo-webmail` (bei Bedarf neu seeden via Skript) →
  `IMAP LOGIN` **OK**, `INBOX (MESSAGES 1)`; Ops `addAppPassword`/
  `removeAppPassword` → 200.

**Hinweis:** Webmail nutzt den SSO-Token (XOAUTH2); App-Passwörter sind für
direkte IMAP/SMTP-Clients (Thunderbird/Mobile). Zitadel-Access-Tokens laufen
ab — erneuter Webmail-Login verlängert die Session.

## Portal-Login (OIDC-RP) — Live-Provisionierung

Der Portal-Rust (Rust `portal/`) ist ein eigener OIDC-Client und schaltet
seinen Login **fail-closed** ab, solange eine der folgenden Env-Variablen
fehlt (`GET /api/auth/login` → 503 "login is not configured", Header zeigt
nie "Anmelden"):

- `IDP_URL` (Issuer, extern erreichbar)
- `IDP_INTERNAL_URL` (optional — server-seitige Basis für Token/Discovery;
  leer ⇒ dieselbe externe Basis)
- `PORTAL_OIDC_CLIENT_ID` / `PORTAL_OIDC_CLIENT_SECRET`
- `LOGIN_SESSION_COOKIE_SECRET` (≥32 Bytes, rotiert)

Client registrieren (idempotent, schreibt die Credentials in `.env`):

```sh
scripts/zitadel-client.sh --app portal
# → Redirect-URIs: https://<PORTAL_DOMAIN>/api/auth/callback (+ http)
#   Post-Logout:   https://<PORTAL_DOMAIN>/logged-out
```

Endpunkte:

- `GET /api/auth/login` → 303 an Zitadels `/oidc/v1/authorize`
  (**nur legacy/404**) bzw. `/oauth/v2/authorize` — der Autorize-Pfad muss
  aus der Discovery stammen; ein hartkodiertes `/oidc/v1/authorize` landet
  auf Zitadels "404 page not found" (fix: d9b7eab).
- `GET /api/session` → immer 200: `{"authenticated":false}` oder
  `{"authenticated":true,"user":{sub,name,email}}`; der Header zeigt
  anhand dessen **Anmelden** (anonym) bzw. **Abmelden** (Session).

Blockierter Container (Wedge/runc-EOF): Login live freischalten über den
Host-Standin des Rust-Binaries (`docker create` → `docker cp` → Host-Start
mit `PORTAL_PORT` + erweitertem Env; `docker cp` braucht keinen
Container-Start). Env des laufenden Containers via `docker inspect`.

## OpenCloud — Edge/Live-Fixes (2026-10-28)

Zwei Fehler machten den OC-Login unbrauchbar, beide ohne
Container-Restart behoben:

1. **CSP blockt OIDC-Discovery.** OC-web deploys eine harte
   `connect-src 'self' blob: …`-CSP (kein Config-Knopf im Image); die
   Login-Seite holt die Discovery vom IdP (`auth.home…`/
   `.well-known/openid-configuration`) → CSP-Refusal → [JsonService]
   Network Error → leere "Anmeldung erlaubt?"-Seite. Edge-Fix: Traefik-
   Datei-Router (Priority > Label-Router) auf `cloud.home…` override-t die
   CSP-Header-Zeile und erlaubt die IdP-Origin in `connect-src` (Rest
   byte-identisch). Siehe VM `/root/git/docker-traefik/dynamic/cloud-csp.yml`.
2. **OC-web braucht einen Public/PKCE-Client.** OC-web (oidc-client-ts)
   tauscht den Code **browser-seitig** mit `client_id` + `code_verifier`,
   aber **ohne** Client-Secret; ein confidencial registrierter Zitadel-Client
   antwortet `invalid_client: empty client secret` → Callback hängt auf
   `/web-oidc-callback`. Fix: Zitadel-App auf `authMethodType:
   OIDC_AUTH_METHOD_TYPE_NONE` (Public/PKCE) ziehen:
   `PUT $IDP/management/v1/projects/{pid}/apps/{aid}/oidc_config` mit
   `{"appType":"OIDC_APP_TYPE_PKCE","authMethodType":"OIDC_AUTH_METHOD_TYPE_NONE",…}`
   (v1-Flat-Body, siehe `scripts/zitadel-client.sh` für die API-/PAT-Ingredienzen;
   PAT nur via `docker compose cp zitadel:/machinekey/pat` lesbar — NULs
   strippen). Danach: Cloud-Login → Personal-Files.

Rest-Gap (bekannt, kein Edge-Fix möglich): Webmail/SOGo6 — `/api/user/v1/system`
antwortet 412 `S000001 "Sogo Has Not Been Configured Yet"`; der Server hat
keine `SOGO_LDAP_URI` und im Stack existiert kein LDAP-User-Source
(SOGo6 kann nur gegen LDAP authentifizieren).

**Lösung ohne LDAP: relationale SQL-User-Source (umgesetzt).** SOGo6-server
(„PostgreSQL user source“) authentifiziert Webmail-Logins gegen eine kleine
`users`-Tabelle **in derselben relationalen Datenbank**, die der Server
ohnehin nutzt (`opensme-sogo6-postgres`, db `sogo`; leerer
`US_SQL_USER_URL` ⇒ Single-DB-Fallback auf die App-DB). Kein zweites
System-of-Record, kein LDAP:

- `app/manager/user_source/ClientPostgreSQLUserSource.py` — implementiert
  den `ClientUserSource`-Vertrag (`check_login`): uid+password gegen
  `users`, Kontakt (`cn`/`mail`) aus derselben Zeile; fail-closed
  Passwortprüfung (`{bcrypt}` / `{pbkdf2-sha256}`, unbekannte Schemata ⇒
  Login schlägt fehl, nie ein 500); `US_SQL_DOMAIN_FIELD` für
  Domain-Filterung; Tabelle/Spalten über die User-Source-Settings.
- `ModuleUserSource` routet `postgresql` jetzt über die dedizierten
  `MAP_KEY_AUTH_*` (Adressbuch-MAP unverändert; SQL-*Directory*-Suche
  bleibt dokumentierter Stub).
- Init-Default: `scripts/init/domain_settings.json` seedet eine
  `postgresql`-User-Source (`users_table`) statt des LDAP-Beispiels —
  frische Installationen booten LDAP-frei.
- Wiring (wedge-gated, siehe Deploy): `sogo6-users-init.sql` in
  `roles/opensme_suite/files/` (idempotentes `CREATE TABLE IF NOT EXISTS
  users …`, in `mail-sogo6.yml` unter `sogo6-postgres` gemountet) + Neuanlage
  der Demo-Webmail-User über `scripts/provision-demo-users.sh --component
  sogo6` (pbkdf2-sha256, Identität `<user>@<DOMAIN>` = uid = mail =
  IdP-Email-Claim, sodass Plain-Login und Webmail-SSO eine Identität teilen).
- **Webmail-Login ist LIVE (2026-10-29 Abend):** `testuer@…`/`Eval2026` → 200
  (JWT), falsches Passwort → 401, `/api/user/v1/system` → 200 statt 412.
  Realisiert per **Hot-Patch des laufenden Containers** (runc-Keil blockte
  jeden Recreate): `git archive HEAD app scripts/init` → `docker cp` →
  Init via `docker exec … python` mit `SOGO_INIT_*`-Env → `kill -HUP 1`
  (gunicorn-Reload). Zwei Folge-Bugs dabei behoben: (1) init-JSON hatte
  `US_UID="users_table"` (Quellen-ID) statt der uid-**Spalte** `uid`
  (gefixt `f60ffdd`) — sonst „table/column missing“ → 401; (2) der
  manuell geseedete Hash war malformed (keine `$`-Separatoren) — neu
  geseedet im kanonischen `{pbkdf2-sha256}$iter$salt$digest`-Format
  (Generator: `provision-demo-users.sh` `sogo6_users_password_hash`).
  Beim Keil-Ende startet der Wedge-Watcher paperless/promo und **überspringt**
  die sogo6-Aktivierung (Skip-Guard: 200). Ein Recreate auf Image
  `f60ffdd+` ist damit optional/nachholen.
