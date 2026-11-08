<!--
SPDX-FileCopyrightText: 2026 Tobias Weiss
SPDX-License-Identifier: Apache-2.0
-->

# Architektur (arc42)

openSME ist ein **Docker-Compose-Deployment** einer self-hosted Digital
Workplace für kleine und mittlere Unternehmen. Die Architektur-Dokumentation
folgt der [arc42](https://arc42.org/)-Gliederung — die normative Detailtiefe
liegt in den ausführbaren Spezifikationen ([specs/](../specs/),
[contracts/](../contracts/)) und in der Test-Pyramide ([tests/README.md](../tests/README.md)).

## 1. Einführung & Ziele

- **Ein Stack, ein Login, ein Einstiegspunkt**: Dateien, Mail, Groupware,
  Dokumente, Chat, Ticketing, Store, Videokonferenz und lokale KI —
  integriert und *integriert haltend* durch eine Contract-Test-Pyramide, die
  Builds scheitern lässt, wenn Integrationen still brechen.
- Qualitätsziele: Datenhoheit (self-hosted), geringer Ressourcenfußdruck
  (Reservation-Budgets pro Tier), Wartbarkeit (Overlays, gepinnte Images,
  ausführbare Verträge), kein Vendor-Lock-in (Apache-2.0).
- Kein Ziel: Multi-Node/Cluster-Betrieb — SME-Fokus, vertikal skaliert.

## 2. Randbedingungen

- Docker 24+/Compose v2.2+, Linux-Host, Ports 80/443, Domain mit A-Records.
- Öffentliches Repository: keine internen Hostnamen/IPs/Secrets (CI-Gates).
- Apache-2.0; Images aus öffentlichen Registries, Kern-Pins erzwungen
  (`MUTABLE_IMAGES` dokumentiert Ausnahmen).
- OpenVZ-Kernels im Lab: UDP in Bridge-Netze ist tot — vc-livekit läuft
  daher `network_mode: host`; Builds brauchen `--network host`.

## 3. Kontextabgrenzung

Externe Systeme: Let's Encrypt (TLS), HuggingFace (einmaliger Modell-Download
bei `--profile ai`), optionale externe LLM-Endpunkte (`OLLAMA_URL`).
Administratoren/Endnutzer erreichen alles über Traefik als TLS-Edge.

## 4. Bausteinsicht

