# Tasks: component-intercom

## 1. Komponenten-Bus als Vertrag (RED zuerst)

- [ ] 1.1 `portal/web/src/lib/bridge.test.ts` (RED): Registry-Vertrag —
      kein Anbieter ⇒ Slot rendert `null`; ein Anbieter ⇒ Affordance
      erscheint; Doppel-Registrierung überschreibt mit Warnung;
      SSR-sicher (kein `window`/`document` am Modul-Top-Level) — Tests
      müssen gegen `src/lib/bridge.ts` fehlschlagen
- [ ] 1.2 `portal/web/src/lib/bridge.ts` (GREEN): typisierte Slot-Namen
      (`share.open`, `cloud.pick`), `registerProvider(slot, provider)`,
      `useSlot(slot)`, getrennte Render-/Data-Kontrakte, kein Client-only
      Import — Suite grün
- [ ] 1.3 `tests/00-static/check_bridge.py` (RED→GREEN): parst
      `lib/bridge.ts` (Slot-Definitionen), zählt Konsumentenzeilen je Slot
      (ungennutzte Slot-Definitionen ⇒ FAIL), pinnt „Teilen-Post nutzt
      `postIntercom`" und „URL-Fallback bleibt in Notiz-Komposer"; Fehler
      vor Fix, grün danach; in `tests/run.py` registriert (Coverage-
      Vertrag Layer 0)
- [ ] 1.4 Mutationen in `check_mutations.py`: Slot-Konstante umbenennen
      und Konsumentenzeile entfernen ⇒ Wächter schlägt (RED), 40+ Suite
      wieder grün

## 2. Share-to-Team (EPIC T, RED zuerst)

- [ ] 2.1 `IntercomPanel` erhält prefill-Kontrakt (`openSharePayload({text,
      url, attachment_url?})`): RED-Test — fake Provider ruft Slot auf,
      Komposer öffnet mit Titel+Link vorbefüllt; Tab-Umschaltung in den
      Notizen-Tab über die Shell (PortalShell steuert Tabs, reicht Payload
      als Prop weiter — kein Peer-Import)
- [ ] 2.2 `FeedColumn`: Artikelkarte bietet „Ins Team teilen" (Provider
      `share.open` mit Titel+URL), nur wenn ein Konsument-Slot bedient
      wird; RED-Test (ohne Konsument kein Knopf), dann GREEN
- [ ] 2.3 Cloud-Datei als Teilen-Quelle (EPIC-T-T2): `CloudFilePicker`/
      `CloudFileBrowser`-Eintrag „Ins Team teilen" mit `attachment_url` =
      Cloud-Freigabelink; RED-Test, GREEN; Notiz trägt den Link als Anhang
- [ ] 2.4 Fehlerpfad Teilen: Notiz-API lehnt ab (abgelaufene Session) ⇒
      Inline-Fehler, vorbefüllter Komposer-Inhalt bleibt erhalten;
      RED-test, GREEN

## 3. Geteilter Cloud-Anhang-Slot (EPIC D, RED zuerst)

- [ ] 3.1 Extraktion: `CloudFilePicker` wird eigenes Anbieter-Modul
      (Provider `cloud.pick`; Renderer/testids unverändert) — bestehende
      Notiz-Tests bleiben grün (RED→GREEN Umbau abgesichert)
- [ ] 3.2 `IntercomPanel` konsumiert `useSlot("cloud.pick")` statt
      direktem Import; RED-Test mit fake Anbieter, GREEN; URL-
      Eingabe-Fallback bleibt (Wächter 1.3 pinnt das)
- [ ] 3.3 `PollsPanel`: prüfen, ob Umfragen Dateianhänge akzeptieren —
      falls ja: denselben `cloud.pick`-Slot konsumieren (+ RED-Test);
      falls nein: Konsument entfällt, Kern (Provider+Notiz) bleibt
      (Entscheidung im Task kommentieren, Spec als bedingt formuliert)
- [ ] 3.4 „In Cloud ablegen" an hochgeladener Notiz-Datei: Knopf ⇒
      `postCloudShare`-Pfad (Kopie in DAV-Root → Freigabelink), Notiz
      wechselt den Anhang auf den Cloud-Link; Fehlerpfad Inline-Meldung,
      Notiz unverändert; RED-Test, GREEN

## 4. Integration & Qualität

- [ ] 4.1 Gesamt-Suiten grün: `bunx vitest run` (portal/web, komplett),
      `bun run lint` (0 Fehler), `python3 tests/run.py --static`
      (inkl. `check_bridge.py`), `python3 tests/00-static/check_mutations.py`
      (inkl. 1.4-Mutationen)
- [ ] 4.2 Doku-Drift prüfen: docs-Index/Architektur-Absatz zu `lib/bridge.ts`
      und den beiden Slots („modular integration") in Kürze ergänzen —
      Docs sind Norm (Drift = Bug)
- [ ] 4.3 Commit/Push (R22-Präfix) + VPS-Deploy: portal-web bauen
      (buildx `--network host`), `--force-recreate portal-web` NACH
      `portal` (netns-merge! Router-IP bei Bedarf refreshen)
- [ ] 4.4 Live-Verify (Playwright): geteilter Feed-Artikel öffnet Notizen-
      Tab mit vorbefülltem Komposer und legt Notiz mit Titel+Link an;
      Cloud-Teilens aus Datei erzeugt Notiz mit Freigabelink; „In Cloud
      ablegen" stellt Anhang um; ohne Anbieter (anonym/kein Feed) keine
      Teilen-Affordance — alles wie Spec-Szenarien
