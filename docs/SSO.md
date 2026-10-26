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

Stand: **2026-10-26, live verifiziert auf opensme.graphwiz.ai**.

| Dienst | Client | Status | Anmerkung |
|---|---|---|---|
| OpenCloud (Dateien/ocis) | `opencloud` (`OC_OIDC_CLIENT_ID`) | ✅ VERIFIZIERT | Headless-Beweis: authorize → v2 Session API → JWT → OCS `/ocs/v1.php/cloud/user` 200 **+** WebDAV 200 als `zitadel-admin@opensme.auth.home.opensme.graphwiz.ai`. Braucht zwingend `OIDC_TOKEN_TYPE_JWT` + Backchannel-Pin + Autoprovisioning (Abschnitte unten). |
| Chat (Element/Synapse) | `opensme-synapse` (`SYNAPSE_OIDC_CLIENT_ID`) | ✅ VERIFIZIERT | Synapse-SSO: `/login/sso/redirect` → 302 auf Zitadel-Authorize mit PKCE. Element: `base_url` **und** `server_name` sind beide `matrix.home.opensme.graphwiz.ai` — `server_name` wird im Entrypoint aus `ELEMENT_DEFAULT_SERVER_URL` abgeleitet (kein `ELEMENT_HOMESERVER_NAME`), kann also nicht desyncen. |
| Notes (Impress) | `opensme-notes` (`NOTES_OIDC_CLIENT_ID`) | 🟡 verdrahtet | Client registriert, Redirect auf den IdP funktioniert; **positiver Flow-Test (Login durchspielen) steht noch aus.** |
| Webmail (SOGo6) | `sogo-webmail` (`SOGO_OIDC_CLIENT_ID`) | ❌ nicht OIDC-verdrahtet | Registrierungs-Branch + e2e-Journey (`tests/05-e2e/webmail.py`) existieren für den Demo-Stack; auf der Live-Instanz ist SSO (noch) nicht aktiv. |
| Paperless | — | ❌ nicht OIDC-verdrahtet | Kein Client registriert. |
| Invoice (Invoice-Ninja) | — | ❌ nicht OIDC-verdrahtet | Kein Client registriert. |
| Operaton (Camunda-Nachfolger) | — | ❌ nicht OIDC-verdrahtet | Kein Client registriert. |
| Surveys (LimeSurvey) | — | ❌ nicht OIDC-verdrahtet | Kein Client registriert. |
| Portal | — (braucht keinen eigenen) | ✅ SSO aktiv | Portal-Session-Cookie (`OPENSMESESSION`) trägt das OIDC-ID-Token der IdP-Session; Logout = RP-initiated Logout an den IdP (Single Sign-Out). Verifiziert via `tests/05-e2e/logout.py` + `session_lifecycle.py`. |

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

# Auflösung prüfen: muss host-gateway (172.17.0.1) sein, NICHT 172.19.0.18
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
