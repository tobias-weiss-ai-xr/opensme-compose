# Design: component-intercom (portal-intercom capability)

## Context

Siehe `proposal.md` (Warum) und `specs/portal-intercom/spec.md` (Was). Die
portal-web-Komponenten existieren bereits und sind einzeln getestet
(`IntercomPanel`, `FeedColumn`, `PollsPanel`, `CloudFilePicker`,
`CloudFileBrowser`, `PortalShell`). Sie kommunizieren heute nur über
lokalen State und eigenständige `lib/api.ts`-Aufrufe; es gibt keinen
Quer-Mechanismus. Die Notiz-API (`POST /api/intercom` mit `text` +
`attachment_url`) ist die bestehende Schreibfläche für „Team teilen" —
kein neuer Endpoint nötig. Der Cloud-Freigabe-Pfad (`postCloudShare`) ist
ebenfalls vorhanden.

## Goals / Non-Goals

**Goals:**
- Ein typisierter Provider-Bus (`lib/bridge.ts`), über den Slots
  („Teilen", „Cloud-Picker") gefüllt werden — ohne Quer-Imports.
- Notiz-Komposer als einziges Schreibziel für geteilte Inhalte.
- `CloudFilePicker` wird ein registrierter `cloud.pick`-Provider; Notizen
  und Umfragen konsumieren denselben Slot.
- „In Cloud ablegen" als explizite, nutzerangestoßene Umstellung einer
  flüchtigen Upload-Datei auf eine dauerhafte Cloud-Freigabe.
- Statischer Wächter + Mutationen pin Flag Force die Registry-Verträge.

**Non-Goals:**
- Keine neuen Backend-Endpoints, keine neuen Env-Variablen, keine neuen
  Dienste.
- Kein automatisches Persistieren aller Anhänge (Design der Notiz: bewusst
  flüchtig).
