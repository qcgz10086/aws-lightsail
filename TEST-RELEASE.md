# 2026-11-19 优化测试版

本分支用于测试已审计的优化版本。测试镜像标签为 `test-optimization-20261119`；发布完成以远端分支、两个标签及 GitHub Actions 校验为准。默认分支、`billing-getcredits`、镜像 `latest` 和 `4732edf` 不随测试发布更新。

## 版本

- 已审正式交付：`6d5483e2ff6dde075a3c0b22fc21a6f089464c97`
- 产品、测试与工具：`cd5816aa0ab29ca9d692537845687b1bc7060333`
- 后端已审 Docker 镜像 ID：`sha256:3268474131a02dbb96cfab7413272fa56b80ecebd0c59f32f5e03436dbeea481`
- 前端已审 Docker 镜像 ID：`sha256:d9696a213e47cedb661729b3269ae6baad273e9d679fe0fd39f9788449dbc7ae`

这里的 Docker 镜像 ID 来自审计环境的 image inspect；该环境使用 OCI image index ID，并非底层 config digest。发布校验解析 linux/amd64 平台 manifest，核对 config、文件系统层及运行配置与已审镜像一致。此分支增加测试部署配置和说明，backend/frontend/tools 保持已审版本内容。

## 独立测试启动

使用 `docker-compose.test.yml`，不要使用仍指向正式 `latest` 的默认 Compose 文件。

1. 将 `.env.example` 复制为 `.env.test`，设置独立的 `SECRET_KEY`、`ENCRYPTION_KEY`、`ADMIN_PASSWORD`。示例占位值不能启动服务。
2. 使用新数据库卷进行测试，不要把生产 `ls_data` 挂载到测试容器，也不要复制生产 AWS 凭证。
3. 发布完成后运行：

```sh
docker compose -f docker-compose.test.yml pull
docker compose -f docker-compose.test.yml up -d
docker compose -f docker-compose.test.yml ps
```

默认地址为 `http://127.0.0.1:8081`。远端主机可通过 SSH 端口转发访问该地址。如本机端口冲突，设置 `TEST_HTTP_PORT` 为另一个空闲端口。

Compose 项目名称固定为 `lightsail-optimization-test`，数据库卷独立命名为 `ls_test_data`。应用仍支持真实 AWS 操作；本次发布和离线验证不会自动配置或执行真实 AWS 操作。

## 停止测试

```sh
docker compose -f docker-compose.test.yml down
```

此命令停止测试容器并保留测试数据库卷，不会切换正式部署。

如果在已有生产数据库上进行升级试验，必须事先独立备份；旧版本没有新任务锁，不能把切回旧镜像视为安全回退。恢复写入前须核对备份后任务差集与未解决操作，未知 AWS 结果不得自动重放。本次独立回退演练保持 `service_may_resume=false`。

## 已有验证

- 最终实际后端镜像 Linux 294/294，全量无失败、错误或跳过，真实 AWS transport 为 0。
- 认证、冷依赖、原始库迁移、前端竞态、持久任务、容量与恢复检查通过。
- 同环境每场景 20 样本，总览/实例/账单冷首批中位耗时降低 31.54%/31.77%/30.76%。
- 两镜像未压缩合计 434,788,845 字节。

完整交付证据位于 `docs/optimization/evidence/T07-cd5816a/`，交接说明位于 `docs/optimization/handoffs/T07.md`。真实 AWS 权限、生产网络/负载、生产卷 ACL、满盘和硬断电不在已完成验证范围内。
