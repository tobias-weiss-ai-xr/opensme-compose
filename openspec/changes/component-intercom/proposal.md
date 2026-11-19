# Proposal: component-intercom

## Why

Das portal-web rendert seine Komponenten (Notizen, Umfragen, News-Feeds,
Cloud-Picker, Dienste-Katalog) als isolierte Module in Tabs. Der echte
Team-Workflow — etwas lesen/sehen, ins Team bringen, diskutieren, Termin
treffen — verlangt aber, dass diese Module zusammenarbeiten. Heute muss man
Links manuell aus dem Feed/der Cloud in eine Notiz kopieren, und der
Cloud-Picker gehört fest zur Notiz-Komponente statt an jede
Anhang-Stelle. Das ist keine Kernfunktion, sondern eine Lücke des
Integrationsmusters: KEINE neue Einzel-Funktion, sondern die modulare
Klammer (registrierter Aktions-Slot + geteilter Anhang-Slot), über die
bestehende Funktionen verlustfrei verbunden werden.

Prinzip: Ein Komponenten-Bus (`lib/bridge.ts`) als schlanke, typisierte
Registry. Module registrieren Fähigkeiten (z. B. „Ins Team teilen",
„Cloud-Datei auswählen"); Host-Komponenten rendern deklarative Slots, die
gefüllt werden, wenn ein Anbieter registriert ist — ohne Anbieter
verschwindet der Slot, kein Modul importiert ein anderes (keine Zyklen,
jede Komponente bleibt isoliert testbar).

## What Changes

- **Share-to-Team-Aktion (`EPIC T`)**: Ein `ShareToTeam`-Slot, den Quellen
  (News-Feed-Artikel, Cloud-Datei/-Freigabe, später Buchung/Meeting)
  registrieren. Auslösen öffnet den Notiz-Komposer mit vorbefülltem
  Text + Link (Titel/Quelle) und postet über die bestehende Notiz-API
  (`POST /api/intercom`). Ohne registrierte Quelle (z. B. Feed leer)
  verschwindet die Aktion ersatzlos.
- **Geteilter Cloud-Anhang-Slot (`EPIC D`)**: `CloudFilePicker` wird ein
  registrierter Provider (`cloud.pick`) statt Privatteil der
  Notiz-Komponente; Notiz- UND Umfrage-Komposer nutzen denselben Slot.
  Lokale Datei-Uploads bleiben bewusst flüchtig (dokumentiertes
  Notiz-Design); neu: expliziter „In Cloud ablegen"-Knopf an einer
  hochgeladenen Notiz-Datei, der daraus eine dauerhafte Cloud-Freigabe
  macht und die Notiz auf den Cloud-Link umstellt.
- **Komponenten-Bus als Vertrag (`EPIC 0`)**: `lib/bridge.ts` mit
  typisierten Provider-Kontrakten (`share.provider`, `cloud.pick`),
  SSR-sicher, Fall-soft (kein Anbieter = unverändertes Verhalten).
  Statischer Wächter (Stil `check_tiles.py`) + Mutations-Tests binden die
  Registry-Verträge dauerhaft.
- **NICHT Teil dieser Änderung** (bewusst ausgespart, Wert-unabhängig):
  Meeting-Room-Erzeugung aus Notizen (kein Room-Create-Stack), Palette-
  Aktionen, Thread→Umfrage-Konvertierung, Super-Suche, CalRS-Buchungs-
  Karte (eigenes, spike-gated Change-«Buchung & Raum greifbar»).

## Capabilities

### New Capabilities
- `portal-intercom`: In-Portal-Komponenten-Interoperabilität — typisierter
  Provider-Bus (`lib/bridge.ts`), registrierte Aktions-Slots („Ins Team
  teilen") und der geteilte Cloud-Anhang-Slot (`cloud.pick`); jede
  Integration fall-soft ohne Anbieter, jede Komponente unabhängig testbar.

### Modified Capabilities
- (keine — bestehende Specs bleiben unverändert; die neue Capability
  ergänzt das In-Portal-Verhalten, das bisher un- spezifiziert war)

## Impact

- **Code**: `portal/web/src/lib/bridge.ts` (neu), Provider/Registrierung in
  `IntercomPanel` (Anhang-Slot, Teilen-Ziel), `FeedColumn` (Teilen-Quelle),
  `PollsPanel` (Anhang-Slot), `CloudFilePicker` (Provider), `lib/api.ts`
  (Teilen-Post bleibt die bestehende Notiz-API — kein neuer Endpoint).
- **Tests**: Vitest pro Modul mit Fake-Bus (kein Anbieter / ein Anbieter /
  Fehlerpfad); `check_bridge.py` statisch + Mutationen in
  `check_mutations.py`.
- **Betrieb**: keine neuen Dienste, keine neuen Env-Variablen; alle
  Funktionen laufen ohne Konfiguration unverändert (Fall-soft).
