# AWS Lightsail 管理平台

通过 Docker 一键部署的多用户 Lightsail 管理网站。

## 功能

- 多用户登录（默认管理员可创建其他用户）
- **每用户可绑定多组 AWS Access Key**（加密存储，可设默认、按凭证筛选实例）
- 自动扫描各区域已有 Lightsail 实例（可跨多组 Key 汇总）
- 开通实例：地区 / 套餐 / 系统镜像（Ubuntu、Debian、Windows 等）/ 名称 / **自定义密码** / **默认开放全部防火墙端口** / 自动静态 IP / 选择凭证
- 开机、关机、重启
- **一键换 IP**（分配新静态 IP → 解绑旧 IP → 绑定新 IP → 释放旧 IP）
- **删除实例并释放关联静态 IP**（避免残留计费）
- 实例流量监控（NetworkIn + NetworkOut）
- 实例月流量限额 + **可选「超限自动关机」勾选**（默认关闭，仅告警）
- **按地区汇总流量**：同区 2 台各 1024 GB → 显示 2048 GB
- **账单与赠金**：AWS Billing GetCredits 查官方赠金真实余额（remaining / estimated），Cost Explorer 本月费用（总费用 / 净费用 / 已抵扣 / Lightsail 费用 / 本月预测 / 按服务明细），手动赠金记录作为补充看剩余估算与到期提醒

> 流量基于 Lightsail 指标估算，与官方账单可能存在差异。月份按 **UTC 自然月** 统计。

## 快速开始

### 1. 准备环境变量

```bash
cd lightsail-manager
cp .env.example .env
```

编辑 `.env`，**务必修改**：

```env
SECRET_KEY=随机长字符串
ENCRYPTION_KEY=用下面命令生成
ADMIN_USERNAME=admin
ADMIN_PASSWORD=请改成强密码
```

生成 `ENCRYPTION_KEY`：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

> 更换 `ENCRYPTION_KEY` 后，已绑定的 AWS Key 将无法解密，需要用户重新绑定。

### 2. 启动

```bash
docker compose up -d --build
```

浏览器打开：http://localhost:8080

默认账号（若用户表为空时自动创建）：

- 用户名：`admin`（或 `.env` 中配置）
- 密码：`admin123`（或 `.env` 中配置）

### 3. 使用流程

1. 使用管理员登录
2. （可选）在「用户管理」创建普通用户
3. 在「AWS 凭证」添加一组或多组 Access Key，并可设默认
4. 在「实例」查看已有机器（可按凭证筛选），或「开通实例」时选择使用哪组 Key
5. 在实例「限额」中设置月流量上限，并**可选勾选「超限自动关机」**
6. 在「流量」页可手动同步当月用量，查看地区汇总与超限告警
7. 在「账单」页查看本月费用与官方赠金余额（先绑定凭证；官方赠金由 Billing GetCredits 自动查，手动记录仅作补充）

## 架构

```
Browser → frontend(nginx:8080) → /api 反代 → backend(FastAPI/uvicorn)
                                         → SQLite (/data volume)
                                         → 用户各自的 AWS Lightsail API
```

- 后端单 worker（适配 SQLite）
- 每小时自动采集流量（`COLLECT_INTERVAL_MINUTES`，可用「立即同步」手动触发）

## 本地开发（可选）

### 后端

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
set DATABASE_URL=sqlite:///./dev.db
set SECRET_KEY=dev
set ENCRYPTION_KEY=（Fernet key）
uvicorn app.main:app --reload --port 8000
```

### 前端

```bash
cd frontend
npm install
npm run dev
```

Vite 已将 `/api` 代理到 `http://127.0.0.1:8000`。

