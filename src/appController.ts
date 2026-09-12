import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { sendBackend } from "./backend";
import { createProviderController } from "./providerController";
import { createTaskController } from "./taskController";
import type { BackendEnvelope } from "./types";

/** Load native bindings and data only after the actual application has painted. */
export async function initializeApp(): Promise<void> {
  let providers: ReturnType<typeof createProviderController> | undefined;
  let tasks: ReturnType<typeof createTaskController> | undefined;
  if ("__TAURI_INTERNALS__" in window) {
    await listen<BackendEnvelope>("backend-event", ({ payload }) => {
      if (!providers?.handleEvent(payload)) tasks?.handleEvent(payload);
    });
  }
  providers = createProviderController(sendBackend);
  tasks = createTaskController(sendBackend, providers);
  await providers.loadSettings();
  await tasks.requestArtifactsScan();
}

export function reportStartup(): void {
  if (!("__TAURI_INTERNALS__" in window)) return;
  const navigation = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined;
  // Native logging is disabled unless DOCKIT_STARTUP_TRACE names a local file.
  void invoke("startup_profile", {
    timings: {
      time_origin_ms: performance.timeOrigin,
      navigation: navigation ? {
        responseStart: navigation.responseStart, responseEnd: navigation.responseEnd,
        domInteractive: navigation.domInteractive,
        domContentLoaded: navigation.domContentLoadedEventEnd,
        loadEnd: navigation.loadEventEnd,
      } : null,
      paints: performance.getEntriesByType("paint").map((entry) => ({
        name: entry.name, start: entry.startTime,
      })),
      ...Object.fromEntries(performance.getEntriesByType("mark")
        .filter((entry) => entry.name.startsWith("dockit:"))
        .map((entry) => [entry.name, entry.startTime])),
      resources: performance.getEntriesByType("resource")
        .filter((entry) => new URL(entry.name).origin === location.origin)
        .map((entry) => ({ path: new URL(entry.name).pathname, start: entry.startTime, duration: entry.duration })),
    },
  }).catch(() => {});
}
