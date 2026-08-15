import { chromium } from 'playwright'
const OUT='/tmp/claude-0/-home-user-wow-dps-breakdown/d047e082-5089-5664-9841-4a04447d5eae/scratchpad/shots'
const BASE='http://localhost:4210/wow-dps-breakdown/'
const browser = await chromium.launch({
  executablePath: '/opt/pw-browsers/chromium',
  args: process.env.HTTPS_PROXY ? [`--proxy-server=${process.env.HTTPS_PROXY}`] : [],
})
const page = await browser.newPage({ viewport: { width: 1400, height: 1800 }, deviceScaleFactor: 2 })
const url = `${BASE}?view=spec&focus=death_knight_unholy_sanlayn`
await page.goto(url, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(4000)
const n = await page.locator('.talent-node').count()
const taken = await page.locator('.talent-node-taken').count()
console.log('talent nodes rendered:', n, ' taken:', taken)
await page.screenshot({ path: `${OUT}/talents.png`, fullPage: true })
// also the panel alone
const panel = page.locator('.talent-node').first()
if (await panel.count()) {
  const box = await page.locator('div.grid').first().boundingBox()
  console.log('first grid box:', JSON.stringify(box))
}
await browser.close()
