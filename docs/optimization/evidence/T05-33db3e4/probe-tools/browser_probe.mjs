import fs from 'node:fs/promises'
import http from 'node:http'
import path from 'node:path'
import { createRequire } from 'node:module'
import crypto from 'node:crypto'

const [distArg, outputArg, playwrightArg] = process.argv.slice(2)
if (!distArg || !outputArg || !playwrightArg) throw new Error('Usage: node browser_probe.mjs DIST OUTPUT PLAYWRIGHT_PACKAGE')
const dist = path.resolve(distArg)
const output = path.resolve(outputArg)
await fs.mkdir(output, { recursive: true })
const require = createRequire(import.meta.url)
const { chromium } = require(path.resolve(playwrightArg))
const chrome = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const counts = []
const pageErrors = []
let signedInUser = null
let delayedMeResponse = false
let delayedMeStarted = false
let slowInitialUnauthorized = false
let slowRegionCredential = null
let createPayload = null
const user = { id: 101, username: 'audit-panel-user', is_admin: false, monthly_limit_gb: null,
  created_at: '2026-11-01T00:00:00Z', has_credentials: true, credential_count: 2 }
const credentials = [{ id: 1, account_label: 'ALPHA', account_id: '123456789012', access_key_masked: 'AKIA***A',
  is_default: true, account_identity_status: 'confirmed' },
{ id: 2, account_label: 'BETA', account_id: '210987654321', access_key_masked: 'AKIA***B',
  is_default: false, account_identity_status: 'confirmed' },
{ id: 3, account_label: 'GAMMA', account_id: '333333333333', access_key_masked: 'AKIA***C',
  is_default: false, account_identity_status: 'confirmed' }]
let slowFirstAccount = false
let slowStarted = false
let slowMutation = false
let mutationStarted = false
let coldUntil = 0
let coldSnapshot = false
let slowTraffic = false
let slowSync = false
let syncStarted = false
let slowBillingRefresh = false
let billingRefreshStarted = false
const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms))
const metadata = { status: 'fresh', stale: false, refreshing: false,
  last_success_at: '2026-11-19T00:00:00Z', error: null }
const instance = (id, region = 'us-east-1') => ({ name: id === 1 ? 'alpha-resource-only' : 'beta-resource-only', region,
  state: 'running', public_ip: id === 1 ? '192.0.2.11' : '192.0.2.22', private_ip: null,
  blueprint_name: 'Audit Linux', bundle_id: 'nano_3_0', is_static_ip: false, credential_id: id,
  account_label: id === 1 ? 'ALPHA' : 'BETA', account_id: credentials[id - 1].account_id,
  auto_stop_on_limit: false, traffic: { in_gb: 1, out_gb: 2, total_gb: 3, limit_gb: null, over_limit: false } })
const traffic = { year_month: '2026-11', instances: [1, 2].map(id => ({ ...instance(id),
  in_gb: 1, out_gb: 2, total_gb: 3, limit_gb: null, over_limit: false, year_month: '2026-11' })),
  by_region: [{ region: 'us-east-1', total_gb: 6, instance_count: 2 }], note: 'Audit fixture' }

