# T01 数据库迁移修复实施报告

**状态：代码实施、验证与作者自查完成，待独立审计。** 本报告不能作为独立审计放行结论。

- 项目：qcgz10086/aws-lightsail；计划 V1.0，2026-11-17；交付日期 2026-11-18（Asia/Shanghai）。
- 工作目录：C:\Users\liyang\.codex\worktrees\t01-db-migration\aws-lightsail；分支 codex/t01-db-migration。
- 原始项目 BASE：024d9d5664fa60a49c4710d129de504bfffbce2c。GitHub 只读查询确认 billing-getcredits 仍指向该 SHA。
- 本任务起始 BASE：7932688b7dd610310b4955b514b80909efd3ef1d；沿用用户提供的 T00-revision1 独立审计放行提交，祖先关系已核对。
- 实施 HEAD：68b3979fcb2e5984f3827fe82c4356c848b575ac。
- 审计交付提交：本报告和 handoffs/T01.md 所在的交接提交；最终回复提供其完整 SHA。可执行 git log -1 --format=%H -- docs/optimization/handoffs/T01.md 固定该提交，backend 与实施 HEAD 完全相同。交接提交只保存文档及原始证据字节属性，独立审计应绑定交付 SHA。
- T00 审计依据：C:\Users\liyang\Documents\ChatGPT\aws-lightsail\.audit\T00-revision1-7932688b\AUDIT.md。

## 修改范围与设计

只修改 backend/app/database.py，新增 app/migrations.py、app/sqlite_backup.py 和 tests/test_migrations.py；其他为 T01 计划、恢复说明和证据。没有改变 requirements、Dockerfile、Compose、前端、凭证加密/登录/代理逻辑，也没有增加 Redis、队列、语言或进程。没有推送、合并、发布、部署或访问真实 AWS。

数据库入口在任何 create_all 之前执行迁移。所有 SQLite DDL、数据复制和 schema_migrations 版本 1 记录在同一 BEGIN IMMEDIATE 事务；未版本化非空磁盘库先检查并备份，失败停止启动。使用模型 DDL 与明确列映射重建，不再手写遗漏字段的凭证表。旧默认标志、密文、代理、配额、时间戳和子记录保留；只有缺少字段的旧库补适当缺省。

重建使用“新建替代表、复制、DROP 原表、RENAME 替代表”的顺序。关闭外键发生在事务之前且限于迁移连接，提交前检查外键并最终打开。旧策略和流量按面板用户的唯一凭证绑定，缺失/多条凭证停止而非过滤丢行或任选 Key。历史赠金缺少 credential_id 时保留 NULL，归属处理留 T03。

备份使用 SQLite backup API，验证 integrity_check 和 foreign_key_check，记录 SHA256；文件暂存、fsync、原子硬链接发布，拒绝覆盖已有备份。独立 CLI 支持 backup/verify/restore，恢复要求 --offline、正确 SHA256、当前库安全快照及侧文件检查，原子替换后保留回执。实际停机、配对镜像和未来未终结任务核对流程见 T01-MIGRATION-RECOVERY.md。

## 触发条件、影响、证据和复核要求

