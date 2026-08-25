// `vitest/config`, not `vite`: vite's own `defineConfig` does not know the
// `test` key and rejects it at type level, which fails `npm run build`
// (it runs `tsc --noEmit` first). Same function, wider type.
import { defineConfig } from 'vitest/config'
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
  // `node`, not `jsdom`: what needs covering here is the pure logic under
  // `src/lib/` -- the rules that decide what the published page claims -- and a
  // DOM environment would be a dependency bought for nothing. A test that needs
  // a document should say so with `// @vitest-environment jsdom` and bring the
  // package with it.
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
