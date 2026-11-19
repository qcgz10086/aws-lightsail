import fs from 'node:fs/promises'
import path from 'node:path'
import crypto from 'node:crypto'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'

const [frontendSourceArg, depsArg, outputArg] = process.argv.slice(2)
if (!frontendSourceArg || !depsArg || !outputArg) throw new Error('Usage: node probe_api_cache.mjs FRONTEND_SOURCE DEPENDENCIES OUTPUT')
const source = path.resolve(frontendSourceArg)
const dependencies = path.resolve(depsArg)
const output = path.resolve(outputArg)
await fs.mkdir(output, { recursive: true })
const require = createRequire(import.meta.url)
const ts = require(path.join(dependencies, 'typescript'))
const raw = await fs.readFile(path.join(source, 'src/api/client.ts'), 'utf8')
const cacheRaw = await fs.readFile(path.join(source, 'src/api/queryCache.mjs'))
const compiled = ts.transpileModule(raw, { compilerOptions: { target: ts.ScriptTarget.ES2022,
  module: ts.ModuleKind.ES2022 } }).outputText
await fs.writeFile(path.join(output, 'client.mjs'), compiled)
await fs.writeFile(path.join(output, 'queryCache.mjs'), cacheRaw)
try { await fs.symlink(dependencies, path.join(output, 'node_modules'), 'junction') } catch (error) {
  if (error.code !== 'EEXIST') throw error
}
const m = await import(pathToFileURL(path.join(output, 'client.mjs')).href)
const cache = await import(pathToFileURL(path.join(output, 'queryCache.mjs')).href)
let sequence = 0
const calls = []
m.default.defaults.adapter = async config => {
  calls.push({ method: config.method, url: config.url, params: config.params, body: config.data })
  return { status: 200, statusText: 'OK', headers: {}, config,
    data: config.method === 'get' ? { instances: [], scopes: [], refreshing: false, marker: ++sequence } : { accepted: true } }
}
const cases = {
  a_east: { credential_id: 1, region: 'us-east-1' }, a_west: { credential_id: 1, region: 'us-west-2' },
  b_east: { credential_id: 2, region: 'us-east-1' }, all: {}, a_all: { credential_id: 1 }, east_all: { region: 'us-east-1' },
}
const records = []
for (const explicitTags of [false, true]) {
  cache.clearQueryCache()
  m.establishQueryCacheScope('audit-user')
  const options = explicitTags ? { tags: ['instances'] } : {}
  const before = {}, after = {}
  for (const [name, params] of Object.entries(cases)) before[name] = (await m.readApi('/v2/instances', params, options)).marker
  await m.default.post('/v2/refresh', { kind: 'instances', credential_id: 1, region: 'us-east-1' })
  for (const [name, params] of Object.entries(cases)) after[name] = (await m.readApi('/v2/instances', params, options)).marker
  const changed = Object.fromEntries(Object.keys(cases).map(name => [name, before[name] !== after[name]]))
  const checks = { changed_a_east: changed.a_east, preserved_a_west: !changed.a_west,
    preserved_b_east: !changed.b_east, changed_all: changed.all, changed_a_all: changed.a_all, changed_east_all: changed.east_all }
  records.push({ scenario: explicitTags ? 'page_explicit_domain_tags' : 'default_derived_tags', changed, checks,
    passed: Object.values(checks).every(Boolean) })
}
cache.clearQueryCache()
const before = await m.readApi('/v2/instances', { credential_id: 1 })
await m.default.patch('/credentials/1', { proxy_url: 'http://fixture.invalid:8888' })
const after = await m.readApi('/v2/instances', { credential_id: 1 })
records.push({ scenario: 'credential_mutation_invalidates_derived_instance_tags', passed: before.marker !== after.marker })
cache.clearQueryCache()
{
  let count = 0
  m.default.defaults.adapter = async config => ({ status: 200, statusText: 'OK', headers: {}, config,
    data: { instances: [], scopes: [], refreshing: ++count === 1, marker: count } })
  const first = await m.readApi('/v2/instances')
  const second = await m.readApi('/v2/instances')
  const third = await m.readApi('/v2/instances')
  records.push({ scenario: 'refreshing_response_not_frozen_by_success_ttl',
    markers: [first.marker, second.marker, third.marker], adapter_calls: count,
    passed: first.marker === 1 && second.marker === 2 && third.marker === 2 && count === 2 })
}
const record = { source_sha256: crypto.createHash('sha256').update(raw).digest('hex'),
  query_cache_sha256: crypto.createHash('sha256').update(cacheRaw).digest('hex'),
  scope: 'Axios adapter only; no network', records, calls, passed: records.filter(r => r.passed).length, total: records.length }
await fs.writeFile(path.join(output, 'result.json'), `${JSON.stringify(record, null, 2)}\n`)
console.log(JSON.stringify({ records, passed: record.passed, total: record.total }, null, 2))
