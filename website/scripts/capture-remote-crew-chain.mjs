/**
 * Screenshot harness for the crew switcher's CHAIN TREE.
 *
 * A crew reached through another crew renders under its parent, indented one step
 * with a connector glyph, and its subtitle names the crew it goes through as well
 * as the machine. A third level indents once more. The tab bar itself stays flat:
 * a chained crew's chip carries the path (`parent > child`) instead of an indent,
 * so it does not read as just another top-level crew. When a parent's hop is down,
 * the crews behind it grey out with it, because they ride that tunnel.
 *
 * Runs against the REAL built SPA (website/dist) with a stubbed instances API --
 * the same approach as `capture-crew-tab-stable-order.mjs`, so the switcher
 * photographed here is the one the header actually renders. Nothing in CI runs
 * this file; the ordering and reachability rules are unit-tested in
 * `src/test/remoteCrewChaining.test.ts`.
 *
 * Usage: npm run build && node scripts/capture-remote-crew-chain.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/remote-crew-chain'

const VIEWPORT = { width: 1280, height: 760 }

/** One crew row as `/api/instances` reports it. */
const crew = (id, name, sshHost, port, extra = {}) => ({
  id,
  name,
  ssh_host: sshHost,
  remote_port: 7777,
  local_port: port,
  ttl: '20h',
  remote_bin: '',
  connection_method: 'ssh',
  ssm_target: '',
  ssm_run_as: '',
  aws_profile: '',
  aws_region: '',
  via_instance_id: '',
  via_remote_port: 0,
  was_connected: true,
  status: { instance_id: id, state: 'connected', local_port: port, remote_port: 7777 },
  ...extra,
})

/** A crew reached through `parent`'s hop, on `hopPort` of that parent. */
const chained = (id, name, sshHost, port, parent, hopPort, extra = {}) =>
  crew(id, name, sshHost, port, { via_instance_id: parent, via_remote_port: hopPort, ...extra })

// Generic fixture names only: nothing here names a real machine.
const HEALTHY = [
  crew('build-host', 'build-host', 'build-host-alias', 7801),
  chained('fresh-desktop', 'fresh-desktop', 'fresh-desktop-alias', 7802, 'build-host', 7901),
  chained('gpu-box', 'gpu-box', 'gpu-box-alias', 7803, 'fresh-desktop', 7902),
  crew('lab-host', 'lab-host', 'lab-host-alias', 7804),
]

// The same shape with the PARENT's hop down, so its crews grey out together.
const PARENT_DOWN = HEALTHY.map(c =>
  c.id === 'build-host'
    ? { ...c, status: { ...c.status, state: 'error', error: 'ssh: connect refused' } }
    : c,
)

const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }

const SLOTS = [
  {
    key: 'chain-shot',
    title: 'Reaching a third machine',
    running: false,
    last_message: 'Connected the fresh desktop from the build host.',
    messages: 2,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    folder_id: '',
    modified: Math.floor(Date.now() / 1000),
    source_links: [],
    source_links_total: 0,
  },
]

const TRIGGER = '[aria-label^="Switch crew"]'
const MENU_ROW = '[role="menuitemradio"]'

async function shoot(base, browser, crews, name) {
  const context = await browser.newContext({ viewport: VIEWPORT })
  const page = await context.newPage()
  logPageProblems(page)
  // A `connected` crew makes InstancesViewport mount a warm-pane iframe at its
  // forwarded port; nothing serves those here, so answer them a blank doc.
  await page.route(/127\.0\.0\.1:7[89]\d\d/, route =>
    route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>pane</title>' }),
  )
  const extra = async (path, route) => {
    if (path === '/api/instances') {
      await json(route, { active: true, instances: crews, warm_set_cap: 5, sso: SSO })
      return true
    }
    const tunnel = /^\/api\/instances\/([^/]+)\/(connect|refresh-token)$/.exec(path)
    if (tunnel) {
      const found = crews.find(c => c.id === decodeURIComponent(tunnel[1]))
      if (!found) return false
      await json(route, { ...found.status, token: 'stub-token' })
      return true
    }
    return false
  }
  await stubDashboardApi(page, { theme: 'dark', slots: SLOTS, extra })
  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(TRIGGER, { timeout: 20000 })
  await page.click(TRIGGER)
  await page.waitForSelector(MENU_ROW, { timeout: 10000 })
  // The menu fades in; a short settle beats a fixed long wait.
  await page.waitForTimeout(500)
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log(`wrote ${OUT}/${name}.png`)
  await context.close()
}

async function shootPane(base, browser, crews, name) {
  // `isEmbeddedPane()` is `window.self !== window.top`, so the only honest way to
  // photograph the pane's own Settings rail is to load the SPA inside an iframe.
  const context = await browser.newContext({ viewport: VIEWPORT })
  const page = await context.newPage()
  logPageProblems(page)
  const extra = async (path, route) => {
    if (path === '/api/instances') {
      await json(route, { active: true, instances: crews, warm_set_cap: 5, sso: SSO })
      return true
    }
    return false
  }
  await stubDashboardApi(page, { theme: 'dark', slots: SLOTS, extra })
  await page.route('**/pane-host.html', route =>
    route.fulfill({
      contentType: 'text/html',
      body:
        '<!doctype html><title>pane host</title>' +
        '<style>html,body{margin:0;height:100%;background:#0b0d12}' +
        'iframe{border:0;width:100%;height:100%}</style>' +
        `<iframe src="${base}/settings/instances"></iframe>`,
    }),
  )
  await page.goto(`${base}/pane-host.html`, { waitUntil: 'domcontentloaded' })
  const frame = page.frameLocator('iframe')
  // The Remote Crew rail entry used to be filtered out of an embedded pane.
  await frame.getByRole('button', { name: /remote crew/i }).first().waitFor({ timeout: 25000 })
  await page.waitForTimeout(800)
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log(`wrote ${OUT}/${name}.png`)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch()
  try {
    await shoot(base, browser, HEALTHY, 'switcher-tree')
    await shoot(base, browser, PARENT_DOWN, 'switcher-tree-parent-down')
    await shootPane(base, browser, HEALTHY, 'remote-crew-inside-a-pane')
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
