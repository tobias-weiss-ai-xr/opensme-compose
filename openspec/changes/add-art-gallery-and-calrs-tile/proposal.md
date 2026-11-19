# Proposal: add-art-gallery-and-calrs-tile

## Why

Die Logged-out-Seite (`/logged-out`, SLO-Landing des IdP) ist eine karge
Karte — der Abschluss der User Journey verdient ein gepflegtes Gegenstück
wie die rotierende Kunstgalerie der openEduSuite-Landing. Gleichzeitig ist
CalRS (Terminbuchung, `calendar.<domain>`) live deployt und vertraglich im
Katalog (`contracts/tiles.yml`, `CALENDAR_URL`), erscheint aber **nicht im
Portal**: der Next.js-Katalog (`buildServices`) kennt es nicht — obwohl
Rust-Katalog, compose-Pass-through und `.env`-Kette es alle schon haben.
Derselbe Drift trifft `vc` („Meet"). Der statische Wächter pinnt bisher nur
`tiles.yml ↔ portal/src/main.rs`, nicht den Next.js-Katalog, der die
Live-Seite rendert (portal-web vor portal, Port 3000).

## What Changes

- Neue `ArtGallery`-Komponente im portal-web (Port der openEduSuite-
  ArtGallery: Crossfade-Karussell, 7 s Rotation, Pause bei Hover/Fokus,
  Pfeile + Punkte, A11y-Labels deutsch) mit 4 neuen openSME-themed
  SVG-Postern unter `portal/web/public/brand/art/`
- Einbettung: immer auf `/logged-out`; auf `/` nur für anonyme Besucher
  (`probeSession === false`)
- **Katalog-Fix**: `calendar` (Calendar, `CALENDAR_URL`) und `vc` (Meet,
  `VC_URL`) in `loadServiceSpecs` + `buildServices` — Tile erscheint nur
  bei konfigurierter URL (bestehendes Muster)
- **Enforcement-Fix**: `check_tiles.py` pinnt zusätzlich
  `contracts/tiles.yml ↔ portal/web/src/lib/config.ts` (Katalog-IDs
  vollständig, Tile-Namen konsistent) — TDD: RED bevor der Fix

## Capabilities

### New Capabilities
- `portal-landing`: öffentliche Landing-Flächen des Portals (anonyme
  Startseite, Logged-out-Seite) inkl. Kunstgalerie

### Modified Capabilities
- `portal-catalog`: Funktioncatalog um Calendar (CalRS) und Meet (VC)
  erweitert; Web-Katalog-Parität als erzwungene Anforderung