Vollständige Bausteine mit Begründungen: [README — What's inside](../README.md#whats-inside)
und [README — Architecture (Mermaid)](../README.md#architecture).
Schichtenlogik der Compose-Files:

```
docker-compose.yml (Kern: Traefik, PostgreSQL, Redis, Memcached, Portal,
                    VC: livekit-server + vc-room)
  + idm/*.yml            Identität (Zitadel, alternativ Casdoor)
  + opencloud/*.yml      Files/Office + SeaweedFS-S3 (Produktion)
  + mail/*.yml           Stalwart SMTP/IMAP + SOGo Groupware
  + services/*.yml       optionale Fachdienste (Profile: workflow, invoice,
                          archive, notes, support, cms, calendar, chat,
                          surveys, store, cryptpad, paperless, ai, …)
  + monitoring/*.yml     Agents (beobachten, vorhersagen, heilen, orchestrieren)
  + profiles/*.yml       Tier- und Demo-Ressourcen Overrides (letzter gewinnt)
```

Zwei selbstgebaute Images ergänzen Upstream-Lücken (je mit `images/`-Ordner,
Patch und README): `opensme/paperless-ngx:3.3.4-oidc` (allauth-OIDC-Provider
in INSTALLED_APPS — Stock-Image lädt ihn nie) und `calrs:1.6.0` (OIDC-SSO
in der SQLite-`auth_config`, per Admin-API aktiviert).

## 5. Laufzeitsicht

Boot-Reihenfolge via `depends_on` + Healthchecks; das Portal `:8080`
antwortet sofort, Dienste kommen gesund hoch und erscheinen auf der Landing
Page. Die E2E-Journeys in [tests/05-e2e/](../tests/05-e2e/) verifizieren die
tatsächlichen Login-Durchgänge automatisiert über HTTP — inklusive der
signierten SSO-Roundtrips gegen Invoice, Paperless, Operaton und Synapse
(Epic J in `stories.py`), die denselben Zitadel-v2-Session-API-Weg nutzen wie
die Login-UI. In CI, nicht per Screenshot; Details in
[E2E-JOURNEYS.md](E2E-JOURNEYS.md).

## 6. Verteilungssicht

Ein Host, ein `opensme-net`-Bridge-Netzwerk (keine published Ports außer
80/443; Ausnahme: VC wegen OpenVZ als `network_mode: host`), benannte
Volumes pro Dienst, PostgreSQL mit per-service Rollen, PgBouncer als
Connection-Pooler. Backup: `scripts/backup.sh` (PG-Dump + Volumes, 7 Tage
Retention).

## 7. Querschnittliche Konzepte

- **SSO-first**: OIDC über Zitadel; der Portal-Proxy und alle SSO-Dienste
  teilen eine Identität. Neu provisionierte SSO-Nutzer brauchen Defaults
  pro App — bei Paperless z. B. Gruppe `oidc-users`
  (`PAPERLESS_SOCIAL_ACCOUNT_DEFAULT_GROUPS` + idempotentes
  `scripts/provision-paperless-perms.sh`), sonst 403 auf jeder API.
- **Contracts, not hope**: Specs → Contracts → Boot-Contracts → E2E; die
  statische Suite (`tests/run.py --static`) ist Build-Gate.
- **Ressourcen-Budgets**: `deploy.resources` mit CI-erzwungenen
  Reservation-Summen pro Tier.
- **Datenschutz in den Agents**: Strip-then-Review-Anonymisierung vor jedem
  LLM-Kontakt; Healing nur mit explizitem Consent.
- **Funktionskatalog (Portal-Tiles)**: Das Portal zeigt genau die
  SME-Geschäftsfunktionen (OpenCloud, Webmail, Workflow, Invoice,
  Archive, Chat, Notes, Support, Website, Shop, Calendar) — jede nur wenn
  ihre URL konfiguriert ist, keine Infrastruktur-Tiles (IdP, Collabora,
  Mail-Backend). Contract: `contracts/tiles.yml`, geprüft von
  `tests/00-static/check_tiles.py`.
- **Interoperabilität (Saga-Mechanismen)**: Jeder Saga-Schritt reitet auf
  einem deklarierten Contract — OIDC-IdM-Swappbarkeit (Zitadel ↔
  Casdoor), interne SMTP-Submission über Stalwart (Invoice, OpenCloud),
  Share-Link-Shape über Intercom-Host-Allowlist, http(s)-only Feeds mit
  Graceful Degradation, REST-only Prozess-Engine ohne geteilte DB,
  Quote-to-Cash als Komposition öffentlicher APIs (Invoice-Exports →
  BPMN-Freigabe → DMS-Ingest). Contract: `contracts/interop.yml`,
  geprüft von `tests/00-static/check_interop.py`; Saga-Smoke:
  `tests/05-e2e/saga_smoke.py`.

## 8. Architektur-Entscheidungen

Wichtige Entscheidungen sind in den Commit-Historien und OpenSpec-Archiven
nachvollziehbar; die markantesten: Zitadel statt Keycloak+OpenLDAP
(RAM/Boot-Zeit), SeaweedFS statt MinIO (upstream tot), PostgreSQL als
gemeinsame Datenbank mit per-service Rollen, Rust/Axum-Portal statt
Nginx-Static-Seite (dynamische Karten, ⌘K, Intercom, Polls, AI-Proxy),
LiveKit+vc-room statt externem VC-Dienst (Datenhoheit), calrs+Radicale
statt kommerziellem Kalender (SSO-fähig, Kalender-Öffentlichkeit per
Radicale-ACL).

## 9. Qualitätsziele

Die Qualitätsziele sind ausführbar: Layer 0–8 der Test-Pyramide
([VALIDATION.md](VALIDATION.md)) — statische Gates in CI, Live-E2E gegen
die Zitadel-Session-API (166 Stories, 0 fehlschlagend auf dem Live-Stack),
Sicherheits-Audit, Benchmarks. Portal-interne Verträge: `cargo test`
in `portal/` (173 Tests, inkl. Upload-Guards, Dateiserver-Token-Prüfung,
Serde-Default-Verhalten).

## 10. Risiken & technische Schulden

- OpenVZ: UDP in Bridge-Netze tot → VC-Host-Mode; externe UDP-Erreichbarkeit
  des LiveKit-Ports nicht überall bewiesen (TCP-Fallback dokument).
- Paperless-Stock-Image kann OIDC nicht allein → Fork-Image nötig
  (Upstream-Design, kein Konfigurationsfehler).
- Stale-Shell-Env schlägt `.env` (`cfg()` = env > dotenv) — Doku in
  [SSO.md](SSO.md) Ops-Runbook; Stories lösen Hosts env-first + Konvention
  + Erreichbarkeitsprobe auf.

## 11. Glossar

| Begriff | Bedeutung |
|---|---|
| Portal | Rust/Axum-Frontend (`opensme-portal`, :8080) — Einstieg, Tiles, Intercom, Polls, AI-Proxy |
| IdP | Zitadel (`opensme-zitadel`), OIDC-Issuer `auth.<domain>` |
| Saga | Geschäftsreise über mehrere Dienste (S1–S6), reitet auf Contracts |
| Epic / Story | E2E-Anforderungsebene in `tests/05-e2e/stories.py` bzw. den Journey-Skripten |
| Contract | ausführbare Regel in `contracts/*.yml`, geprüft von Layer-0-Checks |
| Tier | Ressourcen-Profil `profiles/{soho,small,medium}.yml` |
| PAT | Zitadel Personal Access Token (`/machinekey/pat` im Container) für Management-API |

## 12. Offene Punkte

Siehe [ROADMAP.md](ROADMAP.md) und die OpenSpec-Changes unter
[openspec/](../openspec/) — dort liegen die geplanten, noch nicht
archivierten Architektur-Schritte.
