import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  // 代理目标默认指向本机后端；连别的实例时在仓库根 .env 写 PEBBLE_BACKEND_URL。
  const backend = loadEnv(mode, "..", "PEBBLE_").PEBBLE_BACKEND_URL ?? "http://127.0.0.1:8000";

  return {
    plugins: [react()],
    server: {
      // host 开放局域网，手机可直接访问开发页面验证移动端表现。
      host: true,
      port: 5173,
      proxy: {
        "/api": { target: backend, changeOrigin: true },
      },
    },
    test: {
      setupFiles: ["./src/test-setup.ts"],
    },
  };
});
