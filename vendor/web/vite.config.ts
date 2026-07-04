/// <reference types="vitest" />
import { execSync } from "node:child_process";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Resolve the git short SHA to bake into the bundle as VITE_BUILD_SHA so the
// Settings "About" version label always reflects the deployed commit. An
// explicit VITE_BUILD_SHA env (e.g. CI) wins; otherwise we read it from git.
// Falls back to "dev" only when git is unavailable. This exists because a
// deploy that ran `vite build` WITHOUT VITE_BUILD_SHA set regressed the label
// to "dev" — computing it here means ANY build carries the real SHA.
function resolveBuildSha(): string {
  if (process.env.VITE_BUILD_SHA) return process.env.VITE_BUILD_SHA;
  try {
    return execSync("git rev-parse --short HEAD", {
      stdio: ["ignore", "pipe", "ignore"],
    })
      .toString()
      .trim();
  } catch {
    return "dev";
  }
}

// GRACE-2 web client dev server. Host 0.0.0.0 + port 5173 so the dev
// server is reachable on the LAN for cross-browser spot checks (NFR-PO-1).
export default defineConfig(({ command }) => ({
  plugins: [react()],
  // Inject the build SHA only for production builds; serve/test leave it unset
  // so SettingsPopup's buildSha() shows "dev" locally (matching test expectations).
  define:
    command === "build"
      ? { "import.meta.env.VITE_BUILD_SHA": JSON.stringify(resolveBuildSha()) }
      : {},
  server: {
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
    // Use polling for HMR file watching. The Debian dev host hits inotify
    // ENOSPC under typical session load (max_user_instances=128 is exhausted
    // by other long-running tools). Polling sidesteps the limit at the cost
    // of a little CPU — acceptable for a stub.
    watch: {
      usePolling: true,
      interval: 1000,
    },
  },
  test: {
    environment: "happy-dom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
  },
}));
