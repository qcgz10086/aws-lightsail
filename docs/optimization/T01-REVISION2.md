# T01 第二轮返修：失败重试与旧表列约束

**状态：待独立复审，暂不放行 T02。** 项目 `qcgz10086/aws-lightsail`，计划 V1.0（2026-11-17）。独立审计在 `73655ef8baed876e1cbf2bcae0c578bc94b931df` 确认 F04、F05；本轮在 Windows 和 Linux 用同一探针分别复现两项，再对实施 HEAD `a2c1dcc4217fbac67be129b17ea01cf8bddc4f30` 重跑。工作目录 `C:\Users\liyang\.codex\worktrees\t01-db-migration\aws-lightsail`，分支 `codex/t01-db-migration`。只改后端迁移、备份及配套测试；交接提交不含产品代码改动。

## F04 / P2：相同失败启动累积完整备份

**触发与影响。** 非空未版本化库含不支持重建的 `AUTOINCREMENT`。服务拒绝启动，编排器可能反复重启。在 BASE 上连续四次启动期间，源库字节不变、没有台账，但每轮新增一份相同 SHA 的 32,768 字节备份。第四次共有 131,072 字节。未做满盘实验。

**修复与实测。** 迁移持有 `BEGIN IMMEDIATE` 写锁，先分类要重建的表，再取得一致性备份。快照按其 SHA256 命名，原子回执保存主文件及有效 WAL 指纹和登记的快照 SHA。相同指纹直接校验并复用快照；指纹变而快照字节不变时，丢弃暂存副本，保留原路径。四次拒绝后均只有 32,768 字节的快照，快照路径与哈希完全相同，源库字节不变、无台账。另测了新提交只增加一份、rollback/checkpoint 不增加、短暂迁移失败后成功登记第一次的路径/SHA、首次回执发布失败后的恢复。手工 `backup --output` 仍要求全新目标。

**复核要求。** 对照 [Windows BASE](evidence/T01-revision2/windows/impact-base.json) / [候选](evidence/T01-revision2/windows/impact-candidate.json) 和 [Linux BASE](evidence/T01-revision2/linux-impact-base.json) / [候选](evidence/T01-revision2/linux-impact-candidate.json) 的 `retry`：每次的 `source_sha256`、台账存在性、快照文件名、SHA 与总字节。复跑 `test_migration_retries.py`，包括污染侧文件、快照被替换/丢失、损坏回执和链接替身。独立子进程在创建快照和复用回执两条路径均应拿不到写锁；POSIX 下读取源库指纹的文件描述符直到事务结束才关闭。

## F05 / P2：重建已知列时静默丢失非空和默认值

**触发与影响。** 旧单凭证库的 `account_label` 声明了 `NOT NULL DEFAULT 'unlabelled'`。BASE 备份保持原义，但升级成功、写入 v1 后，活动库允许写 NULL，丢失 SQL 默认值。

**修复与实测。** 需要重建的凭证、实例策略、流量表使用 `PRAGMA table_xinfo`、原表 DDL、约束/索引和模型逐列比较。无法保留的类型、非空、默认值、生成列、校验或排序规则在改表前被拒绝。历史可空日期、策略 `note TEXT` 和特定 `DEFAULT 0` 按原语义生成替代表，不改共享 ORM 模型。在候选上，两个平台均停止启动；原活动库和已通过完整性、外键及 SHA 校验的快照均拒绝写 NULL，保留原默认值。失败前后活动库 SHA 相同，无 `schema_migrations` 表。

**复核要求。** 检查同四份探针中的 `column_rule`（DDL 属性、实际写 NULL 的结果、台账是否存在、库和备份 SHA）。分别对定制父表及子表重跑；用标准 5 代模型升级两次，校验原字段值、离线恢复后的 schema 和恢复前安全快照中的后续写入。此逻辑不能补救早先版本已放宽并提交 v1 的库。

## 测试记录与性能证据

两平台使用同一 Git 导出，实施 tree `4cd27e082d68b74488d7d476420652c78bdd2d3e`。开始和结束分别核对候选 `backend/app`、`backend/tests` 全部 **38** 个 `.py`；BASE 核对 **37** 个。`identity.json` 记录实际输入字节的 SHA256；相对 Git blob，Windows 导出的 35 个候选文件和 34 个 BASE 文件仅有 LF → CRLF 的差异，其余逐字节相同。两平台收到相同字节；原始日志和压缩包不做换行转换。测试只使用临时库和模拟数据。Linux 使用固定镜像 `sha256:b15660664c13e949f6ccdf151eac6523a7d6682375e8805604be935b73192f9f`、`appuser`、`--pull never`、`--network none`、输入挂载只读。未构建候选部署镜像。

