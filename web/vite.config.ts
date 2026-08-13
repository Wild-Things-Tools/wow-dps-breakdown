import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The site is served from https://<owner>.github.io/<repo>/ by default, so assets
// need that path prefix. Set BASE_PATH=/ when deploying to a custom domain.
const base = process.env.BASE_PATH ?? '/wow-dps-breakdown/'

export default defineConfig({
  base,
  plugins: [react(), tailwindcss()],
  // The dataset is fetched at runtime rather than bundled, so a data refresh
  // never invalidates the app chunk.
  build: {
    outDir: 'dist',
  },
})
