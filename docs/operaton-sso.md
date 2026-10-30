# Operaton (BPM) SSO with ZITADEL — no LDAP

Status: **switch LIVE and verified end-to-end (2026-10-30)** — Cockpit/Tasklist
login via ZITADEL as admin (see *Verified* below).

Operaton runs its own OIDC SSO (native CE feature, `operaton.bpm.oauth2.*`).
What was missing for a fully ZITADEL-backed engine is the **user/group query
backend**: whom may Cockpit/Tasklist show, which groups give authorizations.
This is provided by a custom `ReadOnlyIdentityProvider` plugin:

## The plugin

Repo: [`~/git/operaton-zitadel`](../../../operaton-zitadel) (new, public-ready)
— a fork of the `operaton-keycloak` plugin architecture ported to the ZITADEL
Management API.

* **Users** ← `POST /management/v1/users/_search` (live: 16 users)
* **Groups** ← derived from **user grants** (`/users/grants/_search`): each
  project role `roleKey` becomes an Operaton group
  `zitadel-<roleKey>`; the role configured as `administratorRoleKey`
  (`operaton-admin`) maps onto the `administratorGroupName` group
  (`camunda-admin`).
* Deterministic admin mapping (no remote admin-group lookup).
* Strictly read-only: `checkPassword` always `false` (SSO only), native user
  queries rejected.
* No container HTTP/JSON dependency: JDK `HttpClient` + Gson **shaded** into
  the run jar (the operaton image ships Jackson 3, not Jackson 2).
* Build: `mvn test` (34 unit/E2E tests) + `ZITADEL_LIVE=1` probe — **proven
  against the live instance** (commits `a363415`, `b155c73`):

```
Groups: [camunda-admin]
tweiss groups: [camunda-admin]
camunda-admin members: [tweiss@opensme.auth.home.opensme.graphwiz.ai]
```

## Why Keycloak-plugin cannot be reused

`operaton-keycloak` requires Keycloak's `/admin/realms/...` API, which
ZITADEL does not implement (own management API). ZITADEL also has no queryable
group concept → groups must come from project-role grants.

## Architecture at runtime

```
Browser ──OIDC──► ZITADEL (login)                  engine queries
   │                 │ token                          │
   ▼                 ▼                                ▼
Cockpit/Tasklist ── Spring Security OAuth2  ──► ZITADEL IdentityProvider
 (webapp filter, auth only)                  (plugin: users+groups)
                                                 ▲  ▲
                                                 │  └─ authz: groups → task/process ACLs
                                                 └─ members of camunda-admin → Admin
```

* Authentication = native Operaton OAuth2 Spring Security path
  (`OAuth2AuthenticationProvider`, SSO logout) — **unchanged, reused**.
* Identity queries = the ZITADEL read-only provider.
* `operaton.bpm.oauth2.identity-provider.enabled: false` — prevents the
  native (DB-backed) identity provider from overwriting our factory.
* **No `operaton.bpm.admin-user`** — would fail against a read-only provider;
  admins come from the `operaton-admin` grant.

## Live switch (done, verified 2026-10-30)

All pieces are deployed on the host (`/opt/opensme-compose/compose`) and
rolled back into this repo.

1. **ZITADEL OIDC client** created via `scripts/zitadel-client.sh` (branch
   `operaton`): Web type, confidential, **client id `397414680370348052`**,
   token type JWT, redirects
   `https://bpm.home.opensme.graphwiz.ai/login/oauth2/code/operaton` (+http),
   post-logout root + callback; scopes `openid profile email`.  Env:
   `OPERATON_OIDC_CLIENT_ID` / `OPERATON_OIDC_CLIENT_SECRET`.
2. **userlib jar**: `operaton-zitadel-run-1.0.0-SNAPSHOT.jar` in the
   `camunda-data` volume (= container `/operaton/configuration/userlib`).
   Named volume ⇒ survives recreates.
3. **env delta** in `services/camunda.yml` (already committed here):