| 项 | 触发条件 | 影响与本次处理 | 已执行证据 | 独立审计复核要求 |
|---|---|---|---|---|
| M01 凭证字段遗漏 | user_id 唯一旧表同时已有代理/配额/默认标志 | 原手写表重建漏字段；现复制所有已有模型列，已有 is_default=0 不被改写 | legacy_rebuild_preserves_all_credential_fields_and_children；原版 RED 与最终 GREEN | 逐字段比较密文、所有配额、默认值、日期和行数；再插入第二凭证 |
| M02 策略与流量 | 旧表没有 credential_id | 原策略 auto_stop 被重置；缺 Key 的行被丢弃；多 Key 任意绑定。现保留旧策略，只按 user_id 的唯一凭证迁移 | minimal_legacy、unowned_legacy、ambiguous_legacy_owner、legacy_binding_stays_with_each_panel_user | 检查多用户及缺失/多 Key 条件下原行原列仍在，回滚台账不存在 |
| M03 外键与约束 | 重建被子表引用的凭证表 | 原 RENAME 可能改写子 FK，DROP 可能级联。现替代表流程、事务外 FK OFF、提交前校验 | 显式索引/触发器重放，DELETE CASCADE 和 credit SET NULL 测试 | 复跑真实 FK 删除规则；检查模型必需唯一约束、旧单凭证限制已移除 |
| M04 迁移中失败 | 备份失败、DDL 后异常、进程退出 | 不得部分升级或先写版本。现备份前无 DDL，台账和业务 DDL 同事务 | backup_failure、failure_rolls_back_ddl、os._exit(86) 后原 sqlite_master 相同且可重试 | 校验原模式、记录和升级前备份；不能把进程退出测试等同断电测试 |
| M05 WAL 一致性 | WAL 已提交且未 checkpoint；同时有未提交写 | 只复制主库会漏数据。backup API 包含已提交写并排除未提交写 | upgrade_snapshot_includes_uncheckpointed_wal；backup_includes_committed_wal_excludes_uncommitted | 保持 WAL 非空进行独立复跑，读取快照中的值并校验 SHA/integrity/FK |
| M06 版本与重启 | 新库、当前多 Key、重复启动、高版本或缺列 | 新库/当前库记录 v1；重复启动不重复备份、不改 applied_at；不认识版本/模式漂移停止 | new_database、current_multi_key、restart_does_not_create_more_backups、future_version_and_schema_drift | 检查模式、版本行与备份数量；列检查不代表全部定制模式严格等价 |
| M07 离线恢复 | 新库/旧单 Key/多 Key；源损坏/错 SHA/活跃 WAL | 正确源恢复原状态；错误输入拒绝替换；保留当前安全快照；不自动删除侧文件 | 三代库 roundtrip；5 项 RecoveryTests；两边各6条 CLI 子进程命令 | 检查原表 SQL、数据、FK、安全快照，以及错误输入后的目标未被污染；另验证生产卷停机与镜像配对 |
| M08 自定义模式 | 未知列或额外内联唯一/CHECK 等 | 模型重建可能静默丢自定义定义。自查复现后增加拒绝；显式索引/触发器保留 | self-review-tests-red.txt 23 项1失败，修复后23项通过 | 扩展历史组合复核拒绝范围；人工修改前必须保留副本与备份 |
| M09 启动阻断 | init_db 抛异常 | seed/定时收集器不得继续启动 | startup_failure_does_not_seed_or_start_scheduler | 检查 FastAPI lifespan 顺序与错误传播，不能容错继续跑业务 |

测试名以上为简写，完整名称和断言见 backend/tests/test_migrations.py；作者逐项自查记录见 evidence/T01/author-review.md。

## 测试命令与结果

Windows 使用 T00 已固定的 Python 3.12.2 venv、SQLite 3.43.1、SQLAlchemy 2.0.36；本任务没有重新安装依赖。候选源码是独立 T01 工作树。

