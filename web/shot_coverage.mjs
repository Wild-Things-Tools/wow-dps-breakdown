import { chromium } from '/opt/node22/lib/node_modules/playwright/index.mjs'
import fs from 'node:fs'
import path from 'node:path'

const OUT = process.env.SHOT_OUT
const CACHE = process.env.SHOT_ICON_CACHE || null
const BASE = 'http://localhost:4241/wow-dps-breakdown/'

const browser = await chromium.launch({
  executablePath: '/opt/pw-browsers/chromium',
  // No proxy at all: the only third party this page wants is wow.zamimg.com, and
  // both runs intercept it -- served from a local cache, or aborted, which is the
  // blocked-CDN case a reader can genuinely be in. Chromium's bypass list does not
  // reliably exempt the preview server, and a proxied localhost request comes back
  // 405 with a blank page that looks exactly like a data bug.
})
const page = await browser.newPage({ viewport: { width: 1280, height: 2200 }, deviceScaleFactor: 2 })
if (CACHE) {
  await page.route('**/wow.zamimg.com/**', async (route) => {
    const file = path.join(CACHE, path.basename(new URL(route.request().url()).pathname))
    if (fs.existsSync(file)) {
      await route.fulfill({ body: fs.readFileSync(file), contentType: file.endsWith('.webp') ? 'image/webp' : 'image/jpeg' })
    } else await route.abort()
  })
} else {
  await page.route('**/wow.zamimg.com/**', (route) => route.abort())
}
page.on('console', (m) => { if (m.type() === 'error') console.log('CONSOLE ERROR:', m.text()) })
page.on('requestfailed', (r) => console.log('FAILED', r.url(), r.failure()?.errorText))
page.on('response', (r) => { if (r.status() >= 400) console.log('HTTP', r.status(), r.url()) })
await page.goto(`${BASE}?view=overview`, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(3500)
console.log('body text head:', (await page.locator('body').innerText()).slice(0, 400))
const panel = page.locator('section').filter({ hasText: 'Which specs and hero trees this covers' }).first()
console.log('panel found:', await panel.count())
await panel.screenshot({ path: `${OUT}/coverage-${CACHE ? 'icons' : 'noicons'}.png` })
await page.screenshot({ path: `${OUT}/overview-${CACHE ? 'icons' : 'noicons'}.png`, fullPage: true })
const imgs = await page.locator('img[src*="zamimg"]').count()
console.log('zamimg <img> elements on page:', imgs)
await browser.close()
