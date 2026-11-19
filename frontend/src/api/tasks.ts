import axios from 'axios'
import api, { invalidateQueryTags } from './client'
import { getQueryCacheScope } from './queryCache.mjs'

export type TaskKind = 'create' | 'start' | 'stop' | 'reboot' | 'change_ip' | 'delete' | 'traffic_sync'
export interface TaskRequest {
  kind: TaskKind
  credential_id: number
  region?: string
  resource_name?: string
  payload?: Record<string, unknown>
}
export interface OperationTask {
  task_id: string
  kind: TaskKind
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'partial' | 'cancelled' | 'reconciliation_required' | 'resolved'
  credential_id: number
  account_id: string | null
  region: string | null
  resource_name: string | null
  current_step: string | null
  steps: { sequence: number; name: string; status: string; error: string | null; operation_id: string | null }[]
  error: string | null
  result: Record<string, unknown> | null
  created_at: string
  can_resume: boolean
  can_cancel: boolean
}
export interface PendingIntent { key: string; slot: string; created_at: string }
export const taskLabels: Record<OperationTask['status'], string> = {
  queued: '已接受，等待执行', running: '执行中', succeeded: '已确认完成', failed: '明确失败',
  partial: '部分完成', cancelled: '已取消', reconciliation_required: '待核对，资源锁保留',
  resolved: '已人工决议，AWS 结果未确认',
}
const storageKey = (userId: number) => `lightsail-task-intents:${userId}`
const liveRequests = new Map<string, string>()
export function clearTaskRequests() { liveRequests.clear() }
export function pendingIntents(userId: number): PendingIntent[] {
  try { return JSON.parse(localStorage.getItem(storageKey(userId)) || '[]') } catch { return [] }
}
function saveIntents(userId: number, intents: PendingIntent[]) {
  localStorage.setItem(storageKey(userId), JSON.stringify(intents))
}
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
    .filter(([, v]) => v !== undefined).map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(',')}}`
  return JSON.stringify(value)
}
export async function recoverIntent(userId: number, intent: PendingIntent, signal?: AbortSignal) {
  const scope = getQueryCacheScope()
  const { data } = await api.get<OperationTask>('/v2/tasks/by-key', { params: { key: intent.key }, signal })
  if (scope !== getQueryCacheScope()) throw new DOMException('Session changed', 'AbortError')
  saveIntents(userId, pendingIntents(userId).filter((row) => row.key !== intent.key))
  liveRequests.delete(intent.key)
  return data
}
export async function submitTask(userId: number, body: TaskRequest): Promise<OperationTask> {
  const scope = getQueryCacheScope()
  const slot = canonical([body.kind, body.credential_id, body.region, body.resource_name])
  if (scope !== getQueryCacheScope()) throw new DOMException('Session changed', 'AbortError')
  let intent = pendingIntents(userId).find((row) => row.slot === slot)
  const request = canonical(body)
  if (intent && liveRequests.has(intent.key) && liveRequests.get(intent.key) !== request) {
    throw new Error('该资源此前提交结果尚未确认，本次变更未提交。请在任务中心先找回原任务，再决定后续操作。')
  }
  if (!intent) {
    const bytes = crypto.getRandomValues(new Uint8Array(24))
    intent = { key: Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join(''), slot,
      created_at: new Date().toISOString() }
    // Store only submission identity, never passwords, userData or request bodies.
    saveIntents(userId, [...pendingIntents(userId), intent])
    liveRequests.set(intent.key, request)
  } else {
    // After reload, resend the original key with the re-entered request. The
    // server fingerprint decides equality; a 409 is never reported as success.
    liveRequests.set(intent.key, request)
  }
  try {
    const { data } = await api.post<OperationTask>('/v2/tasks', body, {
      headers: { 'Idempotency-Key': intent.key }, timeout: 15000,
    })
    if (scope !== getQueryCacheScope()) throw new DOMException('Session changed', 'AbortError')
    saveIntents(userId, pendingIntents(userId).filter((row) => row.key !== intent.key))
    liveRequests.delete(intent.key)
    return data
  } catch (error) {
    if (scope !== getQueryCacheScope()) throw new DOMException('Session changed', 'AbortError')
    if (axios.isAxiosError(error) && error.response && [400, 404, 409, 422].includes(error.response.status)) {
      if (error.response.status === 409) {
        if (error.response.data?.detail !== '幂等键已用于不同请求') {
          saveIntents(userId, pendingIntents(userId).filter((row) => row.key !== intent.key))
          liveRequests.delete(intent.key)
        }
        throw new Error('请求发生冲突，本次变更未提交。请在任务中心核对原任务或资源锁。')
      }
      if (scope !== getQueryCacheScope()) throw new DOMException('Session changed', 'AbortError')
      saveIntents(userId, pendingIntents(userId).filter((row) => row.key !== intent.key))
      liveRequests.delete(intent.key)
      throw error
    }
    // A timeout may hide a fingerprint conflict after reload. A by-key lookup
    // can recover the prior task, but cannot prove it matches this new body.
    throw new Error('提交结果尚未确认，已保存找回标识。可在任务中心找回原任务，或使用原参数重复提交相同标识。')
  }
}
export function invalidateTaskData(task: OperationTask) {
  if (['succeeded', 'partial', 'failed'].includes(task.status)) invalidateQueryTags('instances', 'traffic', 'metrics')
}
