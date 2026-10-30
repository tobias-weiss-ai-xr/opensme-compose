# Operaton (BPM) SSO with ZITADEL — no LDAP

Status: **identity plugin built + live-verified — switch packaged, wedge-gated.**

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

## Switch package (atomic; blocked by the runc wedge → no container recreate)

All pieces deploy together on the next safe recreate:

1. **ZITADEL OIDC client** (`zitadel-client.sh` pattern — Web, confidential,
   redirect `https://bpm.home.opensme.graphwiz.ai/login/oauth2/code/operaton`,
   scopes `openid profile email`, PKCE off).
2. **userlib jar**:
   `~/git/operaton-zitadel/extension-run/target/operaton-zitadel-run-1.0.0-SNAPSHOT.jar`
   → host `/opt/opensme-compose` volume `camunda-data` (= container
   `/operaton/configuration/userlib`). Named volume ⇒ survives recreates.
3. **env delta** in `services/camunda.yml` (service `camunda`):

```yaml
environment:
  # ── OAuth2 SSO (native)
  SPRING_SECURITY_OAUTH2_CLIENT_REGISTRATION_OPERATON_CLIENT_ID: ${ZITADEL_OPERATON_CLIENT_ID:?}
  SPRING_SECURITY_OAUTH2_CLIENT_REGISTRATION_OPERATON_CLIENT_SECRET: ${ZITADEL_OPERATON_CLIENT_SECRET:?}
  SPRING_SECURITY_OAUTH2_CLIENT_REGISTRATION_OPERATON_CLIENT_AUTHENTICATION_METHOD: client_secret_basic
  SPRING_SECURITY_OAUTH2_CLIENT_REGISTRATION_OPERATON_AUTHORIZATION_GRANT_TYPE: authorization_code
  SPRING_SECURITY_OAUTH2_CLIENT_REGISTRATION_OPERATON_REDIRECT_URI: "{baseUrl}/login/oauth2/code/operaton"
  SPRING_SECURITY_OAUTH2_CLIENT_REGISTRATION_OPERATON_SCOPE: "openid,profile,email"
  SPRING_SECURITY_OAUTH2_CLIENT_PROVIDER_OPERATON_ISSUER_URI: "https://auth.home.opensme.graphwiz.ai"
  SPRING_SECURITY_OAUTH2_CLIENT_PROVIDER_OPERATON_USER_NAME_ATTRIBUTE: preferred_username
  OPERATON_BPM_OAUTH2_USER_NAME_ATTRIBUTE: preferred_username
  OPERATON_BPM_OAUTH2_SSO_LOGOUT_ENABLED: "true"
  OPERATON_BPM_OAUTH2_IDENTITY_PROVIDER_ENABLED: "false"   # ← ours replaces it
  # remove: nothing else admin-user related
  # ── ZITADEL identity provider plugin
  PLUGIN_IDENTITY_ZITADEL_APIURL: "https://auth.home.opensme.graphwiz.ai"
  PLUGIN_IDENTITY_ZITADEL_PAT: ${ZITADEL_MACHINE_PAT:?}     # or clientId/clientSecret
  PLUGIN_IDENTITY_ZITADEL_USEUSERNAMEASOPERATONUSERID: "true"
  PLUGIN_IDENTITY_ZITADEL_ADMINISTRATORROLEKEY: operaton-admin
  PLUGIN_IDENTITY_ZITADEL_ADMINISTRATORGROUPNAME: camunda-admin
```

   (Spring Boot 4 / relaxed binding: `PLUGIN_IDENTITY_ZITADEL_*` binds to
   `plugin.identity.zitadel.*`; `SPRING_SECURITY_OAUTH2_*` to
   `spring.security.oauth2.*`.)
4. **Remove** any `operaton.bpm.*admin-user*` settings (current container used
   `demo` bootstrap — obsolete once SSO is live).
5. Recreate the `camunda` service (wedge-gated), then verify:

```bash
# unauthenticated REST stays reachable (login redirect on webapps):
curl -sI https://bpm.home.opensme.graphwiz.ai/operaton/app/cockpit/ | head -1   # 302
# engine user/group queries now come from ZITADEL:
curl -s https://bpm.home.opensme.graphwiz.ai/engine-rest/engine | jq .name
```

Then browser: cockpit login → redirects to ZITADEL → login as
`tweiss@…` → lands in Cockpit as Admin (via `camunda-admin` group).

## Documented gaps / follow-ups

* `/engine-rest/**` stays unauthenticated in CE — put an auth-forward (Tailscale
  / Traefik forwardAuth) in front of the engine REST API.
* Org-member roles are not mapped to groups (ZITADEL REST gap) — project-role
  grants only.
* `camunda-data` volume already contains the demo DB users (17) — they become
  invisible once the provider switches to ZITADEL (harmless, table untouched).
