import { lazy, Suspense, useEffect, type ReactNode } from 'react'
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { useAuth } from './auth/AuthContext'

const AppLayout = lazy(() => import('./components/Layout'))
const initialPages = {
  '/': () => import('./pages/Dashboard'),
  '/instances': () => import('./pages/Instances'),
  '/billing': () => import('./pages/Billing'),
}
function pageImporter(pathname: string) {
  if (Object.prototype.hasOwnProperty.call(initialPages, pathname)) {
    return initialPages[pathname as keyof typeof initialPages]
  }
  if (pathname.startsWith('/instances/')) return () => import('./pages/InstanceDetail')
  if (pathname === '/create') return () => import('./pages/CreateInstance')
  if (pathname === '/traffic') return () => import('./pages/Traffic')
  if (pathname === '/tasks' || pathname.startsWith('/tasks/')) return () => import('./pages/Tasks')
  if (pathname === '/credentials') return () => import('./pages/Credentials')
  if (pathname === '/settings') return () => import('./pages/Settings')
  if (pathname === '/users') return () => import('./pages/Users')
  return undefined
}
const DashboardPage = lazy(initialPages['/'])
const InstancesPage = lazy(initialPages['/instances'])
const CreateInstancePage = lazy(() => import('./pages/CreateInstance'))
const InstanceDetailPage = lazy(() => import('./pages/InstanceDetail'))
const CredentialsPage = lazy(() => import('./pages/Credentials'))
const TrafficPage = lazy(() => import('./pages/Traffic'))
const BillingPage = lazy(initialPages['/billing'])
const UsersPage = lazy(() => import('./pages/Users'))
const TasksPage = lazy(() => import('./pages/Tasks'))
const SettingsPage = lazy(() => import('./pages/Settings'))

function LoadingPage() {
  return <div className="app-loading" role="status">正在加载…</div>
}

function PrivateRoute({ children, adminOnly = false }: { children: ReactNode; adminOnly?: boolean }) {
  const { user, loading } = useAuth()
  const { pathname } = useLocation()
  const canPreload = Boolean(user && !loading && (!adminOnly || user.is_admin))
  useEffect(() => {
    // Start the current page while the authenticated layout is downloading.
    const importer = canPreload ? pageImporter(pathname) : undefined
    if (importer) {
      void importer().catch(() => {})
    }
  }, [canPreload, pathname])

  if (loading) return <LoadingPage />
  if (!user) return <Navigate to="/login" replace />
  if (adminOnly && !user?.is_admin) return <Navigate to="/" replace />
  return <>{children}</>
}

export default function AuthenticatedApp() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Navigate to="/" replace />} />
        <Route
          path="/"
          element={
            <PrivateRoute>
              <Suspense fallback={<LoadingPage />}>
                <AppLayout />
              </Suspense>
            </PrivateRoute>
          }
        >
          <Route index element={<DashboardPage />} />
          <Route path="instances" element={<InstancesPage />} />
          <Route path="instances/:region/:name" element={<InstanceDetailPage />} />
          <Route path="create" element={<CreateInstancePage />} />
          <Route path="traffic" element={<TrafficPage />} />
          <Route path="tasks" element={<TasksPage />} />
          <Route path="tasks/:taskId" element={<TasksPage />} />
          <Route path="billing" element={<BillingPage />} />
          <Route path="credentials" element={<CredentialsPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route
            path="users"
            element={
              <PrivateRoute adminOnly>
                <UsersPage />
              </PrivateRoute>
            }
          />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
