import assert from 'node:assert/strict'
import test from 'node:test'

import {
  clearQueryCache,
  invalidateQueryTags,
  queryCacheKey,
  queryCacheStats,
  queryGet,
  setQueryCacheScope,
} from '../src/api/queryCache.mjs'

test.beforeEach(() => setQueryCacheScope('test-user'))
test.afterEach(() => clearQueryCache())

test('normalizes parameter order and reuses values until TTL expires', async () => {
  let calls = 0
  const firstKey = queryCacheKey('/traffic', { credential_id: 7, month: '2026-11' })
  const secondKey = queryCacheKey('/traffic', { month: '2026-11', credential_id: 7 })
  assert.equal(firstKey, secondKey)
  const load = () => queryGet(firstKey, async () => ++calls, { ttlMs: 1000, tags: ['traffic'] })
  assert.equal(await load(), 1)
  assert.equal(await load(), 1)
  assert.equal(calls, 1)
})

test('deduplicates in-flight work but aborts transport only after the last subscriber leaves', async () => {
  let calls = 0
  let transportSignal
  let finish
  const key = queryCacheKey('/instances', {})
  const loader = (signal) => {
    calls += 1
    transportSignal = signal
    return new Promise((resolve) => { finish = resolve })
  }
  const firstController = new AbortController()
  const secondController = new AbortController()
  const first = queryGet(key, loader, { ttlMs: 1000, tags: ['instances'], signal: firstController.signal })
  const second = queryGet(key, loader, { ttlMs: 1000, tags: ['instances'], signal: secondController.signal })
  firstController.abort()
  await assert.rejects(first, { name: 'AbortError' })
  assert.equal(transportSignal.aborted, false)
  finish([{ name: 'current' }])
  assert.deepEqual(await second, [{ name: 'current' }])
  assert.equal(calls, 1)

  const thirdController = new AbortController()
  const pending = queryGet(`${key}-last`, (signal) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('cancelled', 'AbortError')))
  }), { tags: ['instances'], signal: thirdController.signal })
  thirdController.abort()
  await assert.rejects(pending, { name: 'AbortError' })
})

test('tag invalidation clears cached and in-flight results without caching an error', async () => {
  let calls = 0
  const key = queryCacheKey('/billing', {})
  const load = () => queryGet(key, async () => ++calls, { ttlMs: 1000, tags: ['billing'] })
  assert.equal(await load(), 1)
  invalidateQueryTags('instances')
  assert.equal(await load(), 1)
  invalidateQueryTags('billing')
  assert.equal(await load(), 2)

  const failing = queryCacheKey('/failing', {})
  await assert.rejects(queryGet(failing, async () => { throw new Error('offline') }, { ttlMs: 1000, tags: ['billing'] }))
  await assert.rejects(queryGet(failing, async () => { calls += 1; throw new Error('offline') }, { ttlMs: 1000, tags: ['billing'] }))
  assert.equal(calls, 3)
})

test('scope changes clear cached values and prevent cross-user reuse', async () => {
  const key = queryCacheKey('/credentials', {})
  assert.equal(await queryGet(key, async () => 'alice', { ttlMs: 1000, tags: ['credentials'] }), 'alice')
  setQueryCacheScope('other-user')
  assert.equal(queryCacheStats().cached, 0)
  const nextKey = queryCacheKey('/credentials', {})
  assert.notEqual(key, nextKey)
  assert.equal(await queryGet(nextKey, async () => 'bob', { ttlMs: 1000, tags: ['credentials'] }), 'bob')
})

test('forced refresh bypasses and replaces an existing value', async () => {
  const key = queryCacheKey('/summary', {})
  assert.equal(await queryGet(key, async () => 1, { ttlMs: 1000, tags: ['billing'] }), 1)
  assert.equal(await queryGet(key, async () => 2, { ttlMs: 1000, tags: ['billing'], force: true }), 2)
  assert.equal(await queryGet(key, async () => 3, { ttlMs: 1000, tags: ['billing'] }), 2)
})
