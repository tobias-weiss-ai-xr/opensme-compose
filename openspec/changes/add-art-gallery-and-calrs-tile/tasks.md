# Tasks: add-art-gallery-and-calrs-tile

## 1. Contract-Wächter (RED zuerst)

- [x] 1.1 `tests/00-static/check_tiles.py`: Web-Katalog-Parität prüfen
      (tiles.yml cataloged ↔ config.ts loadServiceSpecs/Order-IDs,
      Tile-Namen-Konsistenz) — Run muss RED liefern (calendar/vc fehlen)
- [x] 1.2 `portal/web/src/lib/config.ts`: `calendar` + `vc` in
      `loadServiceSpecs` und `buildServices`-Order — Check GREEN,
      bestehende vitest-Suite grün

## 2. ArtGallery-Komponente (RED zuerst)

- [x] 2.1 `src/components/ArtGallery.test.tsx`: Slides, Dot-Nav, Pfeile,
      Auto-Advance (Fake-Timer), Pause bei Hover/Fokus, testids
- [x] 2.2 `src/components/ArtGallery.tsx` implementieren — Tests GREEN
- [x] 2.3 4 SVG-Poster unter `public/brand/art/` (mondrian, kandinsky,
      matisse, vangogh; < 10 KB, 1680×1040, openSME-Sujet)

## 3. Einbettung (RED zuerst)

- [x] 3.1 `logged-out/page.test.tsx` erweitern: Galerie sichtbar,
      Login-/Portal-Links unverändert — RED
- [x] 3.2 `/logged-out` einbetten — GREEN
- [x] 3.3 `/`: Galerie nur wenn `authenticated === false` (page.tsx),
      Komponenten-Deckung via 2.1

## 4. Validation

- [x] 4.1 `bun run test` (portal/web) komplett grün
- [x] 4.2 `python3 tests/run.py --static` komplett grün (inkl.
      check_tiles + mutation-coverage unverändert 22/22)
- [x] 4.3 Commit + Push (Konvention: Rxx-Präfix, Scan-Sauberkeit der
      neuen Dateien)

## 5. Deploy & Live-Verify

- [x] 5.1 VPS dev copy pull; live `.env`: `CALENDAR_URL` vorhanden?
- [x] 5.2 portal-web Image neu bauen (buildx `--network host`) und
      `--force-recreate` nach erfolgreichem Build
- [x] 5.3 Live-Verify: anon `/` + `/logged-out` enthalten `art-slide-0`
      und `/brand/art/*.svg` wird ausgeliefert (200); Calendar-/Meet-Tile
      im Markup; angemeldet kein Galerie-Node

> **R21-Nachtrag (2026-11-19, separate Fix-Serie, Commits `76a14d1`,
> `4513c14`, `a70b31c`):** Die Live-Verify der Abmelde-Journey deckte drei
> verbundene Logout/Nonce-Bugs auf, die mit der Galerie-Doku zusammenhängen:
> (a) CSP `form-action 'self'` blockte den Cross-Origin-303-Hop zum IdP
> (Route `portal/web/src/lib/csp.ts` erlaubt `IDP_URL`-Origin; Wächter R21);
> (b) das Callback verwarf das `id_token` → `OPENSMESESSION` wird jetzt
> beim Login persistiert, damit `logout.rs` ein valides `id_token_hint`
> erzeugt und Zitadel direkt auf `post_logout_redirect_uri` (`/logged-out`)
> bounce (statt Sitzungspicker/`/logout/done`); (c) Next taggt Inline-Scripts
> nur, wenn der Nonce im REQUEST-Header ankommt (Middleware spiegelt die
> CSP jetzt in `NextResponse.next({request})`) — und statische Prerender
> haben keinen Buildzeit-Nonce, daher `export const dynamic =
> "force-dynamic"` auf `/logged-out` (Mutanten `slo-portal-csp-xreq`,
> `slo-loggedout-dynamic`). Live verifiziert: Login → id_token-Cookie →
> Abmelden → Redirect auf `/logged-out` mit Galerie + „Erneut anmelden“;
> IdP-Session nach Logout wirklich beendet (Passwort erneut verlangt).
