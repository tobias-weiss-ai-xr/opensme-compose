import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import ArtGallery, { ART_PIECES, SLIDE_MS } from "./ArtGallery";

/**
 * Suite for the public-landing art gallery: crossfade carousel over the
 * openSME artist-style SVG posters, with auto-rotation (paused on
 * hover/focus), arrow + dot navigation and German a11y labels.
 */
describe("ArtGallery", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders every poster with the first slide active", () => {
    render(<ArtGallery />);
    const slides = screen.getAllByRole("img", { hidden: true });
    expect(slides).toHaveLength(ART_PIECES.length);
    expect(screen.getByTestId("art-slide-0").getAttribute("aria-hidden")).toBe("false");
    for (let i = 1; i < ART_PIECES.length; i++) {
      expect(screen.getByTestId(`art-slide-${i}`).getAttribute("aria-hidden")).toBe("true");
    }
    for (const piece of ART_PIECES) {
      expect(screen.getByAltText(piece.alt).getAttribute("src")).toBe(piece.src);
    }
  });

  it("exposes the carousel region with a German label", () => {
    render(<ArtGallery />);
    expect(screen.getByRole("region", { name: "Kunstgalerie" })).toBeTruthy();
  });

  it("auto-advances every SLIDE_MS and wraps around", () => {
    render(<ArtGallery />);
    act(() => { vi.advanceTimersByTime(SLIDE_MS); });
    expect(screen.getByTestId("art-slide-1").getAttribute("aria-hidden")).toBe("false");
    for (let i = 0; i < ART_PIECES.length - 1; i++) {
      act(() => { vi.advanceTimersByTime(SLIDE_MS); });
    }
    expect(screen.getByTestId("art-slide-0").getAttribute("aria-hidden")).toBe("false");
  });

  it("navigates via next/prev arrows with wrap-around", () => {
    render(<ArtGallery />);
    fireEvent.click(screen.getByTestId("art-prev"));
    expect(screen.getByTestId(`art-slide-${ART_PIECES.length - 1}`).getAttribute("aria-hidden")).toBe("false");
    fireEvent.click(screen.getByTestId("art-next"));
    expect(screen.getByTestId("art-slide-0").getAttribute("aria-hidden")).toBe("false");
    fireEvent.click(screen.getByTestId("art-next"));
    expect(screen.getByTestId("art-slide-1").getAttribute("aria-hidden")).toBe("false");
  });

  it("jumps to a slide via its dot and restarts the rotation timer", () => {
    render(<ArtGallery />);
    fireEvent.click(screen.getByTestId("art-dot-2"));
    expect(screen.getByTestId("art-slide-2").getAttribute("aria-hidden")).toBe("false");
    // the timer restarted on manual navigation: a partial interval
    // must NOT advance yet, a full one must
    act(() => { vi.advanceTimersByTime(SLIDE_MS - 1); });
    expect(screen.getByTestId("art-slide-2").getAttribute("aria-hidden")).toBe("false");
    act(() => { vi.advanceTimersByTime(1); });
    expect(screen.getByTestId("art-slide-3").getAttribute("aria-hidden")).toBe("false");
  });

  it("pauses rotation while hovered and resumes on leave", () => {
    render(<ArtGallery />);
    const region = screen.getByRole("region", { name: "Kunstgalerie" });
    fireEvent.mouseEnter(region);
    act(() => { vi.advanceTimersByTime(SLIDE_MS * 3); });
    expect(screen.getByTestId("art-slide-0").getAttribute("aria-hidden")).toBe("false");
    fireEvent.mouseLeave(region);
    act(() => { vi.advanceTimersByTime(SLIDE_MS); });
    expect(screen.getByTestId("art-slide-1").getAttribute("aria-hidden")).toBe("false");
  });

  it("pauses rotation while a control has focus and resumes on blur", () => {
    render(<ArtGallery />);
    const dot = screen.getByTestId("art-dot-0");
    // jsdom feuert kein focusin auf natives .focus() — Reacts onFocus
    // basiert aber auf focusin-Delegation → fireEvent nutzen.
    fireEvent.focus(dot);
    act(() => { vi.advanceTimersByTime(SLIDE_MS * 3); });
    expect(screen.getByTestId("art-slide-0").getAttribute("aria-hidden")).toBe("false");
    fireEvent.blur(dot);
    act(() => { vi.advanceTimersByTime(SLIDE_MS); });
    expect(screen.getByTestId("art-slide-1").getAttribute("aria-hidden")).toBe("false");
  });

  it("marks the active dot with aria-current", () => {
    render(<ArtGallery />);
    expect(screen.getByTestId("art-dot-0").getAttribute("aria-current")).toBe("true");
    expect(screen.getByTestId("art-dot-1").getAttribute("aria-current")).toBe("false");
    fireEvent.click(screen.getByTestId("art-dot-1"));
    expect(screen.getByTestId("art-dot-1").getAttribute("aria-current")).toBe("true");
    expect(screen.getByTestId("art-dot-0").getAttribute("aria-current")).toBe("false");
  });
});
