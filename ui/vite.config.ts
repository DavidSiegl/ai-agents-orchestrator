/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// tauri dev loads the app from this port (src-tauri/tauri.conf.json's devUrl), so it must be this one or none.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: { port: 1420, strictPort: true, watch: { ignored: ["**/src-tauri/**"] } },
  test: { environment: "jsdom", setupFiles: ["./src/test/setup.ts"] },
});