## API 概览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 登录 |
| GET | `/api/auth/me` | 当前用户 |
| GET/POST | `/api/credentials` | 列出 / 新增 AWS 凭证（可多组） |
| PUT/DELETE | `/api/credentials/{id}` | 更新 / 删除指定凭证 |
| POST | `/api/credentials/{id}/default` | 设为默认凭证 |
| GET | `/api/catalog/regions?credential_id=` | 区域（可指定凭证） |
| GET | `/api/instances?credential_id=` | 跨区实例列表（空=全部凭证） |
| POST | `/api/instances` | 创建（body 可带 credential_id） |
| POST | `/api/instances/{region}/{name}/start\|stop\|reboot\|change-ip?credential_id=` | 生命周期 |
| PATCH | `/api/instances/{region}/{name}/settings` | 限额 / **auto_stop_on_limit** / 备注 |
| DELETE | `/api/instances/{region}/{name}` | 删除+释放静态 IP |
| GET | `/api/traffic/summary` | 流量汇总 |
| POST | `/api/traffic/sync` | 手动同步流量（并执行已勾选的超限关机） |
| GET | `/api/billing/summary?credential_id=` | 本月费用（Cost Explorer）+ 官方赠金（Billing GetCredits）+ 手动赠金记录 |
| GET/POST | `/api/billing/credits` | 列出 / 新增赠金记录（手动录入，补充用） |
| DELETE | `/api/billing/credits/{id}` | 删除自己的赠金记录 |

## 账单与赠金

### 官方赠金（AWS Billing GetCredits，优先展示）

账单页的官方赠金直接调 AWS Billing API `GetCredits`（boto3 `billing`，固定 `us-east-1`）：

- accountId 不手填：先用 STS `GetCallerIdentity` 从当前凭证自动取 12 位账号 ID。
- `GetCredits` 的 startDate 必须是过去 1 年内的日期，本应用取今天往前 364 天，endDate 取今天；只返回该范围内的赠金。
- 每条赠金返回 `initialAmount` / `remainingAmount` / `estimatedAmount`：remaining 是截至上个账单周期的官方余额，estimated 是含未出账单的估算剩余，两者都是 AWS 返回的，不是本应用推算的。
- **boto3 版本要求**：`boto3>=1.44.3`。旧版 boto3 1.35.86 的 billing 客户端只有 CreateBillingView / GetBillingView 等操作，没有 GetCredits，升级后才可用。
- GetCredits 与费用查询相互独立：一方失败只影响自己那块展示，不会让整个账单页报错。

### 本月费用（Cost Explorer）

- 本月费用：`GetCostAndUsage`，MONTHLY，`UnblendedCost` + `NetUnblendedCost`，按 SERVICE 分组；已抵扣 = 总费用 − 净费用；Lightsail 费用为服务名含 Lightsail 的合计。
- 本月预测：`GetCostForecast`（UNBLENDED_COST），预测失败时显示为“—”，不影响主汇总。

### 手动赠金记录（补充）

- 手动记录的剩余是**估算**：剩余估算 = 赠金总额 − 自开始日起 Cost Explorer 累计已抵扣额（Unblended − Net），与官方赠金分开显示，不要混用。
- 手动记录不会自动插入：如需补充官方赠金之外的记录，请按官方 Credits 页在账单页手动添加（名称、总额、开始日期、到期日、Credit ID、备注）。

使用前需要：

1. 在 AWS 控制台启用 Cost Explorer（控制台 → Cost Explorer → 启用），启用后数据通常需要 24-48 小时才会出现；未启用或暂无数据时账单页显示 `unavailable` 提示，不会报 500。
2. 给 IAM 用户/角色添加只读权限：`billing:GetCredits`、`sts:GetCallerIdentity`（官方赠金）、`ce:GetCostAndUsage`、`ce:GetCostForecast`（本月费用）；缺权限时账单页对应区域显示 `no_permission` 提示。不要用根账号 Access Key。

## 安全建议

1. 修改默认管理员密码
2. 使用强随机 `SECRET_KEY` 与 `ENCRYPTION_KEY`
3. 生产环境建议置于反向代理 / HTTPS 之后
4. 为 AWS IAM 用户最小化 Lightsail 权限
5. 不要将 `.env` 提交到公开仓库

## 目录结构

```
lightsail-manager/
├── docker-compose.yml
├── .env.example
├── backend/          # FastAPI + boto3
└── frontend/         # React + Vite + Ant Design
```

## 许可

仅供授权的账号管理与运维使用。请遵守 AWS 服务条款与当地法律法规。
