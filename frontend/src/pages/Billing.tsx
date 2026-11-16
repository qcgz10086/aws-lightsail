import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Progress,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import dayjs, { type Dayjs } from 'dayjs'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import api, {
  AwsCredit,
  BillingSummary,
  CreditCreateInput,
  CreditRecordOut,
  CredentialItem,
  CredentialOut,
  getErrorMessage,
} from '../api/client'
import { useAuth } from '../auth/AuthContext'

const fmtMoney = (v: number | null | undefined) =>
  v == null ? '-' : `$${Number(v).toFixed(2)}`

function daysToExpiry(expiresAt?: string | null): number | null {
  if (!expiresAt) return null
  return dayjs(expiresAt).startOf('day').diff(dayjs().startOf('day'), 'day')
}

export default function BillingPage() {
  const { user } = useAuth()
  const [summary, setSummary] = useState<BillingSummary | null>(null)
  const [creds, setCreds] = useState<CredentialItem[]>([])
  const [filterCred, setFilterCred] = useState<number | 'all'>('all')
  const [loading, setLoading] = useState(false)
  const [form] = Form.useForm()

  const load = async () => {
    setLoading(true)
    try {
      const params = filterCred === 'all' ? {} : { credential_id: filterCred }
      const res = await api.get<BillingSummary>('/billing/summary', { params })
      setSummary(res.data)
    } catch (err) {
      message.error(getErrorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (user?.has_credentials) {
      void (async () => {
        try {
          const res = await api.get<CredentialOut>('/credentials')
          setCreds(res.data.items || [])
        } catch {
          /* ignore */
        }
      })()
      void load()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.has_credentials, filterCred])

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

  const credits = summary?.credits || []
  const awsCredits = summary?.aws_credits || []
  const isOk = summary?.status === 'ok'
  const awsRemainingTotal =
    summary?.aws_credits_status === 'ok'
      ? awsCredits.reduce((s, c) => s + (c.remaining_amount ?? 0), 0)
      : null

  const onAddCredit = async (values: {
    name: string
    total_amount: number
    start_date: Dayjs
    expires_at?: Dayjs | null
    credit_id?: string
    note?: string
    credential_id?: number | null
  }) => {
    const body: CreditCreateInput = {
      name: values.name,
      total_amount: values.total_amount,
      start_date: values.start_date.format('YYYY-MM-DD'),
      expires_at: values.expires_at ? values.expires_at.format('YYYY-MM-DD') : null,
      credit_id: values.credit_id || null,
      note: values.note || null,
      credential_id: values.credential_id ?? null,
    }
    try {
      await api.post('/billing/credits', body)
      message.success('赠金记录已添加')
      form.resetFields()
      await load()
    } catch (err) {
      message.error(getErrorMessage(err))
    }
  }

  const onDeleteCredit = async (id: number) => {
    try {
      await api.delete(`/billing/credits/${id}`)
      message.success('已删除')
      await load()
    } catch (err) {
      message.error(getErrorMessage(err))
    }
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
        <Typography.Title level={3} style={{ margin: 0 }}>
          账单与赠金
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
          <Button type="primary" onClick={() => void load()} loading={loading}>
            刷新
          </Button>
        </Space>
      </div>

      <Alert
        type="info"
        showIcon
        message="官方赠金来自 AWS Billing GetCredits（真实余额）"
        description="上方“AWS 赠金（官方）”是 Billing API 直接返回的 remainingAmount（截至上个账单周期的余额）和 estimatedAmount（估算剩余，含未出账单），不是推算的。下方手动记录的剩余仍是估算（总额 − 自开始日起 Cost Explorer 累计已抵扣）。费用本身按 UTC 自然月统计。"
      />

      {summary?.status === 'unavailable' && (
        <Alert
          type="warning"
          showIcon
          message="Cost Explorer 未启用或暂无数据"
          description={
            <>
              <div>{summary.message || 'Cost Explorer 未启用或暂无数据。'}</div>
              <div style={{ marginTop: 8 }}>
                开启路径：AWS 控制台 → Cost Explorer → 启用 Cost Explorer；启用后数据通常需要 24-48
                小时才会出现，之后再刷新本页。费用查询约 $0.01/次。
              </div>
            </>
          }
        />
      )}

      {summary?.status === 'no_permission' && (
        <Alert
          type="error"
          showIcon
          message="没有 Cost Explorer 查询权限"
          description={
            <>
              <div>{summary.message || '当前凭证没有 Cost Explorer 权限。'}</div>
              <div style={{ marginTop: 8 }}>
                所需 IAM 权限：{(summary.required_permissions || ['ce:GetCostAndUsage', 'ce:GetCostForecast']).join('、')}
                。请给该 IAM 用户/角色附加仅 Cost Explorer 只读的策略，不要用根账号 Access Key。
              </div>
            </>
          }
        />
      )}

      {summary?.aws_credits_status === 'no_permission' && (
        <Alert
          type="error"
          showIcon
          message="没有查询官方赠金的权限"
          description={
            <>
              <div>{summary.aws_credits_error || '当前凭证没有 Billing 赠金查询权限。'}</div>
              <div style={{ marginTop: 8 }}>
                所需 IAM 权限：billing:GetCredits、sts:GetCallerIdentity。请给该 IAM
                用户/角色附加只读策略，不要用根账号 Access Key。费用汇总不受影响。
              </div>
            </>
          }
        />
      )}

      {(summary?.aws_credits_status === 'unavailable' || summary?.aws_credits_status === 'invalid') && (
        <Alert
          type="warning"
          showIcon
          message="官方赠金暂不可用"
          description={
            <>
              <div>{summary.aws_credits_error || '暂时查不到官方赠金。'}</div>
              <div style={{ marginTop: 8 }}>
                GetCredits 只返回过去 1 年内的赠金；若账号确实有赠金但查不到，请确认凭证的
                billing:GetCredits 权限，或稍后再刷新。费用汇总与手动记录不受影响。
              </div>
            </>
          }
        />
      )}

      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} lg={8}>
          <Card>
            <Statistic title={`本月总费用（${summary?.year_month || '-'}）`} value={fmtMoney(summary?.total_cost)} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={8}>
          <Card>
            <Statistic title="抵扣后净费用" value={fmtMoney(summary?.net_cost)} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={8}>
          <Card>
            <Statistic title="本月已抵扣赠金" value={fmtMoney(summary?.credits_applied)} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={8}>
          <Card>
            <Statistic title="Lightsail 本月费用" value={fmtMoney(summary?.lightsail_cost)} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={8}>
          <Card>
            <Statistic title="本月费用预测" value={fmtMoney(summary?.forecast_cost)} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={8}>
          <Card>
            <Statistic title="官方赠金剩余（合计）" value={fmtMoney(awsRemainingTotal)} />
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={8}>
          <Card>
            <Statistic title="赠金记录（手动）" value={credits.length} suffix="笔" />
          </Card>
        </Col>
      </Row>

      <Card
        title="AWS 赠金（官方）"
        extra={
          summary?.aws_account_id ? (
            <Typography.Text type="secondary">账号 {summary.aws_account_id}</Typography.Text>
          ) : null
        }
      >
        <Table
          rowKey={(r: AwsCredit) => r.credit_id || r.description || String(Math.random())}
          loading={loading}
          dataSource={awsCredits}
          pagination={false}
          locale={{
            emptyText:
              summary?.aws_credits_status === 'ok'
                ? '该账号在过去 1 年内没有 GetCredits 返回的赠金记录'
                : '官方赠金不可用，见上方提示',
          }}
          columns={[
            {
              title: '赠金',
              render: (_, r: AwsCredit) => (
                <div>
                  <div>{r.description || r.credit_type || '-'}</div>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {r.credit_id || ''}
                    {r.credit_type ? ` · ${r.credit_type}` : ''}
                  </Typography.Text>
                </div>
              ),
            },
            {
              title: '状态',
              dataIndex: 'status',
              width: 100,
              render: (v: string | null) =>
                v ? <Tag color={v === 'Active' ? 'green' : 'default'}>{v}</Tag> : '-',
            },
            {
              title: '初始金额',
              dataIndex: 'initial_amount',
              render: (v: number | null) => fmtMoney(v),
            },
            {
              title: '剩余金额',
              dataIndex: 'remaining_amount',
              render: (v: number | null) => (v == null ? '-' : fmtMoney(v)),
            },
            {
              title: '估算剩余',
              dataIndex: 'estimated_amount',
              render: (v: number | null) => (v == null ? '-' : fmtMoney(v)),
            },
            {
              title: '到期日',
              dataIndex: 'end_date',
              width: 150,
              render: (v: string | null, r: AwsCredit) => {
                const end = v || r.exhaust_date
                if (!end) return '-'
                const days = daysToExpiry(end)
                if (days == null) return end
                if (days < 0) return <Tag color="red">{end} 已过期</Tag>
                if (days <= 30) return <Tag color="red">{end}（剩 {days} 天）</Tag>
                if (days <= 90) return <Tag color="orange">{end}（剩 {days} 天）</Tag>
                return `${end}（剩 ${days} 天）`
              },
            },
            {
              title: '适用产品',
              dataIndex: 'applicable_products',
              render: (v: string[] | null) =>
                v && v.length > 0 ? (
                  <Typography.Text style={{ fontSize: 12 }}>{v.slice(0, 3).join('、')}{v.length > 3 ? ` 等 ${v.length} 项` : ''}</Typography.Text>
                ) : (
                  '-'
                ),
            },
          ]}
        />
        <Typography.Text type="secondary" style={{ display: 'block', marginTop: 12 }}>
          remainingAmount 为截至上个账单周期的官方余额，estimatedAmount 为含未出账单的估算剩余；
          GetCredits 查询范围只覆盖过去 1 年内的赠金。数据来自 AWS Billing API，不是本应用推算的。
        </Typography.Text>
      </Card>

      <Card title="按服务费用（本月）">
        <Table
          rowKey="service"
          loading={loading}
          dataSource={summary?.by_service || []}
          pagination={false}
          locale={{ emptyText: isOk ? '本月暂无费用数据' : 'Cost Explorer 不可用，无法展示费用明细' }}
          columns={[
            { title: '服务', dataIndex: 'service' },
            { title: '总费用', dataIndex: 'total', render: (v: number) => fmtMoney(v) },
            { title: '净费用', dataIndex: 'net', render: (v: number) => fmtMoney(v) },
            { title: '已抵扣', dataIndex: 'credits_applied', render: (v: number) => fmtMoney(v) },
          ]}
        />
      </Card>

      <Card title="赠金记录（手动）">
        <Table
          rowKey="id"
          loading={loading}
          dataSource={credits}
          pagination={false}
          locale={{ emptyText: '还没有赠金记录，在下方表单添加' }}
          columns={[
            { title: '名称', dataIndex: 'name' },
            {
              title: '总额',
              dataIndex: 'total_amount',
              render: (v: number, r: CreditRecordOut) => fmtMoney(v) + ` ${r.currency || 'USD'}`,
            },
            {
              title: '自开始日起已抵扣',
              dataIndex: 'credits_applied_since_start',
              render: (v: number | null) => (v == null ? '—（CE 不可用）' : fmtMoney(v)),
            },
            {
              title: '剩余估算',
              width: 220,
              render: (_, r: CreditRecordOut) => {
                if (r.remaining_estimate == null) return '—（CE 不可用）'
                const pct = r.total_amount > 0 ? Math.max(0, Math.min(100, (r.remaining_estimate / r.total_amount) * 100)) : 0
                return (
                  <div>
                    <div>{fmtMoney(r.remaining_estimate)}</div>
                    <Progress percent={Math.round(pct)} size="small" status={pct <= 10 ? 'exception' : 'active'} />
                  </div>
                )
              },
            },
            { title: '开始日期', dataIndex: 'start_date', width: 110 },
            {
              title: '到期日',
              dataIndex: 'expires_at',
              width: 150,
              render: (v: string | null) => {
                if (!v) return '-'
                const days = daysToExpiry(v)
                if (days == null) return v
                if (days < 0) return <Tag color="red">{v} 已过期</Tag>
                if (days <= 30) return <Tag color="red">{v}（剩 {days} 天）</Tag>
                if (days <= 90) return <Tag color="orange">{v}（剩 {days} 天）</Tag>
                return `${v}（剩 ${days} 天）`
              },
            },
            { title: 'Credit ID', dataIndex: 'credit_id', render: (v: string | null) => v || '-' },
            {
              title: '操作',
              width: 90,
              render: (_, r: CreditRecordOut) => (
                <Popconfirm title="确定删除这条赠金记录？" onConfirm={() => void onDeleteCredit(r.id)}>
                  <Button type="link" danger>删除</Button>
                </Popconfirm>
              ),
            },
          ]}
        />
      </Card>

      <Card title="添加赠金记录">
        <Form form={form} layout="vertical" onFinish={onAddCredit}>
          <Row gutter={16}>
            <Col xs={24} sm={12}>
              <Form.Item name="name" label="名称" rules={[{ required: true, message: '请填写名称' }]}>
                <Input placeholder="例如：新账号注册赠金" />
              </Form.Item>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item name="total_amount" label="总额（USD）" rules={[{ required: true, message: '请填写总额' }]}>
                <InputNumber min={0.01} step={1} precision={2} style={{ width: '100%' }} placeholder="例如 100" />
              </Form.Item>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item name="start_date" label="开始日期" rules={[{ required: true, message: '请选择开始日期' }]}>
                <DatePicker style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item name="expires_at" label="到期日（可选）">
                <DatePicker style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item name="credit_id" label="Credit ID（可选）">
                <Input placeholder="Credits 页上的 Credit ID（可选）" />
              </Form.Item>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item name="credential_id" label="关联凭证（可选）">
                <Select
                  allowClear
                  placeholder="不选则为全部凭证"
                  options={creds.map((c) => ({ value: c.id, label: `${c.account_label || '凭证'} #${c.id}` }))}
                />
              </Form.Item>
            </Col>
            <Col span={24}>
              <Form.Item name="note" label="备注（可选）">
                <Input placeholder="备注（可选）" />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item>
            <Button type="primary" htmlType="submit">
              添加赠金记录
            </Button>
          </Form.Item>
        </Form>
        <Typography.Text type="secondary">
          这是手动补充记录，不会自动插入：请按官方 Credits 页手动录入。剩余是估算，需要 Cost
          Explorer 可用；CE 未启用或无权限时剩余显示为“—”。官方赠金请以上方 Billing
          GetCredits 返回为准。
        </Typography.Text>
      </Card>
    </Space>
  )
}
