import App from '../src/App'
import { AuthProvider, useAuth } from '../src/auth/AuthContext'

export { act, createElement } from 'react'
export { createRoot } from 'react-dom/client'
export {
  getQueryCacheScope,
  queryCacheKey,
  queryCacheStats,
  queryGet,
  setQueryCacheScope,
} from '../src/api/queryCache.mjs'
export { liveTaskRequests } from '../src/api/taskState'

function SessionProbe() {
  const { user, loading, sessionKey } = useAuth()
  return <output id="session-probe">{JSON.stringify({ user, loading, sessionKey })}</output>
}

export function AuthRoutingFixture() {
  return <AuthProvider><App /><SessionProbe /></AuthProvider>
}
