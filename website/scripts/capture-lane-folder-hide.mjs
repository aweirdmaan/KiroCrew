/**
 * Screenshots, and an assertion per frame, for the folder hide in every sidebar lane.
 *
 * Eight frames: each of the four lanes that draw session rows, once with nothing
 * hidden and once with one folder unchecked. The pair is the point -- a lane that drew
 * nothing at all would satisfy "the hidden row is gone" on its own, so the before
 * frame is the control that says the row was renderable there in the first place.
 *
 * This ASSERTS as well as photographs. The unit pin already proves which rows render,
 * in jsdom, with framer-motion mocked out; what it cannot show is the lane a person
 * actually looks at. So a blank, wrong-theme or wrong-lane frame has to fail here
 * rather than ship as evidence: every frame checks the rows present by key, and the
 * run exits non-zero on the first mismatch.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort      # in another shell
 *   node scripts/capture-lane-folder-hide.mjs http://127.0.0.1:6841 [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/13775-lane-folder-hide'
mkdirSync(OUT, { recursive: true })

const HIDDEN_KEY = 'k-hidden-conductor'
const OPEN_KEYS = ['k-shown-child', 'k-shown-plain']
/** The two fixture folders, answered over the real `/api/chat/folders` read. */
const FOLDERS = [
  { id: 'folder-hidden', name: 'hidden folder', collapsed: false, order: 0 },
  { id: 'folder-shown', name: 'shown folder', collapsed: false, order: 1 },
]
/** One state-sourced column, which is what puts the board axis in front of the union. */
const COLUMNS = [{ id: 'col-idle', name: '', tag_ids: [], mode: 'any', order: 0, source: 'state', state_key: 'idle' }]

let failed = false
const check = (label, ok, detail) => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` -- ${detail}` : ''}`)
  if (!ok) failed = true
}

const browser = await chromium.launch({ executablePath: chromiumExecutable() })
const context = await browser.newContext({ viewport: { width: 640, height: 760 }, deviceScaleFactor: 2 })
const page = await context.newPage()
page.on('pageerror', e => { console.log(`FAIL pageerror -- ${e.message}`); failed = true })

// Which lane the next navigation is for: the board lane is the only one that needs a
// column, and handing every lane one would preempt the three that are not it.
let wantColumns = false
await stubDashboardApi(page, {
  theme: 'dark',
  folders: FOLDERS,
  // Running against the DEV server, `**/api/**` also matches `/src/api/client.ts`;
  // answering that with JSON serves the browser JSON where it requires JavaScript and
  // the page never mounts. Source paths pass through, gateway paths do not.
  extra: async (path, route) => {
    if (path === '/api/chat/tag-columns') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(wantColumns ? COLUMNS : []) })
      return true
    }
    if (path.startsWith('/api/')) return false
    await route.continue()
    return true
  },
})
logPageProblems(page)

/** Session row keys in rendered document order. */
const renderedKeys = () => page.$$eval('[data-slot-key]', els => [...new Set(els.map(el => el.getAttribute('data-slot-key')))])

/**
 * The colour theme is applied asynchronously after mount, so a frame taken too early
 * carries a different theme than its siblings. Wait for `data-theme` to stop moving
 * rather than sleep, so a slow runner does not quietly go back to catching it early.
 */
async function settledTheme() {
  let prev = null
  for (let i = 0; i < 20; i++) {
    const now = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    if (now && now === prev) return now
    prev = now
    await page.waitForTimeout(200)
  }
  return prev
}

/**
 * One frame. `hide` decides both the fixture seed and what is asserted, because the
 * two must never drift apart: a frame named "hidden" that photographs the unhidden
 * lane is exactly the evidence this harness exists to make impossible.
 */
async function frame(lane, hide, index) {
  wantColumns = lane === 'board'
  await page.goto(`${BASE}/capture/lane-folder-hide.html?lane=${lane}&hide=${hide ? 1 : 0}&theme=dark`)
  await page.waitForSelector('[data-capture-ready]')
  await page.waitForSelector(`[data-slot-key="${OPEN_KEYS[1]}"]`)
  const theme = await settledTheme()
  const label = `${lane} ${hide ? 'folder unchecked' : 'nothing hidden'}`
  check(`${label}: pinned Kiro dark`, theme === 'kiro-dark', String(theme))

  const keys = await renderedKeys()
  for (const k of OPEN_KEYS) {
    check(`${label}: ${k} renders`, keys.includes(k), keys.join(' '))
  }
  check(
    `${label}: ${HIDDEN_KEY} ${hide ? 'is gone' : 'renders'}`,
    keys.includes(HIDDEN_KEY) === !hide,
    keys.join(' '),
  )

  const name = `${String(index).padStart(2, '0')}-${lane}-${hide ? 'hidden' : 'before'}`
  await page.locator('[data-capture-ready]').screenshot({ path: `${OUT}/${name}.png` })
  console.log(`     ${name}.png`)
}

let n = 1
for (const lane of ['conductor', 'board', 'tree', 'flat']) {
  await frame(lane, false, n++)
  await frame(lane, true, n++)
}

await context.close()
await browser.close()
console.log(failed ? 'FAILED' : 'all frames ok')
process.exit(failed ? 1 : 0)
