# T01 数据库迁移：第一轮返修报告

**待独立复审，未进入 T02。** 原审计的三个 P2 问题均已在旧提交上核实，并在本轮候选上用同一组探针复查；非阻断建议 T01-N01 也已处理。以下为实施者的验证结果。

| 交付身份 | 值 |
|---|---|
| 项目 / 计划 | `qcgz10086/aws-lightsail` / V1.0，2026-11-17；返修日期 2026-11-18（Asia/Shanghai） |
| 工作树 / 分支 | `C:\Users\liyang\.codex\worktrees\t01-db-migration\aws-lightsail` / `codex/t01-db-migration` |
| 项目基线 | `billing-getcredits` = `024d9d5664fa60a49c4710d129de504bfffbce2c`，交付时只读查询 GitHub 已核对 |
| T01 起点 | `7932688b7dd610310b4955b514b80909efd3ef1d` |
| 本轮 BASE（上次不放行的审计 HEAD） | `7acd675ef41d5200c55b0148575d97fac0df2e9f` |
| 本轮实施 HEAD（实际测试的代码） | `6ee91532a6542381d3a64fc975b9106c0d0b9e29` |
| 复审对象 | 首次加入本报告的交接提交；用 `git log -1 --diff-filter=A --format=%H -- docs/optimization/T01-REVISION1.md` 取得完整 SHA。它是实施 HEAD 的直接子提交，两者的 `backend`、`frontend` 相同。 |

原独立意见及机器记录保留在 `C:\Users\liyang\Documents\ChatGPT\aws-lightsail\.audit\T01-7acd675e\AUDIT.md` 和同目录 `verification.json`。此前提交上的 158 项常规回归通过，但没有覆盖这三个问题。第一轮返修只改两份产品文件：`backend/app/sqlite_backup.py` 和 `backend/app/migrations.py`；增加 `backend/tests/test_migration_boundaries.py`。前端、依赖、部署形态、应用密钥和 AWS 客户端未变。

## 各项触发条件、影响、证据与复核要求

下表的探针名对应 `evidence/T01-revision1/{windows,linux}-boundaries-{before,after}.json`；其余断言可在新增的 11 项回归测试中定位。

| 审计项 | 如何触发，原先会怎样 | 本轮处理与证据 | 独立复核要求 |
|---|---|---|---|
| **F01 / P2** 备份输出残留侧文件 | 目标主文件不存在，但同名有效 WAL 来自另一库。原 CLI 退出 0，源行是 `trusted`，普通连接打开备份却得到 `unrelated-old-data`，打开后哈希也变了。 | `snapshot_output_sidecar` 在两平台均从成功变为拒绝。目标主文件和 `-wal/-shm/-journal`（含空文件）均视为占用；发布后用暂存文件的 SHA 再校验。新增测试逐字比较被占用的文件，检查链接发布时突现侧文件会失败、已被替换的目标会保留；正常 WAL 源仍能备份、打开并离线恢复。 | 用有效 WAL、SHM、journal、空侧文件和既存主文件复跑；比较文件组前后字节，核对失败退出码和无成功回执。对未被占用的目标，普通 SQLite 打开后验证业务行和回执 SHA。输出目录仅供当前备份写入。 |
| **F02 / P2** 重建丢外键或改变 ID 分配 | 带旧 `UNIQUE(user_id)` 的凭证表另有 `account_label` 外键，或用了 `AUTOINCREMENT`。原实现成功写 v1，但非法 label 可写；旧序列高水位 100 后，新 ID 变为 8。 | `custom_inline_foreign_key`、`custom_autoincrement` 均改为拒绝重建。前者的外键列表保持原样；回归测试在启用外键时拒绝非法 label。后者拒绝后仍保有高水位 100，后续正常插入得到 101。测试还比较原库和可信 SHA 快照的 schema/数据，确认没有提交台账；覆盖外键动作、延迟约束及非 rowid 别名主键。 | 在临时副本分别触发两种定义，比较 schema、序列和记录；从失败日志取得备份，复核 SHA、完整性及同样的数据。重跑普通旧单凭证迁移、子表删除规则和 DDL 故障回滚。 |
| **F03 / P2** 单凭证唯一键残留 | 旧策略、流量表缺 `credential_id`，旧唯一规则写成显式索引。迁移后不同凭证写同区域、同名实例（流量还同月）时，两张表都报 `UNIQUE`。 | `legacy_explicit_unique_index` 的两种第二凭证写入均从失败变为成功。仅对标准旧键转换，内联与显式两种写法有相同结果；无关索引和触发器保留。两表同一凭证重复写仍拒绝。遇到相同列集合但顺序、排序规则或条件有歧义时拒绝；已标 v1 仍带旧键的库重启也拒绝，不静默修库。 | 分别以两种 DDL 表达升级，重启后测试不同凭证写入成功与同凭证重复拒绝；验证无关索引存在、触发器确实执行。对已标 v1 的残留旧键，确认失败前后逻辑转储相同。 |
| **N01 / P3** 台账必需列缺失 | 业务表完整，但 `schema_migrations` 仅有 `version INTEGER` 和一行 1。原先允许启动，备份路径/哈希等元数据无处追溯。 | `ledger_required_columns` 在两平台均改为拒绝启动。检查列名、声明类型、非空标志、版本主键和支持的版本行；测试覆盖缺列、无主键及失败后未改写数据库。 | 对正常三代库重启，再对缺列、无主键、未知版本及业务表漂移分别复跑。结构检查不能证明台账内容未被人工篡改，也不会补回历史哈希。 |

