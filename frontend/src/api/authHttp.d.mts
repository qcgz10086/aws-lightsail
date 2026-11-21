export interface AuthRequestOptions {
  method?: 'GET' | 'POST'
  body?: unknown
  signal?: AbortSignal
}

export function authRequest<T>(
  path: '/auth/me' | '/auth/login' | '/auth/logout',
  options?: AuthRequestOptions,
): Promise<T>
export function getAuthErrorMessage(error: unknown, fallback?: string): string
