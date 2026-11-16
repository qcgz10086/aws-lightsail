# AWS Lightsail 管理平台

通过 Docker 一键部署的多用户 Lightsail 管理网站。

## 功能

- 多用户登录（默认管理员可创建其他用户）
- **每用户可绑定多组 AWS Access Key**（加密存储，可设默认、按凭证筛选实例）
- 自动扫描各区域已有 Lightsail 实例（可跨多组 Key 汇总）
- 开通实例：地区 / 套餐 / 系统镜像（Ubuntu、Debian、Windows 等）/ 名称 / **自定义密码**（至少 12 位，须含大小写字母、数字和符号；留空则走密钥登录）/ **默认不开放全部防火墙端口**（需明确勾选才放行）/ 自动静态 IP / 选择凭证
- 开机、关机、重启
- **一键换 IP**（分配新静态 IP → 解绑旧 IP → 绑定新 IP → 释放旧 IP）
- **删除实例并释放关联静态 IP**（避免残留计费）
- 实例流量监控（NetworkIn + NetworkOut）
- 实例月流量限额 + **可选「超限自动关机」勾选**（默认关闭，仅告警）
- **按地区汇总流量**：同区 2 台各 1024 GB → 显示 2048 GB
- **账单与赠金**：AWS Billing GetCredits 查官方赠金真实余额（remaining / estimated），Cost Explorer 本月费用（总费用 / 净费用 / 已抵扣 / Lightsail 费用 / 本月预测 / 按服务明细），手动赠金记录作为补充看剩余估算与到期提醒
- **区域启用**：选了未启用的区域（如香港 ap-east-1）时，页面提示并可一键启用（调 Account Management API，生效后自动重试）

> 流量基于 Lightsail 指标估算，与官方账单可能存在差异。月份按 **UTC 自然月** 统计。

## IAM 权限（最小权限策略）

先在 AWS 建一个专门给本面板用的 IAM 用户，**不要用根账号 Access Key**，再把下面这段 JSON 建成策略挂到该用户上。这是按本项目实际调的接口整理的最小权限，`Resource` 只能写 `*`（Lightsail、Cost Explorer、Billing 都不支持按资源限权）。

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "LightsailManage",
      "Effect": "Allow",
      "Action": [
        "lightsail:GetRegions",
        "lightsail:GetInstances",
        "lightsail:GetInstance",
        "lightsail:GetBlueprints",
        "lightsail:GetBundles",
        "lightsail:GetStaticIps",
        "lightsail:GetInstanceMetricData",
        "lightsail:CreateInstances",
        "lightsail:StartInstance",
        "lightsail:StopInstance",
        "lightsail:RebootInstance",
        "lightsail:DeleteInstance",
        "lightsail:AllocateStaticIp",
        "lightsail:AttachStaticIp",
        "lightsail:DetachStaticIp",
        "lightsail:ReleaseStaticIp",
        "lightsail:PutInstancePublicPorts"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ServiceQuotasRead",
      "Effect": "Allow",
      "Action": [
        "servicequotas:GetServiceQuota",
        "servicequotas:ListServiceQuotas"
      ],
      "Resource": "*"
    },
    {
      "Sid": "BillingRead",
      "Effect": "Allow",
      "Action": [
        "ce:GetCostAndUsage",
        "ce:GetCostForecast",
        "billing:GetCredits",
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    },
    {
      "Sid": "AccountRegionManage",
      "Effect": "Allow",
      "Action": [
        "account:EnableRegion",
        "account:GetRegionOptStatus"
      ],
      "Resource": "*"
    }
  ]
}
```

建策略路径：IAM → 策略 → 创建策略 → JSON → 粘贴上面这段 → 创建后附加到 IAM 用户。只想只读不建机时，把 `CreateInstances`、`Start/Stop/Reboot/DeleteInstance` 和静态 IP 那几项删掉即可。

## 部署（Docker Compose）

前置要求：已安装 Docker 和 Docker Compose（v2，`docker compose` 命令可用）；先按上一节建好 IAM 用户备用。

### 1. 拉代码、准备环境变量

```bash
git clone -b billing-getcredits https://github.com/qcgz10086/aws-lightsail.git
cd aws-lightsail
cp .env.example .env
```

编辑 `.env`，**三项均为必填、无默认值**：`SECRET_KEY`、`ENCRYPTION_KEY`、`ADMIN_PASSWORD` 缺失、为空或是默认值/占位值时，应用启动即拒绝，不会用弱值静默启动。`ADMIN_PASSWORD` 填好后，首次启动时若用户表为空才会创建管理员账号：

```env
SECRET_KEY=随机长字符串（至少 32 位，且须含大小写字母、数字、符号中至少 3 类、至少 8 个不同字符）
ENCRYPTION_KEY=用下面命令生成
ADMIN_USERNAME=admin
ADMIN_PASSWORD=强管理员密码（禁止用默认密码 admin123）
```

生成密钥：

```bash
# SECRET_KEY（任选一种）
python -c "import secrets; print(secrets.token_urlsafe(48))"

