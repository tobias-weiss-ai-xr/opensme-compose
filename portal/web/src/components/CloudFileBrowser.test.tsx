// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import CloudFileBrowser from "./CloudFileBrowser";

const FILES = [
  {
    name: "report.pdf",
    path: "report.pdf",
    size: 12,
    is_dir: false,
    modified_at: null,
    type: "file" as const,
  },
];

const listing = () =>
  Promise.resolve(
    new Response(JSON.stringify({ files: FILES, path: "" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );

describe("CloudFileBrowser built-in actions", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("creates a folder via POST /api/cloud/files/mkdir", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => listing())
      .mockImplementationOnce((url: string, init: RequestInit) => {
        expect(url).toBe("/api/cloud/files/mkdir");
        expect(init.method).toBe("POST");
        expect(JSON.parse(String(init.body))).toEqual({ path: "notes" });
        return Promise.resolve(new Response(null, { status: 201 }));
      })
      .mockImplementationOnce(() => listing());
    vi.stubGlobal("fetch", fetchMock);

    render(<CloudFileBrowser />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: /new folder/i }));
    fireEvent.change(screen.getByPlaceholderText(/enter folder name/i), {
      target: { value: "notes" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^create$/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("deletes a file via DELETE /api/cloud/files/<encoded path>", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => listing())
      .mockImplementationOnce((url: string, init: RequestInit) => {
        expect(url).toBe("/api/cloud/files/report.pdf");
        expect(init.method).toBe("DELETE");
        return Promise.resolve(new Response(null, { status: 204 }));
      })
      .mockImplementationOnce(() => listing());
    vi.stubGlobal("fetch", fetchMock);

    render(<CloudFileBrowser />);
    await waitFor(() => expect(screen.getByText("report.pdf")).toBeTruthy());
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /^delete/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("shows the server error when mkdir fails", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => listing())
      .mockImplementationOnce(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({ error: "directory already exists" }),
            {
              status: 409,
              headers: { "content-type": "application/json" },
            },
          ),
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    render(<CloudFileBrowser />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: /new folder/i }));
    fireEvent.change(screen.getByPlaceholderText(/enter folder name/i), {
      target: { value: "notes" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^create$/i }));
    await waitFor(() =>
      expect(screen.getByText(/directory already exists/i)).toBeTruthy(),
    );
  });
});
