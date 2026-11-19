/**
 * Typed provider registry for in-portal component integration
 * (portal-intercom capability).
 *
 * Modules NEVER import each other to interoperate — they register named
 * capabilities here and hosts render whatever a registered provider offers.
 * Fail-soft contract: with zero providers every host behaves exactly as
 * without the bridge (slots render nothing, no error paths).
 *
 * SSR-safe: module top level only creates a Map — no window/document,
 * no effects. useSlot() may only be called from client components.
 */
"use client";

import { useSyncExternalStore } from "react";

/** What the team-note composer can receive from a source component. */
export interface SharePayload {
  /** Human-readable reference (source title / name), prefilled into the note. */
  text: string;
  /** Link to the source, prefilled into the note text. */
  url?: string;
  /** Optional cloud share link used as the note attachment. */
  attachmentUrl?: string;
}

/** Provider for the "share into team notes" slot — registered by the shell. */
export interface ShareOpenProvider {
  open(payload: SharePayload): void;
}

/** A cloud entry returned by the shared picker provider. */
export interface CloudPickResult {
  path: string;
  name: string;
  size?: number;
  /** Cloud share link for the entry when already shared/available. */
  url?: string;
}

/** Provider for the shared cloud-file selection slot (`cloud.pick`). */
export interface CloudPickProvider {
  /** Open the picker; resolves to the selected entry or null (cancelled/absent). */
  pick(): Promise<CloudPickResult | null>;
}

/** Slot name → provider contract. Add new slots here (and in the static guard). */
export interface BridgeProviderMap {
  "share.open": ShareOpenProvider;
  "cloud.pick": CloudPickProvider;
}

export type BridgeSlot = keyof BridgeProviderMap;

const providers = new Map<BridgeSlot, BridgeProviderMap[BridgeSlot]>();
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of [...listeners]) listener();
}

/**
 * Register a provider for a slot. Latest registration wins (a warning is
 * logged — duplicate providers are a wiring smell). Returns a cleanup that
 * removes ONLY this registration.
 */
export function registerProvider<K extends BridgeSlot>(
  slot: K,
  provider: BridgeProviderMap[K],
): () => void {
  if (providers.has(slot)) {
    console.warn(`[bridge] provider "${slot}" already registered — replacing`);
  }
  providers.set(slot, provider);
  emit();
  return () => {
    if (providers.get(slot) === provider) {
      providers.delete(slot);
      emit();
    }
  };
}

/** Synchronous lookup — sources use this to decide whether to render an affordance. */
export function getProvider<K extends BridgeSlot>(
  slot: K,
): BridgeProviderMap[K] | null {
  return (providers.get(slot) as BridgeProviderMap[K] | undefined) ?? null;
}

/** Subscribe to registry changes (backing for useSlot; also usable directly). */
export function subscribeBridge(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Reactive provider lookup for client components: re-renders when a
 * provider registers/unregisters. Server-side it always resolves to null
 * (registration happens in effects on the client).
 */
export function useSlot<K extends BridgeSlot>(
  slot: K,
): BridgeProviderMap[K] | null {
  return useSyncExternalStore(subscribeBridge, () => getProvider(slot));
}

/** Test helper only — clears every registration between tests. */
export function resetBridgeForTests(): void {
  providers.clear();
  emit();
}
