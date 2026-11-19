const cache = new Map()
const inFlight = new Map()
const MAX_CACHED_QUERIES = 256
let scope = 'anonymous'

function abortError() {
  return new DOMException('Request cancelled', 'AbortError')
}

function trimExpired(now = Date.now()) {
  for (const [key, entry] of cache) {
    if (entry.expiresAt <= now) cache.delete(key)
  }
}

function hasTag(entry, tags) {
  return tags.some((tag) => {
    if (entry.tags.includes(tag)
      || (!tag.includes(':') && entry.tags.some((candidate) => candidate.startsWith(`${tag}:`)))) return true

    const target = parseScopeTag(tag)
    if (!target) return false
    return entry.tags.some((candidate) => {
      const cached = parseScopeTag(candidate)
      return cached && cached.domain === target.domain
        && (cached.credential === '*' || target.credential === '*' || cached.credential === target.credential)
        && (cached.region === '*' || target.region === '*' || cached.region === target.region)
    })
  })
}

function parseScopeTag(tag) {
  const match = tag.match(/^([^:]+):scope:([^:]+):([^:]+)$/)
  if (!match) return null
  return { domain: match[1], credential: match[2], region: match[3] }
}

function storeValue(key, value, expiresAt, tags) {
  trimExpired()
  cache.delete(key)
  cache.set(key, { value, expiresAt, tags })
  while (cache.size > MAX_CACHED_QUERIES) cache.delete(cache.keys().next().value)
}

function retireEntry(key, entry) {
  if (inFlight.get(key) === entry) inFlight.delete(key)
  if (entry.invalidated) return
  entry.invalidated = true
  entry.controller.abort()
  for (const rejectSubscriber of [...entry.subscribers]) rejectSubscriber()
}

function subscribe(entry, signal) {
  if (signal?.aborted) return Promise.reject(abortError())
  return new Promise((resolve, reject) => {
    let done = false
    const rejectSubscriber = () => finish(reject, abortError())
    entry.subscribers.add(rejectSubscriber)
    const release = () => {
      entry.subscribers.delete(rejectSubscriber)
      if (!entry.settled && entry.subscribers.size === 0) retireEntry(entry.key, entry)
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

export function queryGet(key, loader, { ttlMs = 0, tags = [], signal, force = false,
  cacheIf = () => true } = {}) {
  if (signal?.aborted) return Promise.reject(abortError())
  const now = Date.now()
  if (force) {
    cache.delete(key)
    const previous = inFlight.get(key)
    if (previous) retireEntry(key, previous)
  }
  trimExpired(now)
  const cached = cache.get(key)
  if (cached && cached.expiresAt > now) {
    cache.delete(key)
    cache.set(key, cached)
    return Promise.resolve(cached.value)
  }
  if (cached) cache.delete(key)

  let entry = inFlight.get(key)
  if (!entry) {
    const controller = new AbortController()
    entry = { key, controller, subscribers: new Set(), tags, settled: false, invalidated: false, promise: null }
    inFlight.set(key, entry)
    const current = entry
    entry.promise = Promise.resolve()
      .then(() => loader(controller.signal))
      .then((value) => {
        if (!current.invalidated && !controller.signal.aborted && inFlight.get(key) === current
          && ttlMs > 0 && cacheIf(value)) {
          storeValue(key, value, Date.now() + ttlMs, tags)
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
    if (hasTag(entry, tags)) retireEntry(key, entry)
  }
}

export function clearQueryCache() {
  cache.clear()
  for (const [key, entry] of [...inFlight]) retireEntry(key, entry)
}

export function queryCacheStats() {
  return { cached: cache.size, inFlight: inFlight.size }
}
