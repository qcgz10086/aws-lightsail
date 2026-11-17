# T01 作者自查记录（不是独立审计）

依据 docs/optimization/plans/T01.md 和 V1.0。起始 BASE：7932688b7dd610310b4955b514b80909efd3ef1d；当前变更仅为数据库入口、迁移模块、恢复模块、迁移测试及 T01 文档/证据。远端 billing-getcredits 于本次 GitHub 只读查询仍为 024d9d5664fa60a49c4710d129de504bfffbce2c，本任务继承其后已独立通过的 T00 提交。

## 核实与证据

| 触发条件/关注点 | 原影响或潜在影响 | 本次核实结果及证据 | 独立审计复核要求 |
|---|---|---|---|
| 旧凭证 user_id 唯一表已含代理/配额/默认标志 | 旧手写 DDL 漏字段或默认值，密文/子表也可能受影响 | test_legacy_rebuild_preserves_all_credential_fields_and_children 逐字比较全部凭证字段，并检查子表绑定；原版 RED 失败，最终回归通过 | 对不同历史字段组合复跑，核对默认 0 不变、非零配额和时间戳保留 |
| 旧策略/流量无 credential_id | auto_stop 被写成 0；缺失凭证记录被过滤删除；多 Key 任意归属 | minimal_legacy、unowned_legacy、ambiguous_legacy_owner、legacy_binding_stays_with_each_panel_user 覆盖保留原策略、拒绝无法归属、跨面板用户绑定 | 检查回滚后原行/原列及 user_id→credential_id，不扩展为 STS 账号验收 |
| 重建父表与子表 FK | RENAME 旧父表可能改写子 FK；DROP 可能触发级联 | 新建替代表、FK OFF 在事务外、DROP 原表再 RENAME 替代表；显式索引/触发器与 CASCADE/SET NULL 测试通过 | 检查 FK 打开时的真实删除行为、重复记录唯一约束和定制定义拒绝 |
| DDL 中异常或进程退出 | 部分表升级、数据与版本台账不一致 | injected exception 及 os._exit(86) 后原 sqlite_master 一致，后续连接回滚并可升级 | 复核台账和业务 DDL 同事务；崩溃测试不等同断电持久性 |
| 升级前有未 checkpoint 的 WAL | 只复制主文件漏掉已提交数据 | backup API、integrity/FK check；upgrade_snapshot_includes_uncheckpointed_wal 与 committed/uncommitted 测试通过 | 检查快照确实含已提交值、不含未提交值；备份失败时不执行 DDL |
| 回退源损坏/哈希错误/活跃 WAL 读者 | 错误恢复或侧文件污染；覆盖最新数据无法撤回 | 错误源拒绝；恢复前安全快照；活跃读者及侧文件拒绝；新库/单凭证/多凭证升级恢复 roundtrip 通过 | 在副本核对目标不被错误输入污染、离线前提与安全快照；另做生产卷及配对镜像演练 |
| 未知自定义内联唯一约束 | 用模型重建可能无声移除约束 | 自查先新增复现测试，self-review-tests-red.txt 中 23 项有 1 项失败；增加拒绝后通过 | 复查 unknown columns/custom constraints 的拒绝范围及显式索引/触发器重放 |
| 版本已记录、未来版本或模式缺列 | 重复备份、错误应用版本访问库 | 重启台账时间不变且不新增备份；未来版本和缺列拒绝 | 复核版本识别与必要 FK/唯一约束；不把列存在检查当作所有定制模式完整比对 |

最终 Windows 全回归 158 项，27.649 秒，退出码 0；Linux 非 root、network none 的原运行镜像挂载最终 Git 源文件，全回归 158 项，35.496 秒，退出码 0。每边另执行 6 条 CLI 子进程检查和每代 10 个小型临时库启动/备份观测。原始 Windows 日志在本目录；Linux 全部原始日志、失败记录和收据保留于 remote-receipt.json 指定的 Instabox 路径，关键交付收据已在本地按 SHA256 核对。

## 明确保留的限制与后续任务

- --offline 依赖操作者实际停止所有数据库访问进程；排他锁和侧文件检查不能证明空闲 DELETE 模式进程已退出。恢复文档明确停机、禁止自动重启、配对镜像和核对流程。
- 非标准表定义、文件系统无硬链接、URI 选项、备份超过 30 秒时拒绝自动迁移；需要在副本/真实卷另验证。损坏当前库不能生成安全快照时需人工保全原库及侧文件后向新目标恢复。
- 本次未构建候选生产镜像，未进行实际前后端配对镜像回退、生产卷、断电、极端容量、真实性能目标或真实凭证解密验证。小库耗时/磁盘观测不等于首屏、内存、CPU或镜像验收。
- STS 身份和代理统一留 T02；B01 手动赠金及金额统计留 T03；缓存、SPA 请求竞态、持久任务留 T04–T06；锁文件、CI、镜像与集成留 T07。T00 的 N01/N04/N05 等非阻断测量事项未夹带修复。
- T06 任务表尚不存在，当前恢复不会宣称完成任务恢复/真实资源核对；未来回退要求先核对未终结任务，禁止盲目重放。
- requesting-code-review 所要求的内部只读复核代理尝试均在运行前因 unsupported_encrypted_agent_content 协议错误失败，没有返回审查结论。此记录是作者自查，交付仍待另一个聊天的独立审计。

作者逐项核对后未发现尚未处理的本任务阻断；该判断不代替独立审计，收到意见后仍须核实、返修并重新绑定提交。