- Keine CalRS-Buchungs-Karte, keine Meeting-Erzeugung, keine Palette-
  Aktionen, keine Suche (Backlog, siehe Proposal „NICHT Teil").
- Kein Umbau bestehender Tabs/Layouts; beide Slots sind additive
  Erweiterungen der betroffenen Komponenten.

## Decisions

### 1. Registry statt Event-Bus oder zentralem Store
`lib/bridge.ts` exponiert `registerProvider(slot, provider)` und
`useSlot(slot)` (React-Hook) bzw. `getProvider(slot)`. Provider sind
**reine Daten + Render-Callables** (label, testId, renderer), nie
seiteneffektbehaftete Module.
- Alternativen: (a) globaler Event-Emitter — schwer testbar, kein
  SSR-sicherer Render-vertrag; (b) Kontext-Tree zentral im PortalShell —
  koppelt jeden Host an eine Baumfeste; (c) direkte Props von page.tsx an
  alle Komponenten — wäre bei 14 Tiles/Wachstum eine Props-Explosion.
  Registry ist bewusst die dokumentierte, Rest-wertlose Mitte: jeder Host
  konsumiert deklarativ, jeder Anbieter registriert lokal, Tests nutzen
  einen Fake-Bus.

### 2. Host rendert Slots deklarativ, Anbieter ist optional
`IntercomPanel` verwendet `<ActionSlot kind="share.prefill">` gegenüber
einem `<ShareToTeamTrigger source={...}/>`-Wrapper; ohne registrierten
Anbieter rendert der Slot `null`. Der Teilen-Fluss wird als
**melde-dich-an-vom-Host** umgesetzt: Host (z. B. `FeedColumn`) ruft über
den Slot `share.open(quellDaten)` auf; der geslotete `IntercomPanel`-
Kontrakt bekommt `open({ text, url, attachment_url? })` und füllt seinen
bestehenden Komposer-State. Dadurch bleibt die Notiz-Komponente die
einzige Stelle, die `POST /api/intercom` spricht.
- Trade-off: Host-Interkom lief andersherum (Notiz pusht in Feed) wäre
  falsche Kopplung — Teilen ist die natürliche Richtung (Quelle → Team).

### 3. `cloud.pick` als eigener Slot, `CloudFilePicker` als Anbieter
`CloudFilePicker` wird aus `IntercomPanel` in ein eigenes Anbieter-Modul
extrahiert (Renderer bleibt derselbe, testids unverändert). Der Picker
rendert ein Modal und liefert das gewählte Cloud-Entry per Callback;
Anbieter registriert sich unter `cloud.pick`. Verbraucher (`IntercomPanel`,
optional `PollsPanel`) rufen `useSlot("cloud.pick")` und rendern einen
Triggser-Knopf. Der bisherige URL-Eingabe-Fallback bleibt in beiden
Verbrauchern erhalten (Spec: „Cloud provider absent").
- Warum Umfragen: nur wenn Polls tatsächlich Anhänge akzeptieren,
  sonst reicht der Notiz-Nutzer, um die Abstraktion zu beweisen. Task
  markiert das als bedingten Schritt (Spec-konform, da „inkl. weiterer
  Verbraucher" optional formuliert).

### 4. „In Cloud ablegen" nutzt den bestehenden Share-Pfad
An der hochgeladenen Datei einer Notiz erscheint ein Knopf; die
Handler-Kette ist identisch zum bestehenden `onCloudPick`-Pfad
(`postCloudShare(path)` → `attachment_url`), nur dass der Quellpfad aus
der transienten Upload-Referenz stammt (Upload → Kopie in den
DAV-Root → Share). Die Notiz wird im lokalen State auf den Cloud-Link
umgestellt und der flüchtige Upload-Rest unverändert gelassen.
- Trade-off: Kopieren statt Verschieben — der transiente Upload bleibt
  bis zum FIFO-Cap bestehen; Konsistenzrisiko gering (Notiz zeigt nur
  noch den Cloud-Link).

### 5. Statischer Wächter analog `check_tiles.py`
`tests/00-static/check_bridge.py`: parst `lib/bridge.ts` (Slot-Definitionen,
Registrierungspunkte) und `IntercomPanel`/`FeedColumn`/`PollsPanel`
(Slot-Nutzung, Teilen-Wiring, Fallback-Präsenz); Fehlschlag = RED.
Zusätzlich eine Mutation je Integration in `check_mutations.py`
(z. B. Slot-Name-Konstante ändern → Wächter schlägt). Kein neuer Test-
Framework — vorhandene Muster wiederverwendet.

## Risks / Trade-offs

- **[Slot wird nie konsumiert / toter Code]** → Wächter zählt
  Nutzungsstellen; unkonsumierte Slot-Definitionen sind RED.
- **[Teilen-API-Drift (Notiz-Payload ändert sich)]** → Teilen-Post läuft
  über die bestehende Notiz-API-Funktion (`postIntercom`); der Wächter
  pinnt, dass der Teilen-Fluss genau diese Funktion nutzt.
- **[Cloud-Picker-Regression bei Extraktion]** → testids/Snapshots der
  Notiz-Komponente bleiben gültig; Extraktion ist mit den vorhandenen
  Vitest-Tests abgesichert (RED→GREEN).
- **[„In Cloud ablegen" schlägt fehl (Auth/DAV)]** → Fehlerpfad zeigt
  Inline-Meldung, Notiz bleibt unverändert (bestehendes Muster aus
  `onCloudPick`).

## Migration Plan

- Rein additiv: `lib/bridge.ts` ist neu; bestehende Komponenten erhalten
  optionale Slots. Rollback = Commit-Revert; keine Datenmigration, keine
  Service-Neustarts. Deploy wie üblich: portal-web neu bauen
  (buildx `--network host`), `--force-recreate portal-web` **nach** `portal`
  (netns-merge), Router-IP ggf. refreshen.

## Open Questions

- Soll `PollsPanel` Anhänge grundsätzlich unterstützen? (Entscheidung
  verzögert bis nach dem Notiz-Nutzer; Spec formuliert es bedingt —
  Task markiert den Schritt entsprechend).
