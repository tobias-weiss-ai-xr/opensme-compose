import { render, screen, fireEvent } from "@testing-library/react";
import ThemeToggle from "./ThemeToggle";

describe("ThemeToggle", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    try {
      localStorage.clear();
    } catch {
      /* ignore */
    }
    document.documentElement.classList.remove("dark");
  });

  it("renders a toggle button", () => {
    render(<ThemeToggle />);
    expect(
      screen.getByRole("button", { name: /Farbschema wechseln|Zu .* Modus wechseln/ }),
    ).toBeInTheDocument();
  });

  it("toggles the dark class on the document element", async () => {
    render(<ThemeToggle />);
    const btn = await screen.findByRole("button", { name: /Zu .* Modus wechseln/ });
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    fireEvent.click(btn);
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    fireEvent.click(btn);
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("persists the chosen theme to localStorage", async () => {
    render(<ThemeToggle />);
    const btn = await screen.findByRole("button", { name: /Zu .* Modus wechseln/ });
    fireEvent.click(btn);
    expect(localStorage.getItem("opensme-theme")).toBe("dark");
  });
});