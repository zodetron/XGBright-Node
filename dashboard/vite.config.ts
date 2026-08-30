import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dashboard runs on its own dev server but talks to the Python process on :8000.
// Proxying both the REST API and the live WebSocket keeps every URL in the app
// origin-relative, so the same build works when served from the server itself.
export default defineConfig({
  plugins: [react()],
  server: {
    // A distinctive port: 5173/5174 are commonly already taken by other Vite
    // projects, and silently landing on a different port each run makes the
    // dashboard hard to find.
    port: 5180,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
})
