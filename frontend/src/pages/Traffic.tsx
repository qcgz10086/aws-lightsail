import { Alert, Button, Card, Col, Row, Space, Table, Tag, Typography, message, Statistic, Select } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import api, { CredentialItem, CredentialOut, readApi, TrafficSummary, getErrorMessage,
  invalidateQueryScope } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { submitTask, taskLabels, type OperationTask } from '../api/tasks'

const trafficSteps: Record<string, string> = { instances: '实例列表', NetworkIn: '入站流量',
  NetworkOut: '出站流量', instance_state: '实例状态', auto_stop: '自动关机' }

export default function TrafficPage() {
  const { user, sessionKey } = useAuth()
  const navigate = useNavigate()
  const [data, setData] = useState<TrafficSummary | null>(null)
  const [creds, setCreds] = useState<CredentialItem[]>([])
  const [filterCred, setFilterCred] = useState<number | 'all'>('all')
  const filterCredRef = useRef(filterCred)
  filterCredRef.current = filterCred
  const [loading, setLoading] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const requestVersion = useRef(0)
  const readController = useRef<AbortController | null>(null)
  const latestLoad = useRef<() => Promise<void>>(async () => {})
  const actionScope = useRef('')
  actionScope.current = `${user?.id}:${sessionKey}:${filterCred}`
  useEffect(() => () => { actionScope.current = '' }, [])

  const load = async () => {
    readController.current?.abort()
    const controller = new AbortController()
    readController.current = controller
    const version = ++requestVersion.current
    setLoading(true)
    try {
      const params = filterCred === 'all' ? {} : { credential_id: filterCred }
      const result = await readApi<TrafficSummary>('/traffic/summary', params, { signal: controller.signal, tags: ['traffic'] })
      if (requestVersion.current === version) setData(result)
    } catch (err) {
      if (requestVersion.current === version && !controller.signal.aborted) message.error(getErrorMessage(err))
    } finally {
      if (requestVersion.current === version) setLoading(false)
    }
  }
  latestLoad.current = load

  useEffect(() => {
    const credentialController = new AbortController()
    if (user?.has_credentials) {
      setData(null)
      setCreds([])
      void (async () => {
        try {
          const res = await readApi<CredentialOut>('/credentials', {}, { signal: credentialController.signal })
          if (!credentialController.signal.aborted) setCreds(res.items || [])
        } catch {
          /* ignore */
        }
      })()
      void load()
    }
    return () => { requestVersion.current += 1; readController.current?.abort(); credentialController.abort() }
  }, [user?.has_credentials, user?.id, sessionKey, filterCred])

  if (!user?.has_credentials) {
    return (
      <Alert
        type="info"
        showIcon
        message="请先绑定 AWS 凭证"
        description={<Link to="/credentials">前往绑定</Link>}
      />
    )
  }

  const totalAll = (data?.by_region || []).reduce((s, r) => s + r.total_gb, 0)
  const autoStopCount = (data?.instances || []).filter((i) => i.over_limit && i.auto_stop_on_limit).length

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
        <Typography.Title level={3} style={{ margin: 0 }}>
          流量监控
        </Typography.Title>
        <Space wrap>
          <Select
            style={{ minWidth: 200 }}
            value={filterCred}
            onChange={setFilterCred}
            options={[
              { value: 'all', label: '全部凭证' },
              ...creds.map((c) => ({
                value: c.id,
                label: `${c.account_label || '凭证'} #${c.id}`,
              })),
            ]}
          />
          <Button
            type="primary"
            loading={syncing}
            onClick={async () => {
              const scope = actionScope.current
              setSyncing(true)
              try {
                if (!user) return
                const groups = new Map<string, CredentialItem>()
                for (const cred of creds.filter((c) => filterCred === 'all' || c.id === filterCred)) {
                  const identity = cred.account_id || `credential:${cred.id}`
                  if (!groups.has(identity) || cred.is_default) groups.set(identity, cred)
                }
                for (const cred of groups.values()) {
                  if (scope !== actionScope.current) return
                  await submitTask(user.id, { kind: 'traffic_sync', credential_id: cred.id })
                }
                if (scope !== actionScope.current) return
                message.info('流量同步任务已接受，请查看执行进度')
                navigate('/tasks')
              } catch (err) {
                if (scope === actionScope.current) message.error(getErrorMessage(err))
              } finally {
                if (scope === actionScope.current) setSyncing(false)
              }
            }}
          >
            立即同步
          </Button>
          <Button onClick={() => {
            const credential_id = filterCredRef.current === 'all' ? undefined : filterCredRef.current
            invalidateQueryScope('traffic', { credential_id })
            void latestLoad.current()
          }} loading={loading}>
            刷新
          </Button>
        </Space>
      </div>

      <Alert
        type="warning"
        showIcon
        message={data?.note || '流量基于 Lightsail 指标估算，与账单可能存在差异'}
        description={
          <>
            统计月份（UTC）：{data?.year_month || '-'}。默认仅告警；仅当实例勾选「超限自动关机」时才会 stop。
            {autoStopCount > 0 ? ` 当前有 ${autoStopCount} 台超限且已开启自动关机。` : ''}
          </>
        }
      />

      <Row gutter={[16, 16]}>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic title="全部地区总流量" value={Number(totalAll.toFixed(2))} suffix="GB" />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic title="实例数（有记录）" value={data?.instances.length || 0} />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic
              title="超限实例"
              value={(data?.instances || []).filter((i) => i.over_limit).length}
              valueStyle={{
                color: (data?.instances || []).some((i) => i.over_limit) ? '#cf1322' : undefined,
              }}
            />
          </Card>
        </Col>
      </Row>

      {(data?.latest_syncs || []).map((sync) => <Card key={sync.task_id}
        title={`最近同步 · ${sync.account_id || `凭证 #${sync.credential_id}`}`}
        extra={<Link to={`/tasks/${sync.task_id}`}>查看任务</Link>}>
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text>{taskLabels[sync.status as OperationTask['status']] || sync.status}
            {` · 提交于 ${sync.created_at}`}</Typography.Text>
          {typeof sync.result?.count === 'number' && <Typography.Text>
            成功采集 {sync.result.count} 台实例
            {sync.result.successful_regions !== undefined ? ` · 成功 ${sync.result.successful_regions} 个区域` : ''}
            {sync.result.failed_regions !== undefined ? ` · 失败 ${sync.result.failed_regions} 个区域` : ''}
          </Typography.Text>}
          {sync.result?.message && <Alert type="info" showIcon message={sync.result.message} />}
          {sync.error && <Alert type="error" showIcon message={sync.error} />}
          {!!sync.result?.errors?.length && <>
            <Alert type="warning" showIcon message="同步部分失败"
              description="下方流量包含已保存的观测值；失败范围可能仍显示上次记录，缺失数据未计入本次成功数量。" />
            <Table rowKey={(row, index) => `${row.region}/${row.name || ''}/${row.step}/${index}`}
              dataSource={sync.result.errors} pagination={false} scroll={{ x: 800 }} columns={[
                { title: '失败区域', dataIndex: 'region' }, { title: '实例', dataIndex: 'name' },
                { title: '步骤', dataIndex: 'step', render: (step: string) => trafficSteps[step] || step },
                { title: '错误码', dataIndex: 'code', render: (value?: string) => value || '历史任务未记录' },
                { title: '原因', dataIndex: 'reason', render: (value?: string) => value || '请重新同步以获取详情' },
                { title: '重试条件', dataIndex: 'retry_condition' },
              ]} />
          </>}
        </Space>
      </Card>)}

      <Card title="按地区汇总（同区多机相加）">
        <Table
          rowKey="region"
          loading={loading}
          dataSource={data?.by_region || []}
          pagination={false}
          columns={[
            { title: '地区', dataIndex: 'region' },
            { title: '实例数', dataIndex: 'instance_count', width: 100 },
            {
              title: '总流量 (GB)',
              dataIndex: 'total_gb',
              render: (v: number) => <b>{v}</b>,
            },
          ]}
        />
      </Card>

      <Card title="按实例明细">
        <Table
          rowKey={(r) => `${r.credential_id}/${r.account_id || 'unknown'}/${r.region}/${r.name}`}
          loading={loading}
          dataSource={data?.instances || []}
          columns={[
            {
              title: '账号',
              width: 120,
              render: (_, r) => r.account_id && r.account_label
                ? `${r.account_label} (${r.account_id})`
                : r.account_id ? `账号 ${r.account_id}` : r.account_label || (r.credential_id ? `#${r.credential_id}` : '-'),
            },
            { title: '地区', dataIndex: 'region', width: 130 },
            { title: '实例', dataIndex: 'name' },
            { title: '入站 GB', dataIndex: 'in_gb', width: 90 },
            { title: '出站 GB', dataIndex: 'out_gb', width: 90 },
            { title: '合计 GB', dataIndex: 'total_gb', width: 90 },
            {
              title: '限额 GB',
              dataIndex: 'limit_gb',
              width: 90,
              render: (v) => (v == null ? '-' : v),
            },
            {
              title: '超限',
              dataIndex: 'over_limit',
              width: 80,
              render: (v: boolean) =>
                v ? <Tag color="red">超限</Tag> : <Tag color="green">正常</Tag>,
            },
            {
              title: '自动关机',
              dataIndex: 'auto_stop_on_limit',
              width: 90,
              render: (v: boolean, r) => r.auto_stop_conflict
                ? <Tag color="red">冲突，已暂停</Tag>
                : v ? <Tag color="orange">开</Tag> : <Tag>关</Tag>,
            },
          ]}
        />
      </Card>
    </Space>
  )
}
