<!--
SPDX-FileCopyrightText: 2026 Tobias Weiss
SPDX-License-Identifier: Apache-2.0
-->

# Architektur (arc42)

openSME ist ein **Docker-Compose-Deployment** einer Self-hosted Digital
Workplace für kleine und mittlere Unternehmen. Die Architektur-Dokumentation
folgt der [arc42](https://arc42.org/)-Gliederung in Kurzform — die
normative Detailtiefe liegt in den ausführbaren Spezifikationen
([specs/](../specs/), [contracts/](../contracts/)) und im
[Test-Pyramide-README](../tests/README.md).

## 1. Einführung & Ziele

- **Ein Stack, ein Login, ein Einstiegspunkt**: Dateien, Mail, Groupware,
  Dokumente, Chat, Ticketing, Store und lokale KI — integriert und
  *integriert haltend* durch eine Contract-Test-Pyramide, die Builds
  scheitern lässt, wenn Integrationen still brechen.
- Qualitätsziele: Datenhoheit (self-hosted), geringer Ressourcenfußdruck
  (Reservation-Budgets pro Tier), Wartbarkeit (Overlays, Pinned Images,
  ausführbare Verträge), kein Vendor-Lock-in (Apache-2.0).
- Kein Ziel: Multi-Node/Cluster-Betrieb — SME-Fokus, vertikal skaliert.

## 2. Randbedingungen

- Docker 24+/Compose v2.2+, Linux-Host, Ports 80/443, Domain mit A-Records.
- Öffentliches Repository: keine internen Hostnamen/IPs/Secrets (CI-Gates).
- Apache-2.0; Images aus öffentlichen Registries, Kern-Pins erzwungen
  (`MUTABLE_IMAGES` dokumentiert Ausnahmen).

## 3. Kontextabgrenzung

Externe Systeme: Let's Encrypt (TLS), HuggingFace (einmaliger Modell-Download
bei `--profile ai`), optionale externe LLM-Endpunkte (`OLLAMA_URL`).
Administratoren/Endnutzer erreichen alles über Traefik als TLS-Edge.

## 4. Bausteinsicht

Vollständige Bausteine mit Begründungen: [README — What's inside](../README.md#whats-inside)
und [README — Architecture (Mermaid)](../README.md#architecture).
Schichtenlogik der Compose-Files:

```
docker-compose.yml (Kern: Traefik, PostgreSQL, Redis, Memcached, Portal)
  + idm/*.yml            Identität (Zitadel, alternativ Casdoor)
  + opencloud/*.yml      Files/Office + SeaweedFS-S3 (Produktion)
  + mail/*.yml           Stalwart SMTP/IMAP + SOGo Groupware
  + services/*.yml       optionale Fachdienste (Profile)
  + monitoring/*.yml     Agents (beobachten, vorhersagen, heilen, orchestrieren)
  + profiles/*.yml       Tier- und Demo-Ressourcen Overrides (letzter gewinnt)
```

## 5. Laufzeitsicht

Boot-Reihenfolge via `depends_on` + Healthchecks; Portal `:8080` antwortet
sofort, Dienste kommen gesund hoch und erscheinen auf der Landing Page.
E2E-Journeys (SSO-Login, Session-Wiederverwendung, Logout) sind automatisiert
in [tests/05-e2e/](../tests/05-e2e/run.py) verifiziert — in CI, nicht per
Screenshot.

## 6. Verteilungssicht

Ein Host, ein `opensme-net`-Bridge-Netzwerk (keine published Ports außer
80/443), benannte Volumes pro Dienst, PostgreSQL mit per-service Rollen,
PgBouncer als Connection-Pooler. Backup: `scripts/backup.sh` (PG-Dump +
Volumes, 7 Tage Retention).

## 7. Querschnittliche Konzepte

- **SSO-first**: OIDC über Zitadel; der Portal-Proxy und alle SSO-Dienste
  teilen eine Identität.
- **Contracts, not hope**: Specs → Contracts → Boot-Contracts → E2E; die
  statische Suite (`tests/run.py --static`) ist Build-Gate.
- **Ressourcen-Budgets**: `deploy.resources` mit CI-erzwungenen
  Reservation-Summen pro Tier (soho 6G / small 20G / medium 40G).
- **Datenschutz in den Agents**: Strip-then-Review-Anonymisierung vor jedem
  LLM-Kontakt; Healing nur mit explizitem Consent.
- **Funktionskatalog (Portal-Tiles)**: Das Portal zeigt genau die
  SME-Geschäftsfunktionen (OpenCloud, Webmail, Workflow, Invoice,
  Archive, Chat, Notes, Support, Website, Shop) — jede nur wenn ihre
  URL konfiguriert ist, keine Infrastruktur-Tiles (IdP, Collabora,
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
Nginx-Static-Seite (dynamische Karten, ⌘K, Intercom, AI-Proxy).
