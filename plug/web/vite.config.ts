import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development Vite serves the UI and proxies the API to the console process
// (uvicorn console.main:app --port 8003, or main:app on 8000 when both servers
// and the console run in one process),
// so the browser sees one origin and there is no CORS to configure. In
// production there is no proxy at all: `npm run build` emits ../web/dist, which
// console/main.py mounts at "/", and the whole thing is one uvicorn process.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.PLUG_API ?? "http://127.0.0.1:8003",
        changeOrigin: true,
        // SSE dies behind a buffering proxy; Vite needs telling not to.
        configure: (proxy) => {
          proxy.on("proxyRes", (proxyRes) => {
            if (proxyRes.headers["content-type"]?.includes("text/event-stream")) {
              proxyRes.headers["cache-control"] = "no-cache";
            }
          });
        },
      },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
