# Design: add-art-gallery-and-calrs-tile

## Context

Die Live-Landing läuft auf dem **Next.js-Frontend** (portal-web, Port 3000,
Router `portal-standin`), nicht auf dem Rust-Katalog. Der Rust-Katalog
(`portal/src/main.rs`) kennt `calendar` (📅, Zeile ~842) und `VC_URL`
bereits; compose reicht `CALENDAR_URL`/`VC_URL` an beide Container durch;
`.env.example` definiert beide. Einzig `portal/web/src/lib/config.ts`
(`buildServices`/`loadServiceSpecs`) fehlen beide Einträge — und
`check_tiles.py` pinnt nur `tiles.yml ↔ main.rs`, deshalb konnte der Drift
entstehen. Referenz-Implementierung der Galerie:
`openedusuite-content/collab-dashboard/src/components/ArtGallery.tsx`
(+ `public/brand/art/*.svg`), dort embedded via `authed === false`.

## Goals / Non-Goals

**Goals:**
- Galerie auf `/logged-out` (unter der Karte) und anonym auf `/`
- Calendar- und Meet-Tile im Next-Katalog, contract-paritätisch
- Statischer Wächter gegen Web-Katalog-Drift (RED vor GREEN)

**Non-Goals:**
- Kein Umbau der Rust-Landing (Legacy-Pfad bleibt)
- Keine i18n (Portal ist deutsch, wie alle Seiten)
- Keine Kopie der openEduSuite-SVGs (fremdes Branding)
- Keine Galerie für angemeldete Besucher

## Decisions

1. **Komponenten-Port statt Neuerfindung**: `ArtGallery.tsx` adaptiert den
   openEduSuite-Bewährungsstand (Crossfade, 7 s, Pause bei Hover/Fokus,
   Pfeile + Punkte, `data-testid` `art-slide-N`/`art-prev`/`art-next`/
   `art-dot-N`, `aspectRatio 1680/1040`), aria-Labels hardcoded deutsch,
   Tailwind-Klassen an openSME-Farbwelt (`accent` statt `indigo`).
2. **Assets**: 4 handgebaute SVGs (`mondrian`-Grid „Struktur",
   `kandinsky`-Kreise „Kommunikation", `matisse`-Cutouts „Dokumente",
   `vangogh`-Wirbel „KI-Nacht") unter `portal/web/public/brand/art/`,
   je < 10 KB, viewBox 1680×1040, keine externen Referenzen.
3. **Tile-Einträge folgen dem bestehenden Muster**: `specs.calendar`/
   `specs.vc` via `envOr(..., "")`, Order-Einträge mit `url: specs.*` —
   der vorhandene `.filter((s) => s.url)` hält unkonfigurierte Tiles
   hidden. Position: `calendar` nach `webmail`, `vc` nach `chat`
   (grpcuppert nach Funktion).
4. **Paritäts-Wächter in `check_tiles.py`** statt neuem Check: gleiche
   Verantwortung (Katalog-Vollständigkeit), ein Runner-Eintrag, M-Nummern
   unverändert. Er parst `buildServices`-Order-IDs + `loadServiceSpecs`-
   Keys und vergleicht mit `contracts/tiles.yml` (`cataloged`-Liste);
   Tile-Namen (`tile:` vs. `name:`) als Warnung + Fail bei Abweichung.
5. **Deploy**: `portal/web` Dockerfile Rebuild via buildx
   `--network host` (runc-EOF-Muster auf dem VPS), dann
   `docker compose up -d --no-deps --force-recreate portal-web`.
   Vorab prüfen, dass `CALENDAR_URL` in der Live-`.env` steht.

## Risiken / Rollback

- Rebuild schlägt fehl → alte Image-Version läuft weiter (compose
  `--force-recreate` erst nach erfolgreichem Build).
- Galerie-SVGs sind statisch, kein Server-Risiko; Rollback = Revert +
  Rebuild.
