import { createServer } from 'node:http'
import { readFile, mkdir, stat } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { gzipSync } from 'node:zlib'

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const args = process.argv.slice(2)
const option = (name, fallback) => {
  const position = args.indexOf(name)
  return position < 0 ? fallback : args[position + 1]
}
const dist = path.resolve(option('--dist', path.join(repository, 'frontend/dist')))
const output = path.resolve(option('--output', path.join(repository, 'docs/optimization/evidence/cold-login-regression.json')))
const budgetOnly = args.includes('--budget-only')
const browserExecutable = option('--browser-executable', process.env.BROWSER_EXECUTABLE)
const playwrightModule = option('--playwright-module', process.env.PLAYWRIGHT_MODULE ?? 'playwright')
const cdpEndpoint = option('--cdp', process.env.PLAYWRIGHT_CDP_ENDPOINT)
const report = { created_at: new Date().toISOString(), dist, checks: [], scenarios: [], requests: [] }
const check = (name, condition, details = {}) => {
  report.checks.push({ name, passed: Boolean(condition), ...details })
  console.log(`${condition ? 'PASS' : 'FAIL'} ${name} ${JSON.stringify(details)}`)
}
const user = (name = 'admin') => ({ id: name === 'admin' ? 1 : 2, username: name,
  is_admin: name === 'admin', has_credentials: true, credential_count: 1,
  monthly_limit_gb: null, created_at: '2026-11-21T00:00:00Z' })
const sessions = new Map()
const apiCalls = []
const unexpectedApi = []
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' }
const instance = (owner) => ({ name: `instance-${owner?.username ?? 'anonymous'}`, region: 'ap-southeast-1',
  state: 'running', public_ip: '192.0.2.10', private_ip: '10.0.0.10', is_static_ip: false,
  credential_id: 1, account_label: '合成测试账号', traffic: null, monthly_limit_gb: null,
  auto_stop_on_limit: false })
let origin
const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url, origin ?? 'http://localhost')
    const sessionId = request.headers.cookie?.match(/(?:^|;\s*)fixture_session=([^;]+)/)?.[1]
    const json = (status, value, headers = {}) => {
      response.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...headers })
      response.end(JSON.stringify(value))
    }
    if (url.pathname.startsWith('/api/')) {
      apiCalls.push({ method: request.method, path: url.pathname, session: sessionId ?? null })
      if (url.pathname === '/api/auth/me') {
        const current = sessions.get(sessionId)
        return json(current ? 200 : 401, current ?? { detail: '未登录' })
      }
      if (url.pathname === '/api/auth/login' && request.method === 'POST') {
        let body = ''
        for await (const part of request) body += part
        const values = JSON.parse(body)
        if (values.password !== 'fixture-password') return json(401, { detail: '测试夹具：用户名或密码错误' })
        const id = `session-${sessions.size + 1}`
        const next = user(values.username)
        sessions.set(id, next)
        return json(200, { user: next }, { 'Set-Cookie': `fixture_session=${id}; HttpOnly; SameSite=Lax; Path=/` })
      }
      if (url.pathname === '/api/auth/logout' && request.method === 'POST') {
        sessions.delete(sessionId)
        return json(200, { ok: true }, { 'Set-Cookie': 'fixture_session=; Max-Age=0; HttpOnly; Path=/' })
      }
      if (url.pathname === '/api/credentials') return json(200, { has_credentials: true, items: [
        { id: 1, access_key_masked: 'TEST****ONLY', account_label: '合成测试账号', is_default: true },
      ] })
      if (url.pathname === '/api/v2/query-instances') {
        if (!sessions.has(sessionId)) return json(401, { detail: '登录已失效' })
        return json(200, { instances: [instance(sessions.get(sessionId))], scopes: [], refreshing: false,
          rounds: [], discovery: [] })
      }
      if (url.pathname === '/api/traffic/summary') return json(200, { year_month: '2026-11', instances: [], by_region: [], note: '合成数据' })
      if (url.pathname.startsWith('/api/instances/')) return json(200, instance(sessions.get(sessionId)))
      if (url.pathname.startsWith('/api/metrics/')) return json(200, { points: [] })
      if (url.pathname === '/api/v2/tasks') return json(200, [])
      if (url.pathname.startsWith('/api/v2/tasks/')) return json(200, {
        task_id: 'fixture-task', credential_id: 1, account_id: 'fixture-account', region: 'ap-southeast-1',
        kind: 'traffic_sync', resource_name: null, status: 'succeeded', current_step: 'done',
        can_cancel: false, can_resume: false, result: { count: 0 }, error: null, steps: [],
      })
      unexpectedApi.push({ method: request.method, path: url.pathname })
      return json(404, { detail: `未定义测试夹具：${url.pathname}` })
    }
    const relative = url.pathname === '/' ? 'index.html' : decodeURIComponent(url.pathname).replace(/^\/+/, '')
    let target = path.resolve(dist, relative)
    if (!(target === dist || target.startsWith(`${dist}${path.sep}`))) {
      response.writeHead(403).end()
      return
    }
    try { if (!(await stat(target)).isFile()) throw new Error('not file') }
    catch { target = path.join(dist, 'index.html') }
    const body = await readFile(target)
    response.writeHead(200, { 'Content-Type': mime[path.extname(target)] ?? 'application/octet-stream', 'Cache-Control': 'no-store' })
    response.end(body)
  } catch (error) {
    response.writeHead(500).end(String(error))
  }
})

