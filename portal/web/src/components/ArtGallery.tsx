"use client";

// ArtGallery.tsx — Kunstgalerie der öffentlichen Portal-Flächen.
//
// Port der openEduSuite-ArtGallery (collab-dashboard): leichte SVG-Poster
// im Stil großer Künstler:innen, openSME-Sujet. Rotiert automatisch
// (Crossfade), pausiert bei Hover/Fokus, manuell per Pfeilen + Punkten
// navigierbar — vollständig ohne three.js/WebGL. Eingebettet auf
// /logged-out (immer) und / (nur für anonyme Besucher).

import { useEffect, useState } from "react";

export interface ArtPiece {
  /** Pfad zur SVG-Ressource unter /brand/… */
  src: string;
  /** Künstler:in bzw. Stil (für title/Tooltip). */
  artist: string;
  /** Alt-Text (Screenreader). */
  alt: string;
}

/** Galerie-Reihenfolge: Mondrian zuerst (Struktur → Kommunikation →
 *  Dokumente → KI). */
export const ART_PIECES: ArtPiece[] = [
  {
    src: "/brand/art/mondrian.svg",
    artist: "Piet Mondrian",
    alt: "openSME — der SME-Arbeitstag als Neoplastizismus nach Piet Mondrian: Dateien, E-Mail, Workflow und Buchhaltung in klaren Farbblöcken",
  },
  {
    src: "/brand/art/kandinsky.svg",
    artist: "Wassily Kandinsky",
    alt: "openSME — digitale Kommunikation als konkrete Kunst nach Wassily Kandinsky: Chat, Meet und Terminbuchung in Kreisen und Linien",
  },
  {
    src: "/brand/art/matisse.svg",
    artist: "Henri Matisse",
    alt: "openSME — Dokumente und Archiv als Papier-Collagen nach Henri Matisse",
  },
  {
    src: "/brand/art/vangogh.svg",
    artist: "Vincent van Gogh",
    alt: "openSME — KI-Assistenz als nächtlicher Sternenhimmel nach Vincent van Gogh",
  },
];

export const SLIDE_MS = 7000;

interface ArtGalleryProps {
  /** Rotationsintervall in ms (Tests injizieren kurze Zeiten). */
  intervalMs?: number;
}

export default function ArtGallery({ intervalMs = SLIDE_MS }: ArtGalleryProps) {
  const [active, setActive] = useState(0);
  const [paused, setPaused] = useState(false);

  const show = (i: number) =>
    setActive((i + ART_PIECES.length) % ART_PIECES.length);

  // Rotation: das Intervall hängt an `active`, damit jede Folienänderung
  // (auto wie manuell) den Timer neu startet.
  useEffect(() => {
    if (paused || ART_PIECES.length < 2) return;
    const t = setInterval(
      () => setActive((a) => (a + 1) % ART_PIECES.length),
      intervalMs,
    );
    return () => clearInterval(t);
  }, [intervalMs, paused, active]);

  return (
    <div
      role="region"
      aria-roledescription="carousel"
      aria-label="Kunstgalerie"
      data-testid="art-gallery"
      className="mx-auto w-full max-w-5xl px-4 sm:px-6 lg:px-8 pt-8 pb-2"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocusCapture={() => setPaused(true)}
      onBlurCapture={() => setPaused(false)}
    >
      <div className="relative mx-auto w-full max-w-[840px]" style={{ aspectRatio: "1680 / 1040" }}>
        {ART_PIECES.map((piece, i) => {
          const isActive = i === active;
          return (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              key={piece.src}
              src={piece.src}
              alt={piece.alt}
              title={piece.artist}
              aria-hidden={!isActive}
              data-testid={`art-slide-${i}`}
              className={`absolute inset-0 h-full w-full rounded-2xl border border-border bg-card shadow-lg transition-opacity duration-1000 ${isActive ? "opacity-100" : "pointer-events-none opacity-0"}`}
            />
          );
        })}
      </div>
      <div className="mx-auto mt-3 flex max-w-[840px] items-center justify-center gap-3">
        <button
          type="button"
          data-testid="art-prev"
          aria-label="Vorheriges Bild"
          onClick={() => show(active - 1)}
          className="inline-flex h-9 w-9 items-center justify-center rounded-full border border-border bg-card text-lg leading-none text-foreground transition hover:bg-accent/10"
        >
          ‹
        </button>
        <div className="flex items-center gap-2" role="group" aria-label="Kunstgalerie">
          {ART_PIECES.map((piece, i) => (
            <button
              key={piece.src}
              type="button"
              data-testid={`art-dot-${i}`}
              aria-label={`Bild ${i + 1} von ${ART_PIECES.length}`}
              aria-current={i === active ? "true" : "false"}
              onClick={() => show(i)}
              title={piece.artist}
              className={`h-2.5 rounded-full transition-all ${
                i === active
                  ? "w-6 bg-accent"
                  : "w-2.5 bg-border hover:bg-dim"
              }`}
            />
          ))}
        </div>
        <button
          type="button"
          data-testid="art-next"
          aria-label="Nächstes Bild"
          onClick={() => show(active + 1)}
          className="inline-flex h-9 w-9 items-center justify-center rounded-full border border-border bg-card text-lg leading-none text-foreground transition hover:bg-accent/10"
        >
          ›
        </button>
      </div>
    </div>
  );
}
