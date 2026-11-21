import assert from 'node:assert/strict'
import test from 'node:test'

import { authRequest, getAuthErrorMessage } from '../src/api/authHttp.mjs'
import {
  clearQueryCache,
  getQueryCacheScope,
  queryCacheKey,
  queryCacheStats,
  queryGet,
  setQueryCacheScope,
} from '../src/api/queryCache.mjs'

test.beforeEach(() => {
  setQueryCacheScope('anonymous')
  globalThis.window = { location: { pathname: '/', href: '/' } }
})
test.afterEach(() => {
  clearQueryCache()
  delete globalThis.window
})

test('reads the current user with the session cookie', async (t) => {
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/api/auth/me')
    assert.equal(options.method, 'GET')
    assert.equal(options.credentials, 'include')
    assert.equal(options.body, undefined)
    return Response.json({ id: 7, username: 'alice', has_credentials: true })
  })
  assert.deepEqual(await authRequest('/auth/me'), { id: 7, username: 'alice', has_credentials: true })
})

test('submits login as JSON and invalidates cached authentication data', async (t) => {
  await queryGet(queryCacheKey('/auth/me'), async () => ({ id: 2 }), { ttlMs: 1000, tags: ['auth'] })
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/api/auth/login')
    assert.equal(options.method, 'POST')
    assert.equal(options.credentials, 'include')
    assert.equal(new Headers(options.headers).get('Content-Type'), 'application/json')
    assert.deepEqual(JSON.parse(options.body), { username: 'alice', password: 'correct-password' })
    return Response.json({ access_token: 'cookie-token', user: { id: 7 } })
  })
  assert.deepEqual(await authRequest('/auth/login', {
    method: 'POST', body: { username: 'alice', password: 'correct-password' },
  }), { access_token: 'cookie-token', user: { id: 7 } })
  assert.equal(queryCacheStats().cached, 0)
})

test('surfaces FastAPI credential errors without redirecting an anonymous login', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: '用户名或密码错误' }, { status: 401 }))
  await assert.rejects(authRequest('/auth/login', { method: 'POST', body: {} }), (error) => {
    assert.equal(getAuthErrorMessage(error, '登录失败'), '用户名或密码错误')
    return true
  })
  assert.equal(getQueryCacheScope(), 'anonymous')
  assert.equal(window.location.href, '/')
})

test('formats FastAPI validation detail arrays for the login form', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => Response.json({
    detail: [{ loc: ['body', 'username'], msg: '用户名不能为空', type: 'missing' },
      { loc: ['body', 'password'], msg: '密码不能为空', type: 'missing' }],
  }, { status: 422 }))
  await assert.rejects(authRequest('/auth/login', { method: 'POST', body: {} }), {
    message: '用户名不能为空; 密码不能为空',
  })
})

test('keeps a useful error when the server returns a non-JSON failure', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => new Response('upstream unavailable', { status: 502 }))
  await assert.rejects(authRequest('/auth/me'), { message: 'Request failed with status code 502' })
  assert.equal(getAuthErrorMessage({}, '登录失败'), '登录失败')
})

test('cancels an authentication request after 120 seconds', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let transportSignal
  t.mock.method(globalThis, 'fetch', (_url, { signal }) => {
    transportSignal = signal
    return new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(signal.reason), { once: true })
    })
  })
  const pending = authRequest('/auth/me')
  const rejection = assert.rejects(pending, { name: 'TimeoutError', message: '请求超时，请重试' })
  t.mock.timers.tick(119_999)
  assert.equal(transportSignal.aborted, false)
  t.mock.timers.tick(1)
  await rejection
  assert.equal(transportSignal.aborted, true)
})

test('forwards caller cancellation and hides cancelled-request errors', async (t) => {
  const controller = new AbortController()
  t.mock.method(globalThis, 'fetch', (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), { once: true })
  }))
  const pending = authRequest('/auth/me', { signal: controller.signal })
  controller.abort()
  await assert.rejects(pending, (error) => {
    assert.equal(error.name, 'AbortError')
    assert.equal(getAuthErrorMessage(error), '')
    return true
  })
})

test('rejects an already cancelled request before starting transport', async (t) => {
  const controller = new AbortController()
  controller.abort()
  t.mock.method(globalThis, 'fetch', async () => assert.fail('cancelled request reached the server'))
  await assert.rejects(authRequest('/auth/me', { signal: controller.signal }), { name: 'AbortError' })
})

test('expires the active cache scope and redirects when its session returns 401', async (t) => {
  setQueryCacheScope('7:1')
  await queryGet(queryCacheKey('/credentials'), async () => ['alice'], { ttlMs: 1000, tags: ['credentials'] })
  const location = { pathname: '/', href: '/' }
  globalThis.window = { location }
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: '登录已过期' }, { status: 401 }))
  await assert.rejects(authRequest('/auth/me'), { message: '登录已过期' })
  assert.equal(getQueryCacheScope(), 'anonymous')
  assert.equal(queryCacheStats().cached, 0)
  assert.equal(location.href, '/login')
})

test('ignores a late 401 from a previous session', async (t) => {
  setQueryCacheScope('7:1')
  const location = { pathname: '/instances', href: '/instances' }
  globalThis.window = { location }
  let respond
  t.mock.method(globalThis, 'fetch', () => new Promise((resolve) => { respond = resolve }))
  const pending = authRequest('/auth/me')
  setQueryCacheScope('8:2')
  await queryGet(queryCacheKey('/credentials'), async () => ['bob'], { ttlMs: 1000, tags: ['credentials'] })
  respond(Response.json({ detail: '旧会话已过期' }, { status: 401 }))
  await assert.rejects(pending, { message: '旧会话已过期' })
  assert.equal(getQueryCacheScope(), '8:2')
  assert.equal(queryCacheStats().cached, 1)
  assert.equal(location.href, '/instances')
})
