import { Alert, Collapse, Space, Typography } from 'antd'

const readStatements = [
  {
    Sid: 'LightsailRead',
    Effect: 'Allow',
    Action: [
      'lightsail:GetRegions', 'lightsail:GetInstances', 'lightsail:GetInstance',
      'lightsail:GetBlueprints', 'lightsail:GetBundles', 'lightsail:GetStaticIps',
      'lightsail:GetInstanceMetricData', 'lightsail:GetOperation',
    ],
    Resource: '*',
  },
  {
    Sid: 'ServiceQuotasRead',
    Effect: 'Allow',
    Action: ['servicequotas:GetServiceQuota', 'servicequotas:ListServiceQuotas'],
    Resource: '*',
  },
  {
    Sid: 'BillingRead',
    Effect: 'Allow',
    Action: ['ce:GetCostAndUsage', 'ce:GetCostForecast', 'billing:GetCredits'],
    Resource: '*',
  },
  {
    Sid: 'RegionStatusRead',
    Effect: 'Allow',
    Action: ['account:GetRegionOptStatus'],
    Resource: '*',
  },
]

const managedStatements = [
  ...readStatements,
  {
    Sid: 'LightsailManage',
    Effect: 'Allow',
    Action: [
      'lightsail:CreateInstances', 'lightsail:TagResource', 'lightsail:StartInstance',
      'lightsail:StopInstance', 'lightsail:RebootInstance', 'lightsail:DeleteInstance',
      'lightsail:AllocateStaticIp', 'lightsail:AttachStaticIp', 'lightsail:DetachStaticIp',
      'lightsail:ReleaseStaticIp', 'lightsail:PutInstancePublicPorts',
    ],
    Resource: '*',
  },
  {
    Sid: 'EnableSelectedRegion',
    Effect: 'Allow',
    Action: ['account:EnableRegion'],
    Resource: '*',
  },
]

function policy(statements: typeof readStatements) {
  return JSON.stringify({ Version: '2012-10-17', Statement: statements }, null, 2)
}

export default function AwsPermissionHelp() {
  return <Alert type="info" showIcon message="AWS 权限说明（测试版）" description={
    <Space direction="vertical" style={{ width: '100%' }}>
      <span>查询地区是否启用需要 <Typography.Text code>account:GetRegionOptStatus</Typography.Text>；
        手动启用未开通地区还需要 <Typography.Text code>account:EnableRegion</Typography.Text>（按需授权）。</span>
      <span>缺少区域查询权限时，无法确认该地区是否启用；单个地区返回 token 无效，也不能据此判断整组密钥失效。</span>
      <Collapse ghost size="small" style={{ width: '100%' }} items={[
        {
          key: 'permissions',
          label: '查看各功能所需权限',
          children: <Space direction="vertical" style={{ width: '100%' }}>
            <div><Typography.Text strong>实例、套餐、系统镜像与流量查询：</Typography.Text>
              <Typography.Text code>lightsail:GetRegions</Typography.Text>、
              <Typography.Text code>GetInstances</Typography.Text>、
              <Typography.Text code>GetInstance</Typography.Text>、
              <Typography.Text code>GetBlueprints</Typography.Text>、
              <Typography.Text code>GetBundles</Typography.Text>、
              <Typography.Text code>GetStaticIps</Typography.Text>、
              <Typography.Text code>GetInstanceMetricData</Typography.Text>。
              其中省略前缀的操作均属于 lightsail 服务。</div>
            <div><Typography.Text strong>任务结果核对：</Typography.Text>
              <Typography.Text code>lightsail:GetOperation</Typography.Text>。
              创建、启停和换 IP 等任务都需要读取操作结果。</div>
            <div><Typography.Text strong>配额查询：</Typography.Text>
              <Typography.Text code>servicequotas:GetServiceQuota</Typography.Text>、
              <Typography.Text code>servicequotas:ListServiceQuotas</Typography.Text>。</div>
            <div><Typography.Text strong>账单与赠金：</Typography.Text>
              <Typography.Text code>ce:GetCostAndUsage</Typography.Text>、
              <Typography.Text code>ce:GetCostForecast</Typography.Text>、
              <Typography.Text code>billing:GetCredits</Typography.Text>。</div>
            <div><Typography.Text strong>实例创建与管理（按需）：</Typography.Text>
              创建需要 <Typography.Text code>lightsail:CreateInstances</Typography.Text> 和
              <Typography.Text code>lightsail:TagResource</Typography.Text>（任务追踪标签）；
              启停、删除、静态 IP 和防火墙权限见下方管理策略。
              流量超限自动关机需要 <Typography.Text code>lightsail:StopInstance</Typography.Text>。</div>
            <div><Typography.Text strong>区域状态与启用：</Typography.Text>
              当前版本逐地区调用 <Typography.Text code>account:GetRegionOptStatus</Typography.Text>。
              <Typography.Text code>account:ListRegions</Typography.Text> 用于批量列出账号区域状态，
              当前面板未调用；通过 AWS 控制台批量查看时可按需添加。
              地区授权和地区启用是两件事，启用操作需等待 AWS 完成处理。</div>
            <div>使用专门的 IAM 用户和长期 Access Key + Secret Key。
              身份校验接口 <Typography.Text code>sts:GetCallerIdentity</Typography.Text> 无需额外 IAM Allow；
              校验成功后，各功能仍需上面对应的授权。
              面板操作当前凭证所属账号；组织成员账号可在有效权限允许时启用自身区域，
              若受组织策略或权限边界限制，请联系管理员。</div>
          </Space>,
        },
        {
          key: 'read-policy',
          label: '复制只读查询策略',
          children: <>
            <Typography.Paragraph>适用于实例、地区状态、配额、流量和费用查询；
              创建实例、启用地区及自动关机请使用下方按需管理策略。</Typography.Paragraph>
            <Typography.Paragraph copyable={{ text: policy(readStatements) }}
              style={{ whiteSpace: 'pre-wrap', fontFamily: 'monospace', maxHeight: 320, overflow: 'auto' }}>
              {policy(readStatements)}
            </Typography.Paragraph>
          </>,
        },
        {
          key: 'manage-policy',
          label: '复制完整功能策略（含创建、删除、换 IP、启用地区）',
          children: <>
            <Typography.Paragraph>将策略添加到绑定密钥所属的 IAM 用户。
              此示例为支持本面板完整功能使用 Resource: "*"；有资源范围要求时可进一步按 ARN 和条件收紧。</Typography.Paragraph>
            <Typography.Paragraph copyable={{ text: policy(managedStatements) }}
              style={{ whiteSpace: 'pre-wrap', fontFamily: 'monospace', maxHeight: 320, overflow: 'auto' }}>
              {policy(managedStatements)}
            </Typography.Paragraph>
          </>,
        },
      ]} />
    </Space>
  } />
}
