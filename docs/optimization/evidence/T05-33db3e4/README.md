# T05 产品 33db3e4 证据包

产品身份固定为 `33db3e4f6b04653b93d60e9e88f53d11d785ec94`，tree `9d4dd144c4fc3c6d426fd03c835b63099f5b47fd`，项目 BASE `234d1a91e8e24934d2730b4a5a88c8205ac1f649`。docs-only delivery HEAD 不写入自身交付索引；在仓库根目录运行 `git log -1 --format=%H -- docs/optimization/handoffs/T05.md` 可解析最终交付 SHA。

`delivery-index.json` 列出本目录、handoff 和可重跑工具的文件长度与 SHA-256。`run/` 保存固定产品 HEAD 的可复现执行结果：命令行/CWD/退出码日志、9项 Node 测试、TSC、Vite 构建、缓存4探针、adapter4探针、12+11项浏览器逐场景 JSON、截图、产品 `git archive` 及全部20个构建产物。`probe-tools/` 保存 audit runner 实际使用的五个脚本快照和各自哈希；adapter 子目录只含 `client.mjs`、`queryCache.mjs`、`result.json`，不含 `node_modules`。根目录的独立审计报告/验证记录只记录审计者结果，与实施 runner 分开。

从仓库根目录复跑（PowerShell）：

```powershell
# 先按项目既有 T00 Node 依赖环境接通 frontend/node_modules。
$playwrightRoot = Join-Path $env:TEMP 't05-playwright-1.56.1'
npm install --prefix $playwrightRoot playwright@1.56.1 --no-save --ignore-scripts
node tools/optimization/verify_t05_frontend.mjs .baseline-runs/T05-replay-<new-id> (Join-Path $playwrightRoot 'node_modules/playwright')
```

输出路径必须是仓库内未存在的新目录。runner 以固定产品 SHA 导出源快照，先检查 delivery checkout 的 backend/frontend 与产品提交无路径差异，再把 26 个 tracked frontend 文件逐个与 Git blob 内容比较。随后顺序执行：Node 单测、TypeScript、production build、queryCache 确定性探针、Axios adapter 探针、catalog/status 浏览器、前端浏览器回归、BASE bundle 对比。命令退出码和结果 JSON 均检查；任一探针行失败、场景数不符、浏览器 error 非空或 dashboard→instances 额外 HTTP 不为零都会使 runner 失败。默认运行 12+11 个本地合成 API 场景；API 夹具拦截资源创建，不调用真实 AWS。

本项目当前未提交前端依赖锁文件。因此独立重跑需准备 React/Vite/TypeScript 等与交付时相同的 `frontend/node_modules`；参考 T00 固定依赖环境。Playwright 安装至临时目录，不修改产品 package。仅 Vite 确认超过500 kB 的共享 chunk 警告；生产 P95、真实 AWS、Linux、容器体积、CPU/内存不由该 runner 覆盖。BASE..产品没有 backend/tools 修改，未在此重复 T04 后端套件；最终集成仍需按计划复跑。
