import fs from 'node:fs/promises'
import http from 'node:http'
import path from 'node:path'
import crypto from 'node:crypto'
import { createRequire } from 'node:module'

const [distArg, outputArg, playwrightArg] = process.argv.slice(2)
if (!distArg || !outputArg || !playwrightArg) throw new Error('Usage: node browser_catalog_probe.mjs DIST OUTPUT PLAYWRIGHT_PACKAGE')
const dist = path.resolve(distArg), output = path.resolve(outputArg)
await fs.mkdir(output, { recursive: true })
const { chromium } = createRequire(import.meta.url)(path.resolve(playwrightArg))
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
const requests = [], errors = [], results = []
let fixture = {}
const credentials = ['ALPHA', 'BETA', 'GAMMA'].map((label, i) => ({ id: i + 1, account_label: label,
  account_id: `${i + 1}`.repeat(12), access_key_masked: `AKIA***${label[0]}`, is_default: i === 0,
  account_identity_status: 'confirmed' }))
function catalogId(cid, kind, region) { return `${cid}-${region}-${kind}` }
async function respond(req, res, url) {
  let body = ''
  if (!['GET', 'HEAD'].includes(req.method)) for await (const chunk of req) body += chunk
  const record = { path: url.pathname, method: req.method, query: url.search, body, at: performance.now() }
  requests.push(record)
  const cid = Number(url.searchParams.get('credential_id')) || 1
  const region = url.searchParams.get('region') || 'us-east-1'
  let data, status = 200
  if (url.pathname === '/api/auth/me') data = { id: 71, username: 'catalog-audit', is_admin: false,
    has_credentials: true, credential_count: 3, monthly_limit_gb: null, created_at: '2026-11-01T00:00:00Z' }
  else if (url.pathname === '/api/credentials') data = { has_credentials: true, items: credentials }
  else if (url.pathname === '/api/v2/instances') data = {
    instances: fixture.snapshotState === 'stale' ? [{ name: 'old-snapshot-resource', region: 'us-east-1',
      state: 'running', credential_id: 1, account_label: 'ALPHA', is_static_ip: false }] : [], refreshing: false,
    scopes: [{ credential_id: 1, region: fixture.snapshotState === 'catalog_failure' ? null : 'us-east-1',
      status: fixture.snapshotState === 'catalog_failure' ? 'no_permission' : fixture.snapshotState || 'fresh',
      stale: fixture.snapshotState === 'stale', refreshing: false,
      last_success_at: fixture.snapshotState === 'stale' ? '2026-11-18T09:08:07Z' : null,
      error: fixture.snapshotState ? 'audit-permission-error-marker' : null }],
  }
  else if (url.pathname === '/api/traffic/summary') data = { year_month: '2026-11', instances: [], by_region: [], note: 'fixture' }
  else if (url.pathname === '/api/catalog/regions') {
    if (fixture.slowRegionCredential === cid) await sleep(1400)
    if (fixture.failedRegionCredential === cid) { status = 503; data = { detail: 'fixture region catalog unavailable' } }
    else data = ['us-east-1', 'eu-west-1'].map(name => ({ name, display_name: name }))
  } else if (['/api/catalog/blueprints', '/api/catalog/bundles'].includes(url.pathname)) {
    if (fixture.delayedCatalog?.credential === cid && fixture.delayedCatalog?.region === region) await sleep(1400)
    if (fixture.disabledCredential === cid && region === 'us-east-1') {
      status = 400; data = { detail: 'UnrecognizedClientException: fixture region disabled' }
    } else if (fixture.failedCatalog?.credential === cid && fixture.failedCatalog?.region === region) {
      status = 503; data = { detail: 'fixture catalog unavailable' }
    } else data = url.pathname.endsWith('blueprints')
      ? [{ blueprint_id: catalogId(cid, 'image', region), name: catalogId(cid, 'image', region),
        platform: cid === 1 ? 'WINDOWS' : 'LINUX_UNIX', is_active: true, type: 'os' }]
      : [{ bundle_id: catalogId(cid, 'bundle', region), name: catalogId(cid, 'bundle', region), is_active: true, price: 3 }]
  } else if (/\/credentials\/\d+\/regions\/.+\/status$/.test(url.pathname)) {
    if (fixture.delayStatus) await sleep(1400)
    fixture.statusReads = (fixture.statusReads || 0) + 1
    if (fixture.autoPoll && fixture.statusReads > 1) await sleep(1800)
    data = { region: 'us-east-1', status: fixture.autoPoll
      ? fixture.statusReads === 1 ? 'ENABLING' : 'ENABLED' : 'DISABLED' }
  } else if (/\/credentials\/\d+\/regions\/.+\/enable$/.test(url.pathname)) {
    if (fixture.delayEnable) await sleep(2200)
    if (fixture.enableMakesCatalogReady) fixture.disabledCredential = null
    data = { region: 'us-east-1', status: 'ENABLED', message: 'fixture old region enabled' }
  } else if (url.pathname === '/api/instances' && req.method === 'POST') {
    status = 400; data = { detail: 'Audit fixture intercepted create; no resource operation' }
  } else if (url.pathname.startsWith('/api/instances/')) data = { name: 'audit-detail', region: 'us-east-1', state: 'running',
    credential_id: cid, account_label: 'ALPHA', bundle_id: 'audit', is_static_ip: false }
  else if (url.pathname.startsWith('/api/metrics/')) {
    const period = url.searchParams.get('period')
    if (period === 'week') await sleep(1400)
    data = { points: [{ timestamp: period === 'week' ? '2026-11-11T01:00:00Z' : period === 'month'
      ? '2026-10-22T01:00:00Z' : '2026-11-19T01:00:00Z', network_in_bytes: 1048576, network_out_bytes: 2097152 }] }
  } else { status = 501; data = { detail: `Unhandled fixture ${url.pathname}` } }
  if (res.destroyed) return
  res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' })
  res.end(JSON.stringify(data))
}
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://127.0.0.1')
    if (url.pathname.startsWith('/api/')) return await respond(req, res, url)
    let target = path.resolve(dist, decodeURIComponent(url.pathname).replace(/^\/+/, '') || 'index.html')
    if (!target.startsWith(dist + path.sep)) throw new Error('Path outside fixture dist')
    let data
    try { data = await fs.readFile(target) } catch { target = path.join(dist, 'index.html'); data = await fs.readFile(target) }
    res.writeHead(200, { 'content-type': ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' })[path.extname(target)] || 'application/octet-stream' })
    res.end(data)
  } catch (error) { res.writeHead(500); res.end(String(error)) }
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const origin = `http://127.0.0.1:${server.address().port}`
const browser = await chromium.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true })
const contexts = []
async function open(url = '/create') {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, locale: 'zh-CN' })
  contexts.push(context)
  const page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort())
  await page.goto(origin + url)
  if (url === '/create') await page.getByRole('heading', { name: '开通实例' }).waitFor()
  return page
}
const field = (page, id) => page.locator('.ant-form-item').filter({ has: page.locator(`label[for="${id}"]`) })
async function choose(page, id, text) {
  await field(page, id).locator('.ant-select-selector').click()
  await page.locator('.ant-select-dropdown:visible .ant-select-item-option').filter({ hasText: text }).first().click()
}
async function fillSelections(page, cid, region = 'eu-west-1') {
  if (cid !== 1) await choose(page, 'credential_id', credentials[cid - 1].account_label)
  await choose(page, 'region', region)
  await choose(page, 'blueprint_id', catalogId(cid, 'image', region))
  await choose(page, 'bundle_id', catalogId(cid, 'bundle', region))
  await page.getByLabel('实例名称', { exact: true }).fill('audit-intercepted-create')
}
const posts = () => requests.filter(row => row.path === '/api/instances' && row.method === 'POST')
const values = async page => Object.fromEntries(await Promise.all(['region', 'blueprint_id', 'bundle_id'].map(async id =>
  [id, await field(page, id).locator('.ant-select-selection-item').allTextContents()])))
