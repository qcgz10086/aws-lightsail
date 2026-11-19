import { pathToFileURL } from 'node:url'
import fs from 'node:fs/promises'
import crypto from 'node:crypto'

const [source, output] = process.argv.slice(2)
if (!source || !output) throw new Error('Usage: node probe_query_cache.mjs <queryCache.mjs> <output.json>')
const bytes = await fs.readFile(source)
const m = await import(pathToFileURL(source).href)
const records = []

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const outcome = (promise) => promise.then(value => ({ kind: 'resolved', value }), error => ({ kind: 'rejected', name: error?.name }))

m.clearQueryCache()
m.setQueryCacheScope('a')
{
  const load = deferred()
  const first = outcome(m.queryGet(m.queryCacheKey('/instances', {}), () => load.promise,
    { ttlMs: 10000, tags: ['instances'] }))
  await Promise.resolve()
  m.setQueryCacheScope('b')
  load.resolve('private-account-a-value')
  const observed = await first
  records.push({ scenario: 'scope_change_late_uncancellable_response', observed,
    passed: observed.kind === 'rejected' && observed.name === 'AbortError' })
}
m.clearQueryCache()
{
  const load = deferred()
  const controller = new AbortController()
  const key = m.queryCacheKey('/instances', {})
  let calls = 0
  const first = outcome(m.queryGet(key, () => { calls++; return load.promise }, { signal: controller.signal }))
  await Promise.resolve()
  controller.abort()
  await first
  const second = outcome(m.queryGet(key, async () => { calls++; return 'replacement-value' }))
  load.resolve('cancelled-old-value')
  const observed = await second
  records.push({ scenario: 'last_subscriber_abort_immediate_new_subscriber', observed, loader_calls: calls,
    passed: observed.kind === 'resolved' && observed.value === 'replacement-value' && calls === 2 })
}
m.clearQueryCache()
{
  const load = deferred()
  const key = m.queryCacheKey('/billing', {})
  const first = outcome(m.queryGet(key, () => load.promise, { ttlMs: 10000, tags: ['billing'] }))
  await Promise.resolve()
  m.invalidateQueryTags('billing')
  const second = outcome(m.queryGet(key, async () => 'new-billing-value', { ttlMs: 10000, tags: ['billing'] }))
  load.resolve('pre-invalidation-value')
  const observed = { old: await first, current: await second }
  records.push({ scenario: 'mutation_invalidates_uncancellable_inflight_response', observed,
    passed: observed.old.kind === 'rejected' && observed.current.value === 'new-billing-value' })
}
m.clearQueryCache()
{
  const key = m.queryCacheKey('/billing', {})
  let calls = 0
  let currentTime = 1000
  const previous = Date.now
  Date.now = () => currentTime
  try {
    const values = []
    values.push(await m.queryGet(key, async () => ++calls, { ttlMs: 100 }))
    currentTime = 1099
    values.push(await m.queryGet(key, async () => ++calls, { ttlMs: 100 }))
    currentTime = 1100
    values.push(await m.queryGet(key, async () => ++calls, { ttlMs: 100 }))
    records.push({ scenario: 'ttl_boundary_no_real_time_wait', observed: values,
      passed: values.join(',') === '1,1,2' && calls === 2 })
  } finally { Date.now = previous }
}
m.clearQueryCache()
const result = { module_sha256: crypto.createHash('sha256').update(bytes).digest('hex'),
  source, records, passed: records.filter(row => row.passed).length, total: records.length }
await fs.writeFile(output, `${JSON.stringify(result, null, 2)}\n`)
console.log(JSON.stringify(result, null, 2))
