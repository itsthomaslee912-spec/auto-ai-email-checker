import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // Bind all interfaces so Android Chrome on the same LAN can open the
    // Vite URL via the machine's Wi-Fi IP (e.g. http://192.168.x.x:5173).
    host: true,
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
