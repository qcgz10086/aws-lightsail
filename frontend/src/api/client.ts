import axios, { AxiosError } from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 120000,
  // R-5：登录态改走 HttpOnly Cookie，不再把 token 放 localStorage；
  // 同源请求自动带上 Cookie。
  withCredentials: true,
})

api.interceptors.response.use(
  (res) => res,
  (error: AxiosError<{ detail?: string | { msg: string }[] }>) => {
    if (error.response?.status === 401) {
      const path = window.location.pathname
      if (path !== '/login') {
        window.location.href = '/login'
      }
    }
    return Promise.reject(error)
  },
)

export function getErrorMessage(err: unknown, fallback = '请求失败'): string {
  if (axios.isAxiosError(err)) {
    const detail = err.response?.data?.detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) return detail.map((d) => d.msg).join('; ')
    return err.message || fallback
  }
  if (err instanceof Error) return err.message
  return fallback
}

export interface User {
  id: number
  username: string
  is_admin: boolean
  monthly_limit_gb: number | null
  auto_stop_on_limit_default?: boolean
  created_at: string
  has_credentials: boolean
  credential_count?: number
}

export interface CredentialItem {
  id: number
  access_key_masked: string
  account_label?: string | null
  account_id?: string | null
  account_identity_status?: 'confirmed' | 'unknown'
  auto_stop_conflict?: boolean
  /** 脱敏后的代理地址（http://user:***@host:port）；未配代理为 null（直连） */
  proxy_masked?: string | null
  is_default: boolean
  last_validated_at?: string | null
  created_at?: string | null
  /** Lightsail 每 Region 最大 vCPU，如 20 */
  vcpu_quota?: number | null
  /** 社区档位标签：5V / 8V / 32V */
  vcpu_tier?: string | null
  static_ip_quota?: number | null
  used_vcpu?: number | null
  used_instance_count?: number | null
  remaining_vcpu?: number | null
  quota_region?: string | null
  quota_message?: string | null
  quota_checked_at?: string | null
}

export interface CredentialOut {
  has_credentials: boolean
  items: CredentialItem[]
  id?: number | null
  access_key_masked?: string | null
  account_label?: string | null
  last_validated_at?: string | null
}

export interface InstanceTraffic {
  in_gb: number
  out_gb: number
  total_gb: number
  limit_gb: number | null
  over_limit: boolean
  auto_stop_conflict?: boolean
  year_month?: string | null
}

export interface Instance {
  name: string
  region: string
  availability_zone?: string | null
  state: string
  public_ip?: string | null
  private_ip?: string | null
  blueprint_id?: string | null
  blueprint_name?: string | null
  bundle_id?: string | null
  is_static_ip: boolean
  static_ip_name?: string | null
  created_at?: string | null
  traffic?: InstanceTraffic | null
  monthly_limit_gb?: number | null
  auto_stop_on_limit?: boolean
  auto_stop_conflict?: boolean
  auto_stop_identity_changed?: boolean
  configured_auto_stop_on_limit?: boolean
  account_id?: string | null
  note?: string | null
  credential_id?: number | null
  account_label?: string | null
}

export interface BillingServiceCost {
  service: string
  total: number
  net: number
  credits_applied: number
  currency: string
}

export interface CreditRecordOut {
  id: number
  name: string
  credit_id?: string | null
  total_amount: number
  currency: string
  start_date: string
  expires_at?: string | null
  note?: string | null
  credential_id?: number | null
  created_at?: string | null
  remaining_estimate?: number | null
  credits_applied_since_start?: number | null
}

export interface CreditCreateInput {
  name: string
  credit_id?: string | null
  total_amount: number
  currency?: string
  start_date: string
  expires_at?: string | null
  note?: string | null
  credential_id?: number | null
}

export interface AwsCredit {
  credit_id?: string | null
  description?: string | null
  credit_type?: string | null
  status?: string | null
  initial_amount?: number | null
  remaining_amount?: number | null
  estimated_amount?: number | null
  currency: string
  start_date?: string | null
  end_date?: string | null
  exhaust_date?: string | null
  applicable_products: string[]
}

export interface BillingSummary {
  status: 'ok' | 'unavailable' | 'no_permission' | string
  message?: string | null
  year_month: string
  currency: string
  total_cost: number | null
  net_cost: number | null
  credits_applied: number | null
  lightsail_cost: number | null
  forecast_cost: number | null
  by_service: BillingServiceCost[]
  credits: CreditRecordOut[]
  aws_credits: AwsCredit[]
  aws_credits_status: 'ok' | 'unavailable' | 'no_permission' | 'invalid' | string
  aws_credits_error?: string | null
  aws_account_id?: string | null
  required_permissions?: string[]
}

export interface BillingCurrency {
  currency: string
  total_cost: string | null
  net_cost: string | null
  credits_applied: string | null
  refunds: string | null
  discounts: string | null
  lightsail_cost: string | null
  forecast_remaining?: string | null
  forecast_month_total?: string | null
  official_remaining?: string | null
  official_estimated?: string | null
  costs_complete?: boolean
  forecast_complete?: boolean
  credits_complete?: boolean
}

export interface ExactAwsCredit extends Omit<AwsCredit, 'initial_amount' | 'remaining_amount' | 'estimated_amount'> {
  initial_amount: string | null
  remaining_amount: string | null
  estimated_amount: string | null
}

export interface ExactManualCredit extends Omit<CreditRecordOut, 'total_amount' | 'remaining_estimate' | 'credits_applied_since_start'> {
  total_amount: string
  amount_precision: 'legacy_float' | 'exact_decimal'
  precision_note: string
  account_id: string | null
  status: string
  balance_included: boolean
  official_remaining?: string | null
  official_estimated?: string | null
}

export interface BillingV2Summary {
  year_month: string
  status: string
  refreshing?: boolean
  stale?: boolean
  note: string
  currencies: BillingCurrency[]
  manual_credits: ExactManualCredit[]
  pending_assignment: ExactManualCredit[]
  accounts: {
    account_id: string | null
    credential_id: number
    account_label?: string | null
    costs: { status: string; error?: string; cache?: SnapshotMetadata; currencies?: BillingCurrency[];
      by_service?: (BillingCurrency & { service: string })[] }
    forecast: { status: string; error?: string; cache?: SnapshotMetadata; amount?: string; currency?: string }
    credits: { status: string; error?: string; cache?: SnapshotMetadata; credits?: ExactAwsCredit[] }
  }[]
}

export interface SnapshotMetadata {
  status: string
  stale: boolean
  refreshing: boolean
  last_success_at: string | null
  expires_at?: string | null
  error: string | null
}

export interface TrafficSummary {
  year_month: string
  instances: {
    region: string
    name: string
    credential_id?: number | null
    account_label?: string | null
    account_id?: string | null
    in_gb: number
    out_gb: number
    total_gb: number
    limit_gb: number | null
    over_limit: boolean
    auto_stop_on_limit?: boolean
    auto_stop_conflict?: boolean
    year_month: string
  }[]
  by_region: {
    region: string
    total_gb: number
    instance_count: number
  }[]
  note: string
}

export default api
