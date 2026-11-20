import { Alert, Button, Card, Space, Tag, Typography } from 'antd'
import { useEffect, useState } from 'react'
import type { QueryRoundProgress, RegionDiscovery, CachedInstancesResponse } from '../api/client'

const labels: Record<string, string> = {
  queued: '排队中', discovering: '发现区域中', running: '查询中', succeeded: '已完成',
  partial: '部分成功', failed: '失败', cancelling: '正在取消后续读取', cancelled: '已取消',
  invalidated: '凭据待校验',
  cooldown: '冷却中', disabled: '区域未启用',
}

export function RetryCountdown({ value }: { value?: string | null }) {
  const [now, setNow] = useState(Date.now())
  const deadline = value ? Date.parse(value) : 0
  useEffect(() => {
    if (!deadline || deadline <= Date.now()) return
    const timer = setInterval(() => {
      const current = Date.now()
      setNow(current)
      if (current >= deadline) clearInterval(timer)
    }, 1000)
    return () => clearInterval(timer)
  }, [deadline])
  if (!deadline) return null
  const remaining = Math.max(0, Math.ceil((deadline - now) / 1000))
  return <span>{remaining ? ` · 冷却剩余 ${remaining} 秒` : ' · 现在可重新刷新'}</span>
}

export default function QueryProgress({ rounds, discovery, scopes = [], onCancel }: {
  rounds: QueryRoundProgress[]
  discovery: RegionDiscovery[]
  scopes?: CachedInstancesResponse['scopes']
  onCancel: (id: string) => void
}) {
  if (!rounds.length && !discovery.length) return null
  return <Card size="small" title="区域查询进度">
    <Space direction="vertical" style={{ width: '100%' }}>
      {rounds.map((round) => <Space key={round.id} wrap>
        <Typography.Text>{round.account_id || `凭证 #${round.credential_id}`}</Typography.Text>
        <Tag>{round.mode === 'global' ? '全局扫描' : '活跃区域刷新'}</Tag>
        <Tag color={round.failed ? 'warning' : round.status === 'succeeded' ? 'success' : 'processing'}>
          {labels[round.status] || round.status}</Tag>
        <span>{round.total === null ? '发现区域中' : `${round.completed}/${round.total} 个区域`}</span>
        <span>成功 {round.succeeded} · 失败 {round.failed} · 取消 {round.cancelled}</span>
        {round.can_cancel && <Button size="small" onClick={() => onCancel(round.id)}>取消本轮</Button>}
        {round.error && <Typography.Text type="danger">{round.error}</Typography.Text>}
      </Space>)}
      {discovery.map((item) => <div key={item.credential_id}>
        <Typography.Text type="secondary">{item.account_id || `凭证 #${item.credential_id}`}：
          活跃区域 {item.regions.length} · 尚未发现 {item.undiscovered} ·
          上次全局扫描 {item.last_global_finished_at || item.last_global_started_at || '尚未执行'}
          {item.discovery_state === 'partial' ? '（未完成，可执行全局扫描补查）' : ''}
        </Typography.Text>
        {item.state === 'empty' && <Alert type="info" showIcon message="暂无活跃区域，可执行全局扫描" />}
      </div>)}
      <Space wrap>{scopes.map((scope) => <Tag key={`${scope.credential_id}/${scope.region}`}
        color={scope.error ? 'warning' : scope.status === 'succeeded' ? 'success' : undefined}>
        {scope.account_id || `凭证 #${scope.credential_id}`} / {scope.region || '目录'}：
        {labels[scope.status] || scope.status}<RetryCountdown value={scope.next_retry_at} />
        {scope.opt_status && scope.opt_status !== 'UNKNOWN' && scope.opt_status !== 'ENABLED'
          && scope.opt_status !== 'ENABLED_BY_DEFAULT' ? ` · ${scope.opt_status}` : ''}
      </Tag>)}</Space>
      {rounds.some((round) => round.can_cancel) && <Typography.Text type="secondary">
        已完成结果先行显示。关闭页面后本轮继续；再次打开可查看同一轮进度。取消只停止尚未发送的后续读取。
      </Typography.Text>}
    </Space>
  </Card>
}
