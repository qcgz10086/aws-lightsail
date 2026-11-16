import React, { createContext, useContext, useEffect, useMemo, useState } from 'react'
import api, { User, getErrorMessage } from '../api/client'

interface AuthState {
  user: User | null
  loading: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  refreshMe: () => Promise<void>
}

const AuthContext = createContext<AuthState | undefined>(undefined)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  // R-5：登录态只保存在内存与 HttpOnly Cookie 里，不再写 localStorage。
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  const refreshMe = async () => {
    const res = await api.get<User>('/auth/me')
    setUser(res.data)
  }

  useEffect(() => {
    const boot = async () => {
      try {
        await refreshMe()
      } catch {
        setUser(null)
      } finally {
        setLoading(false)
      }
    }
    void boot()
  }, [])

  const login = async (username: string, password: string) => {
    const res = await api.post('/auth/login', { username, password })
    setUser(res.data.user)
  }

  const logout = async () => {
    try {
      await api.post('/auth/logout')
    } catch {
      // 登出失败也不阻塞本地退出
    } finally {
      setUser(null)
    }
  }

  const value = useMemo(
    () => ({ user, loading, login, logout, refreshMe }),
    [user, loading],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}

export { getErrorMessage }
