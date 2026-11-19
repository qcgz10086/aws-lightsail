import { Alert, Button, Card, Descriptions, Modal, Select, Space, Table, Tag, Typography, message } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import api, { getErrorMessage } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { invalidateTaskData, OperationTask, pendingIntents, recoverIntent, taskLabels } from '../api/tasks'

export default function TasksPage() {
  const { user, sessionKey } = useAuth()
  const { taskId } = useParams()
  const [tasks, setTasks] = useState<OperationTask[]>([])
  const [error, setError] = useState('')
  const [filter, setFilter] = useState<number | 'all'>('all')
  const [loading, setLoading] = useState(false)
  const [revision, setRevision] = useState(0)
  const [offset, setOffset] = useState(0)
  const generation = useRef(0)
  useEffect(() => {
    const current = ++generation.current
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    setTasks([])
    setError('')
    const load = async () => {
      setLoading(true)
      try {
        const { data } = await api.get<OperationTask | OperationTask[]>(taskId ? `/v2/tasks/${taskId}` : '/v2/tasks',
          { signal: controller.signal, params: { limit: 100, offset: taskId ? 0 : offset } })
        if (current !== generation.current) return
        const rows = Array.isArray(data) ? data : [data]
        setTasks(rows)
        rows.forEach(invalidateTaskData)
        setError('')
        if (rows.some((row) => ['queued', 'running'].includes(row.status))) timer = setTimeout(load, 1500)
      } catch (err) {
        if (current === generation.current && !controller.signal.aborted) setError(getErrorMessage(err))
      } finally { if (current === generation.current) setLoading(false) }
    }
    void load()
    return () => { generation.current++; controller.abort(); clearTimeout(timer) }
  }, [user?.id, sessionKey, taskId, revision, offset])
  const action = async (task: OperationTask, verb: string) => {
    const current = generation.current
    try {
      await api.post(`/v2/tasks/${task.task_id}/${verb}`, verb === 'resolve' ? { acknowledge_task_id: task.task_id } : undefined)
      if (current === generation.current) setRevision((v) => v + 1)
    } catch (err) { if (current === generation.current) message.error(getErrorMessage(err)) }
  }
  const detail = taskId ? tasks[0] : null
  return <Space direction="vertical" size="large" style={{ width: '100%' }}>
    <Space wrap><Typography.Title level={3} style={{ margin: 0 }}>任务中心</Typography.Title>
      <Button onClick={() => setRevision((v) => v + 1)} loading={loading}>刷新任务</Button>
      {taskId && <Link to="/tasks">全部任务</Link>}
      {!taskId && <Select value={filter} onChange={setFilter} style={{ width: 180 }} options={[
        { value: 'all', label: '全部凭证' }, ...[...new Set(tasks.map((row) => row.credential_id))]
          .map((id) => ({ value: id, label: `凭证 #${id}` })),
      ]} />}</Space>
    <Alert type="info" showIcon message="任务已接受不代表 AWS 操作已完成。刷新页面后可继续查看进度。"
      description="待核对任务保留资源锁。先执行只读核对，确认结果后再继续剩余步骤；不会重放未知写请求。" />
    {error && <Alert type="error" showIcon message={error} />}
    {user && pendingIntents(user.id).map((intent) => <Alert key={intent.key} type="warning"
      message={`提交结果待确认 · ${intent.created_at}`} description={<Button onClick={async () => {
        const current = generation.current
        try {
          await recoverIntent(user.id, intent)
          if (current === generation.current) setRevision((v) => v + 1)
        } catch (err) { if (current === generation.current) message.error(getErrorMessage(err, '尚未找到任务，请保留原提交标识')) }
      }}>按原提交标识找回</Button>} />)}
    <Table rowKey="task_id" dataSource={tasks.filter((row) => taskId || filter === 'all' || row.credential_id === filter)}
      pagination={taskId ? false : { pageSize: 10 }} scroll={{ x: 900 }} columns={[
        { title: '任务', render: (_, row: OperationTask) => <Link to={`/tasks/${row.task_id}`}>{row.kind} · {row.resource_name || '账号流量'}</Link> },
        { title: '凭证 / 区域', render: (_, row: OperationTask) => `#${row.credential_id} / ${row.region || '全部区域'}` },
        { title: '状态', render: (_, row: OperationTask) => <Tag color={row.status === 'succeeded' ? 'green' : row.status === 'reconciliation_required' ? 'orange' : 'blue'}>{taskLabels[row.status]}</Tag> },
        { title: '当前步骤', dataIndex: 'current_step' },
        { title: '操作', render: (_, row: OperationTask) => <Space>
          {row.can_cancel && <Button onClick={() => void action(row, 'cancel')}>取消</Button>}
          {row.status === 'reconciliation_required' && <Button onClick={() => void action(row, 'reconcile')}>只读核对</Button>}
          {row.can_resume && <Button onClick={() => void action(row, 'retry')}>继续剩余步骤</Button>}
          {row.status === 'reconciliation_required' && <Button danger onClick={() => Modal.confirm({
            title: '确认自行处理未决结果并解除资源锁',
            content: `任务 ${row.task_id} 的 AWS 结果尚未确认。解除后系统不会重放或撤销操作，原 AWS 请求仍可能生效。请先核实资源与费用；仅在决定自行处理后确认。`,
            okText: '已核实并决定自行处理', cancelText: '保留锁', onOk: () => action(row, 'resolve'),
          })}>人工决议</Button>}
        </Space> },
      ]} />
    {!taskId && <Space><Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>上一批历史</Button>
      <Typography.Text>第 {offset + 1}–{offset + tasks.length} 条，未完成任务优先</Typography.Text>
      <Button disabled={tasks.length < 100} onClick={() => setOffset(offset + 100)}>下一批历史</Button>
      {offset > 0 && <Button onClick={() => setOffset(0)}>返回未完成任务</Button>}</Space>}
    {detail && <Card title="任务详情">
      <Descriptions column={1} items={[
        { key: 'id', label: '任务 ID', children: detail.task_id },
        { key: 'account', label: 'AWS 账号', children: detail.account_id || '身份未知' },
        { key: 'error', label: '说明', children: detail.error || '-' },
        { key: 'result', label: '结果', children: <pre style={{ whiteSpace: 'pre-wrap' }}>{JSON.stringify(detail.result, null, 2)}</pre> },
      ]} />
      <Table rowKey="sequence" dataSource={detail.steps} pagination={false} columns={[
        { title: '步骤', dataIndex: 'name' }, { title: '状态', dataIndex: 'status' },
        { title: 'Operation ID', dataIndex: 'operation_id' }, { title: '说明', dataIndex: 'error' },
      ]} />
    </Card>}
  </Space>
}
