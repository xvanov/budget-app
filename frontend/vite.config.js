import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The proxy target is overridable so the dev server can run OUTSIDE the compose
// network (a host-run `npm run dev` against a host-run uvicorn), where the
// service name `backend` does not resolve. The default is the compose value, so
// `docker compose up` needs no environment at all.
const API_TARGET = process.env.VITE_API_PROXY || 'http://backend:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: Number(process.env.VITE_PORT || 8080),
    proxy: {
      '/api': API_TARGET
    }
  }
})
