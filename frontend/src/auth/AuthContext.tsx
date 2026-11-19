import React, { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react'
import api, { establishQueryCacheScope, User, getErrorMessage } from '../api/client'

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
    const res = await api.get<User>('/auth/me', { signal })
    if (generation !== sessionGeneration.current) return
    establishQueryCacheScope(`${res.data.id}:${generation}`)
    setUser(res.data)
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
          establishQueryCacheScope(null)
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
    const generation = ++sessionGeneration.current
    authReadController.current?.abort()
    authReadController.current = null
    try {
      const res = await api.post('/auth/login', { username, password })
      if (generation !== sessionGeneration.current) return
      establishQueryCacheScope(`${res.data.user.id}:${generation}`)
      setUser(res.data.user)
      setSessionKey(generation)
    } finally {
      if (generation === sessionGeneration.current) setLoading(false)
    }
  }

  const logout = async () => {
    const generation = ++sessionGeneration.current
    authReadController.current?.abort()
    authReadController.current = null
    establishQueryCacheScope(null)
    setUser(null)
    setSessionKey(generation)
    setLoading(false)
    try { await api.post('/auth/logout') } catch { /* 本地退出不依赖服务端响应 */ }
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
