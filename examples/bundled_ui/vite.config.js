// The widget's two files, per docs/apps.md: one module, one stylesheet, assets inlined.
import { defineConfig } from "vite";

export default defineConfig({
  build: {
    target: "es2022",
    cssCodeSplit: false,
    assetsInlineLimit: 100_000_000,   // the logo becomes a data: URL
    rollupOptions: {
      input: "src/main.js",           // no index.html: micromcp writes the page
      output: {
        codeSplitting: false,
        entryFileNames: "widget.js",
        assetFileNames: "widget.[ext]",
      },
    },
  },
});