```powershell
Set-Location C:\Users\liyang\.codex\worktrees\t01-db-migration\aws-lightsail\backend
& C:\Users\liyang\.codex\worktrees\lightsail-t00-baseline\aws-lightsail\backend\.venv\Scripts\python.exe -m unittest discover -s tests -p test_migrations.py -v
& C:\Users\liyang\.codex\worktrees\lightsail-t00-baseline\aws-lightsail\backend\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

- 最初复现原实现：5 项，3 失败、2 错误；migrations-red.txt。后续最初 GREEN 为5项通过；migrations-green-initial.txt。
- 完整 T01 专项：23 项通过，5.428 秒；migration-recovery-tests.txt。此项早于最后纯空行清理；最终全量回归再次包含同23项。
- 作者自查额外唯一约束 RED：23 项1失败；self-review-tests-red.txt；增加拒绝后转绿。
- 最终 Windows 全量：158 项通过，27.649 秒，退出码0；backend-tests-final.txt 和 .exit。
- 最终 Linux 全量：158 项通过，35.496 秒，退出码0；远端 linux-exact.txt / linux-exact-receipt.json。

CLI/启动观测命令在仓库根目录运行：

```powershell
& C:\Users\liyang\.codex\worktrees\lightsail-t00-baseline\aws-lightsail\backend\.venv\Scripts\python.exe docs/optimization/evidence/T01/validate_cli.py --output docs/optimization/evidence/T01/windows-cli-startup-final.json
```

两边各6条 CLI 子进程命令通过，含预期退出码1的缺少 --offline 和错误 SHA 拒绝；预期失败不冒充异常。检查备份包含 WAL、恢复前安全快照有最新数据、恢复后数据回到快照，并移除应用密钥环境变量验证模块独立性。每代10个临时库记录启动、重启、数据库和备份字节数。

Linux 使用 Instabox m_agqymokr64sggvjgxypiphnoke，Python3.12.16、SQLite3.46.1，SQLAlchemy2.0.36。复用已固定镜像 sha256:b15660664c13e949f6ccdf151eac6523a7d6682375e8805604be935b73192f9f，以 appuser、--network none、只读候选源挂载，复制必要源到临时目录执行；候选36个Python输入与最终 Git 内容逐字节一致。这是现有运行镜像加载候选代码的功能验证，没有构建候选生产镜像。确切 docker argv 在远端收据中。

## 性能和磁盘观测

这些是小型临时数据库观测，不能作为页面/缓存性能、CPU/内存或镜像验收。Linux 最终全量回归与观测容器同窗运行，存在竞争；不据此比较 Windows/Linux 或宣称加速。原始10个样本/代全部保留。

| 平台/小库 | 首次升级中位/P95 ms | 重启中位 ms |
|---|---:|---:|
| Windows 新库 | 11.525 / 12.719 | 0.783 |
| Windows 单凭证 | 27.200 / 35.161 | 0.779 |
| Windows 多凭证 | 23.597 / 24.460 | 0.753 |
| Linux 新库 | 17.311 / 25.743 | 0.746 |
| Linux 单凭证 | 37.125 / 91.108 | 0.826 |
| Linux 多凭证 | 31.518 / 49.698 | 0.814 |

Windows代表样本：新库0→90112字节，无升级前备份；旧单凭证28672→94208字节，加一份28672字节备份；当前多凭证36864→90112字节，加一份36864字节备份。增长包含模型索引/台账及测试页布局，不代表生产库占用。重复启动不新增备份。恢复会另增加一份当前库安全快照，需要预留卷空间。

本次未重测首屏P95、冷缓存首批、完整刷新、AWS调用次数、CPU/内存或候选镜像体积；既定最终指标没有降低，待对应任务和T07集成核验。没有把本次测试耗时差异称作优化收益。

## 证据身份与留存

本地目录 docs/optimization/evidence/T01/：index.json 记录本地原始文件字节/哈希，.gitattributes 使 T01 原始证据不被 Git 换行转换；Windows 原生源码字节与 Git LF 字节分别记录于 source-manifest-final.json、git-source-manifest-final.json。源文件原生CRLF/Git LF差异已区分，不声称原生文件与Linux文件原始哈希相同。

远端完整材料：/data/lightsail-t00-revision1/t01-implementation/。linux-evidence-final.tar.gz 含25项原始日志、失败记录及收据，41114字节，SHA256 4b14293cd3e7e1837bb6c1b38d555beb4589a471b54fbcf6dfcde717bbc56751。最终全量日志 SHA256 744a3745bda693b9fb20de76f30e4c66dfa6f7d07b0d603e884171224cd97a17；CLI日志 SHA256 2364eee193bf07b42324f2b567a183bca0bbaf0befdd4c32f63d5271902bdb24。

本地 remote-receipt.json 与远端 delivery-receipt.json 相同字节，SHA256 dd030aa591405d34851bb86546f8a76680694286eec3f5e476207aa2dd9d1223。它明确是摘要投影，完整Linux原始日志没有冒充本地文件。

三个配置/驱动失败如实留存：首次宿主证据目录不可写未执行测试；一轮额外设置 SECRET_KEY 与既有JWT测试固定常量不符，158项1错误；CLI首次复制整个历史目录遇到受保护T00输出，未执行CLI。分别改为日志采集、沿用固定测试环境、只复制必要源码后通过，没有为这些问题夹带产品改动。

暂停前重新核对完整归档与最终日志哈希，运行容器为空。机器已暂停；暂停回执 storage.synced=false，未据此声称平台磁盘同步完成；原始材料保留于远端持久目录，本地关键收据字节已核对。后续审计请恢复同机器并重新核对归档，禁止删除持久磁盘。

## 未验证项、已知限制和复核门槛

1. 未访问生产卷、真实密文/凭证、真实AWS；没有候选前后端镜像构建、配对镜像实际回退、未来任务核对、极端容量或断电演练。T07实际集成仍是交付条件。
2. --offline依赖全体数据库进程实际停机；空闲DELETE模式进程无法仅靠SQLite锁证明退出。活跃WAL读者拒绝测试不能替代生产停机检查。
3. 定制表定义/未知列、URI选项、无硬链接卷和超过30秒备份拒绝自动迁移，需人工核实副本；当前库损坏时先保全原库和侧文件，再向空目标恢复。已经被历史破坏性迁移丢失的字段须从有效旧备份恢复。
4. 备份后新增合法写入可能不在回退快照中，恢复安全快照供人工核对；不能盲目覆盖。POSIX文件权限600已观察，Windows生产ACL未验证。
5. T02账号/代理、T03 B01账单与历史赠金、T04/T05缓存竞态、T06持久任务、T07构建集成，以及T00 N01/N04/N05测量维护均不在当前修复范围，按任务顺序继续。
6. 内部审查子代理在运行前因协议错误失败，没有审查结论；本报告只有作者自查。独立审计另行核对M01–M09和所有限制，结论应绑定最终交付提交。阻断意见先核实返修，再提供新HEAD和对应证据，审计通过后才能进入T02。
