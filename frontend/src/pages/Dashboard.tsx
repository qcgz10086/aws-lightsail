import { Alert, Button, Card, Col, List, Row, Statistic, Tag, Typography, Spin, Space } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import api, { CachedInstancesResponse, Instance, readApi, SnapshotMetadata, TrafficSummary, getErrorMessage,
  invalidateQueryScope } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { snapshotPollDelay } from '../api/snapshotPolling'

export default function DashboardPage() {
  const { user, sessionKey } = useAuth()
  const [instances, setInstances] = useState<Instance[]>([])
  const [traffic, setTraffic] = useState<TrafficSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [instancesRefreshing, setInstancesRefreshing] = useState(false)
  const [snapshotScopes, setSnapshotScopes] = useState<(SnapshotMetadata & {
    region: string | null; credential_id: number; account_id?: string | null
  })[]>([])
  const requestVersion = useRef(0)
  const activeRead = useRef<AbortController | null>(null)
  const trafficRead = useRef<AbortController | null>(null)
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pollStarted = useRef(0)
  const trafficVersion = useRef(0)

  const load = async (force = false, refreshTraffic = true) => {
    activeRead.current?.abort()
    const controller = new AbortController()
    activeRead.current = controller
    const version = ++requestVersion.current
    setLoading(true)
    setError(null)
    try {
      if (!user?.has_credentials) {
        setInstances([])
        setTraffic(null)
        return
      }
      if (refreshTraffic) void loadTraffic(force)
      const nextInstances = await readApi<CachedInstancesResponse>('/v2/instances', {}, {
        signal: controller.signal, tags: ['instances'], force, cacheIf: (value) => !value.refreshing,
      })
      if (requestVersion.current === version) {
        setInstances(nextInstances.instances)
        setSnapshotScopes(nextInstances.scopes)
        setInstancesRefreshing(nextInstances.refreshing)
        if (nextInstances.refreshing && Date.now() - pollStarted.current < 30000) {
          if (pollTimer.current) clearTimeout(pollTimer.current)
          pollTimer.current = setTimeout(() => void load(false, false), snapshotPollDelay(pollStarted.current))
        }
      }
    } catch (err) {
      if (requestVersion.current === version && !controller.signal.aborted) setError(getErrorMessage(err))
    } finally {
      if (requestVersion.current === version) setLoading(false)
    }
  }

  const loadTraffic = async (force = false) => {
    trafficRead.current?.abort()
    const controller = new AbortController()
    trafficRead.current = controller
    const version = ++trafficVersion.current
    try {
      const nextTraffic = await readApi<TrafficSummary>('/traffic/summary', {}, {
        signal: controller.signal, tags: ['traffic'], force,
      })
      if (trafficVersion.current === version) setTraffic(nextTraffic)
    } catch (err) {
      if (trafficVersion.current === version && !controller.signal.aborted) setError(getErrorMessage(err))
    }
  }

  useEffect(() => {
    setInstances([])
    setTraffic(null)
    setInstancesRefreshing(false)
    setSnapshotScopes([])
    pollStarted.current = Date.now()
    void load()
    return () => {
      requestVersion.current += 1
      trafficVersion.current += 1
      if (pollTimer.current) clearTimeout(pollTimer.current)
      activeRead.current?.abort()
      trafficRead.current?.abort()
    }
  }, [user?.has_credentials, user?.id, sessionKey])

  if (!user?.has_credentials) {
    return (
      <Alert
        type="info"
        showIcon
        message="尚未绑定 AWS 凭证"
        description={
          <span>
            请先到 <Link to="/credentials">AWS 凭证</Link> 页面绑定 Access Key（可绑定多组），再管理
            Lightsail 实例。
          </span>
        }
      />
    )
  }

  const running = instances.filter((i) => i.state === 'running').length
  const overLimit = traffic?.instances.filter((i) => i.over_limit) || []
  const autoStopOver = overLimit.filter((i) => i.auto_stop_on_limit)
  const failedInstanceScopes = snapshotScopes.filter((scope) =>
    scope.error || ['error', 'unavailable', 'no_permission', 'invalidated'].includes(scope.status))
  const hasKnownInstanceScope = snapshotScopes.some((scope) => scope.region
    && (scope.status === 'fresh' || scope.stale))
  const emptyTotalUnavailable = instances.length === 0 && failedInstanceScopes.length > 0 && !hasKnownInstanceScope
  const instanceLabel = failedInstanceScopes.length ? '实例总数（部分范围）' : '实例总数'

  return (
    <Spin spinning={loading}>
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Typography.Title level={3} style={{ margin: 0 }}>
            总览
          </Typography.Title>
          <Button onClick={async () => {
            try {
              await api.post('/v2/refresh', { kind: 'instances' })
              invalidateQueryScope('instances')
              invalidateQueryScope('traffic')
              pollStarted.current = Date.now()
              void load(true)
            } catch (err) { setError(getErrorMessage(err)) }
          }}>刷新</Button>
        </div>

        {error && <Alert type="error" showIcon message={error} />}
        {instancesRefreshing && <Alert type="info" showIcon message="实例快照后台刷新中，成功区域先行显示" />}
        {snapshotScopes.some((scope) => scope.stale || scope.error || scope.status === 'no_permission'
          || scope.status === 'unavailable' || scope.status === 'invalidated' || scope.status === 'error') ? (
          <Alert type="warning" showIcon message="实例汇总不完整：部分账号或地区数据已过期或不可用"
            description={<List size="small" dataSource={snapshotScopes.filter((scope) => scope.stale
              || scope.error || scope.status === 'no_permission' || scope.status === 'unavailable'
              || scope.status === 'invalidated' || scope.status === 'error')}
              renderItem={(scope) => <List.Item>
                <Space direction="vertical" size={0}>
                  <span>{scope.account_id ? `账号 ${scope.account_id}` : `凭证 #${scope.credential_id}`}
                    {' / '}{scope.region || '目录'}</span>
                  {scope.last_success_at ? <Typography.Text type="secondary">
                    最近成功：{scope.last_success_at}</Typography.Text> : null}
                  <Typography.Text type={scope.error ? 'danger' : 'secondary'}>
                    {scope.error || (scope.status === 'no_permission' ? '权限不足' : scope.status)}</Typography.Text>
                </Space>
              </List.Item>} />}
          />
        ) : null}

        <Row gutter={[16, 16]}>
          <Col xs={24} sm={6}>
            <Card>
              <Statistic title={instanceLabel} value={emptyTotalUnavailable ? '—' : instances.length} />
            </Card>
          </Col>
          <Col xs={24} sm={6}>
            <Card>
              <Statistic title={failedInstanceScopes.length ? '运行中（部分范围）' : '运行中'}
                value={emptyTotalUnavailable ? '—' : running} valueStyle={{ color: '#3f8600' }} />
            </Card>
          </Col>
          <Col xs={24} sm={6}>
            <Card>
              <Statistic
                title="超限告警"
                value={overLimit.length}
                valueStyle={{ color: overLimit.length ? '#cf1322' : undefined }}
              />
            </Card>
          </Col>
          <Col xs={24} sm={6}>
            <Card>
              <Statistic title="绑定凭证数" value={user.credential_count || 0} />
            </Card>
          </Col>
        </Row>

        {overLimit.length > 0 && (
          <Alert
            type="warning"
            showIcon
            message="月流量超限"
            description={
              <List
                size="small"
                dataSource={overLimit}
                renderItem={(item) => (
                  <List.Item>
                    {item.account_label ? `[${item.account_label}] ` : ''}
                    {item.region} / {item.name}：{item.total_gb} GB
                    {item.limit_gb != null ? ` / 限额 ${item.limit_gb} GB` : ''}
                    {item.auto_stop_on_limit ? (
                      <Tag color="orange" style={{ marginLeft: 8 }}>
                        已开启自动关机
                      </Tag>
                    ) : (
                      <Tag style={{ marginLeft: 8 }}>仅告警</Tag>
                    )}
                  </List.Item>
                )}
              />
            }
          />
        )}

        {autoStopOver.length > 0 && (
          <Alert
            type="error"
            showIcon
            message={`${autoStopOver.length} 台实例超限且已勾选自动关机，采集任务会尝试 stop（若仍在运行）`}
          />
        )}

        <Card title={`地区总流量（${traffic?.year_month || '-'}）`}>
          <Typography.Paragraph type="secondary" style={{ marginTop: 0 }}>
            同地区多台实例流量会相加。例如 2 台各 1024 GB → 该地区显示 2048 GB。
          </Typography.Paragraph>
          <List
            dataSource={traffic?.by_region || []}
            locale={{ emptyText: '暂无流量数据，可到「流量」页手动同步' }}
            renderItem={(item) => (
              <List.Item>
                <Space>
                  <Tag color="blue">{item.region}</Tag>
                  <span>
                    总流量 <b>{item.total_gb}</b> GB
                  </span>
                  <Typography.Text type="secondary">（{item.instance_count} 台）</Typography.Text>
                </Space>
              </List.Item>
            )}
          />
        </Card>
      </Space>
    </Spin>
  )
}
