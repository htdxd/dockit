import "./style.css";
import { mountLayout, setStartupState } from "./ui";

performance.mark("dockit:module");
const root = document.querySelector<HTMLElement>("#app");
if (!root) throw new Error("Missing app root");
mountLayout(root);
performance.mark("dockit:mounted");
setStartupState("loading");

// Paint the real application before loading native bindings and saved data.
await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
performance.mark("dockit:first-frame");
try {
  const app = await import("./appController");
  await app.initializeApp();
  setStartupState("ready");
  performance.mark("dockit:ready");
  app.reportStartup();
} catch {
  setStartupState("failed", "初始化失败，请重新打开应用。");
}
