import { describe, expect, it } from "vitest";

import {
  defaultProviderName,
  effectiveModel,
  legacyToProvider,
  validateProvider,
  _defaultProvider,
} from "./settings";

describe("provider validation", () => {
  it("accepts a complete openai provider", () => {
    const provider = _defaultProvider({
      name: "OpenAI 主账号",
      kind: "openai",
      api_key: "sk-test",
      model: "gpt-4o",
    });
    expect(validateProvider(provider)).toBe("");
  });

  it("requires base_url for openai_compatible", () => {
    const provider = _defaultProvider({
      name: "兼容网关",
      kind: "openai_compatible",
      api_key: "key",
      model: "deepseek-v3",
    });
    expect(validateProvider(provider)).toContain("Base URL");
  });

  it("rejects missing name / model / api key", () => {
    const empty = _defaultProvider({});
    expect(validateProvider(empty)).not.toBe("");
    const noKey = _defaultProvider({ name: "x", model: "m", api_key: "" });
    expect(validateProvider(noKey)).toContain("API Key");
    const noModel = _defaultProvider({ name: "x", model: "", model_custom: "", api_key: "k" });
    expect(validateProvider(noModel)).toContain("模型名");
  });

  it("accepts custom model when model_custom is set", () => {
    const provider = _defaultProvider({
      name: "x",
      kind: "openai",
      api_key: "k",
      model: "__custom",
      model_custom: "my-model",
    });
    expect(validateProvider(provider)).toBe("");
    expect(effectiveModel(provider)).toBe("my-model");
  });
});

describe("default provider template", () => {
  it("names the card after the kind", () => {
    expect(defaultProviderName("openai")).toBe("OpenAI Chat Completions 供应商");
    expect(defaultProviderName("openai_responses")).toBe("OpenAI Responses 供应商");
    expect(defaultProviderName("anthropic")).toBe("Anthropic 供应商");
    expect(defaultProviderName("openai_compatible")).toBe("兼容接入 供应商");
  });

  it("generates a uuid and defaults vision to auto (null)", () => {
    const provider = _defaultProvider({ kind: "openai_compatible" });
    expect(provider.id).toMatch(/[0-9a-f-]{36}/);
    expect(provider.vision_override).toBeNull();
    expect(provider.tool_calling_override).toBeNull();
    expect(provider.reasoning_level).toBe("auto");
    expect(provider.capability_probe).toBe("");
    expect(provider.base_url).toBe("");
  });
});

describe("legacy localStorage migration mapping", () => {
  it("maps a legacy flat config into one provider card", () => {
    const provider = legacyToProvider({
      "provider-kind": "openai_compatible",
      "base-url": "https://gateway.example/v1",
      "api-key": "sk-legacy",
      model: "deepseek-v3",
      "model-custom": "",
    });
    expect(provider).not.toBeNull();
    expect(provider!.kind).toBe("openai_compatible");
    expect(provider!.base_url).toBe("https://gateway.example/v1");
    expect(provider!.api_key).toBe("sk-legacy");
    expect(provider!.model).toBe("deepseek-v3");
    expect(provider!.name).toBe("默认供应商");
  });

  it("returns null when there is no legacy model (nothing to migrate)", () => {
    expect(legacyToProvider({ "output-dir": "C:/out" })).toBeNull();
  });
});
