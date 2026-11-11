import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": loadEnv(mode, ".", "VITE_").VITE_API_PROXY || "http://127.0.0.1:8000" } },
  build: {
    chunkSizeWarningLimit: 1100,
    rollupOptions: {
      output: {
        onlyExplicitManualChunks: true,
        manualChunks(rawId) {
          const id = rawId.replaceAll("\\", "/");
          if (!id.includes("/node_modules/")) return;
          if (/\/node_modules\/(react|react-dom|scheduler)\//.test(id))
            return "react-runtime";
          return undefined;
        },
      },
    },
  },
}));
