import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * The dashboard talks to the agent-squad FastAPI service.
 *
 * In development the API is proxied so the browser sees a same-origin
 * `/api` and `/healthz`, which keeps the fetch wrappers in `src/api.ts`
 * free of environment-specific base URLs and avoids CORS entirely.
 *
 * The backend origin defaults to http://127.0.0.1:8080 and can be
 * overridden with AGENT_SQUAD_API (for example when the service runs on a
 * different port, or on another host during development).
 */
const BACKEND_ORIGIN = process.env.AGENT_SQUAD_API ?? 'http://127.0.0.1:8080';

export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.DASHBOARD_PORT ?? 5173),
    strictPort: false,
    proxy: {
      '/api': {
        target: BACKEND_ORIGIN,
        changeOrigin: true,
      },
      '/healthz': {
        target: BACKEND_ORIGIN,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
});
