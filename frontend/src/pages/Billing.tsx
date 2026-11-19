import { Alert, Button, Card, DatePicker, Form, Input, InputNumber, Modal, Popconfirm, Select,
  Space, Table, Tag, Typography, message } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { type Dayjs } from 'dayjs'
import api, { BillingV2Summary, BillingCurrency, CredentialItem, CredentialOut,
  ExactManualCredit, getErrorMessage } from '../api/client'
import { useAuth } from '../auth/AuthContext'

const money = (value: string | null | undefined, currency: string) =>
  value == null ? '—（不可用或缺失）' : `${currency} ${value}`
const statusTag = (status: string) => <Tag color={status === 'ok' ? 'green' : 'gold'}>{status}</Tag>

export default function BillingPage() {
  const { user } = useAuth()
  const [summary, setSummary] = useState<BillingV2Summary | null>(null)
  const [creds, setCreds] = useState<CredentialItem[]>([])
  const [filterCred, setFilterCred] = useState<number | 'all'>('all')
  const [loading, setLoading] = useState(false)
  const [form] = Form.useForm()
  const requestVersion = useRef(0)
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pollStarted = useRef(0)
  const load = async () => {
    const version = ++requestVersion.current
    setLoading(true)
    try {
      const params = filterCred === 'all' ? {} : { credential_id: filterCred }
      const res = await api.get<BillingV2Summary>('/v2/billing/summary', { params })
      if (version === requestVersion.current) {
        setSummary(res.data)
        if (res.data.refreshing && Date.now() - pollStarted.current < 30000) {
          pollTimer.current = setTimeout(() => void load(), 500)
        }
      }
    } catch (error) {
      if (version === requestVersion.current) message.error(getErrorMessage(error))
    } finally {
      if (version === requestVersion.current) setLoading(false)
    }
  }
  useEffect(() => {
    if (user?.has_credentials) {
      setSummary(null)
      pollStarted.current = Date.now()
      void load()
      void api.get<CredentialOut>('/credentials').then((res) => setCreds(res.data.items || []))
        .catch((error) => message.error(getErrorMessage(error)))
    }
    return () => {
      requestVersion.current += 1
      if (pollTimer.current) clearTimeout(pollTimer.current)
    }
  }, [user?.has_credentials, filterCred])

  if (!user?.has_credentials) return <Alert type="info" showIcon message="请先绑定 AWS 凭证"
    description={<Link to="/credentials">前往绑定</Link>} />

  const remove = async (record: ExactManualCredit) => {
    try {
      await api.delete(`/billing/credits/${record.id}`)
      message.success('已删除')
      void load()
    } catch (error) { message.error(getErrorMessage(error)) }
  }
  const manualColumns = [
    { title: '名称', dataIndex: 'name' },
    { title: '账号归属', render: (_: unknown, row: ExactManualCredit) => row.account_id || '待归属' },
    { title: '录入总额', render: (_: unknown, row: ExactManualCredit) => <span>
      {money(row.total_amount, row.currency)}{row.amount_precision === 'legacy_float'
        ? <Tag color="gold" title={row.precision_note}>历史浮点近似</Tag> : <Tag>十进制记录</Tag>}</span> },
    { title: 'Credit ID', dataIndex: 'credit_id' },
    { title: '匹配 / 剩余', render: (_: unknown, row: ExactManualCredit) => row.status === 'matched_official'
      ? <span>已匹配官方（不重复计入）：{money(row.official_remaining, row.currency)}</span>
      : row.status === 'historical_account' ? '历史账号记录（未迁移，未查询）' : '无可靠逐笔匹配，不计算剩余' },
    { title: '开始 / 到期', render: (_: unknown, row: ExactManualCredit) => `${row.start_date} / ${row.expires_at || '—'}` },
    { title: '操作', render: (_: unknown, row: ExactManualCredit) => <Popconfirm title="删除这条手动记录？"
      onConfirm={() => remove(row)}><Button type="link" danger>删除</Button></Popconfirm> },
  ]
  const currencyColumns = [
    { title: '币种', dataIndex: 'currency' },
    ...([['total_cost', '费用（Credit/退款/折扣前）'], ['credits_applied', 'Credit 净抵扣'],
      ['refunds', '退款净额'], ['discounts', '折扣净额'], ['net_cost', '账单行净额'],
      ['lightsail_cost', 'Lightsail 费用']] as const).map(([key, title]) => ({ title,
      render: (_: unknown, row: BillingCurrency) => money(row[key], row.currency) })),
  ]
  return <Space direction="vertical" size="large" style={{ width: '100%' }}>
    <Space wrap style={{ justifyContent: 'space-between', width: '100%' }}>
      <Typography.Title level={3} style={{ margin: 0 }}>账单与赠金</Typography.Title>
      <Space wrap><Select value={filterCred} onChange={setFilterCred} style={{ minWidth: 220 }} options={[
        { value: 'all', label: '全部确认账号（重复 Key 合并）' }, ...creds.map((cred) => ({ value: cred.id,
          label: `${cred.account_label || '凭证'} #${cred.id} · ${cred.account_id || '身份未知'}` }))]} />
        <Button loading={loading} type="primary" onClick={async () => {
          try {
            if (pollTimer.current) clearTimeout(pollTimer.current)
            pollStarted.current = Date.now()
            await api.post('/v2/refresh', { kind: 'billing',
              credential_id: filterCred === 'all' ? null : filterCred })
            void load()
          } catch (error) { message.error(getErrorMessage(error)) }
        }}>刷新</Button></Space>
    </Space>
    <Alert type="info" showIcon message="金额按币种分别汇总；不做汇率换算"
      description="费用按 UnblendedCost 账单行和记录类型统计。Credit 冲正、退款和折扣分别显示；缺失不显示为零。官方余额和官方估算余额分开；手动记录不叠加官方余额，也不从账号总抵扣推算每笔剩余。" />
    {summary && summary.status !== 'ok' ? <Alert type="warning" showIcon message="存在未知身份或部分查询失败"
      description="下列汇总只包含成功返回的数据，不代表全部账号总额。请查看各账号费用、预测和赠金的独立状态。" /> : null}
    {summary?.stale ? <Alert type="warning" showIcon message="正在显示最近成功快照，部分数据已过期"
      description="保留的旧值不代表本次刷新成功；各账号分区显示最近成功时间和错误。" /> : null}
    {summary?.refreshing ? <Alert type="info" showIcon message="后台刷新中，已完成的数据先行显示"
      description="本页最多自动跟进30秒；仍未完成时可稍后点击刷新。" /> : null}
    <Card title={`费用汇总 · ${summary?.year_month || '—'}（成功数据小计）`}>
      <Table rowKey="currency" loading={loading} dataSource={summary?.currencies || []}
        columns={currencyColumns} pagination={false} scroll={{ x: 1200 }} />
    </Card>
    <Card title="官方余额和预测（按币种，不与手动记录相加）">
      <Table rowKey="currency" loading={loading} dataSource={summary?.currencies || []} pagination={false}
        columns={[{ title: '币种', dataIndex: 'currency' },
          { title: '官方赠金余额', render: (_, row) => money(row.official_remaining, row.currency) },
          { title: '官方估算余额', render: (_, row) => money(row.official_estimated, row.currency) },
          { title: '本月剩余期间预测', render: (_, row) => money(row.forecast_remaining, row.currency) },
          { title: '完整月预测（已发生净额＋剩余预测）', render: (_, row) => money(row.forecast_month_total, row.currency) },
          { title: '完整性', render: (_, row) => row.costs_complete && row.forecast_complete && row.credits_complete
            ? '全部账号成功' : '部分数据，查看账号状态' }]} />
    </Card>
    {summary?.accounts.map((account) => <Card key={account.credential_id}
      title={`${account.account_label || '凭证'} #${account.credential_id} · ${account.account_id || '身份未知'}`}>
      <Space direction="vertical" style={{ width: '100%' }}>
        {(['costs', 'forecast', 'credits'] as const).map((key) => <div key={key}>
          {{ costs: '费用', forecast: '本月剩余期间预测', credits: '官方赠金' }[key]}：
          {statusTag(account[key].status)}{account[key].error || ''}
          {account[key].cache ? <Typography.Text type="secondary">
            {account[key].cache?.stale ? ' · 已过期' : ''}
            {account[key].cache?.refreshing ? ' · 刷新中' : ''}
            {` · 最近成功：${account[key].cache?.last_success_at || '无'}`}
            {account[key].cache?.error ? ` · ${account[key].cache?.error}` : ''}
          </Typography.Text> : null}</div>)}
        <Table rowKey={(row) => `${row.currency}/${row.service}`} dataSource={account.costs.by_service || []}
          pagination={false} scroll={{ x: 1000 }} columns={[{ title: '服务', dataIndex: 'service' }, ...currencyColumns]} />
        <Table rowKey={(row, index) => `${account.account_id}/${row.credit_id || index}`}
          dataSource={account.credits.credits || []} pagination={false} scroll={{ x: 800 }} columns={[
            { title: '官方 Credit ID', dataIndex: 'credit_id' }, { title: '说明', dataIndex: 'description' },
            { title: '状态', dataIndex: 'status' },
            { title: '初始金额', render: (_, row) => money(row.initial_amount, row.currency) },
            { title: '官方余额', render: (_, row) => money(row.remaining_amount, row.currency) },
            { title: '官方估算余额', render: (_, row) => money(row.estimated_amount, row.currency) },
            { title: '到期日', dataIndex: 'end_date' }]} />
      </Space>
    </Card>)}
    <Card title="手动记录（不加入官方余额）"><Table rowKey="id" loading={loading}
      dataSource={summary?.manual_credits || []} columns={manualColumns} scroll={{ x: 1000 }} /></Card>
    <Card title="历史待归属记录（不能凭当前 Key 猜测账号）"><Table rowKey="id"
      dataSource={summary?.pending_assignment || []} scroll={{ x: 1100 }} columns={[...manualColumns,
        { title: '明确归属', render: (_, row) => <Select placeholder="确认所属账号" style={{ minWidth: 230 }}
          options={creds.filter((cred) => cred.account_id).map((cred) => ({ value: cred.id,
            label: `${cred.account_label || '凭证'} #${cred.id} · ${cred.account_id}` }))}
          onChange={(credential_id) => {
            const credential = creds.find((cred) => cred.id === credential_id)
            Modal.confirm({ title: `确认记录属于 ${credential?.account_id}？`,
              content: '这会给该历史记录保存明确的账号归属，不会推算其剩余金额。',
              onOk: async () => {
                try { await api.patch(`/v2/billing/credits/${row.id}/assign`, { credential_id }); void load() }
                catch (error) { message.error(getErrorMessage(error)); throw error }
              } })
          }} /> }]} /></Card>
    <Card title="添加手动赠金记录"><Form form={form} layout="vertical" onFinish={async (values: {
      name: string; total_amount: string; currency: string; start_date: Dayjs; expires_at?: Dayjs;
      credit_id?: string; credential_id?: number; note?: string
    }) => {
      try {
        await api.post('/v2/billing/credits', { ...values, start_date: values.start_date.format('YYYY-MM-DD'),
          expires_at: values.expires_at?.format('YYYY-MM-DD') || null })
        message.success('记录已添加'); form.resetFields(); void load()
      } catch (error) { message.error(getErrorMessage(error)) }
    }} initialValues={{ currency: 'USD' }}>
      <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input /></Form.Item>
      <Form.Item name="total_amount" label="总额（精确十进制）" rules={[{ required: true }]}>
        <InputNumber stringMode min="0.000000000000000001" style={{ width: '100%' }} /></Form.Item>
      <Form.Item name="currency" label="币种" rules={[{ required: true, pattern: /^[A-Za-z]{3,8}$/ }]}><Input /></Form.Item>
      <Form.Item name="credential_id" label="所属账号（不选进入待归属）"><Select allowClear
        options={creds.filter((cred) => cred.account_id).map((cred) => ({ value: cred.id,
          label: `${cred.account_label || '凭证'} #${cred.id} · ${cred.account_id}` }))} /></Form.Item>
      <Form.Item name="start_date" label="开始日期" rules={[{ required: true }]}><DatePicker /></Form.Item>
      <Form.Item name="expires_at" label="到期日"><DatePicker /></Form.Item>
      <Form.Item name="credit_id" label="官方 Credit ID（可选，用于匹配，避免重复计入）"><Input /></Form.Item>
      <Form.Item name="note" label="备注"><Input /></Form.Item>
      <Button type="primary" htmlType="submit">添加记录</Button>
    </Form></Card>
  </Space>
}
