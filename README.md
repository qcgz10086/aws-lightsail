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

### 2. docker-compose.yml

仓库根目录已自带 `docker-compose.yml`，内容如下；如果不在仓库目录内部署，直接复制这份文件保存为 `docker-compose.yml` 即可：

```yaml
services:
  backend:
    build: ./backend
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
    build: ./frontend
    ports:
      - "8080:80"
    depends_on:
      backend:
        condition: service_healthy
    restart: unless-stopped

volumes:
  ls_data:
```

说明：`backend` 用 `./backend` 本地构建，读 `.env`，SQLite 数据存在 `ls_data` 卷的 `/data/app.db`；`frontend` 构建后由 nginx 提供，映射到本机 `8080` 端口，等 `backend` 健康检查通过后才启动。

### 3. 启动

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f backend
```

浏览器打开：http://localhost:8080

默认账号（首次启动时若用户表为空、且 `.env` 中已配置 `ADMIN_PASSWORD` 时创建）：

- 用户名：`admin`（或 `.env` 中 `ADMIN_USERNAME` 配置）
- 密码：`.env` 中配置的 `ADMIN_PASSWORD`（无默认值，禁止用 `admin123`）

停止用 `docker compose down`（数据在 `ls_data` 卷里，不会丢）；更新代码后重新 `docker compose up -d --build` 即可。

### 镜像版部署（不用本地构建）

不想拉源码、不想在部署机上构建时，用仓库里的 `docker-compose.image.yml`，直接拉 Docker Hub 已发布镜像：

```bash
# 只要这两个文件在同一目录：docker-compose.image.yml、配好的 .env
docker compose -f docker-compose.image.yml pull
docker compose -f docker-compose.image.yml up -d
```

镜像：`qcgz10086/aws-lightsail-backend:latest`、`qcgz10086/aws-lightsail-frontend:latest`（对应 `billing-getcredits` 分支提交 `6f0f11b`，含账单 GetCredits 功能与 H-1/H-2/H-3 安全修复；想钉死版本可把 `latest` 改成 `6f0f11b`）。其余配置（端口 `8080`、`ls_data` 卷、健康检查）与源码版一致。

### 4. 使用流程

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

1. `SECRET_KEY`、`ENCRYPTION_KEY`、`ADMIN_PASSWORD` 均为必填且无默认值：缺失或为默认值/占位值时启动即拒绝；不要把 `.env` 提交到仓库。其中 `SECRET_KEY` 至少 32 位且须有足够熵（大小写字母、数字、符号中至少 3 类、至少 8 个不同字符），`ADMIN_PASSWORD` 12-64 位且须同时含大小写字母、数字、符号
2. 新建实例默认不开放全部端口（`open_all_ports` 默认关闭）；自定义实例密码至少 12 位且须含大小写字母、数字和符号，留空则走密钥登录、不开密码登录；新建/修改用户密码与改密同样要求 12 位加复杂度；同一 IP+用户名 15 分钟内累计登录失败 5 次后锁定 15 分钟（返回 429）
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