# ENCRYPTION_KEY（Fernet 密钥）
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

> 更换 `ENCRYPTION_KEY` 后，已绑定的 AWS Key 将无法解密，需要用户重新绑定。`.env` 不要提交到仓库。

### 环境变量一览

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `SECRET_KEY` | 是 | 无 | JWT 签名密钥；至少 32 位，且须含大小写字母、数字、符号中至少 3 类、至少 8 个不同字符；缺失、为空或弱值时启动即拒绝 |
| `ENCRYPTION_KEY` | 是 | 无 | Fernet 密钥，加密存储 AWS Access Key；更换后已绑定的 Key 无法解密，需重新绑定 |
| `ADMIN_PASSWORD` | 是 | 无 | 管理员密码；12-64 位，须同时含大小写字母、数字、符号；禁止用默认密码 `admin123`；首次启动且用户表为空时创建管理员 |
| `ADMIN_USERNAME` | 否 | `admin` | 管理员用户名 |
| `DATABASE_URL` | 否 | `sqlite:////data/app.db` | SQLite 路径；`docker-compose.yml` 里已配好，一般不用改 |
| `COLLECT_INTERVAL_MINUTES` | 否 | `60` | 流量自动采集间隔（分钟） |
| `CORS_ORIGINS` | 否 | 空（不放行任何来源） | 允许跨域的来源，逗号分隔；生产走 nginx 同源反代不用填，本地开发需要时显式写明 |
| `COOKIE_SECURE` | 否 | `false` | 登录 Cookie 是否带 Secure 标志；走 HTTPS 反代时设 `true` |
| `ENABLE_DOCS` | 否 | `false` | 是否开启 `/docs`、`/redoc`、`/openapi.json`；仅调试时设 `true` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | 否 | `10080`（7 天） | token 有效期（分钟）；未写进 `.env.example`，一般不用改 |
| `JWT_ALGORITHM` | 否 | `HS256` | JWT 签名算法；未写进 `.env.example`，一般不用改 |

### 2. docker-compose.yml

仓库根目录已自带 `docker-compose.yml`，内容如下；如果不在仓库目录内部署，直接复制这份文件保存为 `docker-compose.yml` 即可：

```yaml
services:
  backend:
    image: qcgz10086/aws-lightsail-backend:latest
    env_file: .env
    volumes:
      - ls_data:/data
    environment:
      DATABASE_URL: sqlite:////data/app.db
    restart: unless-stopped
    healthcheck:
      test:
        [
          "CMD",
          "python",
          "-c",
          "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')",
        ]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 20s

  frontend:
    image: qcgz10086/aws-lightsail-frontend:latest
    ports:
      - "8080:8080"
    depends_on:
      backend:
        condition: service_healthy
    restart: unless-stopped

volumes:
  ls_data:
```

