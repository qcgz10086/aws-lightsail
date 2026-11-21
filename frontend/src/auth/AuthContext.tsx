import React, { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { User } from '../api/client'
import { authRequest, getAuthErrorMessage as getErrorMessage } from '../api/authHttp.mjs'
import { setQueryCacheScope } from '../api/queryCache.mjs'
import { clearTaskRequests } from '../api/taskState'

interface AuthState {
  user: User | null
  loading: boolean
  sessionKey: number
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  refreshMe: () => Promise<void>
}

const AuthContext = createContext<AuthState | undefined>(undefined)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  // R-5：登录态只保存在内存与 HttpOnly Cookie 里，不再写 localStorage。
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)
  const [sessionKey, setSessionKey] = useState(0)
  const sessionGeneration = useRef(0)
  const authReadController = useRef<AbortController | null>(null)

  const applyCurrentUser = async (generation: number, signal: AbortSignal) => {
    const data = await authRequest<User>('/auth/me', { signal })
    if (generation !== sessionGeneration.current || signal.aborted) return
    setQueryCacheScope(`${data.id}:${generation}`)
    if (data.has_credentials) {
      const path = window.location.pathname
      const query = path === '/' || path === '/instances' ? '/v2/query-instances'
        : path === '/billing' ? '/v2/billing/summary' : null
      // Share this read with the page while its components render. Scope changes
      // and auth cancellation retire the same request through the normal cache.
      if (query) {
        void import('../api/client').then(({ readApi }) => {
          if (generation !== sessionGeneration.current || signal.aborted) return
          return readApi(query, {}, { signal })
        }).catch(() => {})
      }
    }
    setUser(data)
    setSessionKey(generation)
  }

  const refreshMe = async () => {
    const generation = ++sessionGeneration.current
    authReadController.current?.abort()
    const controller = new AbortController()
    authReadController.current = controller
    await applyCurrentUser(generation, controller.signal)
  }

  useEffect(() => {
    const boot = async () => {
      const generation = ++sessionGeneration.current
      const controller = new AbortController()
      authReadController.current = controller
      try {
        await applyCurrentUser(generation, controller.signal)
      } catch {
        if (generation === sessionGeneration.current) {
          setQueryCacheScope('anonymous')
          setUser(null)
          setSessionKey(generation)
        }
      } finally {
        if (generation === sessionGeneration.current) setLoading(false)
      }
    }
    void boot()
  }, [])

  const login = async (username: string, password: string) => {
    clearTaskRequests()
    const generation = ++sessionGeneration.current
    authReadController.current?.abort()
    authReadController.current = null
    try {
      const data = await authRequest<{ user: User }>('/auth/login', { method: 'POST', body: { username, password } })
      if (generation !== sessionGeneration.current) return
      setQueryCacheScope(`${data.user.id}:${generation}`)
      setUser(data.user)
      setSessionKey(generation)
    } finally {
      if (generation === sessionGeneration.current) setLoading(false)
    }
  }

  const logout = async () => {
    clearTaskRequests()
    const generation = ++sessionGeneration.current
    authReadController.current?.abort()
    authReadController.current = null
    setQueryCacheScope('anonymous')
    setUser(null)
    setSessionKey(generation)
    setLoading(false)
    try { await authRequest('/auth/logout', { method: 'POST' }) } catch { /* 本地退出不依赖服务端响应 */ }
  }

  const value = useMemo(
    () => ({ user, loading, sessionKey, login, logout, refreshMe }),
    [user, loading, sessionKey],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}

export { getErrorMessage }
