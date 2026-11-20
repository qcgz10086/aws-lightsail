import { SaveOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Descriptions, Form, InputNumber, Space, Spin, Typography, message } from 'antd'
import { useEffect, useRef, useState } from 'react'
import api, { getErrorMessage } from '../api/client'
import { useAuth } from '../auth/AuthContext'

interface SettingsView {
  query_interval_ms: number
  traffic_interval_minutes: number
  next_sync_at: string
  updated_at: string
}

interface SettingsFields {
  queryIntervalSeconds: number
  trafficIntervalMinutes: number
}

const MIN_TRAFFIC = 5
const MAX_TRAFFIC = 525_600

function formValues(settings: SettingsView): SettingsFields {
  return {
    queryIntervalSeconds: settings.query_interval_ms / 1000,
    trafficIntervalMinutes: settings.traffic_interval_minutes,
  }
}

function SettingsEditor({ canEdit }: { canEdit: boolean }) {
  const [form] = Form.useForm<SettingsFields>()
  const [settings, setSettings] = useState<SettingsView | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const active = useRef<AbortController | null>(null)

  const load = async () => {
    active.current?.abort()
    const controller = new AbortController()
    active.current = controller
    setLoading(true)
    setError(null)
    try {
      const { data } = await api.get<SettingsView>('/settings', { signal: controller.signal })
      if (controller.signal.aborted) return
      setSettings(data)
      form.setFieldsValue(formValues(data))
    } catch (err) {
      if (!controller.signal.aborted) setError(getErrorMessage(err, '读取设置失败'))
    } finally {
      if (!controller.signal.aborted) setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    return () => active.current?.abort()
  }, [form])

  const save = async (values: SettingsFields) => {
    if (!canEdit || saving) return
    const controller = new AbortController()
    active.current = controller
    setSaving(true)
    setError(null)
    try {
      const { data } = await api.put<SettingsView>('/settings', {
        query_interval_ms: Math.round(values.queryIntervalSeconds * 1000),
        traffic_interval_minutes: values.trafficIntervalMinutes,
      }, { signal: controller.signal })
      if (controller.signal.aborted) return
      setSettings(data)
      form.setFieldsValue(formValues(data))
      message.success(`已保存：${data.query_interval_ms / 1000} 秒，${data.traffic_interval_minutes} 分钟`)
    } catch (err) {
      if (!controller.signal.aborted) setError(getErrorMessage(err, '保存设置失败'))
    } finally {
      if (!controller.signal.aborted) setSaving(false)
    }
  }

  return (
    <Space direction="vertical" size="large" style={{ display: 'flex', maxWidth: 760 }}>
      <Space wrap>
        <Typography.Title level={3} style={{ margin: 0 }}>系统设置</Typography.Title>
        <Button icon={<ReloadOutlined />} disabled={saving || loading} onClick={() => void load()}>重新读取</Button>
      </Space>
      {error && <Alert type="error" showIcon message={error} />}
      {loading && !settings && <Spin />}
      {settings && (
        <>
          <Descriptions title="当前生效" column={1} size="small" bordered>
            <Descriptions.Item label="AWS 读取间隔">{settings.query_interval_ms / 1000} 秒</Descriptions.Item>
            <Descriptions.Item label="自动流量同步周期">{settings.traffic_interval_minutes} 分钟</Descriptions.Item>
            <Descriptions.Item label="下次自动同步">{new Date(settings.next_sync_at).toLocaleString()}</Descriptions.Item>
          </Descriptions>
          {canEdit && (
            <Form form={form} layout="vertical" initialValues={formValues(settings)} onFinish={save} disabled={loading || saving}>
              <Form.Item name="queryIntervalSeconds" label="AWS 读取间隔（秒）"
                rules={[{ required: true }, { type: 'number', min: 0.2, max: 5, message: '请输入 0.2 至 5 秒' }]}>
                <InputNumber min={0.2} max={5} step={0.1} precision={3} style={{ width: '100%', maxWidth: 220 }} />
              </Form.Item>
              <Form.Item name="trafficIntervalMinutes" label="自动流量同步周期（分钟）"
                rules={[{ required: true }, { type: 'integer', min: MIN_TRAFFIC, max: MAX_TRAFFIC, message: '请输入 5 至 525600 的整数' }]}>
                <InputNumber min={MIN_TRAFFIC} max={MAX_TRAFFIC} precision={0} style={{ width: '100%', maxWidth: 220 }} />
              </Form.Item>
              <Button type="primary" htmlType="submit" icon={<SaveOutlined />} loading={saving}>保存设置</Button>
            </Form>
          )}
        </>
      )}
    </Space>
  )
}

export default function SettingsPage() {
  const { user, sessionKey } = useAuth()
  return <SettingsEditor key={`${user?.id}:${sessionKey}`} canEdit={Boolean(user?.is_admin)} />
}