说明：`backend` 与 `frontend` 直接拉 Docker Hub 已发布镜像，不用拉源码、不用本地构建；`backend` 读 `.env`，SQLite 数据存在 `ls_data` 卷的 `/data/app.db`；`frontend` 由 nginx 提供，映射到本机 `8080` 端口，等 `backend` 健康检查通过后才启动。两个容器均以非 root 用户运行（后端 `appuser`、前端 `nginx-unprivileged`，前端容器内监听 `8080`）。镜像：`qcgz10086/aws-lightsail-backend:latest`、`qcgz10086/aws-lightsail-frontend:latest`（对应 `billing-getcredits` 分支提交 `d2ae4d1`，2026-11-16；镜像摘要 backend `sha256:850097c59074d027646c4b263be8c7d11e78ac3833fbf2a73ff3a7590901fd61`、frontend `sha256:a2808a5d0f8c35ca731f7ca3d7dc249ca7e636285555e96c1bdef7093310c580`；想钉死版本可把 `latest` 改成标签 `4732edf`）。**生产必须放在 TLS 反代之后**（见下方「安全建议」）：本应用不做 TLS 终止，浏览器到反代之间走 HTTPS，反代到容器之间走内网 HTTP；此时 `.env` 里把 `COOKIE_SECURE=true`，登录 Cookie 才带 Secure 标志。

### 3. 启动

```bash
docker compose pull
docker compose up -d
docker compose ps
docker compose logs -f backend
```

浏览器打开：http://localhost:8080

默认账号（首次启动时若用户表为空、且 `.env` 中已配置 `ADMIN_PASSWORD` 时创建）：

- 用户名：`admin`（或 `.env` 中 `ADMIN_USERNAME` 配置）
- 密码：`.env` 中配置的 `ADMIN_PASSWORD`（无默认值，禁止用 `admin123`）

停止用 `docker compose down`（数据在 `ls_data` 卷里，不会丢）；更新镜像后 `docker compose pull` 再 `docker compose up -d` 即可。

### 4. 使用流程

1. 使用管理员登录
2. （可选）在「用户管理」创建普通用户
3. 在「AWS 凭证」添加一组或多组 Access Key，并可设默认；每组凭证可单独配代理（见下）
4. 在「实例」查看已有机器（可按凭证筛选），或「开通实例」时选择使用哪组 Key
5. 在实例「限额」中设置月流量上限，并**可选勾选「超限自动关机」**
6. 在「流量」页可手动同步当月用量，查看地区汇总与超限告警
7. 在「账单」页查看本月费用与官方赠金余额（先绑定凭证；官方赠金由 Billing GetCredits 自动查，手动记录仅作补充）

## 代理配置

每组 AWS 凭证可以配自己专用的代理，该凭证名下的全部 AWS 请求（Lightsail、Service Quotas、STS、Billing、Cost Explorer、流量采集、账号管理）都走这个代理；不配就是直连。

- 在「AWS 凭证」页添加凭证时填「代理（可选）」，或在已绑凭证的操作里点「代理」修改/清除。
- 格式：`http://host:port`、`socks5://host:port`，均可带认证（如 `http://user:pass@host:port`）；支持 `http://`、`https://`、`socks5://`、`socks5h://`（SOCKS5 由 PySocks 提供；`socks5h` 由代理解析域名，不泄漏本地 DNS，建议优先用它）。
- 代理地址用 Fernet 加密存在库里（与 Access Key 同等对待）；列表里只显示脱敏后的地址（`http://user:***@host:port`）。
- 新建或改代理时，会立即用新代理校验一次凭证：代理不通当场报错、旧配置保留，不会静默回落到直连。
- 使用时也会重新校验：每次用已存代理发 AWS 请求前，后端都会重新解析代理主机名，要求全部解析结果是公网地址；如果代理域名后来被改到内网地址（如 DNS 重绑定），对应接口会返回 400「代理不可用」，不会静默改走直连。代理域名解析失败或超时（2 秒）时同样拒绝。
- 代理主机只接受公网地址：内网/保留地址（127.0.0.1、10.x、192.168.x、169.254.x 等）、localhost 及 `.local` / `.internal` / `.lan` 等后缀、解析到内网的域名（如 lvh.me、*.nip.io、*.sslip.io）以及 NAT64 内嵌内网地址的域名都会被拒绝。

## 区域启用

香港（ap-east-1）等新区域在 AWS 账号里默认是关闭的（opt-in），没启用时调该区域接口会报 `UnrecognizedClientException`（跟密钥无关）。面板处理方式：

