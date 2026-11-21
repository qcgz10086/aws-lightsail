import { lazy, Suspense, useEffect } from 'react'
import { useAuth } from './auth/AuthContext'
import LoginPage from './pages/Login'

const AuthenticatedApp = lazy(() => import('./AuthenticatedApp'))

function LoadingPage() {
  return <div className="app-loading" role="status">正在加载…</div>
}

export default function App() {
  const { user, loading } = useAuth()

  useEffect(() => {
    if (loading) return
    const normalizeLoginLocation = () => {
      const path = window.location.pathname
      if (!user && path !== '/login') window.history.replaceState(null, '', '/login')
      else if (user && path === '/login') window.history.replaceState(null, '', '/')
    }
    normalizeLoginLocation()
    if (!user) {
      window.addEventListener('popstate', normalizeLoginLocation)
      return () => window.removeEventListener('popstate', normalizeLoginLocation)
    }
  }, [user, loading])

  if (loading && window.location.pathname !== '/login') return <LoadingPage />
  if (!user) return <LoginPage />

  return (
    <Suspense fallback={<LoadingPage />}>
      <AuthenticatedApp />
    </Suspense>
  )
}
