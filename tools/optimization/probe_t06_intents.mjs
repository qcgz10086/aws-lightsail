// Actual TypeScript client and Axios adapter: no network, no persisted secrets.
import fs from 'node:fs/promises'
import path from 'node:path'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import assert from 'node:assert/strict'
const [sourceArg, depsArg, outputArg] = process.argv.slice(2)
const source = path.resolve(sourceArg), deps = path.resolve(depsArg), output = path.resolve(outputArg)
await fs.mkdir(output, { recursive: false })
const require = createRequire(import.meta.url), ts = require(path.join(deps, 'typescript'))
for (const name of ['client.ts', 'tasks.ts', 'queryCache.mjs']) {
  const raw = await fs.readFile(path.join(source, 'src/api', name), 'utf8')
  const compiled = name.endsWith('.ts') ? ts.transpileModule(raw, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022,
  } }).outputText.replaceAll("from './client'", "from './client.mjs'") : raw
  await fs.writeFile(path.join(output, name.replace(/\.ts$/, '.mjs')), compiled)
}
await fs.symlink(deps, path.join(output, 'node_modules'), process.platform === 'win32' ? 'junction' : 'dir')
const storage = new Map()
globalThis.localStorage = { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v) }
globalThis.window = { location: { pathname: '/tasks', href: '' } }
const client = await import(pathToFileURL(path.join(output, 'client.mjs')))
const axios = require(path.join(deps, 'axios/dist/node/axios.cjs'))
client.establishQueryCacheScope('1:session')
let version = 0
const load = () => import(pathToFileURL(path.join(output, 'tasks.mjs')).href + `?reload=${++version}`)
const body = { kind: 'create', credential_id: 1, region: 'us-east-1', resource_name: 'web',
  payload: { instance_name: 'web', region: 'us-east-1', blueprint_id: 'linux', bundle_id: 'nano', password: 'FakeOnly123!' } }
const results = []
async function scenario(name, fn) { storage.clear(); await fn(); results.push({ name, passed: true }) }
function adapter(mode) {
  const accepted = new Map(), calls = []
  const state = { mode, accepted, calls }
  client.default.defaults.adapter = async config => {
    const key = config.method === 'post' ? config.headers.get('Idempotency-Key') : config.params.key
    const request = config.data ? JSON.parse(config.data) : null
    calls.push({ method: config.method, key, bundle: request?.payload?.bundle_id })
    const response = (data, status = 200) => ({ data, status, statusText: 'fixture', headers: {}, config })
    const reject = (status, detail) => { throw new axios.AxiosError(detail, 'ERR_BAD_REQUEST', config, null, response({ detail }, status)) }
    if (config.method === 'get') {
      if (!accepted.has(key)) reject(404, '任务不存在')
      return response(accepted.get(key).task)
    }
    if (state.mode === 'offline-before') throw new axios.AxiosError('offline', 'ERR_NETWORK', config)
    if (state.mode === 'locked') reject(409, '该账号或实例存在未完成任务')
    if (accepted.has(key) && JSON.stringify(accepted.get(key).request) !== JSON.stringify(request)) reject(409, '幂等键已用于不同请求')
    if (!accepted.has(key)) accepted.set(key, { request, task: { task_id: `task-${accepted.size}`, status: 'queued', marker: request.payload?.bundle_id } })
    if (state.mode === 'lost-after') throw new axios.AxiosError('response lost', 'ECONNABORTED', config)
    return response(accepted.get(key).task, 202)
  }
  return state
}
await scenario('lost_response_changed_payload_is_not_old_success', async () => {
  let tasks = await load(); const state = adapter('lost-after')
  await assert.rejects(tasks.submitTask(1, body))
  const changed = { ...body, payload: { ...body.payload, bundle_id: 'large', password: 'ChangedOnly123!' } }
  await assert.rejects(tasks.submitTask(1, changed), /本次变更未提交/)
  assert.equal(state.calls.length, 1)
  tasks = await load(); state.mode = 'online'
  await assert.rejects(tasks.submitTask(1, changed), /冲突/)
  assert.equal(state.accepted.size, 1)
  assert.equal(tasks.pendingIntents(1).length, 1)
  const restored = await tasks.recoverIntent(1, tasks.pendingIntents(1)[0])
  assert.equal(restored.marker, 'nano')
  assert.equal(tasks.pendingIntents(1).length, 0)
  assert.ok([...storage.values()].every(v => !v.includes('FakeOnly') && !v.includes('ChangedOnly')))
})
await scenario('resource_lock_409_allows_new_intent_after_reload', async () => {
  let tasks = await load(); const state = adapter('locked')
  await assert.rejects(tasks.submitTask(1, body))
  assert.equal(tasks.pendingIntents(1).length, 0)
  tasks = await load(); state.mode = 'online'
  await tasks.submitTask(1, body)
  assert.equal(state.calls.length, 2)
  assert.notEqual(state.calls[0].key, state.calls[1].key)
})
await scenario('offline_before_acceptance_reload_reuses_original_key', async () => {
  let tasks = await load(); const state = adapter('offline-before')
  await assert.rejects(tasks.submitTask(1, body))
  const key = tasks.pendingIntents(1)[0].key
  tasks = await load(); state.mode = 'online'
  await tasks.submitTask(1, body)
  assert.equal(state.accepted.size, 1)
  assert.equal(state.calls[1].key, key)
})
await scenario('accepted_request_reload_reuses_original_key', async () => {
  let tasks = await load(); const state = adapter('lost-after')
  await assert.rejects(tasks.submitTask(1, body))
  tasks = await load(); state.mode = 'online'
  await tasks.submitTask(1, body)
  assert.equal(state.accepted.size, 1)
  assert.equal(state.calls[0].key, state.calls[1].key)
})
await scenario('old_user_late_response_never_applies_to_new_session', async () => {
  const tasks = await load(); let release
  client.default.defaults.adapter = config => new Promise(resolve => {
    release = () => resolve({ data: { task_id: 'old' }, status: 202, headers: {}, config })
  })
  const pending = tasks.submitTask(1, body)
  while (!release) await new Promise(resolve => setImmediate(resolve))
  client.establishQueryCacheScope('2:session')
  release()
  await assert.rejects(pending, error => error.name === 'AbortError')
  client.establishQueryCacheScope('1:session')
})
await fs.writeFile(path.join(output, 'result.json'), JSON.stringify({ results, passed: results.length, networkCalls: 0 }, null, 2))
console.log(JSON.stringify(results, null, 2))