async function apiResponse(req, res, url) {
  let requestBody = ''
  if (!['GET', 'HEAD'].includes(req.method)) for await (const chunk of req) requestBody += chunk
  counts.push({ method: req.method, path: url.pathname, query: url.search, body: requestBody, time: performance.now() })
  let data = null
  if (url.pathname === '/api/auth/me') {
    if (slowInitialUnauthorized) {
      await sleep(1800)
      if (res.destroyed) return
      res.writeHead(401, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ detail: 'fixture unauthenticated initial request' }))
      return
    }
    data = signedInUser || user
    if (delayedMeResponse) { delayedMeStarted = true; await sleep(1500) }
  } else if (url.pathname === '/api/auth/login') {
    signedInUser = { ...user, id: 202, username: 'audit-other-user' }
    data = { user: signedInUser }
  } else if (url.pathname === '/api/auth/logout') { signedInUser = null; data = { message: 'logged out' } }
  else if (url.pathname === '/api/credentials/2/default') data = { has_credentials: true, items: credentials }
  else if (url.pathname === '/api/credentials') data = { has_credentials: true, items: credentials }
  else if (url.pathname === '/api/instances' && req.method === 'POST') {
    createPayload = JSON.parse(requestBody)
    res.writeHead(400, { 'content-type': 'application/json' })
    res.end(JSON.stringify({ detail: 'Audit fixture: mutation blocked' }))
    return
  }
  else if (url.pathname === '/api/v2/instances' || url.pathname === '/api/instances') {
    const selected = Number(url.searchParams.get('credential_id'))
    const ids = selected ? [selected] : [1, 2]
    if (slowFirstAccount && selected === 1) { slowStarted = true; await sleep(700) }
    const region = url.searchParams.get('region') || 'us-east-1'
    const instances = ids.map(id => instance(id, region))
    if (coldSnapshot && url.pathname === '/api/v2/instances') {
      if (!coldUntil) coldUntil = performance.now() + 250
      const refreshing = performance.now() < coldUntil
      data = { instances: refreshing ? [] : instances, refreshing,
        scopes: ids.map(id => ({ ...metadata, status: refreshing ? 'refreshing' : 'fresh', refreshing,
          last_success_at: refreshing ? null : metadata.last_success_at,
          credential_id: id, account_id: credentials[id - 1].account_id, region })) }
    } else data = url.pathname === '/api/instances' ? instances : { instances, refreshing: false,
      scopes: ids.map(id => ({ ...metadata, credential_id: id, account_id: credentials[id - 1].account_id, region })) }
  } else if (url.pathname === '/api/traffic/summary') {
    if (slowTraffic) await sleep(1100)
    const selected = Number(url.searchParams.get('credential_id'))
    const rows = selected ? traffic.instances.filter(row => row.credential_id === selected) : traffic.instances
    data = { ...traffic, instances: rows, by_region: [{ region: 'us-east-1', total_gb: rows.length * 3, instance_count: rows.length }] }
  } else if (url.pathname === '/api/traffic/sync') {
    if (slowSync) { syncStarted = true; await sleep(700) }
    data = { message: 'fixture traffic sync finished' }
  }
  else if (url.pathname === '/api/catalog/regions') {
    if (Number(url.searchParams.get('credential_id')) === slowRegionCredential) await sleep(1600)
    data = [{ name: 'us-east-1', display_name: 'US East' }, { name: 'eu-west-1', display_name: 'Europe West' }]
  }
  else if (url.pathname === '/api/catalog/bundles') {
    const selected = Number(url.searchParams.get('credential_id')) || 1
    const region = url.searchParams.get('region') || 'us-east-1'
    if (selected === 1 && region === 'us-east-1') await sleep(700)
    data = [{ bundle_id: selected === 1 ? 'alpha-bundle' : 'beta-bundle',
      name: selected === 1 ? 'alpha-bundle-only' : 'beta-bundle-only', price: 3, cpu_count: 1,
      ram_size_in_gb: 0.5, transfer_per_month_in_gb: 512, is_active: true }]
  } else if (url.pathname === '/api/catalog/blueprints') {
    const selected = Number(url.searchParams.get('credential_id')) || 1
    const region = url.searchParams.get('region') || 'us-east-1'
    if (selected === 1 && region === 'us-east-1') await sleep(700)
    data = [{ blueprint_id: selected === 1 ? 'alpha-linux' : 'beta-linux',
      name: selected === 1 ? 'alpha-image-only' : 'beta-image-only', platform: 'LINUX_UNIX',
      type: 'os', version: '2026', is_active: true }]
  }
  else if (url.pathname === '/api/v2/refresh') {
    if (slowBillingRefresh && requestBody.includes('billing')) { billingRefreshStarted = true; await sleep(700) }
    data = { accepted: true, kind: 'instances', scopes: [] }
  } else if (url.pathname === '/api/v2/billing/summary') {
    const selected = Number(url.searchParams.get('credential_id'))
    const ids = selected ? [selected] : [1, 2]
    const currency = (value) => ({ currency: 'USD', total_cost: value, net_cost: value, credits_applied: '0',
      refunds: '0', discounts: '0', lightsail_cost: value })
    const accounts = ids.map(id => ({ credential_id: id, account_id: credentials[id - 1].account_id,
      account_label: credentials[id - 1].account_label,
      costs: { status: 'ok', currencies: [currency(id === 1 ? '111' : '222')], cache: metadata,
        by_service: [{ ...currency(id === 1 ? '111' : '222'), service: id === 1 ? 'alpha-billing-only' : 'beta-billing-only' }] },
      forecast: { status: 'ok', amount: '5', currency: 'USD', cache: metadata },
      credits: { status: 'ok', credits: [], cache: metadata } }))
    data = { year_month: '2026-11', status: 'ok', refreshing: false, stale: false, note: 'Audit fixture',
      currencies: [currency(ids.length === 2 ? '333' : ids[0] === 1 ? '111' : '222')],
      manual_credits: [], pending_assignment: [], accounts }
  }
  else if (url.pathname.startsWith('/api/instances/')) {
    if (req.method === 'POST' && slowMutation) { mutationStarted = true; await sleep(700) }
    data = req.method === 'GET' ? instance(Number(url.searchParams.get('credential_id')) || 1) : { message: 'fixture operation accepted' }
  }
  else {
    res.writeHead(501, { 'content-type': 'application/json' })
    res.end(JSON.stringify({ detail: `Unimplemented audit API ${url.pathname}` }))
    return
  }
  if (res.destroyed) return
  res.writeHead(url.pathname === '/api/v2/refresh' ? 202 : 200, { 'content-type': 'application/json', 'cache-control': 'no-store' })
  res.end(JSON.stringify(data))
}
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://127.0.0.1')
    if (url.pathname.startsWith('/api/')) return await apiResponse(req, res, url)
    let relative = decodeURIComponent(url.pathname).replace(/^\/+/, '') || 'index.html'
    let file = path.resolve(dist, relative)
    if (!file.startsWith(dist + path.sep) && file !== path.join(dist, 'index.html')) throw new Error('Invalid path')
    let data
    try { data = await fs.readFile(file) } catch { file = path.join(dist, 'index.html'); data = await fs.readFile(file) }
    const ext = path.extname(file)
    res.writeHead(200, { 'content-type': ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' })[ext] || 'application/octet-stream' })
    res.end(data)
  } catch (error) { res.writeHead(500); res.end(String(error)) }
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const baseUrl = `http://127.0.0.1:${server.address().port}`
const browser = await chromium.launch({ executablePath: chrome, headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' })
const page = await context.newPage()
await page.route('**/*', route => new URL(route.request().url()).origin === baseUrl ? route.continue() : route.abort())
page.on('pageerror', error => pageErrors.push(error.message))
const results = []
try {
  coldSnapshot = true
  const coldContext = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' })
  const coldPage = await coldContext.newPage()
  await coldPage.route('**/*', route => new URL(route.request().url()).origin === baseUrl ? route.continue() : route.abort())
  await coldPage.goto(baseUrl)
  await coldPage.getByText('实例总数', { exact: true }).waitFor()
  await coldPage.waitForTimeout(1800)
  const coldValue = await coldPage.locator('.ant-statistic').filter({ hasText: '实例总数' })
    .locator('.ant-statistic-content-value').innerText()
  results.push({ scenario: 'cold_dashboard_continues_until_snapshot_data_available',
    observed_instance_count: coldValue, passed: coldValue === '2',
    snapshot_query_count: counts.filter(row => row.path === '/api/v2/instances').length })
  await coldPage.screenshot({ path: path.join(output, 'cold-dashboard.png'), fullPage: true })
  await coldContext.close()
  coldSnapshot = false
  counts.length = 0
  slowTraffic = true
  const partialContext = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' })
  const partialPage = await partialContext.newPage()
  await partialPage.route('**/*', route => new URL(route.request().url()).origin === baseUrl ? route.continue() : route.abort())
  await partialPage.goto(baseUrl)
  await partialPage.getByText('实例总数', { exact: true }).waitFor()
  await partialPage.waitForTimeout(250)
  const partialValue = await partialPage.locator('.ant-statistic').filter({ hasText: '实例总数' })
    .locator('.ant-statistic-content-value').innerText()
  results.push({ scenario: 'slow_traffic_does_not_block_successful_instances',
    observed_instance_count: partialValue, passed: partialValue === '2' })
  await partialPage.screenshot({ path: path.join(output, 'partial-dashboard.png'), fullPage: true })
  await partialContext.close()
  slowTraffic = false
  counts.length = 0
  await page.goto(baseUrl)
  await page.getByText('实例总数', { exact: true }).waitFor()
  await page.waitForFunction(() => !document.querySelector('.ant-spin-spinning'))
  const before = counts.filter(row => /\/api\/(v2\/)?instances$/.test(row.path)).length
  await page.getByRole('menuitem', { name: /^cloud-server 实例$|^实例$/ }).click()
  await page.getByText('alpha-resource-only', { exact: true }).first().waitFor()
  await page.waitForTimeout(100)
  const after = counts.filter(row => /\/api\/(v2\/)?instances$/.test(row.path)).length
  results.push({ scenario: 'dashboard_to_instances_shared_query', before, after, additional_http_queries: after - before })
  const combo = page.locator('.ant-select-selector').first()
  slowFirstAccount = true
  await combo.click()
  await page.locator('.ant-select-item-option').filter({ hasText: 'ALPHA' }).first().click()
  for (let i = 0; i < 100 && !slowStarted; i++) await sleep(10)
  if (!slowStarted) throw new Error('ALPHA filter request did not start')
  await combo.click()
  await page.locator('.ant-select-item-option').filter({ hasText: 'BETA' }).first().click()
  await page.getByText('beta-resource-only', { exact: true }).first().waitFor()
  await page.waitForTimeout(850)
  const body = await page.locator('body').innerText()
  results.push({ scenario: 'late_account_a_response_after_selecting_b',
    alpha_visible: body.includes('alpha-resource-only'), beta_visible: body.includes('beta-resource-only'),
    passed: !body.includes('alpha-resource-only') && body.includes('beta-resource-only') })
  slowFirstAccount = false
  await combo.click()
  await page.locator('.ant-select-item-option').filter({ hasText: 'ALPHA' }).first().click()
  await page.getByText('alpha-resource-only', { exact: true }).first().waitFor()
  slowMutation = true
  await page.getByRole('button', { name: /关\s*机/ }).first().click()
  for (let i = 0; i < 100 && !mutationStarted; i++) await sleep(10)
  if (!mutationStarted) throw new Error('Delayed mutation did not start')
  await combo.click()
  await page.locator('.ant-select-item-option').filter({ hasText: 'BETA' }).first().click()
  await page.getByText('beta-resource-only', { exact: true }).first().waitFor()
  await page.waitForTimeout(2600)
  const afterMutation = await page.locator('body').innerText()
  results.push({ scenario: 'account_a_operation_completes_after_selecting_b',
    alpha_visible: afterMutation.includes('alpha-resource-only'), beta_visible: afterMutation.includes('beta-resource-only'),
    selected: await page.locator('.ant-select-selection-item').first().innerText(),
    passed: !afterMutation.includes('alpha-resource-only') && afterMutation.includes('beta-resource-only') })
  await page.getByRole('menuitem', { name: /流量/ }).click()
  await page.getByRole('heading', { name: '流量监控' }).waitFor()
  const trafficCombo = page.locator('.ant-select-selector').first()
  await trafficCombo.click()
  await page.locator('.ant-select-item-option').filter({ hasText: 'ALPHA' }).first().click()
  await page.getByText('alpha-resource-only', { exact: true }).first().waitFor()
  slowSync = true
  await page.getByRole('button', { name: '立即同步' }).click()
  for (let i = 0; i < 100 && !syncStarted; i++) await sleep(10)
  if (!syncStarted) throw new Error('Delayed traffic sync did not start')
  await trafficCombo.click()
  await page.locator('.ant-select-item-option').filter({ hasText: 'BETA' }).first().click()
  await page.getByText('beta-resource-only', { exact: true }).first().waitFor()
  await page.waitForTimeout(1100)
  const afterSync = await page.locator('body').innerText()
  results.push({ scenario: 'account_a_traffic_sync_completes_after_selecting_b',
    alpha_visible: afterSync.includes('alpha-resource-only'), beta_visible: afterSync.includes('beta-resource-only'),
    selected: await page.locator('.ant-select-selection-item').first().innerText(),
    passed: !afterSync.includes('alpha-resource-only') && afterSync.includes('beta-resource-only') })
  await page.screenshot({ path: path.join(output, 'traffic-page.png'), fullPage: true })
  await page.getByRole('menuitem', { name: /账单/ }).click()
  await page.getByRole('heading', { name: '账单与赠金' }).waitFor()
  const billingCombo = page.locator('.ant-select-selector').first()
  await billingCombo.click()
  await page.locator('.ant-select-item-option').filter({ hasText: 'ALPHA' }).first().click()
  await page.getByText('alpha-billing-only', { exact: true }).first().waitFor()
  slowBillingRefresh = true
  await page.getByRole('button', { name: /刷\s*新/ }).first().click()
  for (let i = 0; i < 100 && !billingRefreshStarted; i++) await sleep(10)
  if (!billingRefreshStarted) throw new Error('Delayed billing refresh did not start')
  await billingCombo.click()
  await page.locator('.ant-select-item-option').filter({ hasText: 'BETA' }).first().click()
  await page.getByText('beta-billing-only', { exact: true }).first().waitFor()
  await page.waitForTimeout(1100)
  const afterBilling = await page.locator('body').innerText()
  results.push({ scenario: 'account_a_billing_refresh_completes_after_selecting_b',
    alpha_visible: afterBilling.includes('alpha-billing-only'), beta_visible: afterBilling.includes('beta-billing-only'),
    selected: await page.locator('.ant-select-selection-item').first().innerText(),
    passed: !afterBilling.includes('alpha-billing-only') && afterBilling.includes('beta-billing-only') })
  await page.getByRole('menuitem', { name: /开通实例/ }).click()
  await page.getByRole('heading', { name: '开通实例' }).waitFor()
  await page.locator('.ant-form-item').filter({ has: page.locator('label[for="region"]') }).locator('.ant-select-selector').click()
  await page.locator('.ant-select-item-option').filter({ hasText: 'us-east-1' }).first().click()
  for (let i = 0; i < 100 && !counts.some(row => row.path === '/api/catalog/blueprints' && row.query.includes('credential_id=1')); i++) await sleep(10)
  await page.locator('.ant-form-item').filter({ has: page.locator('label[for="credential_id"]') }).locator('.ant-select-selector').click()
  await page.locator('.ant-select-item-option').filter({ hasText: 'BETA' }).first().click()
  await page.waitForTimeout(150)
  await page.locator('.ant-form-item').filter({ has: page.locator('label[for="region"]') }).locator('.ant-select-selector').click()
  await page.locator('.ant-select-item-option').filter({ hasText: 'eu-west-1' }).first().click()
  await page.waitForTimeout(900)
  await page.locator('.ant-form-item').filter({ has: page.locator('label[for="blueprint_id"]') }).locator('.ant-select-selector').click()
  const options = await page.locator('.ant-select-item-option').allTextContents()
  results.push({ scenario: 'create_catalog_account_a_late_response_does_not_replace_b',
    image_options: options, passed: options.some(text => text.includes('beta-image-only'))
      && !options.some(text => text.includes('alpha-image-only')) })
  await page.locator('.ant-select-item-option').filter({ hasText: 'beta-image-only' }).first().click()
  await page.getByLabel('实例名称', { exact: true }).fill('audit-no-real-create')
  await page.locator('.ant-form-item').filter({ has: page.locator('label[for="bundle_id"]') }).locator('.ant-select-selector').click()
  await page.locator('.ant-select-item-option').filter({ hasText: 'beta-bundle-only' }).first().click()
  slowRegionCredential = 3
  await page.locator('.ant-form-item').filter({ has: page.locator('label[for="credential_id"]') }).locator('.ant-select-selector').click()
  await page.locator('.ant-select-item-option').filter({ hasText: 'GAMMA' }).first().click()
  const createDisabled = await page.getByRole('button', { name: /^创\s*建$/ }).isDisabled()
  await page.locator('form').evaluate(form => form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
  await page.waitForTimeout(150)
  results.push({ scenario: 'switch_create_credential_before_new_catalog_can_submit_old_values',
    captured_create_payload: createPayload, create_button_disabled: createDisabled,
    passed: createPayload === null })
  await page.screenshot({ path: path.join(output, 'create-pending-switch.png'), fullPage: true })
  await page.waitForTimeout(1600)
  slowRegionCredential = null
  await page.keyboard.press('Escape')
  await page.getByRole('menuitem', { name: /AWS 凭证/ }).click()
  await page.getByRole('heading', { name: 'AWS 凭证' }).waitFor()
  delayedMeResponse = true
  await page.getByRole('button', { name: '设为默认' }).first().click()
  for (let i = 0; i < 100 && !delayedMeStarted; i++) await sleep(10)
  if (!delayedMeStarted) throw new Error('Delayed auth/me did not start')
  await page.getByText('退出', { exact: false }).first().click()
  await page.getByLabel('用户名', { exact: true }).fill('audit-other-user')
  await page.getByLabel('密码', { exact: true }).fill('FixturePass123!')
  await page.getByRole('button', { name: /登\s*录/ }).click()
  await page.getByText('audit-other-user', { exact: true }).first().waitFor()
  await page.waitForTimeout(1700)
  const afterRelogin = await page.locator('body').innerText()
  results.push({ scenario: 'old_auth_me_response_after_logout_and_other_login',
    old_user_visible: afterRelogin.includes('audit-panel-user'), new_user_visible: afterRelogin.includes('audit-other-user'),
    server_signed_in_user: signedInUser?.username,
    passed: !afterRelogin.includes('audit-panel-user') && afterRelogin.includes('audit-other-user') })
  slowInitialUnauthorized = true
  const initialContext = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' })
  const initialPage = await initialContext.newPage()
  await initialPage.route('**/*', route => new URL(route.request().url()).origin === baseUrl ? route.continue() : route.abort())
  await initialPage.goto(baseUrl + '/login')
  await initialPage.getByLabel('用户名', { exact: true }).fill('audit-other-user')
  await initialPage.getByLabel('密码', { exact: true }).fill('FixturePass123!')
  await initialPage.getByRole('button', { name: /登\s*录/ }).click()
  await initialPage.waitForTimeout(2200)
  const initialBody = await initialPage.locator('body').innerText()
  results.push({ scenario: 'login_before_initial_auth_me_settles', url_path: new URL(initialPage.url()).pathname,
    dashboard_visible: initialBody.includes('实例总数'), spinner_visible: await initialPage.locator('.ant-spin-spinning').count() > 0,
    passed: initialBody.includes('实例总数') })
  await initialPage.screenshot({ path: path.join(output, 'initial-login-race.png'), fullPage: true })
  await initialContext.close()
  await page.screenshot({ path: path.join(output, 'final-page.png'), fullPage: true })
} catch (error) {
  results.push({ scenario: 'probe_execution', error: error.stack })
  await page.screenshot({ path: path.join(output, 'failure-page.png'), fullPage: true })
} finally {
  const assets = []
  for (const name of await fs.readdir(path.join(dist, 'assets'))) {
    const bytes = await fs.readFile(path.join(dist, 'assets', name))
    assets.push({ name, bytes: bytes.length, sha256: crypto.createHash('sha256').update(bytes).digest('hex') })
  }
  await fs.writeFile(path.join(output, 'browser.json'), JSON.stringify({ dist, browser: browser.version(),
    scope: 'Built frontend with local synthetic API responses; not production backend or AWS', results, pageErrors, requests: counts, assets }, null, 2))
  console.log(JSON.stringify({ results, pageErrors }, null, 2))
  await context.close()
  await browser.close()
  await new Promise(resolve => server.close(resolve))
}
