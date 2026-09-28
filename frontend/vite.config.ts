import { createHash } from "node:crypto";
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, relative, sep } from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

/** Injects the list of built files (and a content hash) into dist/sw.js for precaching. */
function precacheManifest(): Plugin {
  return {
    name: "kestrel-precache",
    apply: "build",
    closeBundle() {
      const dist = join(process.cwd(), "dist");
      const files: string[] = [];
      const walk = (dir: string) => {
        for (const f of readdirSync(dir)) {
          const p = join(dir, f);
          if (statSync(p).isDirectory()) walk(p);
          else files.push("/" + relative(dist, p).split(sep).join("/"));
        }
      };
      walk(dist);
      const precache = files.filter((f) => f !== "/sw.js" && !f.startsWith("/splash/") && !f.endsWith(".map"));
      const hash = createHash("sha256");
      for (const f of precache) hash.update(readFileSync(join(dist, f)));
      const swPath = join(dist, "sw.js");
      const sw = readFileSync(swPath, "utf8")
        .replace("self.__PRECACHE__", JSON.stringify(precache))
        .replace("__BUILD_HASH__", hash.digest("hex").slice(0, 12));
      writeFileSync(swPath, sw);
    },
  };
}

export default defineConfig({
  plugins: [react(), precacheManifest()],
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 900 },
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
  },
});
