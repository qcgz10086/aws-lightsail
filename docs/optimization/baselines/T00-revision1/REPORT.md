# T00 独立审计返修报告

本轮仅修复 T00 基线工具与交付证据。独立审计的原结论仍为「证据不足，当前不通过 T00，不放行 T01」；本报告是实施者的返修与自查记录，提交后等待独立复核。T01 未启动。

## 身份与范围

- 计划：V1.0，2026-11-17；需求归档见 `../../plans/V1.0.md`，返修计划见 `../../plans/T00-revision1.md`。
- 项目：`qcgz10086/aws-lightsail`；分支 `billing-getcredits` 的 GitHub GET 核对仍为指定 BASE。
- Worktree：`C:\Users\liyang\.codex\worktrees\lightsail-t00-baseline\aws-lightsail`；工作分支 `codex/t00-baseline`。
- BASE：`024d9d5664fa60a49c4710d129de504bfffbce2c`。
- 原审计 HEAD：`ca23f465843a327539d3f1360c39bb7975db03bd`。
- 本轮冻结工具 HEAD：`79d46dfbceff86fefec89c12db357ee03bfe1701`；后续交付提交仅更新文档和证据。
- 交接提交 HEAD 解析：`git log -1 --format=%H -- docs/optimization/handoffs/T00.md`。最终回复提供该完整 SHA，不将文件内的自引用伪装为提交哈希。
- 原审计报告原样复制到 `audit-source/AUDIT.md`，SHA256 `b95f6e49451c42b5ad9659493d5709f520e2095b877852dcff4625ce2e9b11c2`；原文件及原 T00 证据保持不变。

BASE、原审计 HEAD、本轮工具 HEAD 的 backend/frontend 构建输入相同。未修改产品 API、凭证处理、数据库迁移或部署结构。本轮不包含生产部署或真实 AWS 操作。

交付证据目录通过 .gitattributes 保留原始字节，禁止 Git 自动转换换行；交付清单同时核对工作区文件和暂存 blob 的 SHA256，避免 Windows 原始日志/JSON 的 CRLF 被转换后使哈希失配。

## 审计条目逐项回应

### F01 / P2：容器补测缺少可执行路径

- 触发条件：在有 Docker 的机器按旧 README 执行原生驱动，希望获得两镜像体积、容器 CPU 与内存。
- 影响与核实：原生驱动只查询 Docker 版本，资源仍为 null，且无容器 fixture 命令。确认审计判断；旧原生 RSS 不能填入容器字段。
- 返修：增加 `tools/optimization/container_baseline.py` 及 README Linux 命令。从固定提交的 backend/frontend 树导出构建输入，在临时 Dockerfile 副本固定外部 FROM 的 RepoDigest；记录镜像 ID、层、大小、原 Dockerfile 哈希、固定 Dockerfile、环境和源码指纹。实际两个生产镜像运行只读模拟工具，一次性 SQLite 和内部网络；100 ms cgroup v2 采样，保存窗口边界与原始采样。
- 证据：最终成对容器运行和文件哈希见 `container-verification.json`、`CONTAINER-EVIDENCE.md`；资源公式/缺文件失败、HTTP 转发 cookies/二进制内容、仅导出目标源码树的回归测试见 `tool-tests.txt`。20 项工具测试通过。
- 复核要求：用最终工具 HEAD、同一基础镜像摘要与固定数据执行 README 的成对命令；核对只有两个测量容器、单后端进程、内部网络、镜像内产品导入路径 `/app`、真实 AWS 边界已替换。复算原始 cgroup 与窗口，不把宿主机/原生 RSS 混入容器值。复核者决定补证是否足以解除原阻断。

### V01 / P2：20 样本浏览器复现未完成

- 触发条件：原独立审计在固定 HEAD 的导出副本执行 `--samples 20`，API 完成而浏览器超过 900 秒。
- 影响与核实：该次独立运行不足以证明完整页面 P95。根因仍未确认，未将其归咎于应用、Chrome、资源压力或工具循环；两样本成功不替代标准样本量。
- 返修：逐个上下文、登录、页面/阶段、关闭步骤记录运行 ID、UTC 时刻、总耗时及阶段耗时，每阶段持久化原始结果。以默认 900 秒上限重跑 20 样本。
- 证据：`native-delivery/` 完成 240 API、180 页面、0 错误；run/summary/browser 均 completed，退出 0。`verification.json` 记录 59 项指纹匹配及 30 组统计复算。Instabox 在相同新环境成对运行 BASE/原审计候选，分别保留标准 20 样本；全部结果以最终容器复核记录为准。
- 复核要求：从新的空输出目录独立重跑 20 样本，确认每场景/阶段各 20 个样本、run_id 一致、终态和退出码有效；若再次超时，用阶段日志定位最后一步，保留已完成样本。跨机器绝对耗时不用于宣告优化收益。

