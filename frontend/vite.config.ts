import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "::",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        // The activity feed uses /api/ws/activity. Without WebSocket upgrade
        // proxying, REST calls still work but the browser never receives
        // email.classified (or any other live) events.
        ws: true,
        timeout: 300000,
      },
    },
  },
});
