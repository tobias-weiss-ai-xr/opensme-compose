"use client";

import { useEffect, useRef, useState } from "react";
import CloudFilePicker, { type CloudFileEntry } from "./CloudFilePicker";
import { registerProvider, type CloudPickResult } from "@/lib/bridge";

/**
 * The shared cloud-file selection slot (`cloud.pick`).
 *
 * One provider module owns the picker modal and registers it on the
 * bridge; every consumer (team-note composer, poll composer) calls
 * `useSlot("cloud.pick").pick()` and receives the selected entry — they
 * never import the picker module, and swap/absence of the provider only
 * hides the trigger (URL fallback stays).
 *
 * Mount this ONCE (the shell does); pick() opens the modal and resolves
 * with the entry or null when cancelled/closed.
 */
export default function CloudPickerProvider() {
  const [open, setOpen] = useState(false);
  const resolverRef = useRef<((r: CloudPickResult | null) => void) | null>(
    null,
  );

  const pick = async (): Promise<CloudPickResult | null> => {
    setOpen(true);
    return new Promise<CloudPickResult | null>((resolve) => {
      resolverRef.current = resolve;
    });
  };

  useEffect(() => {
    return registerProvider("cloud.pick", { pick });
  }, []);

  function settle(entry: CloudFileEntry | null) {
    setOpen(false);
    const resolve = resolverRef.current;
    resolverRef.current = null;
    resolve?.(
      entry
        ? { path: entry.path, name: entry.name, size: entry.size ?? undefined }
        : null,
    );
  }

  return (
    <CloudFilePicker
      open={open}
      onClose={() => settle(null)}
      onSelect={(file) => settle(file)}
    />
  );
}