- 在「开通实例」页选了未启用的区域、套餐/镜像加载失败时，页面会提示该区域未启用，并给出「启用该区域」按钮。
- 点确认后，面板用当前凭证调 Account Management API（`account:EnableRegion`，固定走 us-east-1）启用该区域；启用不是立刻生效，面板会每 10 秒查一次状态（`account:GetRegionOptStatus`），最多等 5 分钟，变为可用后自动重载套餐/镜像。
- 需要密钥有 `account:EnableRegion` 与 `account:GetRegionOptStatus` 权限（见上方 IAM 最小权限策略）；如果是组织成员账号，只能由管理账号启用该区域。
- 区域状态查询与启用接口有限流：状态查询每分钟 10 次、启用每分钟 5 次，超限返回 429，请稍后再试；启用成功与失败都会记操作日志。
- 不想启用也可以直接换个已启用的地区（如东京、新加坡、美东）。

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
# SECRET_KEY / ENCRYPTION_KEY / ADMIN_PASSWORD 均为必填（见 .env.example），
# 缺失或为默认值时应用拒绝启动；本地开发也须先配好这三项。
set SECRET_KEY=（至少 32 位随机字符串，且须含大小写字母、数字、符号中至少 3 类）
set ENCRYPTION_KEY=（Fernet key）
set ADMIN_PASSWORD=（强管理员密码）
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
| POST | `/api/auth/logout` | 登出（清除 Cookie 并使当前 token 失效） |
| GET | `/api/auth/me` | 当前用户 |
| GET/POST | `/api/credentials` | 列出 / 新增 AWS 凭证（可多组） |
| PUT/DELETE | `/api/credentials/{id}` | 更新 / 删除指定凭证 |
| POST | `/api/credentials/{id}/default` | 设为默认凭证 |
| GET | `/api/credentials/{id}/regions/{region}/status` | 查询某区域的启用状态（每分钟 10 次限流） |
| POST | `/api/credentials/{id}/regions/{region}/enable` | 启用某区域（opt-in 区域，每分钟 5 次限流） |
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

1. `SECRET_KEY`、`ENCRYPTION_KEY`、`ADMIN_PASSWORD` 均为必填且无默认值：缺失或为默认值/占位值时启动即拒绝；不要把 `.env` 提交到仓库。其中 `SECRET_KEY` 至少 32 位且须有足够熵（大小写字母、数字、符号中至少 3 类、至少 8 个不同字符），`ADMIN_PASSWORD` 12-64 位且须同时含大小写字母、数字、符号
2. 新建实例默认不开放全部端口（`open_all_ports` 默认关闭）；自定义实例密码至少 12 位且须含大小写字母、数字和符号，留空则走密钥登录、不开密码登录；新建/修改用户密码与改密同样要求 12 位加复杂度；同一 IP+用户名 15 分钟内累计登录失败 5 次后锁定 15 分钟（返回 429）
3. **生产必须放在 TLS 反代之后**：本应用不做 TLS 终止，不要把 8000 或 8080 端口直接裸露公网。部署形态：浏览器 --HTTPS--> 反代（Caddy / nginx / Traefik，配好证书）--HTTP 内网--> 本应用的 8080 端口。走 HTTPS 时 `.env` 里设 `COOKIE_SECURE=true`，登录 Cookie 带 Secure 标志；纯 HTTP 内网部署保持 `false` 即可
4. 升级后旧 token 会失效：token 版本机制下，升级到含该修复的版本后，此前签发的 token 全部失效，部署完重新登录一次即可
5. 为 AWS IAM 用户最小化 Lightsail 权限
6. 不要将 `.env` 提交到公开仓库

> 依赖审计说明：`pip-audit` 在最新依赖下仍报 3 项无修复版本的告警，均已逐项核对不可达——`python-jose` PYSEC-2026-4298（需攻击者持有服务的非对称公钥且算法不限定；本应用只有 HS256 对称密钥且解码显式限定算法）；`ecdsa` PYSEC-2026-1325（Minerva 时序攻击，需调用 ecdsa 签名；本应用只用 HS256，从不调 ecdsa，且 0.19.2 已是最新版、无修复版本）。`npm audit` 0 漏洞。

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