| 环境（SQLAlchemy 2.0.36） | 迁移专项（全量的子集） | 后端全量 | 补充验证 |
|---|---:|---:|---|
| Windows / Python 3.12.2 / SQLite 3.43.1 | 46 通过，1 跳过；9.390 s | 181 通过，1 跳过；31.465 s | 5 代升级/恢复，6 次 CLI，原 5 个边界探针均符合预期 |
| Linux / Python 3.12.16 / SQLite 3.46.1 | 47 通过；5.099 s | 182 通过；24.448 s | 同上；符号链接测试实际执行 |

Windows 普通账户创建符号链接报 `WinError 1314`，两套测试中是同一个跳过项；硬链接和跨进程写锁测试在两边均执行。六次 CLI 的退出码依次为 `0,0,1,1,0,0`，两次拒绝分别是缺少离线确认和错误 SHA。两边均核对操作前的已提交 WAL 行和恢复前安全快照。对裸 BASE 加入本轮测试的失败日志 `red.log` 也已保存；其中部分用例依赖候选新增的回执/函数，BASE 上的 error **不计为另外的问题**。F04/F05 的基线事实以不依赖新增接口的 `impact-base.json` 为准。原 F01–F03/N01 探针自带上轮硬编码 `head`，源码身份请用 `identity.json` 与 `runs.json`。

下表为每类 10 个小型临时库的首次升级中位/P95、重复启动中位数（毫秒）；每个样本及文件大小保存在 `cli.json`。这些是小库迁移观测，不代表页面或整机性能验收。

| 库 | Windows 升级中位 / P95；重启 | Linux 升级中位 / P95；重启 |
|---|---:|---:|
| 新库 | 11.993 / 31.141；0.881 | 2.581 / 5.600；0.407 |
| 单凭证旧库 | 41.517 / 44.628；0.903 | 15.317 / 16.805；0.459 |
| 多凭证库 | 30.584 / 32.493；0.854 | 5.316 / 10.573；0.398 |

## 复跑与证据

在包含提交历史的工作树中执行。`$py` 示例为本次 Windows 用的现成 Python 3.12 环境。目录须是新的临时路径。

```powershell
$py = 'C:\Users\liyang\.codex\worktrees\lightsail-t00-baseline\aws-lightsail\backend\.venv\Scripts\python.exe'
$ev = 'docs/optimization/evidence/T01-revision2'
$run = Join-Path $env:TEMP ('t01-r2-' + [guid]::NewGuid())
& $py "$ev/verify_evidence.py"
& $py "$ev/export_inputs.py" "$run/inputs" a2c1dcc4217fbac67be129b17ea01cf8bddc4f30
& $py "$run/inputs/drivers/run_suite.py" "$run/inputs" "$run/results"
```

也可将同一 `inputs` 只读挂到 `/in`，为 `appuser` 提供可写 `/out`，在上述固定镜像运行 `python /in/drivers/run_suite.py /in /out`。运行器按顺序执行 BASE/候选影响探针、BASE 红测、迁移专项、后端全量、历史模型、CLI 和原边界探针，并核对结果。实际 argv、退出码见 [Windows](evidence/T01-revision2/windows/runs.json) 和 [Linux](evidence/T01-revision2/linux-runs.json)。

完整文件清单在 [index.json](evidence/T01-revision2/index.json)。[Linux 归档](evidence/T01-revision2/linux-results.tar.gz) 18,338 字节，SHA256 `a93f57721eb5f71ccdb373fb906e72254fa0143b7bccfb6c2f05c230fe81cc43`；包含 14 份原始结果及执行回执，已下载并逐份核验。Instabox 暂停前无运行容器，[暂停回执](evidence/T01-revision2/instabox-suspend.json) 状态 `suspended`；当时远端 `storage.synced=false`，本地证据不依赖远端。

**边界。** 首次备份及源指纹变化后需要一个完整临时快照的空间；重复失败仍有读取、哈希和完整性校验成本，未做大库、满盘、断电或生产卷/ACL 验证。备份目录及回执要求受控，不是防篡改签名。未测真实 AWS、配对生产镜像实际回退、T06 未终态任务、页面 P95、AWS 请求数、镜像大小或 CPU/内存。上线前按[恢复手册](T01-MIGRATION-RECOVERY.md)处理。上轮审计报告、验证记录及索引位于 `C:\Users\liyang\Documents\ChatGPT\aws-lightsail\.audit\T01-revision1-73655ef8\`；本轮的新结论须由独立审计者给出。
