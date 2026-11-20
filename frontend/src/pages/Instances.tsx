import {
  Alert,
  Button,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Space,
  Table,
  Tag,
  Typography,
  message,
  Modal,
  Card,
  Select,
  Switch,
} from 'antd'
import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import api, { CachedInstancesResponse, CredentialItem, CredentialOut, Instance, readApi,
  SnapshotMetadata, getErrorMessage, invalidateQueryScope, invalidateQueryTags } from '../api/client'
import type { QueryRoundProgress, RegionDiscovery } from '../api/client'
import QueryProgress, { RetryCountdown } from '../components/QueryProgress'
import { useAuth } from '../auth/AuthContext'
import { submitTask, TaskKind } from '../api/tasks'

const stateColor: Record<string, string> = {
  running: 'green',
  stopped: 'default',
  pending: 'processing',
  stopping: 'orange',
  starting: 'processing',
}

export default function InstancesPage() {
  const { user, sessionKey } = useAuth()
  const navigate = useNavigate()
  const [data, setData] = useState<Instance[]>([])
  const [creds, setCreds] = useState<CredentialItem[]>([])
  const [filterCred, setFilterCred] = useState<number | 'all'>('all')
  const [filterRegion, setFilterRegion] = useState<string | 'all'>('all')
  const [loading, setLoading] = useState(false)
  const [rounds, setRounds] = useState<QueryRoundProgress[]>([])
  const [discovery, setDiscovery] = useState<RegionDiscovery[]>([])
  const roundIds = useRef<string[]>([])
  const [snapshotScopes, setSnapshotScopes] = useState<(SnapshotMetadata & { region: string | null; credential_id: number })[]>([])
  const [acting, setActing] = useState<string | null>(null)
  const [limitModal, setLimitModal] = useState<Instance | null>(null)
  const [limitForm] = Form.useForm()
  const editedLimitFields = useRef(new Set<string>())
  const requestVersion = useRef(0)
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pollStarted = useRef(0)
  const readController = useRef<AbortController | null>(null)
  const actionRefreshTimers = useRef(new Set<ReturnType<typeof setTimeout>>())
  const latestLoad = useRef<() => Promise<void>>(async () => {})
  const actionScope = useRef('')
  actionScope.current = `${user?.id}:${sessionKey}:${filterCred}:${filterRegion}`
  useEffect(() => () => { actionScope.current = '' }, [])

  const loadCreds = async (signal?: AbortSignal) => {
    try {
      const res = await readApi<CredentialOut>('/credentials', {}, { signal })
      if (!signal?.aborted) setCreds(res.items || [])
    } catch {
      /* ignore */
    }
  }

  const load = async () => {
    readController.current?.abort()
    const controller = new AbortController()
    readController.current = controller
    const version = ++requestVersion.current
    setLoading(true)
    try {
      const params = {
        ...(filterCred === 'all' ? {} : { credential_id: filterCred }),
        ...(filterRegion === 'all' ? {} : { region: filterRegion }),
        ...(roundIds.current.length ? { round_ids: roundIds.current.join(',') } : {}),
      }
      const res = await readApi<CachedInstancesResponse>('/v2/query-instances', params, {
        signal: controller.signal,
        tags: ['instances'],
        cacheIf: (value) => !value.refreshing,
      })
      if (version === requestVersion.current) {
        setData(res.instances)
        setSnapshotScopes(res.scopes)
        setRounds(res.rounds || [])
        setDiscovery(res.discovery || [])
        roundIds.current = (res.rounds || []).map((round) => round.id)
        if (res.refreshing) {
          pollTimer.current = setTimeout(() => void load(), 1000)
        }
      }
    } catch (err) {
      if (version === requestVersion.current && !controller.signal.aborted) message.error(getErrorMessage(err))
    } finally {
      if (version === requestVersion.current) setLoading(false)
    }
  }
  latestLoad.current = load

  useEffect(() => {
    const credentialController = new AbortController()
    if (user?.has_credentials) {
      setData([])
      setSnapshotScopes([])
      setRounds([])
      setDiscovery([])
      roundIds.current = []
      setCreds([])
      pollStarted.current = Date.now()
      void loadCreds(credentialController.signal)
      void load()
    }
    return () => {
      requestVersion.current += 1
      readController.current?.abort()
      credentialController.abort()
      if (pollTimer.current) clearTimeout(pollTimer.current)
    }
  }, [user?.has_credentials, user?.id, sessionKey, filterCred, filterRegion])

  useEffect(() => () => {
    for (const timer of actionRefreshTimers.current) clearTimeout(timer)
    actionRefreshTimers.current.clear()
  }, [user?.id])

  const refresh = async (mode: 'refresh' | 'global' = 'refresh') => {
    try {
      if (pollTimer.current) clearTimeout(pollTimer.current)
      pollStarted.current = Date.now()
      const response = await api.post<{ round_ids: string[] }>('/v2/queries', { mode,
        credential_id: filterCred === 'all' ? null : filterCred,
        region: mode === 'global' || filterRegion === 'all' ? null : filterRegion })
      roundIds.current = response.data.round_ids
      invalidateQueryScope('instances', {
        ...(filterCred === 'all' ? {} : { credential_id: filterCred }),
        ...(filterRegion === 'all' ? {} : { region: filterRegion }),
      })
      await latestLoad.current()
    } catch (error) { message.error(getErrorMessage(error)) }
  }

  const runAction = async (inst: Instance, action: string) => {
    const key = `${inst.credential_id}/${inst.region}/${inst.name}:${action}`
    const scope = actionScope.current
    setActing(key)
    try {
      if (!user || !inst.credential_id) throw new Error('缺少任务归属凭证')
      const task = await submitTask(user.id, { kind: action.replace('-', '_') as TaskKind,
        credential_id: inst.credential_id, region: inst.region, resource_name: inst.name })
      if (scope !== actionScope.current) return
      message.info('任务已接受，正在确认 AWS 执行结果')
      navigate(`/tasks/${task.task_id}`)
    } catch (err) {
      if (scope === actionScope.current) message.error(getErrorMessage(err))
    } finally {
      if (scope === actionScope.current) setActing(null)
    }
  }

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

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12 }}>
        <Typography.Title level={3} style={{ margin: 0 }}>
          实例列表
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
                label: `${c.account_label || '凭证'} #${c.id}${c.is_default ? ' (默认)' : ''}`,
              })),
            ]}
          />
          <Select
            style={{ minWidth: 160 }}
            value={filterRegion}
            onChange={(value) => { pollStarted.current = Date.now(); setFilterRegion(value) }}
            options={[{ value: 'all', label: '全部地区' }, ...[...new Set([
              ...data.map((item) => item.region),
              ...snapshotScopes.map((item) => item.region).filter((item): item is string => Boolean(item)),
            ])].sort().map((region) => ({ value: region, label: region }))]}
          />
          <Button type="primary" onClick={() => navigate('/create')}>
            开通实例
          </Button>
          <Button onClick={() => void refresh()} loading={loading}>
            刷新
          </Button>
          <Button onClick={() => void refresh('global')} title="扫描所选账号全部支持区域，不受地区筛选限制">
            全局扫描
          </Button>
        </Space>
      </div>

      <QueryProgress rounds={rounds} discovery={discovery} scopes={snapshotScopes} onCancel={(id) => {
        void api.post(`/v2/queries/${id}/cancel`).then(() => {
          invalidateQueryTags('instances'); return latestLoad.current()
        }).catch((error) => message.error(getErrorMessage(error)))
      }} />
      {snapshotScopes.filter((scope) => scope.stale || scope.error).map((scope) => <Alert
        key={`${scope.credential_id}/${scope.region || 'regions'}`} type="warning" showIcon
        message={`凭证 #${scope.credential_id} / ${scope.region || '区域目录'}：${scope.stale ? '已过期快照' : '查询失败'}`}
        description={<span>最近成功：{scope.last_success_at || '无'}；{scope.error || '正在刷新'}
          <RetryCountdown value={scope.next_retry_at} /></span>} />)}
      <Card>
        <Table
          rowKey={(r) => `${r.credential_id}/${r.region}/${r.name}`}
          loading={loading}
          dataSource={data}
          scroll={{ x: 1200 }}
          columns={[
            {
              title: '名称',
              dataIndex: 'name',
              render: (name: string, row) => (
                <Link to={`/instances/${row.region}/${name}?credential_id=${row.credential_id || ''}`}>
                  {name}
                </Link>
              ),
            },
            {
              title: 'AWS 账号',
              width: 140,
              render: (_, row) => row.account_label || (row.credential_id ? `#${row.credential_id}` : '-'),
            },
            { title: '地区', dataIndex: 'region', width: 140 },
            {
              title: '状态',
              dataIndex: 'state',
              width: 110,
              render: (s: string) => <Tag color={stateColor[s] || 'default'}>{s}</Tag>,
            },
            {
              title: '公网 IP',
              dataIndex: 'public_ip',
              width: 150,
              render: (ip: string, row) => (
                <span>
                  {ip || '-'}
                  {row.is_static_ip ? <Tag style={{ marginLeft: 6 }}>静态</Tag> : null}
                </span>
              ),
            },
            {
              title: '当月流量',
              width: 130,
              render: (_, row) => {
                const t = row.traffic
                if (!t) return '-'
                return (
                  <span style={{ color: t.over_limit ? '#cf1322' : undefined }}>
                    {t.total_gb} GB
                    {t.over_limit ? ' 超限' : ''}
                  </span>
                )
              },
            },
            {
              title: '超限关机',
              width: 90,
              render: (_, row) => row.auto_stop_conflict ? <Tag color="red">冲突，已暂停</Tag>
                : row.auto_stop_identity_changed ? <Tag color="gold">账号变化，已暂停</Tag>
                  : row.auto_stop_on_limit ? <Tag color="orange">开</Tag> : <Tag>关</Tag>,
            },
            {
              title: '操作',
              width: 340,
              fixed: 'right',
              render: (_, row) => {
                const busy = (action: string) =>
                  acting === `${row.credential_id}/${row.region}/${row.name}:${action}`
                return (
                  <Space wrap size="small">
                    <Button
                      size="small"
                      disabled={row.state === 'running'}
                      loading={busy('start')}
                      onClick={() => void runAction(row, 'start')}
                    >
                      开机
                    </Button>
                    <Button
                      size="small"
                      disabled={row.state === 'stopped'}
                      loading={busy('stop')}
                      onClick={() => void runAction(row, 'stop')}
                    >
                      关机
                    </Button>
                    <Popconfirm
                      title="确认一键更换静态 IP？"
                      description="将分配新静态 IP，并释放旧静态 IP。"
                      onConfirm={() => void runAction(row, 'change-ip')}
                    >
                      <Button size="small" loading={busy('change-ip')}>
                        换 IP
                      </Button>
                    </Popconfirm>
                    <Button
                      size="small"
                      onClick={() => {
                        setLimitModal(row)
                        editedLimitFields.current.clear()
                        limitForm.resetFields()
                        limitForm.setFieldsValue({
                          monthly_limit_gb: row.monthly_limit_gb,
                          auto_stop_on_limit: row.configured_auto_stop_on_limit ?? !!row.auto_stop_on_limit,
                          note: row.note,
                        })
                      }}
                    >
                      限额
                    </Button>
                    <Popconfirm
                      title="确认删除实例？"
                      description="将删除实例并释放关联静态 IP，不可恢复。"
                      onConfirm={() => void runAction(row, 'delete')}
                    >
                      <Button size="small" danger loading={busy('delete')}>
                        删除
                      </Button>
                    </Popconfirm>
                  </Space>
                )
              },
            },
          ]}
        />
      </Card>

      <Modal
        title={limitModal ? `限额与超限策略 - ${limitModal.name}` : '设置'}
        open={!!limitModal}
        onCancel={() => setLimitModal(null)}
        onOk={async () => {
          if (!limitModal) return
          const values = await limitForm.validateFields()
          try {
            await api.patch(
              `/instances/${limitModal.region}/${limitModal.name}/settings`,
              Object.fromEntries(Object.entries(values).filter(([key]) => editedLimitFields.current.has(key))),
              { params: { credential_id: limitModal.credential_id || undefined } },
            )
            invalidateQueryScope('instances', { credential_id: limitModal.credential_id, region: limitModal.region })
            invalidateQueryScope('traffic', { credential_id: limitModal.credential_id, region: limitModal.region })
            message.success('已保存')
            setLimitModal(null)
            void latestLoad.current()
          } catch (err) {
            message.error(getErrorMessage(err))
          }
        }}
      >
        {limitModal?.auto_stop_identity_changed ? <Alert type="warning" showIcon
          message="显示的是原账号保存配置，尚未对当前账号生效；请确认 AWS 账号后明确设置策略。"
          style={{ marginBottom: 16 }} /> : null}
        {limitModal?.auto_stop_conflict ? <Alert type="warning" showIcon
          message="策略冲突已暂停；保存单条设置不会解除暂停。请协调全部竞争凭证策略后明确确认解除。"
          action={<Button size="small" onClick={async () => {
            try {
              const params = { credential_id: limitModal.credential_id || undefined }
              const path = `/instances/${limitModal.region}/${limitModal.name}`
              const res = await api.get<{
                account_id: string | null
                confirmation_token: string
                strategies: { credential_id: number; auto_stop_on_limit: boolean;
                  limit_gb: number | null; identity_confirmed: boolean }[]
              }>(`${path}/policy-conflict`, { params })
              const strategies = res.data.strategies
              if (!strategies.length) {
                message.info('当前没有待确认的冲突策略，请刷新列表')
                return
              }
              Modal.confirm({
                title: `确认 AWS 账号 ${res.data.account_id} 的全部竞争策略`,
                content: <div>{strategies.map((strategy) => <p key={strategy.credential_id}>
                  凭证 #{strategy.credential_id}：{strategy.auto_stop_on_limit ? '开启' : '关闭'}，
                  限额 {strategy.limit_gb ?? '未设置'} GB
                  {strategy.identity_confirmed ? '' : '（身份未确认，禁止解除）'}
                </p>)}<p>仅在以上策略全部协调一致后解除暂停；未保存的表单内容不参与确认。</p></div>,
                okText: '确认全部策略并解除暂停',
                onOk: async () => {
                  try {
                    await api.patch(`${path}/settings`, {
                      resolve_conflict_credential_ids: strategies.map((strategy) => strategy.credential_id),
                      resolve_conflict_token: res.data.confirmation_token,
                    }, { params })
                    invalidateQueryScope('instances', { credential_id: limitModal.credential_id, region: limitModal.region })
                    invalidateQueryScope('traffic', { credential_id: limitModal.credential_id, region: limitModal.region })
                    message.success('全部策略已确认，冲突暂停已解除')
                    setLimitModal(null)
                    void latestLoad.current()
                  } catch (err) {
                    message.error(getErrorMessage(err))
                    throw err
                  }
                },
              })
            } catch (err) {
              message.error(getErrorMessage(err))
            }
          }}>查看并确认</Button>}
          style={{ marginBottom: 16 }} /> : null}
        <Form form={limitForm} layout="vertical" onValuesChange={(changed) => {
          Object.keys(changed).forEach((key) => editedLimitFields.current.add(key))
        }}>
          <Form.Item
            name="monthly_limit_gb"
            label="月流量限额（GB）"
            tooltip="基于 NetworkIn+Out 估算。清空表示不单独限制。"
          >
            <InputNumber min={0} step={1} style={{ width: '100%' }} placeholder="例如 1024" />
          </Form.Item>
          <Form.Item
            name="auto_stop_on_limit"
            label="超限后自动关机"
            valuePropName="checked"
            extra="默认关闭：仅告警。勾选后，采集任务发现超限且实例运行中时会自动 stop。"
          >
            <Switch checkedChildren="开启" unCheckedChildren="仅告警" />
          </Form.Item>
          <Form.Item name="note" label="备注">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  )
}
