import { defineConfig } from "vite";
import { isAbsolute, relative } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = fileURLToPath(new URL(".", import.meta.url));
const frontendRoots = new Set([
  "src", "public", "index.html", "package.json", "package-lock.json", "vite.config.ts",
]);

export function ignoreNonFrontend(file: string): boolean {
  const path = relative(projectRoot, file);
  if (!path || path.startsWith("..") || isAbsolute(path)) return false;
  const top = path.split(/[\\/]/)[0];
  return !frontendRoots.has(top) && !top.startsWith(".env");
}

export default defineConfig({
  clearScreen: false,
  optimizeDeps: { entries: ["index.html"] },
  server: {
    strictPort: true,
    watch: {
      // Native/backend and template trees are not frontend HMR inputs.
      ignored: ignoreNonFrontend,
    },
  },
});
