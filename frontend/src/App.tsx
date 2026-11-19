import { lazy, Suspense, type ReactNode } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { Spin } from 'antd'
import { useAuth } from './auth/AuthContext'
import AppLayout from './components/Layout'
import LoginPage from './pages/Login'

const initialPages = {
  '/': () => import('./pages/Dashboard'),
  '/instances': () => import('./pages/Instances'),
  '/billing': () => import('./pages/Billing'),
}
const initialPage = initialPages[window.location.pathname as keyof typeof initialPages]
// Fetch the current page's code alongside authentication; other pages stay lazy.
if (initialPage) void initialPage().catch(() => {})
const DashboardPage = lazy(initialPages['/'])
const InstancesPage = lazy(initialPages['/instances'])
const CreateInstancePage = lazy(() => import('./pages/CreateInstance'))
const InstanceDetailPage = lazy(() => import('./pages/InstanceDetail'))
const CredentialsPage = lazy(() => import('./pages/Credentials'))
const TrafficPage = lazy(() => import('./pages/Traffic'))
const BillingPage = lazy(initialPages['/billing'])
const UsersPage = lazy(() => import('./pages/Users'))
const TasksPage = lazy(() => import('./pages/Tasks'))

function PrivateRoute({ children, adminOnly = false }: { children: ReactNode; adminOnly?: boolean }) {
  const { user, loading } = useAuth()
  if (loading) {
    return (
      <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Spin size="large" />
      </div>
    )
  }
  if (!user) return <Navigate to="/login" replace />
  if (adminOnly && !user?.is_admin) return <Navigate to="/" replace />
  return <>{children}</>
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="/"
          element={
            <PrivateRoute>
              <Suspense fallback={<div style={{ padding: 24 }}><Spin /></div>}>
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
