<!--
SPDX-FileCopyrightText: 2026 Tobias Weiss
SPDX-License-Identifier: Apache-2.0
-->

# Agentic AI in openSME — Sagas, Epics, Stories, Specs, Contracts, Tests

Agents sind keine Features, sie sind **Mitarbeiter mit privilegierten
Werkzeugen**. Deshalb folgt ihre Integration in openSME derselben Disziplin
wie die menschlichen Journeys: jede agentic Fähigkeit hängt an einer Saga
(Geschäftsbogen), wird als Epic geliefert, als User Story verifizierbar
formuliert, als OpenSpec-Capability festgeschrieben, per Contract
maschineprüfbar gemacht und auf allen Testschichten bewiesen.

**Nicht-Absichten:** kein Agent erhält freie Schreibrechte auf
Geschäftsdaten; kein Agent versendet, löscht oder commitet ohne
Mensch-Entscheid; keine Daten verlassen den Stack unanonymisiert (dev-agent
spec: strip-then-review).

---

## 1. Agent-Landschaft (Ist-Zustand)

| Agent | Rolle | Schreibmodus | Spec/Code |
|-------|-------|--------------|-----------|
| dev-agent (Go) | reaktiv: unhealthy-Erkennung, LLM-Root-Cause (opt-in, anonymisiert), Consent-gated Healing | `none` → `consent` | `openspec/specs/dev-agent*`, `internal/` |
| dev-maintenance-bot | Runbook-KB (`opensme-knowledge/*.json`), `/status /heal /diag` via pi-Extension | `consent` | `monitoring/dev-agent.yml`, `tests/00-static/check_agent.py` |
| predictive-agent | Kalman/Markov-Risk-Scoring (Vorhersage, kein Eingriff) | `none` | ROADMAP „Monitoring agents“ |
| Portal-Assistant | lokales llama.cpp (`--profile ai`), degradiert unsichtbar | `none` (read-only) | Epic D + O, `services/ai.yml` |
| taskfleet | orchestrierter Repo-Worker (isolierte Worktrees, Acceptance-Gates, Merge erst grün) | `approved` (via CI) | Taskfleet-Orchestrator, `openspec`-Workflow |

Was fehlt (und dieses Dokument strukturiert): ein gemeinsames **Contract-File**,
die **Saga-/Compensation-Disziplin für Agent-Writes**, RAG-Vertrag für
Wissen, Prompt-Injection-Tests, ein Audit-Vertrag je Agent-Aktion.

---

## 2. Agentic Sagas (AS1–AS6)

Analog zu den SME-Sagas (S1–S6, `E2E-JOURNEYS.md`) — hier fährt ein Agent
mit, der Mensch bleibt der Entscheider.

| Saga | Journey | Agents | Compensation (Saga-Pattern) |
|------|---------|--------|------------------------------|
| **AS1 Agentic operations** | Stack wird krank → Agent erkennt, diagnostiziert (Root-Cause, anonymisiert), schlägt Heal vor → Operator bestätigt → Heal läuft → Audit-Eintrag beweist alles | dev-agent, predictive-agent | Heal-Aktionen sind idempotent und read-only bis Consent; jeder Write wird protokolliert und ist rückrechenbar (z. B. recreate → vorheriger Zustand im Audit) |
| **AS2 Agentic support** | Ticket landet im Helpdesk → Agent entwirft Klassifikation + Antwort-Entwurf → Mensch approved → erst dann wird geantwortet | ticketing + LLM | Entwurf bleibt Draft bis Mensch-Commit; Verwerfen ist der Normalfall |
| **AS3 Agentic processes** | BPMN-Prozess (S2 Quote-to-cash) enthält AI-Service-Task (Entwurf/Prüfung) → Human-Approval-Task → Saga-Ziel | BPM-Engine + LLM | Approval-Task gate’t den Side-Effect; Fehlschlag des Agent-Tasks kompensiert wie ein normaler Prozessfehler (BPMN-Boundary) |
| **AS4 Agentic knowledge** | Frage im Portal → RAG über `opensme-knowledge` + seed-content → Antwort **mit Quellen**; ohne Quelle: „nicht belegt“ | Portal-Assistant | read-only; Halluzinations-Guard: quellenlose Antworten sind markiert, nicht fluß |
| **AS5 Agentic documents** | Upload in Paperless → Agent entwirft Klassifikation/Tags → Mensch bestätigt Metadaten | paperless + LLM | Metadaten bleiben Draft bis Confirm; Dokumentinhalt wird nie verändert |
| **AS6 Agentic self-maintenance** | Agent arbeitet am Repo (taskfleet/openspec) → Änderung nur als Commit mit grünen Contract-Tests → Deploy bleibt Operator-Entscheid | taskfleet, dev-agent-extensions | Merge-Gate = Testpyramide; kein Push/Deploy ohne Operator |