### E01 / P2：生产资源证据缺失

- 触发条件：以原交付的 null 容器指标评估镜像不增加、后端空闲增量≤32 MiB、峰值增量≤64 MiB。
- 影响与核实：原证据不能支持这些资源目标，确认缺口。
- 返修与证据：补测实际后端/Nginx 镜像，并保存两个镜像的 ID/层/逻辑体积、cgroup 内存与 CPU 原始数据、10 秒空闲窗、全请求工作窗、数据库文件字节和清理结果。最终数值见后文资源表与容器证据索引。
- 边界：这是生产镜像配固定 fixture 的基线。只读 psutil 和模拟主机、临时 Nginx 统计位置、宿主机本机转发均有测量开销；未覆盖真实 AWS、TLS、后台调度负载或线上容量。镜像大小为两镜像未压缩 Size 的和，共享层按各镜像计入；不是 registry 压缩下载量或去重磁盘占用。内存工作集为 memory.current−inactive_file，100 ms 采样峰值不是绝对峰值。
- 复核要求：保持同机器、相同工具/fixture/基础镜像、样本量、窗口与采样口径，核对两次镜像内容相同及资源复算。未来优化任务另按既定增量目标做配对对比；本轮未降低验收标准。

### B01 / P1：既存跨 AWS 账号手动赠金估算错误

- 触发条件：同一面板用户绑定两个不同 AWS 账号，各有手动赠金；查询默认或显式选择的某组凭证。
- 影响与核实：`backend/app/routers/billing.py` 读取该用户所有手动记录，再用当前一个 CE client 估算每条记录。确认错用账号的调用路径；没有确认跨面板用户泄露。原 fixture 两账号费用相同，不据此量化真实金额误差。
- 证据：原审计 `audit-source/account-probe.json`；路由选择 client 与遍历记录的位置仍在 BASE 和本轮产品源码。原独立审计确认 BETA 记录被 ALPHA 查询结果估算。
- 处置：保留为 T03 的阻断级账单验收项，T00 不修业务代码，不标为已解决。
- 复核要求：T03 必须为不同 AWS 账号提供不同抵扣金额，覆盖默认凭证、显式凭证、同账号重复 Key、无所属历史记录、其他面板用户记录隔离；核对实际 CE/账号范围及输出金额，不能仅检查列表条数。

### N01 / P2：业务就绪口径不保证数据代际和 viewport

- 触发条件：页面保留旧的正业务值，或响应后 React 延迟超过两帧才应用新状态。
- 影响与核实：正业务字段及布局检测不能证明本轮数据已呈现，也不等同位于 viewport；请求结束＋两帧无法覆盖任意延迟的状态更新。此限制未被本轮进度修复消除。
- 处置与证据：保持原首批/完整计时定义，避免改变原基线含义；README 明确限制。原审计已验证首批正值检测与完整阶段两帧行为，本轮相关浏览器回归测试通过。
- 复核要求：T04/T05/v2 测量适配之前增加与用户、账号、请求代际绑定的可观察标识；验证旧缓存值、延迟状态更新、切账号和过期响应，必要时按 viewport 条件采样。不能用本轮指标证明这些未来行为正确。

### N02 / P3：网络 guard 覆盖范围

- 触发条件：将 Python connect/connect_ex guard 或浏览器 HTTP/CONNECT 来源代理称为全部协议的操作系统出站隔离。
- 影响与核实：DNS 与未连接 UDP sendto 不在 Python guard 覆盖范围。原审计使用 mock 证明入口仍可到达，未证明正式工作负载外连。
- 处置与证据：README 与本报告明确边界，保留未知凭证/操作、TCP connect、HTTP/CONNECT 的拒绝测试；容器工作负载增加 `--internal` 网络且不映射外部端口。本轮正式模拟调用未匹配/拒绝违规记录为 0。
- 复核要求：检查内部 Docker 网络和浏览器代理配置；如需要全部协议隔离，另用网络命名空间/防火墙并记录拒绝事件。宿主机浏览器仍不具备已验收的全协议 OS 隔离；不夸大为此类验收通过。

### N03 / P3：失败进度和报告有效性