```yaml
environment:
  # ── OAuth2 SSO (native)
  SPRING_SECURITY_OAUTH2_CLIENT_REGISTRATION_OPERATON_CLIENT_ID: ${OPERATON_OIDC_CLIENT_ID:?}
  SPRING_SECURITY_OAUTH2_CLIENT_REGISTRATION_OPERATON_CLIENT_SECRET: ${OPERATON_OIDC_CLIENT_SECRET:?}
  SPRING_SECURITY_OAUTH2_CLIENT_REGISTRATION_OPERATON_CLIENT_AUTHENTICATION_METHOD: client_secret_basic
  SPRING_SECURITY_OAUTH2_CLIENT_REGISTRATION_OPERATON_AUTHORIZATION_GRANT_TYPE: authorization_code
  SPRING_SECURITY_OAUTH2_CLIENT_REGISTRATION_OPERATON_REDIRECT_URI: "{baseUrl}/login/oauth2/code/operaton"
  SPRING_SECURITY_OAUTH2_CLIENT_REGISTRATION_OPERATON_SCOPE: "openid,profile,email"
  SPRING_SECURITY_OAUTH2_CLIENT_PROVIDER_OPERATON_ISSUER_URI: "https://${ZITADEL_DOMAIN:?}"
  SPRING_SECURITY_OAUTH2_CLIENT_PROVIDER_OPERATON_USER_NAME_ATTRIBUTE: preferred_username
  OPERATON_BPM_OAUTH2_USER_NAME_ATTRIBUTE: preferred_username
  OPERATON_BPM_OAUTH2_SSO_LOGOUT_ENABLED: "true"
  OPERATON_BPM_OAUTH2_IDENTITY_PROVIDER_ENABLED: "false"   # ← ours replaces it
  SERVER_FORWARD_HEADERS_STRATEGY: framework                 # https behind Traefik
  # ── ZITADEL identity provider plugin
  PLUGIN_IDENTITY_ZITADEL_APIURL: "https://${ZITADEL_DOMAIN:?}"
  PLUGIN_IDENTITY_ZITADEL_PAT: ${ZITADEL_PAT:?}              # or clientId/clientSecret
  PLUGIN_IDENTITY_ZITADEL_USEUSERNAMEASOPERATONUSERID: "true"
  PLUGIN_IDENTITY_ZITADEL_ADMINISTRATORROLEKEY: operaton-admin
  PLUGIN_IDENTITY_ZITADEL_ADMINISTRATORGROUPNAME: camunda-admin
```

4. **Launcher `command`** must be set (else the OAuth2 starter jar is never
   on the classpath — no flags only enables REST+Webapps+Example):

```yaml
command: ["./operaton.sh", "--oauth2", "--rest", "--webapps"]
```

5. **Issuer reachability**: docker DNS maps `${ZITADEL_DOMAIN}` onto the
   ZITADEL container itself (its `hostname:` trick) where only `:8080/http`
   listens — discovery via `https:` fails from inside. Route the FQDN to the
   compose gateway so Traefik (host net, `:443`) terminates TLS:

```yaml
extra_hosts:
  - "${ZITADEL_DOMAIN:-auth.opensme.graphwiz.ai}:host-gateway"
```

6. **No `admin-user`** (would run a user query outside a command context →
   NPE against a read-only provider). The image bakes `default.yml` with
   `admin-user: id: demo`; an EMPTY env var still counts as "present" in
   Spring Boot 4, so removal is done at file level: `services/operaton-default.yml`
   (image-faithful copy, `admin-user` block deleted, `run.example.enabled: false`)
   is bind-mounted over the image default:

```yaml
volumes:
  - ./services/operaton-default.yml:/operaton/configuration/default.yml:ro
```

7. **Outside-command-context queries**: Operaton's webapp
   (`ContainerBasedAuthenticationFilter` → `AuthenticationUtil`) runs user AND
   group queries with no `CommandContext`. The plugin publishes a shared
   read-only session (`ZitadelIdentityProviderPlugin.SHARED_SESSION`) that
   `ZitadelUserQuery` / `ZitadelGroupQuery` fall back to when the context is
   null (covered by tests).

### Verified end-to-end (2026-10-30)

```bash
curl -sI https://bpm.home.opensme.graphwiz.ai/operaton/app/cockpit/  # 302 -> /oauth2/authorization/operaton
curl -s https://bpm.home.opensme.graphwiz.ai/engine-rest/engine       # [{"name":"default"}]
```

Browser: cockpit → ZITADEL login → `tweiss@opensme…` → lands in **Cockpit as
Administrator** (live counters: 12 running instances, 9 open human tasks,
4 process defs, 5 deployments). Admin authz comes from the `operaton-admin`
project-role grant (`tweiss`), mapped to the `camunda-admin` group.

To grant BPM admin to another user: give it the `operaton-admin` role in the
`opensme` project (ZITADEL grant, `grantOrgId` = openSME org).

## Documented gaps / follow-ups

* `/engine-rest/**` stays unauthenticated in CE — put an auth-forward (Tailscale
  / Traefik forwardAuth) in front of the engine REST API.
* Org-member roles are not mapped to groups (ZITADEL REST gap) — project-role
  grants only.
* `camunda-data` volume already contains the demo DB users (17) — they become
  invisible once the provider switches to ZITADEL (harmless, table untouched).