---

## 3. Epics & User Stories

Neue Epic-Linie (Fortsetzung von A–AC in `E2E-JOURNEYS.md`). Stories im
üblichen Stil; Akzeptanzkriterien sind direkt testbar.

### Epic AD — Agent guardrails: Macht braucht Zaun

| ID | User Story | Akzeptanzkriterium (Test) |
|----|-----------|---------------------------|
| AD1 | Als **Operator** will ich je Agent definierte Tool-/Service-Allowlists, damit kein Agent Dienste berührt, die er nicht braucht | `contracts/agents.yml` je Agent `tools`-Liste; Layer-0 prüft compose-Wiring dagegen |
| AD2 | Als **Operator** will ich Token-/Anfrage-Budgets je Agent, damit ein Loop mich nicht ruiniert | Budget-Env je Agent (`*_BUDGET_*`); bei Überschreitung: Agent pausiert selbst, Audit-Eintrag |
| AD3 | Als **Operator** will ich einen Kill-Switch pro Agent (`*_ENABLED=0`), damit ich einen Agent ohne Redeploy stilllegen kann | Flag-Test: aus → Agent loggt Disabled und tut nichts |

### Epic AE — Agent privacy: raus geht nur Anonymisiertes

| ID | User Story | Akzeptanzkriterium |
|----|-----------|--------------------|
| AE1 | Als **Datenschutzverantwortlicher** will ich beweisen, dass Secrets/Private-IPs/Hostnamen/User-Pfade jeden Outbound-Payload entfernt werden | Mock-LLM protokolliert Requests; Layer-6-Test: outbound enthält kein Secret/IP-Pattern (strip-then-review, dev-agent-privacy spec) |
| AE2 | Als **Operator** will ich Prompt-Injection-Angriffe gegen Agent-Eingaben (Tickettexte, Dateinamen) abgewehrt sehen | Hostile-Input-Korpus mit Injection-Patterns (`ignore previous`, fake tool output) → Agent verweigert/entwertet, nichts wird ausgeführt |

### Epic AF — Agent sagas: Write heißt idempotent + kompensierbar

| ID | User Story | Akzeptanzkriterium |
|----|-----------|--------------------|
| AF1 | Als **Operator** will ich, dass jede Agent-Schreibaktion einen Audit-Eintrag mit before/after hat, damit jede Änderung rückrechenbar ist | `agent_saga.py` E2E: Heal-Aktion → Audit-Zeile existiert, before-state konserviert |
| AF2 | Als **Prozess-Verantwortlicher** will ich, dass AI-Service-Tasks in BPMN fehlschlagen können, ohne die Saga zu reißen | BPMN-Boundary-Fehlschlag → Kompensationspfad läuft (Saga-S2-Smoke erweitert) |
| AF3 | Als **Operator** will ich Agent-Consent als explizite Aktion (API/Button), nie als Default | Consent-Default überall `false`; Test: ohne Consent wird kein Write ausgeführt (gerade dev-agent `allow-heal=false`) |

