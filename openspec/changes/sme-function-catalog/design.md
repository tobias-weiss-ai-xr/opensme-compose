# Design: SME Function Catalog

## Function model: what an SME does

Personas:
- **MA** — employee (day-to-day work)
- **GF** — management (approvals, oversight)
- **BO** — back office / accounting
- **SUP** — support agent
- **KUNDE** — external customer (website, shop, tickets)
- **ADM** — IT admin (infrastructure, never a landing-page persona)

Core insight: the portal is the **workspace catalog** — it makes MA/GF/BO/SUP
functions one click away. Infrastructure (ADM's world) stays invisible.
Collaborative editors are *features of the file store*, not destinations.

## Sagas → Epics → User Stories

### S1 — Arbeiten im digitalen Büro (daily work)

- **E1.1 Identität (unsichtbar)** — SSO, kein Tile.
  - US: Als MA melde ich mich einmal an und nutze alle Apps ohne erneute
    Login (**A** OIDC-Flow; auth-Zugriff nur als Redirect, nie als Tile).
- **E1.2 Dateien & Zusammenarbeit** — Tile *Cloud* (OpenCloud).
  - US: Als MA speichere, synchronisiere und teile ich Dokumente
    (**A** Teilen-Link erzeugt Cloud-Shares).
  - US: Als MA bearbeite ich Dokumente direkt im Browser — Collabora wird
    aus OpenCloud heraus geöffnet (**A** kein Collabora-Tile im Portal).
- **E1.3 Mail & Kalender** — Tile *Webmail* (SOGo).
  - US: Als MA lese und schreibe ich E-Mail und pflege Kalender/Kontakte
    (**A** Webmail-Tile → SOGo-UI; Stalwart bleibt Backend, kein Tile).
- **E1.4 Workspace-Einstieg** — Portal-Katalog.
  - US: Als MA sehe ich alle für mich freigegebenen Funktionen auf einem
    Bildschirm, deutschsprachig, mobil nutzbar (**A** nur konfigurierte
    Funktionen erscheinen; Reihenfolge stabil).

### S2 — Von Angebot zu Zahlung (quote-to-cash)

- **E2.1 Rechnungswesen** — Tile *Rechnungen* (Invoice Ninja).
  - US: Als BO erstelle ich Angebote und Rechnungen
    (**A** INVOICE_URL-Tile erscheint bei Deployment).
  - US: Als KUNDE bezahle ich online (**A** Zahlungslink aus Invoice).
- **E2.2 Belegarchiv** — Tile *Archiv* (Paperless-ngx).
  - US: Als BO archiviere ich Belege revisionssicher und finde sie per
    Volltext (**A** PAPERLESS_URL-Tile; Upload/Ablegen endet in Paperless).
- **E2.3 Rechnungsfreigabe** — Workflow (Operaton).
  - US: Als GF gebe ich Rechnungen oberhalb eines Schwellwerts frei
    (**A** BPMN `invoice-approval` — Demo-Prozess existiert bereits).

### S3 — Prozesse & Freigaben

- **E3.1 Urlaub & Abwesenheit** — Tile *Workflow*.
  - US: Als MA beantrage ich Urlaub; als GF genehmige ich ihn
    (**A** BPMN `vacation-request`; Status für MA nachvollziehbar).
- **E3.2 Generische Freigaben** — gleicher Engine-Contract (REST-only,
  siehe `bpm-engine` spec).

### S4 — Kundenservice

- **E4.1 Tickets** — Tile *Support* (Ticketing).
  - US: Als KUNDE eröffne ich ein Ticket; als SUP löse ich es
    (**A** TICKETING_URL-Tile bei Deployment).

### S5 — Sichtbarkeit nach außen

- **E5.1 Website & News** — Tile *Website* (CMS).
  - US: Als Marketing publiziere ich News; sie erscheinen auch als
    Portal-Feed (**A** CMS_URL-Tile + PORTAL_FEEDS-Kopplung).
- **E5.2 Shop** — Tile *Shop* (Store).
  - US: Als KUNDE bestelle ich; Aufträge landen im Belegarchiv
    (**A** SHOP_URL-Tile bei Deployment).

### S6 — Wissen & Zusammenarbeit

- **E6.1 Team-Notizen** — Portal-Intercom (existiert, immer sichtbar).
- **E6.2 Chat** — Tile *Chat* (Element/Synapse).
  - US: Als MA chatte ich mit dem Team (**A** CHAT_URL-Tile bei Deployment).
- **E6.3 Notizen/Ablage** — Tile *Notizen* (Impress Notes).
  - US: Als MA lege ich Link-Notizen ab (**A** NOTES_URL-Tile bei Deployment).
- **E6.4 Wissensbasis & KI** — AI-Card (**A** AI_API_URL; Frage→Antwort mit
  Quellen).

## Non-catalog (never tiled)

IdP (Zitadel/Casdoor), Stalwart mail backend, Collabora (embedded editor),
Postgres/pgbouncer/Redis/Memcached, Traefik, dev-agent/predictive-agent/
taskfleet, Prometheus/Grafana, opensme-promo apex. Rationale: no
end-user journey starts there; exposure is admin tooling.

## Interoperability matrix (saga → mechanism)

| Surface | Provider | Consumer | Mechanism | Saga |
|---|---|---|---|---|
| Identity/session | Zitadel (swap: Casdoor) | all catalog apps + portal | OIDC env namespace | S1–S6 |
| Mail out | Stalwart (SMTP submission) | OpenCloud notifications, Invoice Ninja, Paperless ingest | SMTP env wiring | S1, S2 |
| Mail read | SOGo (webmail UI) | MA via portal tile | session + tile URL | S1 |
| Files/shares | OpenCloud | Intercom attachments, Collabora editing | share-link shape `/s/…`; embedded editor | S1, S6 |
| News | CMS | portal feed columns | PORTAL_FEEDS URLs, http(s)-only | S5 |
| Process deploy/exec | Operaton | bootstrap (BPMN), portal tile, apps by process key | REST-only | S2, S3 |
| Document flow | Invoice Ninja → Operaton → Paperless | BO/GF saga | public APIs/exports + ingest | S2 |
| Health/metrics | every participant | monitoring agents | healthchecks + /metrics | ops |

Rule: every saga step rides an existing service contract (API/env), never a
portal-side bespoke integration; the portal navigates, it does not mediate
business data.

## Derivations

- **Spec** `portal-catalog`: catalog completeness, infrastructure exclusion,
  wiring contract, saga-journey executability (see specs delta).
- **Contract** `contracts/tiles.yml`: machine-readable catalog — per
  function: `url_env`, `service`, `domain_env`, `healthcheck`, plus
  `excluded:` list. This is the single source both the portal env mapping
  and the static check consume.
- **Tests**:
  - Layer 0/2 `tests/00-static/check_tiles.py`: (1) every contract entry's
    `url_env` is passed through to the portal service in compose;
    (2) if a URL is set in the active env → service must exist in the
    active chain with Traefik labels + healthcheck; (3) every user-facing
    service in the chain is either in the catalog or in `excluded`
    (completeness); (4) excluded ids never render tiles (grep-level).
  - E2E `tests/05-e2e/` saga blueprints: per saga a smoke that probes the
    participating public endpoints, skipping services that are down
    (deployment-parameterized, follows `run.py` conventions).
  - Layer 2 `tests/00-static/check_interop.py`: consumes
    `contracts/interop.yml` and validates provider wiring per declared
    consumer (OIDC env namespace present for login apps, SMTP submission
    wiring for mail senders, feed URLs http(s) + network-internal,
    share-link shape matches the configured cloud domain); missing
    provider ⇒ clean skip/report, never a broken deploy.

## Alternatives considered

- *Hardcode tiles per profile (soho/small/medium)* — rejected: profile-coupled
  config duplicates the compose chain; env-gating stays the single truth.
- *Admin tiles section (Grafana, Traefik dashboard)* — deferred: separate
  admin surface decision; not part of the SME employee catalog.
- *Tiles in a database/CMS* — rejected: portal is env-config-only by design
  (no runtime deps beyond Postgres for intercom).