## 实际运行与复现

| 环境 | 迁移与恢复（含 11 项新增） | 后端全量（已包含左列） | 真 CLI 子进程 |
|---|---:|---:|---|
| Windows，Python 3.12.2 / SQLite 3.43.1 | 34 通过，7.011 s | 169 通过，29.322 s | 6 条退出码符合预期 |
| Linux，Python 3.12.16 / SQLite 3.46.1 | 34 通过，8.505 s | 169 通过，28.137 s | 6 条退出码符合预期 |

两边使用 SQLAlchemy 2.0.36。六条 CLI 的退出码依次是 `0, 0, 1, 1, 0, 0`：缺少 `--offline`、SHA 错误分别应拒绝。两边均验证恢复后有已提交的 WAL 行、安全快照保留恢复前的值。原 23 项还覆盖三代库升级/恢复、凭证密文和附加字段、用户归属、事务故障、活跃 WAL 读者、启动失败不继续 seed/启动定时器。

**失败记录也保留。** Windows 初版 10 项回归在旧实现上记录 `failures=18, errors=2`；早期夹具的触发器引用了旧表没有的 `credential_id`，最终改为旧表已有的 `region`，并补全发布失败清理和替换文件断言。Linux 用最终 11 项在旧实现上得到 `failures=19, errors=2`（包含子用例；两个 error 是 F03 的预期 UNIQUE 冲突）。这些计数不是独立缺陷数。旧审计探针只采集事实，**退出 0 不表示问题已修复**；应检查 `observations`。探针的 `head` 是原作者硬编码的旧 SHA，前后结果均忠实保留。候选源码身份用 Git blob 和 Linux 源码清单核实。

复跑用无应用配置、无真实 AWS 凭证的测试会话，使用测试环境自带的虚拟值。以下从工作树根目录运行，把新输出写入临时目录，保留交付原件：

```powershell
$py = 'C:\Users\liyang\.codex\worktrees\lightsail-t00-baseline\aws-lightsail\backend\.venv\Scripts\python.exe'
$env:AWS_EC2_METADATA_DISABLED = 'true'
$env:AWS_ACCESS_KEY_ID = 'testing'
$env:AWS_SECRET_ACCESS_KEY = 'testing'
Push-Location backend
& $py -m unittest discover -s tests -p 'test_migrat*.py' -v
& $py -m unittest discover -s tests -v
Pop-Location
& $py docs/optimization/evidence/T01/validate_cli.py --output "$env:TEMP\t01-revision1-cli.json"
& $py docs/optimization/evidence/T01-revision1/probe_boundaries.py --source . --output "$env:TEMP\t01-revision1-probes.json"
& $py docs/optimization/evidence/T01-revision1/verify_evidence.py
```