### Epic AG — Agent knowledge: Antworten mit Beleg

| ID | User Story | Akzeptanzkriterium |
|----|-----------|--------------------|
| AG1 | Als **Nutzer** will ich Antworten mit Quellenangabe, damit ich prüfen kann, worauf sich die Aussage stützt | RAG-Antwort ohne Treffer → „nicht belegt“ statt Erfindung |
| AG2 | Als **Datenschutzverantwortlicher** will ich, dass der Index PII-frei gebaut wird | Index-Build strippt PII (Test: Personenbezug im Index-Query-Log nicht auffindbar) |

### Epic AH — Agent observability: was tat der Agent?

| ID | User Story | Akzeptanzkriterium |
|----|-----------|--------------------|
| AH1 | Als **Operator** will ich je Agent Aktionen, Kosten (Tokens) und Entscheidungen abfragbar | `/status`-Erweiterung (pi-Extension) zeigt Aktionen + Budgetverbrauch |
| AH2 | Als **Auditor** will ich eine lückenlose Agent-Historie über Restarts | Audit-Log auf Volume, Append-only, in `backup.sh` enthalten |

---

## 4. Specs (OpenSpec-Capabilities)

Existierend: `dev-agent`, `dev-agent-compose`, `dev-agent-extensions`,
`dev-agent-knowledge`, `dev-agent-privacy`, `agent-guardrails`,
`agent-knowledge`, `agent-assistant`, `agent-saga`, `agent-self-maintenance`
(AS6-Gate = Opt-in, nicht default: nur mit `AGENTIC_STRICT=1` erzwingend; sonst
dokumentierend und nie rot — `tests/00-static/check_agentic.py`, `make agentic-gate`),
`agent-feedback` (HITL-Sichtbarkeit `GET /drafts`, Ablehnung mit Grund als
first-class Outcome, abfragbares `GET /feedback`-Aggregat, Major-Flag,
Loop-Closure: aktuelle Rejections fließen anonymisiert in den LLM-Kontext).

## 5. Contract — `contracts/agents.yml`

Vorhanden, analog `contracts/tiles.yml`: je Agent ein Eintrag
`name, compose_service, tools[], write_mode(none|draft|consent|approved),
consent_env, kill_switch_env, privacy{strip[]}, compensation{on_failure},
invariants[]`. Statischer Check (`check_agent.py`) validiert: Contract ↔
compose (env-Flags verdrahtet, consent default false, Mounts read-only,
keine Ports, one-shot für Consent-Writer) ↔ Specs. *Kein Agent ohne
Contract-Zeile.*

## 6. Tests (Pyramide je Epic)

| Layer | Was | Wo |
|-------|-----|----|
| 0 static | agents.yml-Konsistenz, dev-agent-Wiring (read-only docker.sock, restart no), KB-Schema | `check_agents.py` (erweitern) |
| 4/5 e2e | Offline: `ai_mock.py`; live lokal: `ai_journey.py` (Epic O) | bestehend |
| 5 e2e neu | `agent_saga.py`: AS1-Krank→Diagnose→Consent→Heal→Audit; AS2-Draft-Flow am Mock; Injection-Versuche | neu |
| 6 security | Outbound-Hygiene (AE1), Management-Exposure der Agent-APIs | `audit.py` erweitern |
| 7 bench | LLM-Latenz/Tokenbudget je Journey gegen Budget | `run_bench.py` Erweiterung |

## 7. Reihenfolge

1. ✅ `contracts/agents.yml` + Layer-0-Erweiterung (5 Agenten unter
   Guardrails: dev-agent, dev-maintenance-bot, predictive-agent, taskfleet,
   portal-assistant — Sabotage-geprüft).
