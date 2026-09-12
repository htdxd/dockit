// Server-side Vite configuration runs in Node, outside the browser TS project.
import { resolve } from "node:path";
import { expect, it } from "vitest";
import { ignoreNonFrontend } from "../vite.config";

it("keeps frontend/config hot updates while excluding unrelated resource trees", () => {
  for (const path of ["", "src/ui.ts", "src/nested/view.ts", "public/resume-templates/t109.png",
    "index.html", "package.json", "package-lock.json", "vite.config.ts", ".env.local"]) {
    expect(ignoreNonFrontend(resolve(path)), path).toBe(false);
  }
  for (const path of ["backend/skill_toolbox/skill_defs/ppt-master/templates",
    "ResumeCollection", "output/report.docx", ".tmp_t/startup-launch.jsonl", "src-tauri/target"]) {
    expect(ignoreNonFrontend(resolve(path)), path).toBe(true);
  }
});