- 触发条件：浏览器在完成部分页面后超时/异常，或复用含旧 JSON 的输出目录。
- 影响与核实：旧代码退出时才写页面结果，容易丢失进度，自动消费方无法可靠识别不完整批次。
- 返修：逐阶段原子写 browser.json，追加进度 JSONL；run_id 和 running/failed/completed 状态贯穿。拒绝已有核心结果的目录。成功消费要求对应 run_id、completed、退出 0、完整样本数及零错误；超时清理本次拥有的进程树并保存失败和部分进度。
- 证据：`timeout-probe/` 使用 4 秒浏览器上限，整体退出 1，保留 1 个完成阶段；run/browser/summary 为 failed；该结果没有进入成功样本。`verification.json` 确认探针测试的核心工具指纹与最终版本一致。running、错误 run_id、不完整样本、旧目录和子进程失败的回归测试通过。
- 复核要求：再次定向超时，检查失败状态、部分样本、日志及本次进程清理；确认旧结果不能满足成功条件。保留原审计「未确认进程泄漏」的判断，不凭超时探针额外宣告泄漏根因。

## 测试与正式测量

| 命令/验证 | 本轮结果 | 证据 |
|---|---|---|
| `backend/.venv/Scripts/python.exe -m unittest discover -s tools/optimization/tests -v` | 20 通过，0 失败，9.146 秒 | tool-tests.txt |
| `node --test tools/optimization/offline_proxy.test.mjs tools/optimization/readiness.test.mjs tools/optimization/progress_report.test.mjs` | 3 通过，0 失败 | browser-tool-tests.txt |
| `backend/.venv/Scripts/python.exe -m unittest discover -s tests -v`（cwd=backend，虚构凭证/临时库环境） | 135 通过，0 失败，23.473 秒 | backend-tests.txt |
| `.venv/Scripts/python.exe tests/test_traffic_sum.py`（cwd=backend） | 通过 | traffic-test.txt |
| `node node_modules/typescript/bin/tsc --noEmit`（cwd=frontend） | 退出 0，无诊断 | frontend-types.txt |
| `backend/.venv/Scripts/python.exe -m tools.optimization.run_baseline --samples 20 --output docs/optimization/baselines/T00-revision1/native-delivery` | 退出 0；240 API、180 页面，0 错误 | native-delivery-run.txt、native-delivery/ |
| `... -m tools.optimization.container_baseline --source-ref <BASE/候选> --samples 20 --base-images <固定摘要> --output <新目录>` | 最终成对结果见容器复核记录 | container-verification.json、CONTAINER-EVIDENCE.md |
| `... verify_evidence.py native .` / `... verify_evidence.py containers /data/lightsail-t00-revision1` | 独立公式复核；不调用测量实现 | evidence-check.txt、container-verification.json |

RED 和诊断日志只作为返修过程证据，不算通过。目录导出测试首先揭示 Windows Git autocrlf 转换换行；修复固定原始 blob 换行后上述 20 项通过。Instabox 初次内部网络无法返回映射端口、psutil root 文件导致临时目录清理失败，分别修为本机 HTTP 转发与宿主用户安装。最终运行使用新的空目录；此前批次不进入最终统计。

前端构建在原交付和原独立审计中通过，审计重建 JS/CSS/HTML 哈希与原交付一致；本轮没有修改前端，类型检查为本轮执行。Linux 成对流程重新从源码构建生产镜像。本报告不将原审计构建写成本轮 Windows 重建。此前使用旧工具指纹的原生诊断批次保留在 Worktree 的 .baseline-runs/t00-revision1-diagnostics/，最终统计只使用 native-delivery。

## 性能与资源

原生有效运行 ID：`99ad4193-4d8a-473c-a8aa-654cb54581fc`；2026-11-17 14:42:05–14:48:50 UTC。Windows、Python 3.12.2、Node 20.11.1、Chrome 157.0.8103.52。以下延迟单位为 ms，格式为中位数 / P95。

| 原生页面 | 首访首批 | 重复访问首批 | 点击刷新完整 |
|---|---|---|---|
| 总览 | 1237.669 / 1365.404 | 991.561 / 1045.528 | 929.912 / 972.102 |
| 实例 | 1351.648 / 1611.356 | 1064.687 / 1232.317 | 1306.617 / 1511.979 |
| 账单 | 1170.500 / 1475.083 | 793.617 / 972.694 | 969.827 / 1010.413 |

原生空闲 RSS 104.71 MiB；各请求/页面窗口最高采样 RSS 107.70 MiB；空闲单核 CPU 1.557%。这些是原生 fixture 主机值。