2. ✅ `agent-guardrails`-Spec + Budget-Enforcement im dev-agent
   (Go): `DEV_AGENT_LLM_MAX_REQUESTS` per-run Outbound-Cap, Cache-Hits
   budgetfrei, Pause + evidence kind `budget` + Metrik bei Exhaust,
   Consent-Defaults off; Contract/Compose/.env verdrahtet, Layer-0 prüft
   Budget-Env.
3. ✅ `agent_saga.py` (AS1 E2E, `tests/05-e2e/agent_saga.py`, eigener
   CI-Step): fährt serve-mode-Bot + Mock (fehlschlagender Healthcheck)
   isoliert hoch, treibt die private API per docker exec — Finding
   (krank→Diagnose), Dry-run ohne Consent, echter Restart mit Consent,
   Evidence kind heal (audit), heals_total-Metrik. 11/11 Checks grün,
   teardown räumt die opensme-saga-* Container ab.
4. ✅ `agent-knowledge`/`agent-assistant` (RAG-Vertrag, Quellenpflicht):
   jede KB-Datei trägt `kb_version` (Aktualität — Loader verweigert Start
   sonst), `knowledge.Match` führt `source`+`kb_version` (source duty,
   im `/status`-JSON + PrintStatus sichtbar), Layer-0 erzwingt beides +
   PII-Freiheit + `contracts/knowledge.yml`-Regeln; 2 neue OpenSpec-Specs
   (`agent-knowledge`, `agent-assistant`). 16/16 Specs valid.
5. ✅ Generisches Draft→Approval→Commit-Gate für AS2/AS3/AS5:
   `agent-saga`-Capability + `contracts`-Semantik, am dev-agent real
   (`/draft` legt inerten Draft an, `approved:false`; `/draft/{id}/approve`
   commitet mit `kind:"commit"`-Audit; jede Stufe evidencegeloggt).
   `agent_draft_saga.py` beweist es e2e (B1–B6, 7/7 grün): Draft bleibt
   inert bis Approval, Verwerfen ist Normalfall. Fachliche Anbindung an die
   drei Empfänger (Nosdesk/Camunda/Paperless) bleibt je System ein
   eigener Turn — das Gate selbst ist bewiesen.
6. ✅ AS6 Agentic self-maintenance — Repo-Arbeits-Governance als
   **Opt-in-Fähigkeit (nicht default)**: `agent-self-maintenance`-Capability
   + `tests/00-static/check_agentic.py` + `make agentic-gate`. Merge-Gate =
   Testpyramide (jeder e2e-Journey ∈ `ci.yml` ∪ `mailcow.yml`, kein
   Phantom-Verweis). Änderung nur als Commit mit grüner Pyramide; Push/Deploy
   bleibt Operator-Entscheid (dieser gesamte Ablauf beweist es de facto).
   Verhalten: `AGENTIC_STRICT` ungesetzt/0 → dokumentierend, Exit immer 0
   (bricht nie, reguläre Layer-0-Suite unberührt, nicht in `run.sh`);
   `AGENTIC_STRICT=1`/`--strict` → erzwingt, Exit 1 bei Verstoß (Allowlist
   für Support-Module + dokumentierte Nicht-CI-Journeys). 18/18 Specs valid.
7. ✅ Feedback-Loop + HITL für Major-Entscheidungen (`agent-feedback`):
   `GET /drafts` (der Mensch sieht, worüber er entscheidet),
   `POST /draft/{id}/reject` mit Pflicht-Grund (Ablehnung ist final —
   approven einer abgelehnten Draft → 409, nie commit; Grund bleibt auf der
   Draft, nie im Evidence-Log), `GET /feedback`-Aggregat (Counts + Gründe,
   neueste zuerst), `major:true`-Flag (auditiert als "(major)"), und die
   Schleife schließt: die 3 letzten Rejections fließen anonymisiert in den
   LLM-Prompt (Teil des Cache-Keys → neues Feedback invalidiert). E2E:
   `agent_draft_saga.py` B1–B6 + C1–C6 (13/13 grün). 19/19 Specs valid.
