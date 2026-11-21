import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { mkdtemp, readFile, realpath, rm } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { MessageChannel } from 'node:worker_threads'
import test from 'node:test'
import { build, stop } from 'esbuild'
import { CookieJar, JSDOM, VirtualConsole } from 'jsdom'

const frontend = fileURLToPath(new URL('..', import.meta.url))
const nativeFetch = globalThis.fetch
const temporaryRoot = await realpath(path.join(frontend, 'node_modules'))
const compiled = await mkdtemp(path.join(temporaryRoot, '.auth-routing-'))
const mutation = process.env.AUTH_ROUTING_MUTATION
await build({
  entryPoints: [path.join(frontend, 'tests/authRouting.fixture.tsx')],
  outdir: compiled,
  bundle: true,
  splitting: true,
  format: 'esm',
  platform: 'browser',
  jsx: 'automatic',
  outExtension: { '.js': '.mjs' },
  external: ['react', 'react/*', 'react-dom', 'react-dom/*'],
  define: { 'process.env.NODE_ENV': '"test"' },
  loader: { '.css': 'empty' },
  logLevel: 'silent',
  plugins: mutation ? [{
    name: 'auth-routing-mutation',
    setup(builder) {
      builder.onLoad({ filter: /[\\/]src[\\/](?:App|AuthenticatedApp|auth[\\/]AuthContext)\.tsx$/ }, async ({ path: sourcePath }) => {
        let contents = await readFile(sourcePath, 'utf8')
        if (mutation === 'anonymous-route' && sourcePath.endsWith(`${path.sep}App.tsx`)) {
          contents = contents.replace("if (!user && path !== '/login')", 'if (false)')
        }
        if (mutation === 'scope-switch' && sourcePath.endsWith('AuthContext.tsx')) {
          contents = contents.replace('setQueryCacheScope(`${data.user.id}:${generation}`)', 'void 0')
        }
        if (mutation === 'cached-login' && sourcePath.endsWith('AuthenticatedApp.tsx')) {
          contents = contents.replace(
            '<Route path="/login" element={<Navigate to="/" replace />} />',
            '<Route path="/login" element={<Navigate to="/instances" replace />} />',
          )
        }
        return { contents, loader: 'tsx' }
      })
    },
  }] : [],
})
test.after(async () => {
  stop()
  const resolved = await realpath(compiled)
  assert.equal(path.dirname(resolved), temporaryRoot)
  assert.ok(path.basename(resolved).startsWith('.auth-routing-'))
  await rm(resolved, { recursive: true, force: true })
})
let ui

const user = (name = 'admin') => ({
  id: name === 'admin' ? 1 : 2,
  username: name,
  is_admin: name === 'admin',
  has_credentials: false,
  credential_count: 0,
  monthly_limit_gb: null,
  created_at: '2026-11-21T00:00:00Z',
})

