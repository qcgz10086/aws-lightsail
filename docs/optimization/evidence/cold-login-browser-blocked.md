# 冷登录浏览器验证环境记录

2026-11-21，本轮浏览器自动化启动受到本机环境阻塞。

| 尝试 | 实际结果 |
| --- | --- |
| Playwright 启动已安装的 Chrome | `spawn chrome.exe EACCES`，`pid=N/A` |
| Playwright 启动已安装的 Edge | `spawn msedge.exe EACCES`，`pid=N/A` |
| PowerShell 为 Chrome 创建隔离 profile 后启动 CDP | 启动命令挂起，未观察到对应 Chrome 进程，未生成 `DevToolsActivePort`；已终止启动命令 |

独立浏览器回归未运行，未取得浏览器 FCP、LCP 和认证请求时序。产物预算已用 Node 独立执行，结果见 `cold-login-before.json` 与 `cold-login-after.json`。

启动命令已停止，最终进程检查未发现本轮 profile 对应的浏览器进程。空 profile 目录 `C:\Users\admin\AppData\Local\Temp\lightsail-cold-login-6b2549d628e8487abfa082aab13d0240` 的删除被自动审批以 `blocked by policy` 拒绝，目录保留。

`tools/optimization/cold_login_regression.mjs` 支持以下执行方式：

```sh
node tools/optimization/cold_login_regression.mjs --budget-only --output docs/optimization/evidence/cold-login-after.json
```

功能回归通过 `--playwright-module` 或 `PLAYWRIGHT_MODULE` 指定现有 Playwright 模块，也可使用已安装的 `playwright`。浏览器路径通过 `--browser-executable` 或 `BROWSER_EXECUTABLE` 指定；已有自动化浏览器通过 `--cdp` 或 `PLAYWRIGHT_CDP_ENDPOINT` 连接。

该工具在随机本地端口提供构建产物和合成 API，检查冷登录资源预算、匿名深链、错误密码、登录退出、已登录深链、普通用户路由、迟到认证响应、切换账号缓存与会话失效。
