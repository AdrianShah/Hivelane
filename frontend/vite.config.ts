import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * The path the dashboard is served under. '/' for `npm run dev`, Vercel and the
 * copy the relay bundles (Docker image, `synergo relay`); '/Synergo/' for the
 * GitHub Pages build (.github/workflows/pages.yml sets SYNERGO_BASE). Asset
 * URLs and the router's basename (import.meta.env.BASE_URL) both follow it.
 */
function base(): string {
  const raw = process.env.SYNERGO_BASE?.trim()
  if (!raw) return '/'
  return `/${raw.replace(/^\/+|\/+$/g, '')}/`.replace(/^\/\/$/, '/')
}

// https://vite.dev/config/
export default defineConfig({
  base: base(),
  plugins: [react()],
})
