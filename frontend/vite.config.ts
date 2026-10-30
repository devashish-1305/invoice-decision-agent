import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The /api proxy is for `npm run dev` only. In production FastAPI serves frontend/dist itself.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": "http://localhost:8000" } },
});
