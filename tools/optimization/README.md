# T00 离线基线工具

本工具对应优化计划 V1.0（2026-11-17）。只运行固定模拟 AWS 数据，不改变生产代码，也不启动生产 Compose。T00 交接文件位于 docs/optimization/handoffs/T00.md。

## 固定输入和测量边界

- Python 3.12.2、Node 20.11.1；完整依赖版本见 docs/optimization/baselines/T00/python-freeze.txt 及两个 package-lock 快照。
- 浏览器为本机 Chrome，精确版本和路径见 browser.json。后续对比须保持硬件、OS、浏览器版本、依赖和样本量一致；环境变化时，在新环境对 BASE 和候选版本成对重测。
- fixtures/aws.json 固定 UTC 日期、80 ms/次的模拟 AWS 延迟、两个 AWS 账号、同账号两组 Key、另一账号一组 Key、两个区域、每区域六实例、分页大小二。
- 每个场景/样本使用独立面板用户，避免前一场景为后一场景预热账号缓存。正式运行生成 180 个面板用户，每用户三组凭证及两条手动赠金记录。
- 使用真实登录 Cookie、用户归属检查、路由、服务、SQLAlchemy、SQLite 和生产构建前端；仅 AWS 客户端被替换。后台定时采集在工具进程中禁用。
- 工具进程拒绝非本机 socket 连接；未支持的 AWS 服务、凭证和操作直接失败。浏览器使用只转发精确本机来源的代理，外部 HTTP/CONNECT 均拒绝。未开启 Playwright request routing，浏览器 HTTP 缓存保持正常。
- SQLite 使用每次运行独有的临时目录；已有数据库不会被覆盖。进程树及临时库在结束时清理，结果目录保留。
- 原生 Python 内存读数包括工具、采样线程和静态文件托管，不等同于生产容器。资源变更、后台恢复和迁移验收属于后续任务。

## 在 T00 交付提交的新 Worktree 复现

下面是本机 Windows 的命令；不要在已有后续任务修改的目录覆盖锁文件。其他机器须使用相同版本并重新建立同环境对照。

~~~powershell
$taskPython312 = 'C:\Users\liyang\AppData\Local\Programs\Python\Python312\python.exe'
& $taskPython312 -m venv backend/.venv
& backend/.venv/Scripts/python.exe -m pip install -r docs/optimization/baselines/T00/python-freeze.txt

Copy-Item docs/optimization/baselines/T00/frontend-package-lock.json frontend/package-lock.json
npm.cmd --prefix frontend ci
npm.cmd --prefix tools/optimization ci
npm.cmd --prefix frontend run build
node frontend/node_modules/typescript/bin/tsc --project frontend/tsconfig.json --noEmit

& backend/.venv/Scripts/python.exe -m unittest discover -s tools/optimization/tests -v
node --test tools/optimization/offline_proxy.test.mjs tools/optimization/readiness.test.mjs
node --check tools/optimization/browser_benchmark.mjs

& backend/.venv/Scripts/python.exe -m tools.optimization.run_baseline --samples 20 --output .baseline-runs/recheck --chrome 'C:/Program Files/Google/Chrome/Application/chrome.exe'
~~~

run_baseline 会先检查浏览器入口语法；浏览器退出失败、报告缺失或样本数不足都会返回失败，不能沿用旧报告。数据和源码指纹位于 environment.json、manifest.json。临时前端锁文件只是 T00 环境输入，正式前端锁文件与 CI 仍归 T07。

原有后端测试需另行执行，函数式流量测试不会被 unittest discovery 收集：

~~~powershell
$env:DATABASE_URL='sqlite:///:memory:'
$env:AWS_EC2_METADATA_DISABLED='true'
$env:AWS_ACCESS_KEY_ID='T00_TEST_ONLY'
$env:AWS_SECRET_ACCESS_KEY='fictional-test-only'
$env:AWS_ENDPOINT_URL='http://127.0.0.1:9'
$env:PYTHONPATH=(Join-Path (Get-Location) 'backend')
Push-Location backend
& .venv/Scripts/python.exe -m unittest discover -s tests -v
& .venv/Scripts/python.exe tests/test_traffic_sum.py
Pop-Location
~~~

## 计时与资源口径

- API：六种查询，每种 20 个首次请求和 20 个同用户重复请求，共 240 个样本。登录在计时窗外，HTTP 响应体接收完成即停止 API 计时。
- 页面：总览、实例、账单；每页 20 个用户，分别执行首访、同上下文完整导航重访、点击刷新，共 180 个样本。重访不代表已经存在应用缓存，也不是 SPA 切页场景。
- 首批业务数据：从导航/刷新开始，逐帧观察实例总数、实例行或本月总费用等固定业务元素，随后跨两帧；不会用登录凭证数量或加载标题代替数据。
- 完整阶段：在首批就绪之后，等待当前观察到的 API 请求全部结束，再跨两帧。原始文件同时保存每条 API 的完成时刻，便于独立复算和识别保守帧等待。
- P95 使用 nearest-rank：20 个样本取排序后的第 19 个；中位数取第 10、11 个的平均值。缺失样本不能记为零。
- SQL 计数只覆盖测量窗口内真实 SQLAlchemy 查询。AWS 调用按实际模拟客户端操作逐次计数，包括分页。
- 原生 RSS 峰值以 10 ms 间隔采样；CPU 为进程 CPU 时间差除以测量窗时长的单核占比，可能超过 100%。空闲采样窗为 1 秒。
- SQLite 分别记录主库、WAL、SHM 文件的字节数与 journal_mode。没有 WAL 文件的零值只是当前库状态，不代表验证过 WAL 备份。

## 输出

- environment.json：环境、依赖一致性、源码/制品 SHA256、Docker 可用性。
- manifest.json：固定模拟数据、用户规模、场景和业务数据显示条件。
- api.json / browser.json：逐样本原始结果、调用数、SQL、CPU、RSS 和错误。
- summary.json：延迟中位数/P95、资源汇总及未测项。
- benchmark-run.txt / server.log：正式运行日志。
- 带 red 的测试输出是工具开发期间的预期失败证据，最终测试结果以 tool-tests.txt 和 browser-tool-tests.txt 为准。

T00 不宣称达成优化验收目标：后续仍须验证热缓存首屏 P95≤1秒、冷首批中位耗时改善至少30%（基线低于1秒时不退化）、完整刷新不退化及有效期内不新增同范围 AWS 查询。镜像总体积不增加、后端空闲增量≤32 MiB、刷新峰值增量≤64 MiB 的容器目标须另补实测。新增 v2 接口和 SPA 请求缓存需要后续任务增加相应观测适配，保留同一固定数据与计时定义。
