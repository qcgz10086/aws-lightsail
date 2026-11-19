export interface QueryOptions {
  ttlMs?: number
  tags?: string[]
  signal?: AbortSignal
  force?: boolean
}

export function setQueryCacheScope(scope: string | number | null | undefined): void
export function getQueryCacheScope(): string
export function queryCacheKey(url: string, params?: unknown): string
export function queryGet<T>(
  key: string,
  loader: (signal: AbortSignal) => Promise<T>,
  options?: QueryOptions,
): Promise<T>
export function invalidateQueryTags(...tags: string[]): void
export function clearQueryCache(): void
export function queryCacheStats(): { cached: number; inFlight: number }
