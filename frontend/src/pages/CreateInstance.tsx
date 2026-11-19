import { Alert, Button, Card, Form, Input, Select, Switch, Typography, message, Space } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import api, { CredentialItem, CredentialOut, getErrorMessage, readApi } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { submitTask } from '../api/tasks'

interface Region {
  name: string
  display_name: string
}
interface Bundle {
  bundle_id: string
  name: string
  price?: number
  cpu_count?: number
  ram_size_in_gb?: number
  transfer_per_month_in_gb?: number
  is_active: boolean
}
interface Blueprint {
  blueprint_id: string
  name: string
  platform?: string
  group?: string
  type?: string
  version?: string
  is_active: boolean
}
interface RegionOptStatus {
  region: string
  status: string
  message?: string
}
interface RegionIssue {
  region: string
  status: string
  credentialId: number
  catalogSeq: number
}

/** 镜像下拉文案：必须带上版本，否则多个 Ubuntu/Debian 会显示成同一项 */
function blueprintLabel(b: Blueprint): string {
  const parts: string[] = [b.name || b.blueprint_id]
  if (b.version) parts.push(b.version)
  if (b.type && b.type !== 'os') parts.push(b.type)
  if (b.group && b.group !== b.name) parts.push(b.group)
  // 平台对同类 OS 往往相同，放最后作参考
  if (b.platform) parts.push(b.platform)
  // 同名同版本时用 blueprint_id 区分（极少数情况）
  const base = parts.join(' · ')
  return `${base}  (${b.blueprint_id})`
}

