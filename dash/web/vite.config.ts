import { defineConfig } from 'vite';

// `npm run dev` serves on :5173 and proxies /api (SSE included) to `obs serve` on :8765.
export default defineConfig({
  base: '/',
  build: { outDir: 'dist', target: 'es2022', sourcemap: false, chunkSizeWarningLimit: 600 },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: false,
        // keep SSE unbuffered
        configure: (proxy) => {
          proxy.on('proxyRes', (res) => {
            if (String(res.headers['content-type'] || '').includes('text/event-stream')) {
              res.headers['cache-control'] = 'no-cache';
              res.headers['x-accel-buffering'] = 'no';
            }
          });
        },
      },
    },
  },
});
