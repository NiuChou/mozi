import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 前端 :5173 → 代理 /v1 /health 到本地后端 (本地优先)
// 后端端口可经 VITE_API_TARGET 覆盖 (默认 :8000; 端口冲突时改这里)
const target = process.env.VITE_API_TARGET || "http://127.0.0.1:8000";
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1": { target, changeOrigin: true },
      "/health": { target, changeOrigin: true },
    },
  },
});
