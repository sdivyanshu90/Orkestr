import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * The dashboard talks to the agent-squad FastAPI service.
 * In development the API is proxied so the browser sees a same-origin
 * `/api` and `/healthz`, which keeps the fetch wrappers in `src/api.ts`
 * free of environment-specific base URLs.
 */
const BACKEND_ORIGIN = 'http://127.0.0.1:8080';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
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