`windows-runs.json` 和 `linux-runs.json` 保存候选运行的完整 argv 和退出码。Linux 使用固定镜像 `sha256:b15660664c13e949f6ccdf151eac6523a7d6682375e8805604be935b73192f9f`，`appuser`、`--pull never`、`--network none`、候选源码只读挂载；全量测试复制到容器临时目录。这是既有镜像加载候选源码的验证，没有构建候选生产镜像。

## 小库性能与未验证项

每个平台每代 10 个临时小库；下表是首次升级的中位数/P95 和重启中位数，单位毫秒。原始样本、数据库及备份字节数见两平台的 `*-cli.json`。

| 小库 | Windows 升级中位/P95；重启 | Linux 升级中位/P95；重启 |
|---|---:|---:|
| 新库 | 10.898 / 23.058；0.856 | 4.046 / 5.601；0.515 |
| 旧单凭证库 | 29.019 / 30.423；0.960 | 9.630 / 13.747；0.502 |
| 当前多凭证库 | 25.066 / 29.483；0.868 | 7.549 / 10.181；0.551 |

两平台首个样本分别为：新库 0 → 90112 字节、无备份；旧单凭证库 28672 → 94208 字节，另保留 28672 字节备份；多凭证库 36864 → 90112 字节，另保留 36864 字节备份。仅用于估计小型夹具的启动和空间开销，不据此推断生产容量或性能收益。

本轮没有验证生产卷及 Windows ACL、网络卷或极大库、断电、配对候选镜像实际回退、T06 的未终结任务或真实 AWS。本轮未重测页面 P95、完整刷新、AWS 次数、镜像体积及 CPU/内存；V1.0 的验收值不变。发现旧 v1 残留约束或定制库时，先保全原文件组，再在副本上核对；已由旧版本丢失的外键/序列不能仅凭当前 v1 推断。具体步骤见 [恢复手册](T01-MIGRATION-RECOVERY.md)。

## 证据留存与交审

- [证据索引](evidence/T01-revision1/index.json) 覆盖 18 份本地证据文件，并列出 Linux 归档的 10 个成员。`verify_evidence.py` 可离线核对文件大小/SHA、实施提交中的全部 37 份 Python 源码、归档、两平台运行记录及前后探针断言。Windows 源码按 LF 比较；原始日志逐字节保存。
- [Linux 原始归档](evidence/T01-revision1/linux-evidence.tar.gz) 已完整保存在本地，同一文件也位于 Instabox `m_agqymokr64sggvjgxypiphnoke` 的 `/data/lightsail-t00-revision1/t01-revision1-7acd675e/linux-evidence.tar.gz`。两端均为 16200 字节，SHA256 `017dbde2e7b909dd0f247189c35dbef9167ae6e8266d79096592531067c10306`。所有成员已和远端原始日志核对。
- 入库的原审计探针 SHA256 `ed9d7332f39fbc3f0f2a9ae7df2bb4c44d233d56b4b2d0f9247ba51279122ef2`；37 份源码的清单 SHA256 `5ede5f3dc1f8096d9ce3d111dd24ce364139d54afab58c14728be6a690f23c7d`。
- 暂停前确认没有运行容器；[Instabox 收据](evidence/T01-revision1/instabox-receipt.json) 记录暂停成功、状态 `suspended`，当时平台报 `storage.synced=false`。本地核验不依赖远端机器。原交付 `evidence/T01/` 保留不变。
- 独立审计者请绑定本轮交接提交，重跑上表各项和三代库正常回归，再给出放行或返修意见。本轮仅作本地提交，未推送、合并、发布、部署或操作真实 AWS。
