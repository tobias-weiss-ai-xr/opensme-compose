# Portal: Tabs (Dienste · Umfragen · Notizen) + Logout-Button

Datum: 2026-10-16 · Status: approved · Umfang: `portal/` (Rust/axum + app.js)

## Problem

- Die Widgets „📊 Umfragen" (Polls) und „💬 Team-Notizen" (Intercom) liegen im
  Kartenraster der Sektion „Dienste", obwohl sie interaktive Widgets sind
  (Abstimmung/Senden direkt im Portal), keine Dienstverknüpfungen. Anwender
  erwarten sie als eigene Tabs.
- Es fehlt ein Logout-Button, obwohl das Backend (`POST /api/logout` →
  RP-initiated SSO-Logout über Zitadel, `/logged-out`-Seite) vollständig
  existiert.

## Design

1. **Header-Navigation wird echte Tab-Leiste**: `Dienste | Umfragen | Notizen`.
   - Tab „Umfragen" nur, wenn `config.polls` gesetzt (Option).
   - Tab „Notizen" immer (Intercom-Store ist immer aktiv).
   - „Aktuelles" (Feeds) bleibt Sektion unter „Dienste".
2. **Sichtwechsel per JS** (app.js): aktive Klasse auf selektiertem Tab,
   `hidden` auf inaktive Sections. Widget-Markup bleibt im DOM → Poll/Intercom-
   Fetch-Logik unverändert.
3. **Logout-Button** in der Header-Nav: `<form method="post" action="/api/logout">`
   mit „Abmelden". Sichtbar nur bei aktivem Session-Cookie `OPENSMESESSION`
   (JS-Check in app.js); ohne Cookie bleibt Nav rein tab-basiert.
4. **Dienste-Grid**: enthält nur noch Dienste-Karten + optionale AI-Karte;
   `get_services` unverändert.
5. **Tests**: Rust-Unit-Tests in `main.rs` (Muster wie
   `service_ids_are_stable_unique_and_complete`): Tab-Leiste korrekt (Poll-Tab
   an/aus), Dienste-Grid frei von Widget-Cards, Logout-Formular nur mit Cookie,
   bestehende Tests grün. Statische Suite (check_tiles/interop/spec) grün.

## Nicht im Scope

- Kein neues Backend (logout.rs unverändert).
- Keine Änderung an Poll/Intercom-Funktionalität, `get_services` oder tiles.
