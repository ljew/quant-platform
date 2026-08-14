import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// React 前端（完整版架构）。开发时通过代理把 /api 转发到后端 FastAPI。
export default defineConfig({
    plugins: [react()],
    server: {
        port: 5173,
        proxy: {
            "/api": {
                target: "http://127.0.0.1:8000",
                changeOrigin: true,
            },
        },
    },
    build: {
        outDir: "dist",
        chunkSizeWarningLimit: 1500,
    },
});
