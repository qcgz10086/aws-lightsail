import { useState, type FormEvent } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { getErrorMessage, useAuth } from '../auth/AuthContext'

export default function LoginPage() {
  const { login, user, loading } = useAuth()
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showPassword, setShowPassword] = useState(false)
  const navigate = useNavigate()

  if (!loading && user) return <Navigate to="/" replace />

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (submitting) return
    const form = new FormData(event.currentTarget)
    setSubmitting(true)
    setError(null)
    try {
      await login(String(form.get('username')), String(form.get('password')))
      navigate('/')
    } catch (err) {
      setError(getErrorMessage(err, '登录失败'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <h1 id="login-title">Lightsail 管理平台</h1>
        <p>登录后绑定 AWS Key 管理实例</p>
        {error && <div className="login-error" role="alert">{error}</div>}
        <form method="post" onSubmit={onSubmit} aria-busy={submitting}>
          <label htmlFor="login-username">用户名</label>
          <input id="login-username" name="username" autoComplete="username" defaultValue="admin" required />
          <label htmlFor="login-password">密码</label>
          <div className="login-password">
            <input id="login-password" name="password" type={showPassword ? 'text' : 'password'}
              autoComplete="current-password" required />
            <button type="button" onClick={() => setShowPassword(!showPassword)}>
              {showPassword ? '隐藏' : '显示'}
            </button>
          </div>
          <button className="login-submit" type="submit" disabled={submitting}>
            {submitting ? '正在登录…' : '登录'}
          </button>
        </form>
      </section>
    </main>
  )
}