async function trySubmitWithoutWaiting(page) {
  const button = page.getByRole('button', { name: /^创\s*建$/ })
  const disabled = await button.isDisabled()
  // Native form submit also exercises the onFinish gate even if its button is disabled.
  await page.locator('form').evaluate(form => form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
  await page.waitForTimeout(150)
  return disabled
}
async function run(name, body) {
  fixture = {}
  const start = requests.length
  try { results.push({ scenario: name, ...(await body()), request_start: start, request_end: requests.length }) }
  catch (error) { results.push({ scenario: name, passed: false, error: error.stack, request_start: start, request_end: requests.length }) }
  finally { for (const context of contexts.splice(0)) await context.close() }
}
try {
  await run('new_credential_pending_clears_values_and_blocks_submit_then_valid_reselection', async () => {
    const page = await open()
    await fillSelections(page, 2)
    const before = posts().length
    fixture.slowRegionCredential = 3
    await choose(page, 'credential_id', 'GAMMA')
    const pendingValues = await values(page)
    const disabled = await trySubmitWithoutWaiting(page)
    const pendingPostCount = posts().length - before
    await page.screenshot({ path: path.join(output, 'pending-new-credential.png'), fullPage: true })
    await page.waitForTimeout(1450)
    await choose(page, 'region', 'eu-west-1')
    await choose(page, 'blueprint_id', catalogId(3, 'image', 'eu-west-1'))
    await choose(page, 'bundle_id', catalogId(3, 'bundle', 'eu-west-1'))
    await page.getByRole('button', { name: /^创\s*建$/ }).click()
    await page.waitForTimeout(100)
    const payloads = posts().slice(before).map(row => JSON.parse(row.body))
    return { passed: Object.values(pendingValues).every(v => v.length === 0) && pendingPostCount === 0
      && payloads.length === 1 && payloads[0].credential_id === 3 && payloads[0].blueprint_id === '3-eu-west-1-image'
      && payloads[0].bundle_id === '3-eu-west-1-bundle' && payloads[0].platform === 'LINUX_UNIX',
    disabled, pendingValues, pendingPostCount, payloads }
  })
  await run('failed_credential_regions_do_not_allow_old_catalog_submission', async () => {
    const page = await open(); await fillSelections(page, 2)
    fixture.failedRegionCredential = 3
    const before = posts().length
    await choose(page, 'credential_id', 'GAMMA')
    await page.waitForTimeout(250)
    const afterFailure = await values(page), disabled = await trySubmitWithoutWaiting(page)
    const count = posts().length - before
    return { passed: count === 0 && Object.values(afterFailure).every(v => v.length === 0), afterFailure, disabled, count }
  })
  await run('cached_credential_switch_still_requires_explicit_reselection', async () => {
    const page = await open(); await fillSelections(page, 1)
    await fillSelections(page, 2)
    const before = posts().length
    await choose(page, 'credential_id', 'ALPHA')
    const afterSwitch = await values(page), disabled = await trySubmitWithoutWaiting(page)
    const count = posts().length - before
    return { passed: count === 0 && Object.values(afterSwitch).every(v => v.length === 0), afterSwitch, disabled, count }
  })
  await run('region_change_pending_and_failure_clear_old_choices', async () => {
    const page = await open(); await fillSelections(page, 2)
    fixture.delayedCatalog = fixture.failedCatalog = { credential: 2, region: 'us-east-1' }
    const before = posts().length
    await choose(page, 'region', 'us-east-1')
    const pending = await values(page), disabled = await trySubmitWithoutWaiting(page)
    await page.waitForTimeout(1500)
    const failed = await values(page)
    await trySubmitWithoutWaiting(page)
    return { passed: posts().length === before && pending.blueprint_id.length === 0 && pending.bundle_id.length === 0
      && failed.blueprint_id.length === 0 && failed.bundle_id.length === 0, pending, failed, disabled }
  })
  await run('old_region_status_does_not_add_issue_to_new_credential', async () => {
    fixture.disabledCredential = 1; fixture.delayStatus = true
    const page = await open()
    await choose(page, 'region', 'us-east-1')
    for (let i = 0; i < 100 && !requests.some(row => row.path === '/api/credentials/1/regions/us-east-1/status'); i++) await sleep(10)
    await fillSelections(page, 2)
    await page.waitForTimeout(1500)
    const text = await page.locator('body').innerText(), state = await values(page)
    const button = page.getByRole('button', { name: '启用该区域' })
    const beforeEnable = requests.length
    if (await button.count()) { await button.click(); await page.waitForTimeout(150) }
    const unintendedEnable = requests.slice(beforeEnable).filter(row => row.method === 'POST' && row.path.endsWith('/enable'))
    return { passed: !text.includes('未启用') && unintendedEnable.length === 0
      && state.blueprint_id.some(v => v.includes('2-eu-west-1-image')),
      old_issue_visible: text.includes('未启用'), state, unintendedEnable }
  })
  await run('old_region_enable_callback_does_not_clear_new_credential_selection', async () => {
    fixture.disabledCredential = 1; fixture.delayEnable = true
    const page = await open(); await choose(page, 'region', 'us-east-1')
    await page.getByRole('button', { name: '启用该区域' }).click()
    await fillSelections(page, 2)
    await page.waitForTimeout(2400)
    const state = await values(page), text = await page.locator('body').innerText()
    await page.screenshot({ path: path.join(output, 'late-enable-callback.png'), fullPage: true })
    return { passed: state.blueprint_id.some(v => v.includes('2-eu-west-1-image'))
      && state.bundle_id.some(v => v.includes('2-eu-west-1-bundle')) && !text.includes('未启用'), state }
  })
  await run('metrics_slow_old_period_does_not_replace_new_period', async () => {
    const page = await open('/instances/us-east-1/audit-detail?credential_id=1')
    await page.getByRole('heading', { name: 'audit-detail' }).waitFor()
    const combo = page.locator('.ant-select-selector').first()
    await combo.click(); await page.getByText('近 7 天', { exact: true }).click()
    await page.waitForTimeout(100)
    await combo.click(); await page.getByText('近 30 天', { exact: true }).click()
    await page.waitForTimeout(1700)
    const axis = await page.locator('.recharts-xAxis').textContent()
    return { passed: axis.includes('10-22') && !axis.includes('11-11'), axis }
  })
  for (const state of ['stale', 'no_permission', 'catalog_failure']) await run(`dashboard_preserves_snapshot_${state}_status`, async () => {
    fixture.snapshotState = state
    const page = await open('/')
    await page.getByText(/^实例总数/).waitFor()
    await page.waitForTimeout(250)
    const dashboardText = await page.locator('body').innerText()
    const dashboard = { errorVisible: dashboardText.includes('audit-permission-error-marker'),
      lastSuccessVisible: dashboardText.includes('2026-11-18T09:08:07Z'),
      instanceCount: await page.locator('.ant-statistic').filter({ hasText: '实例总数' })
        .locator('.ant-statistic-content-value').innerText() }
    await page.screenshot({ path: path.join(output, `dashboard-${state}.png`), fullPage: true })
    await page.getByRole('menuitem', { name: /^cloud-server 实例$|^实例$/ }).click()
    await page.getByRole('heading', { name: '实例列表' }).waitFor()
    const instanceText = await page.locator('body').innerText()
    return { passed: dashboard.errorVisible && (state !== 'stale' || dashboard.lastSuccessVisible)
      && (state === 'stale' || dashboard.instanceCount !== '0'), dashboard,
      instancePageControlErrorVisible: instanceText.includes('audit-permission-error-marker'),
      instancePageControlLastSuccessVisible: instanceText.includes('2026-11-18T09:08:07Z') }
  })
  await run('same_scope_region_enable_allows_successful_catalog_reselection', async () => {
    fixture.disabledCredential = 1; fixture.enableMakesCatalogReady = true
    const page = await open(); await choose(page, 'region', 'us-east-1')
    const before = requests.length
    await page.getByRole('button', { name: '启用该区域' }).click()
    await choose(page, 'blueprint_id', catalogId(1, 'image', 'us-east-1'))
    await choose(page, 'bundle_id', catalogId(1, 'bundle', 'us-east-1'))
    await page.getByLabel('实例名称', { exact: true }).fill('audit-valid-enabled-region')
    const posted = posts().length
    await page.getByRole('button', { name: /^创\s*建$/ }).click()
    await page.waitForTimeout(150)
    const payloads = posts().slice(posted).map(row => JSON.parse(row.body))
    return { passed: payloads.length === 1 && payloads[0].credential_id === 1
      && payloads[0].region === 'us-east-1' && payloads[0].platform === 'WINDOWS'
      && requests.slice(before).some(row => row.path === '/api/credentials/1/regions/us-east-1/enable'), payloads }
  })
  await run('inflight_region_poll_after_credential_switch_cannot_clear_new_choices', async () => {
    fixture.disabledCredential = 1; fixture.autoPoll = true
    const before = requests.length
    const page = await open(); await choose(page, 'region', 'us-east-1')
    const count = () => requests.slice(before).filter(row => row.path === '/api/credentials/1/regions/us-east-1/status').length
    for (let i = 0; i < 1400 && count() < 2; i++) await sleep(10)
    if (count() < 2) throw new Error('Expected scheduled status poll to enter transport')
    await fillSelections(page, 2)
    await page.waitForTimeout(2000)
    const state = await values(page)
    return { passed: state.blueprint_id.some(v => v.includes('2-eu-west-1-image'))
      && state.bundle_id.some(v => v.includes('2-eu-west-1-bundle')), state, pollRequests: count() }
  })
} finally {
  const assets = []
  for (const name of ['index.html', ...(await fs.readdir(path.join(dist, 'assets'))).map(name => `assets/${name}`)]) {
    const bytes = await fs.readFile(path.join(dist, name))
    assets.push({ name, bytes: bytes.length, sha256: crypto.createHash('sha256').update(bytes).digest('hex') })
  }
  await fs.writeFile(path.join(output, 'browser-catalog.json'), JSON.stringify({ dist,
    scope: 'Exact built frontend, synthetic local API, all create calls intercepted with 400, no AWS',
    browser: browser.version(), assets, results, errors, requests }, null, 2))
  console.log(JSON.stringify({ results, errors }, null, 2))
  await browser.close()
  await new Promise(resolve => server.close(resolve))
}
