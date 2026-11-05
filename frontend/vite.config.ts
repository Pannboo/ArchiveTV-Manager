import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built into the Python package so FastAPI serves it at /app/ (see main.py).
const backend = process.env.ARCHIVETV_URL ?? "http://127.0.0.1:8090";

export default defineConfig({
  base: "/app/",
  plugins: [react()],
  build: { outDir: "../archivetv/web/app", emptyOutDir: true, sourcemap: false },
  server: {
    proxy: Object.fromEntries(["/api", "/logos", "/admin/preview", "/catchup", "/startover"].map((p) => [p, backend])),
  },
});