export default function CreateInstancePage() {
  const { user, sessionKey } = useAuth()
  const [form] = Form.useForm()
  const [creds, setCreds] = useState<CredentialItem[]>([])
  const [regions, setRegions] = useState<Region[]>([])
  const [bundles, setBundles] = useState<Bundle[]>([])
  const [blueprints, setBlueprints] = useState<Blueprint[]>([])
  const [loadingMeta, setLoadingMeta] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [selectedPlatform, setSelectedPlatform] = useState<string | undefined>()
  const [regionIssue, setRegionIssue] = useState<RegionIssue | null>(null)
  const [enablingRegion, setEnablingRegion] = useState(false)
  // 区域启用轮询定时器：单例，换地区/重复触发时先停掉旧的，卸载时取消，
  // 避免多个 5 分钟循环叠加、旧 region 的成功回调覆盖当前表单（U-4）。
  const pollTimerRef = useRef<number | null>(null)
  // 轮询代次：stopRegionPolling 时 +1，废止已在途的 tick（V-4）。
  // 只清 timeout 不够：在途请求返回后还会重新 setTimeout 并继续
  // 改状态，代次让它直接返回。
  const pollSeqRef = useRef(0)
  const catalogSeqRef = useRef(0)
  const readyCatalogScope = useRef<string | null>(null)
  const regionsCredentialScope = useRef<number | null>(null)
  const catalogController = useRef<AbortController | null>(null)
  const credentialLoadSeq = useRef(0)
  const credentialLoadController = useRef<AbortController | null>(null)
  const stopRegionPolling = () => {
    pollSeqRef.current += 1
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current)
      pollTimerRef.current = null
    }
  }
  useEffect(() => () => {
    stopRegionPolling()
    catalogController.current?.abort()
    credentialLoadController.current?.abort()
    credentialLoadSeq.current += 1
  }, [])
  const navigate = useNavigate()

  const catalogScope = (credentialId: number | undefined, region: string | undefined) =>
    credentialId && region ? `${credentialId}:${region}` : null

  const isCurrentCatalogScope = (credentialId: number, region: string, seq: number) =>
    seq === catalogSeqRef.current
    && form.getFieldValue('credential_id') === credentialId
    && form.getFieldValue('region') === region
    && regionsCredentialScope.current === credentialId

  const setCurrentRegionIssue = (region: string, status: string, credentialId: number, seq: number) => {
    if (isCurrentCatalogScope(credentialId, region, seq)) {
      setRegionIssue({ region, status, credentialId, catalogSeq: seq })
    }
  }

  const clearCatalogSelection = () => {
    catalogSeqRef.current += 1
    catalogController.current?.abort()
    stopRegionPolling()
    readyCatalogScope.current = null
    regionsCredentialScope.current = null
    setRegions([])
    setBundles([])
    setBlueprints([])
    setSelectedPlatform(undefined)
    setRegionIssue(null)
    setEnablingRegion(false)
    form.setFieldsValue({ region: undefined, blueprint_id: undefined, bundle_id: undefined,
      password: undefined, password_confirm: undefined })
  }

  const clearCatalogChoices = () => {
    catalogSeqRef.current += 1
    catalogController.current?.abort()
    stopRegionPolling()
    readyCatalogScope.current = null
    setBundles([])
    setBlueprints([])
    setSelectedPlatform(undefined)
    setRegionIssue(null)
    setEnablingRegion(false)
    form.setFieldsValue({ blueprint_id: undefined, bundle_id: undefined, password: undefined,
      password_confirm: undefined })
  }

  useEffect(() => {
    if (!user?.has_credentials) return
    form.resetFields()
    setCreds([])
    setRegions([])
    setBundles([])
    setBlueprints([])
    readyCatalogScope.current = null
    regionsCredentialScope.current = null
    setSelectedPlatform(undefined)
    setRegionIssue(null)
    setLoadingMeta(true)
    const seq = ++credentialLoadSeq.current
    credentialLoadController.current?.abort()
    const controller = new AbortController()
    credentialLoadController.current = controller
    void (async () => {
      try {
        const cRes = await readApi<CredentialOut>('/credentials', {}, { signal: controller.signal })
        if (seq !== credentialLoadSeq.current || controller.signal.aborted) return
        const items = cRes.items || []
        setCreds(items)
        const defaultId = items.find((c) => c.is_default)?.id || items[0]?.id
        if (defaultId) {
          form.setFieldsValue({ credential_id: defaultId })
          await loadRegions(defaultId)
        } else {
          setLoadingMeta(false)
        }
      } catch (err) {
        if (!controller.signal.aborted && seq === credentialLoadSeq.current) message.error(getErrorMessage(err))
      }
    })()
    return () => {
      credentialLoadSeq.current += 1
      controller.abort()
      stopRegionPolling()
      catalogSeqRef.current += 1
      catalogController.current?.abort()
    }
  }, [user?.has_credentials, user?.id, sessionKey])

  const loadRegions = async (credentialId: number) => {
    stopRegionPolling()
    catalogController.current?.abort()
    clearCatalogSelection()
    const seq = ++catalogSeqRef.current
    setLoadingMeta(true)
    const controller = new AbortController()
    catalogController.current = controller
    try {
      const res = await readApi<Region[]>('/catalog/regions', { credential_id: credentialId }, {
        signal: controller.signal,
      })
      if (seq !== catalogSeqRef.current || form.getFieldValue('credential_id') !== credentialId) return
      setRegions(res)
      regionsCredentialScope.current = credentialId
    } catch (err) {
      if (!controller.signal.aborted && seq === catalogSeqRef.current
        && form.getFieldValue('credential_id') === credentialId) message.error(getErrorMessage(err))
    } finally {
      if (seq === catalogSeqRef.current) setLoadingMeta(false)
    }
  }

  const loadCatalog = async (region: string, credentialId: number) => {
    stopRegionPolling()
    const seq = ++catalogSeqRef.current
    catalogController.current?.abort()
    readyCatalogScope.current = null
    setBundles([])
    setBlueprints([])
    const controller = new AbortController()
    catalogController.current = controller
    setLoadingMeta(true)
    setRegionIssue(null)
    form.setFieldsValue({ blueprint_id: undefined, bundle_id: undefined, password: undefined, password_confirm: undefined })
    setSelectedPlatform(undefined)
    try {
      const [bundles, blueprints] = await Promise.all([
        readApi<Bundle[]>('/catalog/bundles', { region, credential_id: credentialId }, { signal: controller.signal }),
        readApi<Blueprint[]>('/catalog/blueprints', { region, credential_id: credentialId }, { signal: controller.signal }),
      ])
      if (seq !== catalogSeqRef.current || form.getFieldValue('region') !== region
        || form.getFieldValue('credential_id') !== credentialId) return
      setBundles(bundles.filter((b) => b.is_active !== false))
      const bps = blueprints.filter((b) => b.is_active !== false)
      bps.sort((a, c) => {
        // OS 优先，再按名称+版本
        const score = (x: Blueprint) => (x.type === 'os' ? 0 : 1)
        const s = score(a) - score(c)
        if (s !== 0) return s
        const na = `${a.name || ''} ${a.version || ''}`.toLowerCase()
        const nc = `${c.name || ''} ${c.version || ''}`.toLowerCase()
        return na.localeCompare(nc)
      })
      setBlueprints(bps)
      readyCatalogScope.current = catalogScope(credentialId, region)
    } catch (err) {
      if (controller.signal.aborted || seq !== catalogSeqRef.current) return
      const msg = getErrorMessage(err)
      if (msg.includes('UnrecognizedClientException')) {
        await checkRegionOptStatus(region, credentialId, msg, seq)
      } else {
        message.error(msg)
      }
    } finally {
      if (seq === catalogSeqRef.current) setLoadingMeta(false)
    }
  }

  const pollRegionStatus = (region: string, credentialId: number, catalogSeq: number) => {
    stopRegionPolling()
    const seq = pollSeqRef.current
    const deadline = Date.now() + 5 * 60 * 1000
    const tick = async () => {
      if (seq !== pollSeqRef.current || !isCurrentCatalogScope(credentialId, region, catalogSeq)) return
      if (Date.now() > deadline) {
        pollTimerRef.current = null
        message.warning('区域启用超时，请稍后手动刷新重试')
        return
      }
      try {
        const res = await api.get<RegionOptStatus>(
          `/credentials/${credentialId}/regions/${region}/status`,
        )
        if (seq !== pollSeqRef.current || !isCurrentCatalogScope(credentialId, region, catalogSeq)) return
        if (res.data.status === 'ENABLED') {
          pollTimerRef.current = null
          setRegionIssue(null)
          message.success(`区域 ${region} 已启用`)
          await loadCatalog(region, credentialId)
          return
        }
        if (res.data.status === 'DISABLED') {
          pollTimerRef.current = null
          setCurrentRegionIssue(region, 'DISABLED', credentialId, catalogSeq)
          return
        }
        // ENABLING / DISABLING / UNKNOWN：继续等
      } catch {
        if (seq !== pollSeqRef.current || !isCurrentCatalogScope(credentialId, region, catalogSeq)) return
        pollTimerRef.current = null
        return
      }
      if (seq !== pollSeqRef.current || !isCurrentCatalogScope(credentialId, region, catalogSeq)) return
      pollTimerRef.current = window.setTimeout(tick, 10000)
    }
    pollTimerRef.current = window.setTimeout(tick, 10000)
  }

  const checkRegionOptStatus = async (
    region: string,
    credentialId: number,
    fallbackMsg: string,
    catalogSeq: number,
  ) => {
    try {
      const res = await api.get<RegionOptStatus>(
        `/credentials/${credentialId}/regions/${region}/status`,
      )
      if (!isCurrentCatalogScope(credentialId, region, catalogSeq)) return
      const status = res.data.status
      if (status === 'DISABLED') {
        setCurrentRegionIssue(region, status, credentialId, catalogSeq)
      } else if (status === 'ENABLING' || status === 'DISABLING') {
        setCurrentRegionIssue(region, status, credentialId, catalogSeq)
        void pollRegionStatus(region, credentialId, catalogSeq)
      } else {
        message.error(fallbackMsg)
      }
    } catch {
      if (isCurrentCatalogScope(credentialId, region, catalogSeq)) message.error(fallbackMsg)
    }
  }

  const enableRegion = async () => {
    if (!regionIssue) return
    const credentialId = form.getFieldValue('credential_id') as number | undefined
    const selectedRegion = form.getFieldValue('region') as string | undefined
    if (!credentialId || regionIssue.credentialId !== credentialId
      || regionIssue.catalogSeq !== catalogSeqRef.current
      || regionsCredentialScope.current !== credentialId
      || selectedRegion !== regionIssue.region) return
    const region = regionIssue.region
    const seq = catalogSeqRef.current
    const isCurrent = () => seq === catalogSeqRef.current
      && form.getFieldValue('credential_id') === credentialId
      && form.getFieldValue('region') === region
      && regionsCredentialScope.current === credentialId
      && regionIssue.credentialId === credentialId && regionIssue.catalogSeq === seq
    setEnablingRegion(true)
    try {
      const res = await api.post<RegionOptStatus>(
        `/credentials/${credentialId}/regions/${region}/enable`,
      )
      if (!isCurrent()) return
      if (res.data.status === 'ENABLED') {
        setRegionIssue(null)
        message.success(res.data.message || '区域已启用')
        setEnablingRegion(false)
        await loadCatalog(region, credentialId)
      } else {
        setCurrentRegionIssue(region, 'ENABLING', credentialId, seq)
        message.info(res.data.message || '区域正在启用，生效需要几分钟')
        setEnablingRegion(false)
        void pollRegionStatus(region, credentialId, seq)
      }
    } catch (err) {
      if (isCurrent()) message.error(getErrorMessage(err))
    } finally {
      if (isCurrent()) setEnablingRegion(false)
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
    <Space direction="vertical" size="large" style={{ width: '100%', maxWidth: 720 }}>
      <Typography.Title level={3} style={{ margin: 0 }}>
        开通实例
      </Typography.Title>
      <Card>
        <Form
          form={form}
          layout="vertical"
          initialValues={{ allocate_static_ip: true, open_all_ports: false }}
          onFinish={async (values) => {
            const credentialId = Number(form.getFieldValue('credential_id'))
            const region = String(form.getFieldValue('region') || '')
            const blueprint = blueprints.find((item) => item.blueprint_id === values.blueprint_id)
            if (Number(values.credential_id) !== credentialId || String(values.region || '') !== region
              || !region || readyCatalogScope.current !== catalogScope(credentialId, region)
              || !creds.some((item) => item.id === credentialId)
              || regionsCredentialScope.current !== credentialId
              || !regions.some((item) => item.name === region)
              || !bundles.some((item) => item.bundle_id === values.bundle_id)
              || !blueprint) {
              message.error('凭证、地区或目录已变化，请等待当前目录加载完成后重新选择')
              return
            }
            const submitScope = catalogSeqRef.current
            const currentSubmission = () => isCurrentCatalogScope(credentialId, region, submitScope)
            setSubmitting(true)
            try {
              const { password_confirm: _confirm, ...payload } = values
              if (blueprint.platform) payload.platform = blueprint.platform
              // 未填写时不要传 password 字段，让后端走默认行为
              if (!payload.password) delete payload.password
              if (!user) return
              const task = await submitTask(user.id, { kind: 'create', credential_id: values.credential_id,
                region: values.region, resource_name: values.instance_name, payload })
              if (!currentSubmission()) return
              form.setFieldsValue({ password: undefined, password_confirm: undefined })
              message.info('建机任务已接受，请查看执行进度')
              navigate(`/tasks/${task.task_id}`)
            } catch (err) {
              if (currentSubmission()) message.error(getErrorMessage(err))
            } finally {
              if (currentSubmission()) setSubmitting(false)
            }
          }}
        >
          <Form.Item
            name="credential_id"
            label="使用的 AWS 凭证"
            rules={[{ required: true, message: '请选择凭证' }]}
          >
            <Select
              options={creds.map((c) => ({
                value: c.id,
                label: `${c.account_label || '凭证'} #${c.id}${c.is_default ? ' (默认)' : ''} · ${c.access_key_masked}`,
              }))}
              onChange={(v) => {
                clearCatalogSelection()
                void loadRegions(v)
              }}
            />
          </Form.Item>

          <Form.Item name="region" label="地区" rules={[{ required: true, message: '请选择地区' }]}>
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择 Lightsail 地区"
              options={regions.map((r) => ({
                value: r.name,
                label: `${r.display_name} (${r.name})`,
              }))}
              onChange={(v) => {
                const cid = form.getFieldValue('credential_id')
                clearCatalogChoices()
                if (cid && v) void loadCatalog(v, cid)
                else setLoadingMeta(false)
              }}
            />
          </Form.Item>

          {regionIssue && (
            <Alert
              type="warning"
              showIcon
              message={`地区 ${regionIssue.region} 未启用`}
              description={
                <Space direction="vertical">
                  <span>
                    该 AWS 账号尚未启用此区域（新区域默认关闭），所以调该区域接口会报
                    UnrecognizedClientException。
                    {regionIssue.status === 'ENABLING'
                      ? '正在启用中，生效需要几分钟，稍后会自动重试。'
                      : '点下面按钮可以启用它（需要密钥有 account:EnableRegion 权限）。'}
                  </span>
                  {regionIssue.status !== 'ENABLING' && (
                    <Button
                      type="primary"
                      size="small"
                      loading={enablingRegion}
                      onClick={() => void enableRegion()}
                    >
                      启用该区域
                    </Button>
                  )}
                </Space>
              }
            />
          )}

          <Form.Item
            name="instance_name"
            label="实例名称"
            rules={[
              { required: true, message: '请输入名称' },
              {
                pattern: /^[a-zA-Z0-9][a-zA-Z0-9-]*$/,
                message: '仅字母数字和连字符，且以字母或数字开头',
              },
            ]}
          >
            <Input placeholder="my-instance" />
          </Form.Item>

          <Form.Item
            name="blueprint_id"
            label="系统镜像"
            rules={[{ required: true, message: '请选择镜像' }]}
          >
            <Select
              showSearch
              loading={loadingMeta}
              optionFilterProp="label"
              placeholder="Ubuntu / Debian / Windows ..."
              options={blueprints.map((b) => ({
                value: b.blueprint_id,
                label: blueprintLabel(b),
              }))}
              onChange={(v) => {
                const bp = blueprints.find((b) => b.blueprint_id === v)
                setSelectedPlatform(bp?.platform)
              }}
            />
          </Form.Item>

          <Form.Item name="bundle_id" label="套餐" rules={[{ required: true, message: '请选择套餐' }]}>
            <Select
              showSearch
              loading={loadingMeta}
              optionFilterProp="label"
              placeholder="nano / micro / small ..."
              options={bundles.map((b) => ({
                value: b.bundle_id,
                label: `${b.name} · $${b.price ?? '?'}/月 · ${b.cpu_count ?? '?'} vCPU · ${
                  b.ram_size_in_gb ?? '?'
                } GB · 流量 ${b.transfer_per_month_in_gb ?? '?'} GB`,
              }))}
            />
          </Form.Item>

          <Form.Item
            name="password"
            label="自定义密码"
            extra={
              selectedPlatform && selectedPlatform.toUpperCase().includes('WIN')
                ? '可选。将设置 Windows Administrator 密码，实例首次启动后生效。'
                : '可选。将为 root 与常见默认用户设置登录密码，并开启 SSH 密码认证；首次启动后生效。'
            }
            rules={[
              {
                validator: async (_, value) => {
                  if (!value) return
                  if (String(value).length < 12 || String(value).length > 64) {
                    throw new Error('密码长度需为 12-64 位')
                  }
                  if (String(value) !== String(value).trim()) {
                    throw new Error('密码不能包含首尾空格')
                  }
                  if (
                    !/[a-z]/.test(String(value)) ||
                    !/[A-Z]/.test(String(value)) ||
                    !/[0-9]/.test(String(value)) ||
                    !/[^A-Za-z0-9]/.test(String(value))
                  ) {
                    throw new Error('密码须同时包含小写字母、大写字母、数字和符号')
                  }
                },
              },
            ]}
          >
            <Input.Password placeholder="留空则使用系统默认（密钥/随机密码）" autoComplete="new-password" />
          </Form.Item>

          <Form.Item
            name="password_confirm"
            label="确认密码"
            dependencies={["password"]}
            rules={[
              ({ getFieldValue }) => ({
                validator: async (_, value) => {
                  const pwd = getFieldValue('password')
                  if (!pwd && !value) return
                  if (pwd && !value) throw new Error('请再次输入密码')
                  if (pwd !== value) throw new Error('两次输入的密码不一致')
                },
              }),
            ]}
          >
            <Input.Password placeholder="再次输入自定义密码" autoComplete="new-password" />
          </Form.Item>
          <Form.Item
            name="open_all_ports"
            label="开放全部防火墙端口"
            valuePropName="checked"
            extra="默认关闭。开启后创建后自动放行所有协议 0-65535（任意 IPv4/IPv6），并保留 Lightsail 浏览器 SSH/RDP。"
          >
            <Switch />
          </Form.Item>

          <Form.Item
            name="allocate_static_ip"
            label="自动分配并绑定静态 IP"
            valuePropName="checked"
            extra="建议开启。一键换 IP 与删除清理均基于静态 IP。"
          >
            <Switch />
          </Form.Item>

          <Button type="primary" htmlType="submit" loading={submitting}
            disabled={loadingMeta || !readyCatalogScope.current || readyCatalogScope.current !== catalogScope(
              Number(form.getFieldValue('credential_id')), form.getFieldValue('region'))}
            block>
            创建
          </Button>
        </Form>
      </Card>
    </Space>
  )
}
