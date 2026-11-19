import {
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { useEffect, useRef, useState } from 'react'
import api, { User, getErrorMessage, readApi } from '../api/client'
import { useAuth } from '../auth/AuthContext'

// 与后端 app.password_strength 同一标准：12-64 位，含大小写字母、数字、符号。
function validateStrongPassword(pwd: string): string | null {
  if (pwd.length < 12 || pwd.length > 64) return '密码长度需为 12-64 位'
  if (pwd !== pwd.trim()) return '密码不能包含首尾空格'
  if (!/[a-z]/.test(pwd) || !/[A-Z]/.test(pwd) || !/[0-9]/.test(pwd) || !/[^A-Za-z0-9]/.test(pwd)) {
    return '密码须同时包含小写字母、大写字母、数字和符号'
  }
  return null
}

export default function UsersPage() {
  const { sessionKey } = useAuth()
  const [users, setUsers] = useState<User[]>([])
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const requestVersion = useRef(0)
  const controllerRef = useRef<AbortController | null>(null)

  const load = async () => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    const version = ++requestVersion.current
    setLoading(true)
    try {
      const rows = await readApi<User[]>('/users', {}, { signal: controller.signal, tags: ['users'] })
      if (version === requestVersion.current) setUsers(rows)
    } catch (err) {
      if (!controller.signal.aborted && version === requestVersion.current) message.error(getErrorMessage(err))
    } finally {
      if (version === requestVersion.current) setLoading(false)
    }
  }

  useEffect(() => {
    setUsers([])
    void load()
    return () => { requestVersion.current += 1; controllerRef.current?.abort() }
  }, [sessionKey])

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <Typography.Title level={3} style={{ margin: 0 }}>
          用户管理
        </Typography.Title>
        <Button type="primary" onClick={() => setOpen(true)}>
          新建用户
        </Button>
      </div>

      <Table
        rowKey="id"
        loading={loading}
        dataSource={users}
        columns={[
          { title: 'ID', dataIndex: 'id', width: 80 },
          { title: '用户名', dataIndex: 'username' },
          {
            title: '角色',
            dataIndex: 'is_admin',
            render: (v: boolean) => (v ? <Tag color="gold">管理员</Tag> : <Tag>用户</Tag>),
          },
          {
            title: '默认月限额 GB',
            dataIndex: 'monthly_limit_gb',
            render: (v) => (v == null ? '-' : v),
          },
          {
            title: 'AWS 凭证',
            render: (_, row) =>
              row.has_credentials ? (
                <Tag color="green">{row.credential_count || 1} 组</Tag>
              ) : (
                <Tag>未绑定</Tag>
              ),
          },
          {
            title: '操作',
            render: (_, row) => (
              <Space>
                <Button
                  size="small"
                  onClick={() => {
                    let pwd = ''
                    Modal.confirm({
                      title: `重置密码 - ${row.username}`,
                      content: (
                        <Input.Password
                          placeholder="新密码（12-64 位，含大小写字母、数字、符号）"
                          onChange={(e) => {
                            pwd = e.target.value
                          }}
                        />
                      ),
                      onOk: async () => {
                        const err = validateStrongPassword(pwd)
                        if (err) {
                          message.error(err)
                          throw new Error('invalid')
                        }
                        await api.patch(`/users/${row.id}`, { password: pwd })
                        message.success('已重置')
                      },
                    })
                  }}
                >
                  重置密码
                </Button>
                <Popconfirm
                  title="确认删除该用户？"
                  onConfirm={async () => {
                    try {
                      await api.delete(`/users/${row.id}`)
                      message.success('已删除')
                      void load()
                    } catch (err) {
                      message.error(getErrorMessage(err))
                    }
                  }}
                >
                  <Button size="small" danger>
                    删除
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />

      <Modal
        title="新建用户"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={async () => {
          const values = await form.validateFields()
          try {
            await api.post('/users', values)
            message.success('已创建')
            setOpen(false)
            form.resetFields()
            void load()
          } catch (err) {
            message.error(getErrorMessage(err))
          }
        }}
      >
        <Form form={form} layout="vertical" initialValues={{ is_admin: false }}>
          <Form.Item name="username" label="用户名" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item
            name="password"
            label="密码"
            rules={[
              { required: true, message: '请输入密码' },
              {
                validator: async (_, value) => {
                  const err = value ? validateStrongPassword(String(value)) : null
                  if (err) throw new Error(err)
                },
              },
            ]}
          >
            <Input.Password />
          </Form.Item>
          <Form.Item name="is_admin" label="管理员" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item name="monthly_limit_gb" label="默认月流量限额 GB">
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  )
}