容器成对运行：Instabox `m_agqymokr64sggvjgxypiphnoke`，4 vCPU / 配置 8192 MiB，Linux 6.18.54-instabox。驱动 Python 3.12.2、Node 22.23.3、Chromium 154.0.8037.57、Docker 29.8.2、cgroup v2；实际后端镜像 Python 3.12.16，前端 Nginx 1.27.5。每次 240 API / 180 页面 / 0 错误；浏览器分别约 260.089 秒和 256.884 秒。

| 容器页面首访首批 | BASE 中位数 / P95 | 审计候选中位数 / P95 |
|---|---|---|
| 总览 | 1423.644 / 2463.737 | 1363.271 / 1530.800 |
| 实例 | 1454.938 / 1578.699 | 1536.416 / 1733.276 |
| 账单 | 1244.168 / 1487.765 | 1303.978 / 1559.532 |

| 容器/版本 | 空闲工作集均值 MiB | 工作窗采样峰值 MiB | 空闲单核 CPU | 工作窗单核 CPU | 工作窗秒 |
|---|---|---|---|---|---|
| base / backend | 86.03 | 90.12 | 2.494% | 17.577% | 379.700 |
| base / frontend | 4.81 | 6.27 | 0.000% | 0.453% | 379.700 |
| candidate / backend | 87.14 | 90.78 | 2.541% | 18.238% | 363.063 |
| candidate / frontend | 5.05 | 5.63 | 0.000% | 0.417% | 363.063 |

后端镜像 601,138,554 字节，前端镜像 75,987,447 字节，两次总计均为 **677,126,001 字节（645.76 MiB）**。四个索引 ID 见容器摘要，文件层/运行配置/Size 均一致。两次全工作窗长度不同，包含所有 API、登录及页面阶段；CPU 是各自完整窗口的时间差除以墙钟时长。

两个容器批次原始采样分别 3806 / 3669 条；8 个资源窗口复算一致，60 组延迟统计复算一致。两批次 58 个产品输入文件与 14 个工具输入文件指纹相同，且工具指纹与本机一致。三次正式运行数据库主文件均 1,867,776 字节，WAL/SHM 0，journal_mode=delete。完整阶段/逐样本数值与精确窗口见原始归档。

固定数据：两 AWS 账号、三个 Key（其中两个属于同账号），两个区域，每区域六实例，分页大小 2，每模拟 AWS 操作延迟 80 ms。每阶段 20 样本，P95 使用 nearest-rank 的第 19 值。账单每请求 AWS/SQL 为 6/3，实例 27/110，区域 1/2，套餐 2/2，镜像 2/2，流量 0/4；重复请求仍产生查询，不能当作已实现缓存。

原生与容器数值分别报告，不跨环境直接比较。原生是 Windows/Chrome，容器是 Instabox Linux/Chromium。BASE 与候选产品完全相同，成对差异只能描述运行波动，不能归为优化收益。

## 未验证项、已知限制与再次审计

1. 原独立审计 900 秒超时的根因仍未知；补交受控标准运行与阶段证据，待审计者判断是否充分。
2. 真实 AWS、TLS、真实账号规模及生产后台调度负载未测；Docker/fixture 资源包含观测开销。冷/热缓存收益、资源优化增量和全项目集成目标未宣告达成。
3. 迁移版本、WAL 一致性备份、升级/恢复、任务幂等与未知结果恢复、补偿、自动关机冲突、v2/SPA 竞态等属于后续任务，未测试。当前 SQLite delete 模式/WAL 文件 0 字节不代表 WAL 恢复通过。
4. B01 的业务归属错误保留 T03；N01 代际和 viewport 限制保留为未来测量适配前提；N02 全协议隔离未验收。
5. 原前端 Dockerfile 使用 npm install 且无产品锁文件；固定基础镜像不能冻结所有传递依赖或 apt/pip 远端状态。成对最终镜像的层、运行配置、大小与源码指纹相同；两个构建的镜像索引 ID 和构建标识元数据不同，四个 ID 分别记录。精确镜像留存供复核；正式锁文件与 npm ci 仍归 T07。
6. 容器原始证据的保存位置与哈希见 CONTAINER-EVIDENCE.md；本地投影及原始制品的边界明确列出，不能仅把投影当作全部原始数据。已实际暂停、恢复并重读两归档和完整 receipt，三个 SHA256 一致；恢复回执 storage.synced=true。机器最终再次暂停，末次暂停回执 synced=false，末次平台同步尚未确认，见 instabox-retention.json。复审须恢复机器、读取原始归档并核对哈希。
7. 再次审计应绑定新交接 HEAD，核对从原审计 HEAD 到新 HEAD 的修改、有效 run_id、测试退出码、输入指纹、原始统计和资源窗口。当前作者自查不替代独立结论；T00 重新通过后才进入 T01。