async function withApp(t, { route = '/login', signedIn, holdMe = false } = {}, run) {
  const sessions = new Map()
  const requests = []
  const pendingMe = []
  const cookieJar = new CookieJar()
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, 'http://localhost')
    const cookie = request.headers.cookie?.match(/(?:^|;\s*)fixture_session=([^;]+)/)?.[1]
    requests.push({ method: request.method, path: url.pathname, cookie })
    const json = (status, value, headers = {}) => {
      response.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...headers })
      response.end(JSON.stringify(value))
    }
    if (url.pathname === '/api/auth/me') {
      if (holdMe) {
        pendingMe.push((value) => json(200, value))
        return
      }
      const current = sessions.get(cookie)
      json(current ? 200 : 401, current ?? { detail: '未登录' })
    } else if (url.pathname === '/api/auth/login') {
      let body = ''
      for await (const part of request) body += part
      const credentials = JSON.parse(body)
      if (credentials.password !== 'fixture-password') return json(401, { detail: '用户名或密码错误' })
      const id = `session-${sessions.size + 1}`
      const current = user(credentials.username)
      sessions.set(id, current)
      json(200, { user: current }, { 'Set-Cookie': `fixture_session=${id}; HttpOnly; SameSite=Lax; Path=/` })
    } else if (url.pathname === '/api/auth/logout') {
      sessions.delete(cookie)
      json(200, { ok: true }, { 'Set-Cookie': 'fixture_session=; Max-Age=0; HttpOnly; Path=/' })
    } else if (url.pathname === '/api/v2/tasks/fixture-task') {
      if (!sessions.has(cookie)) return json(401, { detail: '未登录' })
      json(200, { task_id: 'fixture-task', credential_id: 1, account_id: 'fixture-account',
        region: 'ap-southeast-1', kind: 'traffic_sync', resource_name: null, status: 'succeeded',
        current_step: 'done', can_cancel: false, can_resume: false, result: { count: 0 },
        error: null, steps: [], created_at: '2026-11-21T00:00:00Z' })
    } else {
      json(404, { detail: `未定义测试接口：${url.pathname}` })
    }
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const origin = `http://127.0.0.1:${server.address().port}`
  if (signedIn) {
    sessions.set('initial', user(signedIn))
    await cookieJar.setCookie('fixture_session=initial; HttpOnly; SameSite=Lax; Path=/', origin)
  }
  const errors = []
  const virtualConsole = new VirtualConsole()
  virtualConsole.on('jsdomError', error => errors.push(error.message))
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: `${origin}${route}`, cookieJar, pretendToBeVisual: true, virtualConsole,
  })
  const { window } = dom
  const channels = new Set()
  window.matchMedia = query => ({ matches: false, media: query, onchange: null,
    addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false } })
  window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const getComputedStyle = window.getComputedStyle.bind(window)
  window.getComputedStyle = element => getComputedStyle(element)
  const globals = {
    window, document: window.document, navigator: window.navigator,
    HTMLElement: window.HTMLElement, Element: window.Element, Node: window.Node,
    SVGElement: window.SVGElement, ShadowRoot: window.ShadowRoot, MutationObserver: window.MutationObserver,
    XMLHttpRequest: window.XMLHttpRequest, FormData: window.FormData,
    getComputedStyle: window.getComputedStyle, ResizeObserver: window.ResizeObserver,
    localStorage: window.localStorage, requestAnimationFrame: window.requestAnimationFrame.bind(window),
    cancelAnimationFrame: window.cancelAnimationFrame.bind(window), IS_REACT_ACT_ENVIRONMENT: true,
    // rc-overflow creates browser channels without closing them; Node ports need explicit teardown.
    MessageChannel: class extends MessageChannel {
      constructor() { super(); channels.add(this) }
    },
    fetch: async (input, options = {}) => {
      const url = new URL(input, window.location.href)
      const headers = new Headers(options.headers)
      const cookie = await cookieJar.getCookieString(url.href)
      if (cookie && options.credentials === 'include') headers.set('Cookie', cookie)
      const response = await nativeFetch(url, { ...options, headers })
      for (const value of response.headers.getSetCookie()) await cookieJar.setCookie(value, url.href)
      return response
    },
  }
  const saved = new Map(Object.keys(globals).map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]))
  for (const [name, value] of Object.entries(globals)) Object.defineProperty(globalThis, name, { configurable: true, writable: true, value })
  let root
  try {
    ui ??= await import(pathToFileURL(path.join(compiled, 'authRouting.fixture.mjs')).href)
    ui.setQueryCacheScope('anonymous')
    root = ui.createRoot(document.getElementById('root'))
    await ui.act(async () => root.render(ui.createElement(ui.AuthRoutingFixture)))
    const waitFor = async (assertion, description) => {
      const deadline = Date.now() + 5000
      let lastError
      while (Date.now() < deadline) {
        await ui.act(async () => new Promise(resolve => setTimeout(resolve, 20)))
        try { assertion(); return } catch (error) { lastError = error }
      }
      throw new Error(`${description}: ${lastError?.message}\nDOM: ${document.body.textContent}`, { cause: lastError })
    }
    const text = () => document.body.textContent
    const session = () => JSON.parse(document.getElementById('session-probe').textContent)
    const login = async (name = 'admin', password = 'fixture-password') => {
      document.getElementById('login-username').value = name
      document.getElementById('login-password').value = password
      await ui.act(async () => document.querySelector('form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true })))
    }
    const dashboard = () => waitFor(() => {
      assert.match(text(), /尚未绑定 AWS 凭证/)
      assert.equal(window.location.pathname, '/')
    }, '总览应显示且地址为 /')
    const loginPage = () => waitFor(() => {
      assert.ok(document.getElementById('login-password'))
      assert.equal(window.location.pathname, '/login')
    }, '登录页应显示且地址为 /login')
    const logout = async () => {
      const link = [...document.querySelectorAll('a')].find(element => element.textContent.includes('退出'))
      assert.ok(link, '应显示真实 Layout 的退出入口')
      await ui.act(async () => link.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })))
      await loginPage()
      await waitFor(() => assert.equal(sessions.size, 0), '服务端会话应退出')
    }
    const releaseMe = async (value = user('stale-user')) => {
      holdMe = false
      for (const release of pendingMe.splice(0)) release(value)
      await ui.act(async () => new Promise(resolve => setTimeout(resolve, 60)))
    }
    await run({ window, text, session, login, logout, dashboard, loginPage, waitFor,
      requests, pendingMe, releaseMe, cookieJar, origin, ui })
    assert.deepEqual(requests.filter(request => !['/api/auth/me', '/api/auth/login', '/api/auth/logout', '/api/v2/tasks/fixture-task'].includes(request.path)), [])
    assert.deepEqual(errors, [])
  } finally {
    if (root) await ui.act(async () => root.unmount())
    for (const release of pendingMe.splice(0)) release(user('stale-user'))
    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
    for (const channel of channels) { channel.port1.close(); channel.port2.close() }
    window.close()
    for (const [name, descriptor] of saved) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor)
      else delete globalThis[name]
    }
  }
}

