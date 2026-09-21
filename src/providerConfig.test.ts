import { describe, expect, it } from "vitest";
import { _defaultProvider, effectiveCapability, emptyProbeReport, probeIdentity, parseStoredProbe } from "./providerConfig";
import { ProviderRequests } from "./providerRequests";

describe("provider capability rules without storage or DOM", () => {
  it("uses verified results and preserves an explicit manual override", () => {
    const report = emptyProbeReport();
    report.vision.status = "verified";
    report.tool_calling.status = "unsupported";
    const provider = _defaultProvider({ model: "unknown-model", capability_probe: JSON.stringify(report) });
    expect(effectiveCapability(provider, "vision")).toBe(true);
    expect(effectiveCapability(provider, "tool_calling")).toBe(false);
    expect(effectiveCapability({ ...provider, vision_override: false }, "vision")).toBe(false);
    expect(effectiveCapability({ ...provider, tool_calling_override: true }, "tool_calling")).toBe(true);
    expect(parseStoredProbe("invalid json")).toBeNull();
  });

  it("normalizes custom model names but separates reasoning settings", () => {
    const provider = _defaultProvider({ base_url: "https://example.test/v1/", model: "__custom", model_custom: "model-a", reasoning_level: "low", api_key: "private-test-key" });
    expect(probeIdentity(provider)).toBe(probeIdentity({ ...provider, base_url: "https://example.test/v1", model: "model-a" }));
    expect(probeIdentity(provider)).not.toBe(probeIdentity({ ...provider, reasoning_level: "high" }));
    expect(probeIdentity(provider)).not.toContain(provider.api_key);
  });
});

describe("provider reply ownership", () => {
  it("ignores an older reply without consuming the current request", () => {
    const requests = new ProviderRequests();
    const old = requests.begin("models-fetch", "provider-a");
    const current = requests.begin("models-fetch", "provider-b");
    expect(requests.take("models-fetch", old, () => { throw new Error("must not read stale UI"); })).toBe(false);
    expect(requests.take("models-fetch", current, () => "provider-b")).toBe(true);
    expect(requests.take("models-fetch", current, () => "provider-b")).toBe(false);
  });

  it("does not accept a result for changed configuration or after a provider switch", () => {
    const requests = new ProviderRequests();
    const probe = requests.begin("probe-capabilities", "reasoning-low");
    expect(requests.take("probe-capabilities", probe, () => "reasoning-high")).toBe(false);
    const picker = requests.begin("mp-models", "provider-a");
    requests.clear();
    expect(requests.take("mp-models", picker, () => "provider-a")).toBe(false);
    expect(requests.owns("models-fetch", "task-123")).toBe(false);
  });
});
