import { getQueryCacheScope, invalidateQueryTags, setQueryCacheScope } from './queryCache.mjs'

export async function authRequest(path, { method = 'GET', body, signal } = {}) {
  if (signal?.aborted) throw signal.reason ?? new DOMException('Request cancelled', 'AbortError')
  const requestScope = getQueryCacheScope()
  const controller = new AbortController()
  const abort = () => controller.abort(signal.reason)
  signal?.addEventListener('abort', abort, { once: true })
  const timeout = setTimeout(() => {
    controller.abort(new DOMException('请求超时，请重试', 'TimeoutError'))
  }, 120_000)

  try {
    const response = await fetch(`/api${path}`, {
      method,
      credentials: 'include',
      signal: controller.signal,
      ...(body === undefined ? {} : {
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }),
    })
    const data = await response.json().catch(() => null)
    if (controller.signal.aborted) throw controller.signal.reason
    if (!response.ok) {
      if (response.status === 401 && requestScope === getQueryCacheScope() && requestScope !== 'anonymous') {
        setQueryCacheScope('anonymous')
        if (window.location.pathname !== '/login') window.location.href = '/login'
      }
      const detail = data?.detail
      const message = typeof detail === 'string' ? detail
        : Array.isArray(detail) ? detail.map((item) => item.msg).join('; ')
          : `Request failed with status code ${response.status}`
      throw new Error(message)
    }
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase())) invalidateQueryTags('auth')
    return data
  } catch (error) {
    if (controller.signal.aborted) throw controller.signal.reason
    throw error
  } finally {
    clearTimeout(timeout)
    signal?.removeEventListener('abort', abort)
  }
}

export function getAuthErrorMessage(error, fallback = '请求失败') {
  if (error instanceof DOMException && error.name === 'AbortError') return ''
  return error instanceof Error ? error.message : fallback
}
