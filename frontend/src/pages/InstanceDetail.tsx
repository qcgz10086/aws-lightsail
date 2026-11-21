import { Alert, Button, Card, Col, Row, Space, Spin, Tag, Typography, message, Select } from 'antd'
import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import api, { Instance, readApi, getErrorMessage } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import type { TrafficPoint } from '../components/TrafficChart'
import dayjs from 'dayjs'

const TrafficChart = lazy(() => import('../components/TrafficChart'))

export default function InstanceDetailPage() {
  const { sessionKey } = useAuth()
  const { region = '', name = '' } = useParams()
  const [search] = useSearchParams()
  const credentialId = search.get('credential_id') || undefined
  const [inst, setInst] = useState<Instance | null>(null)
  const [loading, setLoading] = useState(true)
  const [period, setPeriod] = useState<'day' | 'week' | 'month'>('day')
  const [points, setPoints] = useState<TrafficPoint[]>([])
  const detailVersion = useRef(0)
  const metricsVersion = useRef(0)
  const detailController = useRef<AbortController | null>(null)
  const metricsController = useRef<AbortController | null>(null)

  const params = credentialId ? { credential_id: credentialId } : {}

  const load = async () => {
    detailController.current?.abort()
    const controller = new AbortController()
    detailController.current = controller
    const version = ++detailVersion.current
    setLoading(true)
    try {
      const result = await readApi<Instance>(`/instances/${region}/${name}`, params, {
        signal: controller.signal, tags: ['instances'],
      })
      if (detailVersion.current === version) setInst(result)
    } catch (err) {
      if (!controller.signal.aborted && detailVersion.current === version) message.error(getErrorMessage(err))
    } finally {
      if (detailVersion.current === version) setLoading(false)
    }
  }

  const loadMetrics = async (p: 'day' | 'week' | 'month') => {
    metricsController.current?.abort()
    const controller = new AbortController()
    metricsController.current = controller
    const version = ++metricsVersion.current
    try {
      const result = await readApi<{ points?: { timestamp: string; network_in_bytes: number; network_out_bytes: number }[] }>(
        `/metrics/${region}/${name}`, { period: p, ...params }, { signal: controller.signal, tags: ['metrics'] })
      if (metricsVersion.current !== version) return
      setPoints(
        (result.points || []).map(
          (pt: { timestamp: string; network_in_bytes: number; network_out_bytes: number }) => ({
            time: dayjs(pt.timestamp).format(p === 'day' ? 'HH:mm' : 'MM-DD HH:mm'),
            in_mb: Number((pt.network_in_bytes / (1024 * 1024)).toFixed(2)),
            out_mb: Number((pt.network_out_bytes / (1024 * 1024)).toFixed(2)),
          }),
        ),
      )
    } catch (err) {
      if (!controller.signal.aborted && metricsVersion.current === version) message.error(getErrorMessage(err))
    }
  }

  useEffect(() => {
    setInst(null)
    setPoints([])
    void load()
    return () => { detailVersion.current += 1; detailController.current?.abort() }
  }, [region, name, credentialId, sessionKey])

  useEffect(() => {
    void loadMetrics(period)
    return () => { metricsVersion.current += 1; metricsController.current?.abort() }
  }, [region, name, period, credentialId, sessionKey])

  if (loading && !inst) return <Spin />

  if (!inst) {
    return <Alert type="error" message="实例不存在或加载失败" />
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>
            {inst.name}
          </Typography.Title>
          <Typography.Text type="secondary">
            {inst.account_label ? `[${inst.account_label}] ` : ''}
            {inst.region} · <Tag>{inst.state}</Tag>
          </Typography.Text>
        </div>
        <Space>
          <Button>
            <Link to="/instances">返回列表</Link>
          </Button>
          <Button onClick={() => void load()}>刷新</Button>
        </Space>
      </div>

      <Row gutter={[16, 16]}>
        <Col xs={24} md={8}>
          <Card title="基本信息">
            <p>AWS 凭证：{inst.account_label || (inst.credential_id ? `#${inst.credential_id}` : '-')}</p>
            <p>公网 IP：{inst.public_ip || '-'}</p>
            <p>内网 IP：{inst.private_ip || '-'}</p>
            <p>静态 IP：{inst.is_static_ip ? inst.static_ip_name || '是' : '否'}</p>
            <p>镜像：{inst.blueprint_name || inst.blueprint_id || '-'}</p>
            <p>套餐：{inst.bundle_id || '-'}</p>
            <p>
              当月流量：{inst.traffic?.total_gb ?? 0} GB
              {inst.traffic?.over_limit ? (
                <Tag color="red" style={{ marginLeft: 8 }}>
                  超限
                </Tag>
              ) : null}
            </p>
            <p>{inst.auto_stop_identity_changed ? '原账号保存限额（未生效）' : '月限额'}：
              {inst.monthly_limit_gb ?? inst.traffic?.limit_gb ?? '未设置'} GB</p>
            <p>
              超限自动关机：
              {inst.auto_stop_conflict ? (
                <Tag color="red">策略冲突，已暂停</Tag>
              ) : inst.auto_stop_identity_changed ? (
                <Tag color="gold">AWS 账号变化，已暂停</Tag>
              ) : inst.auto_stop_on_limit ? (
                <Tag color="orange">已开启</Tag>
              ) : (
                <Tag>仅告警</Tag>
              )}
            </p>
            <p>{inst.auto_stop_identity_changed ? '原账号备注' : '备注'}：{inst.note || '-'}</p>
          </Card>
        </Col>
        <Col xs={24} md={16}>
          <Card
            title="流量趋势（估算）"
            extra={
              <Select
                value={period}
                style={{ width: 120 }}
                onChange={(v) => setPeriod(v)}
                options={[
                  { value: 'day', label: '近 24 小时' },
                  { value: 'week', label: '近 7 天' },
                  { value: 'month', label: '近 30 天' },
                ]}
              />
            }
          >
            <div style={{ width: '100%', height: 320 }}>
              <Suspense fallback={<Spin aria-label="正在加载流量图" />}>
                <TrafficChart points={points} />
              </Suspense>
            </div>
          </Card>
        </Col>
      </Row>
    </Space>
  )
}