test('anonymous deep links render login and replace the protected URL', async t => {
  for (const route of ['/instances', '/tasks/fixture-task', '/unknown-route']) {
    await withApp(t, { route }, async ({ loginPage, session }) => {
      await loginPage()
      assert.equal(session().user, null)
      assert.equal(session().loading, false)
    })
  }
})

test('wrong password displays the FastAPI error and keeps the anonymous session', async t => {
  await withApp(t, {}, async ({ loginPage, login, waitFor, text, session, window }) => {
    await loginPage()
    await login('admin', 'wrong-password')
    await waitFor(() => assert.match(document.querySelector('[role="alert"]').textContent, /用户名或密码错误/), '登录错误应可见')
    assert.equal(window.location.pathname, '/login')
    assert.equal(session().user, null)
    assert.doesNotMatch(text(), /尚未绑定 AWS 凭证/)
  })
})

test('login, logout, and login with a cached Router keep DOM and URL aligned', async t => {
  await withApp(t, {}, async ({ loginPage, login, logout, dashboard, cookieJar, origin, session }) => {
    await loginPage()
    await login()
    await dashboard()
    assert.equal(session().user.username, 'admin')
    assert.match(await cookieJar.getCookieString(origin), /fixture_session=/)
    assert.equal(document.cookie, '', '认证 Cookie 应为 HttpOnly')
    await logout()
    assert.equal(session().user, null)
    await login()
    await dashboard()
    assert.equal(session().user.username, 'admin')
  })
})

test('signed-in instance and task deep links retain their route and query', async t => {
  await withApp(t, { route: '/instances?credential_id=4', signedIn: 'admin' }, async ({ waitFor, text, window }) => {
    await waitFor(() => assert.match(text(), /请先绑定 AWS 凭证/), '实例页应显示真实无凭据分支')
    assert.equal(window.location.pathname + window.location.search, '/instances?credential_id=4')
  })
  await withApp(t, { route: '/tasks/fixture-task', signedIn: 'admin' }, async ({ waitFor, text, window, requests }) => {
    await waitFor(() => assert.match(text(), /任务详情/), '任务直链应加载任务详情')
    assert.equal(window.location.pathname, '/tasks/fixture-task')
    assert.ok(requests.some(request => request.path === '/api/v2/tasks/fixture-task' && request.cookie === 'initial'))
  })
})

test('a non-admin user is redirected away from user management', async t => {
  await withApp(t, { route: '/users', signedIn: 'member' }, async ({ dashboard, session }) => {
    await dashboard()
    assert.equal(session().user.is_admin, false)
    assert.ok(![...document.querySelectorAll('[role="menuitem"]')].some(element => element.textContent.includes('用户管理')))
  })
})

for (const exitBeforeRelease of [false, true]) {
  test(`late initial authentication cannot overwrite ${exitBeforeRelease ? 'logout' : 'a new login'}`, async t => {
    await withApp(t, { holdMe: true }, async ({ pendingMe, waitFor, login, logout, dashboard, releaseMe, loginPage, session }) => {
      await waitFor(() => assert.equal(pendingMe.length, 1), '初始认证请求应被延迟')
      await login()
      await dashboard()
      if (exitBeforeRelease) await logout()
      await releaseMe()
      if (exitBeforeRelease) {
        await loginPage()
        assert.equal(session().user, null)
      } else {
        await dashboard()
        assert.equal(session().user.username, 'admin')
      }
    })
  })
}

test('account changes retire cached data and in-flight reads and clear task submissions', async t => {
  await withApp(t, {}, async ({ loginPage, login, logout, dashboard, ui }) => {
    await loginPage()
    await login()
    await dashboard()
    assert.match(ui.getQueryCacheScope(), /^1:/)
    const firstKey = ui.queryCacheKey('/credentials', {})
    await ui.queryGet(firstKey, async () => ['alice-only'], { ttlMs: 60_000, tags: ['credentials'] })
    let transportSignal
    const pending = ui.queryGet(ui.queryCacheKey('/pending'), signal => {
      transportSignal = signal
      return new Promise(() => {})
    }, { tags: ['instances'] })
    const cancelled = assert.rejects(pending, { name: 'AbortError' })
    ui.liveTaskRequests.set('alice-intent', 'alice-body')
    await logout()
    await cancelled
    assert.equal(transportSignal.aborted, true)
    assert.equal(ui.getQueryCacheScope(), 'anonymous')
    assert.deepEqual(ui.queryCacheStats(), { cached: 0, inFlight: 0 })
    assert.equal(ui.liveTaskRequests.size, 0)
    await login('member')
    await dashboard()
    assert.match(ui.getQueryCacheScope(), /^2:/)
    assert.notEqual(ui.queryCacheKey('/credentials', {}), firstKey)
    const member = await ui.queryGet(ui.queryCacheKey('/credentials', {}), async () => ['bob-only'], { ttlMs: 60_000, tags: ['credentials'] })
    assert.deepEqual(member, ['bob-only'])
  })
})
