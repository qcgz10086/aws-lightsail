const cache = new Map()
const inFlight = new Map()
let scope = 'anonymous'

function abortError() {
  return new DOMException('Request cancelled', 'AbortError')
}

function hasTag(entry, tags) {
  return tags.some((tag) => entry.tags.includes(tag))
}

function subscribe(entry, signal) {
  if (signal?.aborted) return Promise.reject(abortError())
  const subscriber = {}
  entry.subscribers.add(subscriber)

  return new Promise((resolve, reject) => {
    let done = false
    const release = () => {
      entry.subscribers.delete(subscriber)
      if (!entry.settled && entry.subscribers.size === 0) entry.controller.abort()
    }
    const finish = (callback, value) => {
      if (done) return
      done = true
      signal?.removeEventListener('abort', onAbort)
      release()
      callback(value)
    }
    const onAbort = () => finish(reject, abortError())
    signal?.addEventListener('abort', onAbort, { once: true })
    entry.promise.then((value) => finish(resolve, value), (error) => finish(reject, error))
    if (signal?.aborted) onAbort()
  })
}

export function setQueryCacheScope(nextScope) {
  const next = String(nextScope ?? 'anonymous')
  if (next === scope) return
  scope = next
  clearQueryCache()
}

export function getQueryCacheScope() {
  return scope
}

export function queryCacheKey(url, params) {
  const normalized = params && typeof params === 'object'
    ? Object.fromEntries(Object.entries(params).sort(([left], [right]) => left.localeCompare(right)))
    : params
  return `${scope}|${url}|${JSON.stringify(normalized ?? null)}`
}

export function queryGet(key, loader, { ttlMs = 0, tags = [], signal, force = false } = {}) {
  if (signal?.aborted) return Promise.reject(abortError())
  const now = Date.now()
  if (force) cache.delete(key)
  const cached = cache.get(key)
  if (cached && cached.expiresAt > now) return Promise.resolve(cached.value)
  if (cached) cache.delete(key)

  let entry = inFlight.get(key)
  if (!entry) {
    const controller = new AbortController()
    entry = { controller, subscribers: new Set(), tags, settled: false, promise: null }
    inFlight.set(key, entry)
    const current = entry
    entry.promise = Promise.resolve()
      .then(() => loader(controller.signal))
      .then((value) => {
        if (!controller.signal.aborted && inFlight.get(key) === current && ttlMs > 0) {
          cache.set(key, { value, expiresAt: Date.now() + ttlMs, tags })
        }
        return value
      })
      .finally(() => {
        current.settled = true
        if (inFlight.get(key) === current) inFlight.delete(key)
      })
    // A cancelled final subscriber may leave a transport rejection after its
    // own promise has already rejected. Keep the shared promise observed.
    entry.promise.catch(() => {})
  }
  return subscribe(entry, signal)
}

export function invalidateQueryTags(...tags) {
  if (!tags.length) return
  for (const [key, entry] of cache) {
    if (hasTag(entry, tags)) cache.delete(key)
  }
  for (const [key, entry] of inFlight) {
    if (hasTag(entry, tags)) {
      inFlight.delete(key)
      entry.controller.abort()
    }
  }
}

export function clearQueryCache() {
  cache.clear()
  for (const entry of inFlight.values()) entry.controller.abort()
  inFlight.clear()
}

export function queryCacheStats() {
  return { cached: cache.size, inFlight: inFlight.size }
}