let browser
try {
  const html = await readFile(path.join(dist, 'index.html'), 'utf8')
  const scripts = [...html.matchAll(/<script\b[^>]*\bsrc=["']([^"']+)["'][^>]*>/gi)].map(match => match[1])
  const staticResources = new Map()
  const visit = async (url) => {
    if (!url.startsWith('/') && !url.startsWith('./')) return
    const assetPath = url.startsWith('/') ? url.slice(1) : url.slice(2)
    if (staticResources.has(assetPath)) return
    const body = await readFile(path.join(dist, assetPath))
    const source = body.toString()
    staticResources.set(assetPath, { url: `/${assetPath}`, raw: body.length,
      gzip: gzipSync(body, { level: 6 }).length, axios: source.includes('AxiosError'),
      router: /react-router v|React Router/.test(source) })
    for (const match of source.matchAll(/(?:^|[;\n])(?:import|export)\s*(?:[\w\s{},*$]+?\s*from\s*)?["']([^"']+)["']/g)) {
      const imported = path.posix.normalize(path.posix.join(path.posix.dirname(assetPath), match[1]))
      await visit(`/${imported}`)
    }
  }
  for (const url of scripts) await visit(url)
  const staticFiles = [...staticResources.values()]
  const staticTotals = staticFiles.reduce((sum, file) => ({ raw: sum.raw + file.raw, gzip: sum.gzip + file.gzip }), { raw: 0, gzip: 0 })
  report.build = { ...staticTotals, resources: staticFiles,
    stylesheets: [...html.matchAll(/<link\b[^>]*\brel=["']stylesheet["'][^>]*>/gi)].length }
  check('冷登录 JavaScript 原始大小低于 190 KiB', staticTotals.raw < 190 * 1024, { bytes: staticTotals.raw })
  check('冷登录 JavaScript gzip 大小低于 65 KiB', staticTotals.gzip < 65 * 1024, { bytes: staticTotals.gzip })
  check('首屏没有外链样式表', report.build.stylesheets === 0, { count: report.build.stylesheets })
  check('初始模块图不包含 Axios', !staticFiles.some(file => file.axios))
  check('初始模块图不包含 React Router', !staticFiles.some(file => file.router))
  if (budgetOnly) throw { budgetDone: true }
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  origin = `http://127.0.0.1:${server.address().port}`
  const moduleSpecifier = path.isAbsolute(playwrightModule) ? pathToFileURL(playwrightModule).href : playwrightModule
  const { chromium } = await import(moduleSpecifier)
  browser = cdpEndpoint ? await chromium.connectOverCDP(cdpEndpoint)
    : await chromium.launch({ ...(browserExecutable ? { executablePath: browserExecutable } : {}), headless: true })
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  const page = await context.newPage()
  const loadedJs = new Set()
  const pageErrors = []
  page.on('pageerror', error => pageErrors.push(error.message))
  page.on('response', response => {
    const url = new URL(response.url())
    report.requests.push({ path: url.pathname, status: response.status() })
    if (url.pathname.endsWith('.js')) loadedJs.add(url.pathname)
  })
  const started = performance.now()
  await page.goto(`${origin}/login`)
  await page.getByRole('heading', { name: 'Lightsail 管理平台', exact: true }).waitFor()
  const titleReadyMs = Math.round(performance.now() - started)
  await page.waitForLoadState('networkidle')
  const resources = []
  for (const url of loadedJs) {
    const source = await readFile(path.join(dist, url.replace(/^\//, '')))
    resources.push({ url, raw: source.length, gzip: gzipSync(source, { level: 6 }).length,
      axios: source.includes('AxiosError'), router: /react-router v|React Router/.test(source.toString()) })
  }
  const totals = resources.reduce((sum, file) => ({ raw: sum.raw + file.raw, gzip: sum.gzip + file.gzip }), { raw: 0, gzip: 0 })
  report.cold_login = { title_ready_ms: titleReadyMs, resources, ...totals,
    stylesheets: await page.locator('link[rel="stylesheet"]').count(),
    inline_styles: await page.locator('style').count(),
    title_font_size: await page.getByRole('heading', { name: 'Lightsail 管理平台', exact: true }).evaluate(element => getComputedStyle(element).fontSize) }
  check('浏览器冷登录 JavaScript 原始大小低于 190 KiB', totals.raw < 190 * 1024, { bytes: totals.raw })
  check('浏览器冷登录 JavaScript gzip 大小低于 65 KiB', totals.gzip < 65 * 1024, { bytes: totals.gzip })
  check('浏览器首屏没有外链样式表', report.cold_login.stylesheets === 0, { count: report.cold_login.stylesheets })
  check('登录前不加载 Axios', !resources.some(file => file.axios))
  check('登录前不加载 React Router', !resources.some(file => file.router))
  check('登录前不下载后台页面', ![...loadedJs].some(url => /\/(?:Layout|AuthenticatedApp|Dashboard|Instances|Tasks|Users)-/.test(url)),
    { paths: [...loadedJs] })
  check('登录页样式生效', report.cold_login.title_font_size === '24px', { title_font_size: report.cold_login.title_font_size })
  const fillLogin = async (target, username = 'admin', password = 'fixture-password') => {
    await target.getByLabel('用户名', { exact: true }).fill(username)
    await target.getByLabel('密码', { exact: true }).fill(password)
    await target.getByRole('button', { name: '登录', exact: true }).click()
  }
  const dashboard = async target => target.getByRole('heading', { name: '总览', exact: true }).waitFor()
  const loginPage = async target => target.getByRole('heading', { name: 'Lightsail 管理平台', exact: true }).waitFor()
  const fixtureSession = async (targetContext, name = 'admin') => {
    const id = `session-${sessions.size + 1}-${name}`
    sessions.set(id, user(name))
    await targetContext.addCookies([{ name: 'fixture_session', value: id, url: origin, httpOnly: true, sameSite: 'Lax' }])
    return id
  }
  const scenario = async (name, run) => {
    const scenarioStarted = performance.now()
    const targetContext = await browser.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' })
    const target = await targetContext.newPage()
    const errors = []
    target.on('pageerror', error => errors.push(error.message))
    try {
      await run(target, targetContext)
      if (errors.length) throw new Error(`运行时异常：${errors.join('; ')}`)
      report.scenarios.push({ name, passed: true, elapsed_ms: Math.round(performance.now() - scenarioStarted) })
      check(name, true)
    } catch (error) {
      const message = error.stack ?? String(error)
      report.scenarios.push({ name, passed: false, error: message, elapsed_ms: Math.round(performance.now() - scenarioStarted) })
      check(name, false, { error: message })
    } finally { await targetContext.close() }
  }
  await scenario('匿名深链跳转登录页', async target => {
    for (const route of ['/instances', '/tasks/fixture-task', '/unknown-route']) {
      await target.goto(`${origin}${route}`)
      await loginPage(target)
      await target.waitForURL(`${origin}/login`)
    }
  })
  await scenario('错误密码显示服务端说明', async target => {
    await target.goto(`${origin}/login`)
    await fillLogin(target, 'admin', 'wrong-password')
    await target.getByRole('alert').filter({ hasText: '测试夹具：用户名或密码错误' }).waitFor()
    if (new URL(target.url()).pathname !== '/login') throw new Error('错误密码后未留在登录页')
  })
  await scenario('登录、退出及缓存路由再次登录', async target => {
    await target.goto(`${origin}/login`)
    await fillLogin(target)
    await dashboard(target)
    await target.waitForURL(`${origin}/`)
    await target.getByText('退出', { exact: true }).click()
    await loginPage(target)
    await target.waitForURL(`${origin}/login`)
    await fillLogin(target)
    await dashboard(target)
    await target.waitForURL(`${origin}/`)
  })
  await scenario('已登录深链保留实例、任务与实例详情路由', async (target, targetContext) => {
    await fixtureSession(targetContext)
    await target.goto(`${origin}/instances`)
    await target.getByRole('heading', { name: '实例列表', exact: true }).waitFor()
    await target.getByText('instance-admin', { exact: true }).first().waitFor()
    await target.goto(`${origin}/tasks/fixture-task`)
    await target.getByRole('heading', { name: '任务中心', exact: true }).waitFor()
    await target.getByText('任务详情', { exact: true }).waitFor()
    await target.goto(`${origin}/instances/ap-southeast-1/instance-admin?credential_id=1`)
    await target.getByRole('heading', { name: 'instance-admin', exact: true }).waitFor()
    if (!target.url().endsWith('/instances/ap-southeast-1/instance-admin?credential_id=1')) throw new Error('实例详情路由或查询参数丢失')
  })
  await scenario('非管理员访问用户管理回总览', async (target, targetContext) => {
    await fixtureSession(targetContext, 'member')
    await target.goto(`${origin}/users`)
    await dashboard(target)
    await target.waitForURL(`${origin}/`)
    if (await target.getByRole('menuitem', { name: '用户管理' }).count()) throw new Error('普通用户看到用户管理菜单')
  })
  for (const exitBeforeRelease of [false, true]) {
    await scenario(exitBeforeRelease ? '迟到的初始认证不覆盖退出状态' : '迟到的初始认证不覆盖新登录', async (target, targetContext) => {
      let release
      const gate = new Promise(resolve => { release = resolve })
      let received = 0
      await targetContext.route('**/api/auth/me', async route => {
        received += 1
        await gate
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(user('stale-user')) }).catch(() => {})
      })
      try {
        await target.goto(`${origin}/login`, { waitUntil: 'domcontentloaded' })
        await loginPage(target)
        await target.waitForFunction(() => document.querySelector('#login-password') !== null)
        await fillLogin(target)
        await dashboard(target)
        if (!received) throw new Error('未拦截初始认证请求')
        if (exitBeforeRelease) {
          await target.getByText('退出', { exact: true }).click()
          await loginPage(target)
          await target.waitForURL(`${origin}/login`)
        }
        release()
        await target.waitForTimeout(250)
        if (exitBeforeRelease) {
          await loginPage(target)
          if (new URL(target.url()).pathname !== '/login') throw new Error('迟到认证改变退出路由')
        } else {
          await dashboard(target)
          await target.getByText('admin（管理员）', { exact: true }).waitFor()
        }
        if (await target.getByText('stale-user', { exact: true }).count()) throw new Error('迟到认证覆盖当前用户')
      } finally { release() }
    })
  }
  await scenario('切换账号后不复用上一账号实例缓存', async target => {
    await target.goto(`${origin}/login`)
    await fillLogin(target)
    await dashboard(target)
    await target.getByRole('menuitem', { name: '实例', exact: true }).click()
    await target.getByText('instance-admin', { exact: true }).first().waitFor()
    await target.getByText('退出', { exact: true }).click()
    await loginPage(target)
    await fillLogin(target, 'member')
    await dashboard(target)
    await target.getByRole('menuitem', { name: '实例', exact: true }).click()
    await target.getByText('instance-member', { exact: true }).first().waitFor()
    if (await target.getByText('instance-admin', { exact: true }).count()) throw new Error('上个账号实例仍可见')
  })
  await scenario('会话失效返回登录页', async (target, targetContext) => {
    const id = await fixtureSession(targetContext)
    await target.goto(`${origin}/`)
    await dashboard(target)
    sessions.delete(id)
    await targetContext.route('**/api/credentials', route => route.fulfill({ status: 401,
      contentType: 'application/json', body: JSON.stringify({ detail: '登录已失效' }) }))
    await target.getByRole('menuitem', { name: '实例', exact: true }).click()
    await loginPage(target)
    await target.waitForURL(`${origin}/login`)
  })
  await context.close()
  check('浏览器无运行时异常', pageErrors.length === 0, { errors: pageErrors })
  check('合成 API 无缺失夹具', unexpectedApi.length === 0, { requests: unexpectedApi })
  report.api_calls = apiCalls
} catch (error) {
  if (!error.budgetDone) check('浏览器回归执行', false, { error: error.stack ?? String(error) })
} finally {
  if (browser) await browser.close()
  if (server.listening) await new Promise(resolve => server.close(resolve))
  report.passed = report.checks.length > 0 && report.checks.every(item => item.passed)
  await mkdir(path.dirname(output), { recursive: true })
  await import('node:fs/promises').then(fs => fs.writeFile(output, JSON.stringify(report, null, 2)))
  console.log(`报告：${output}`)
  process.exitCode = report.passed ? 0 : 1
}
