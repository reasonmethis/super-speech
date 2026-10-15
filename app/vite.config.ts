import path from "node:path";
import { defineConfig } from "vite";
import electron from "vite-plugin-electron/simple";

export default defineConfig({
  clearScreen: false,
  plugins: [
    electron({
      main: {
        entry: "electron/main.ts",
      },
      preload: {
        input: path.join(import.meta.dirname, "electron/preload.ts"),
      },
    }),
  ],
  server: {
    host: "127.0.0.1",
    port: 1420,
    strictPort: true,
    watch: {
      // Packaged installers and prepared engine resources are generated output
      // that the renderer never imports. Crawling them delays the first page
      // by minutes once a few releases have accumulated.
      ignored: ["**/release/**", "**/build-resources/**"],
    },
  },
});
