# 冷登录 DOM 集成验证

验证日期：2026-11-21。运行环境：Windows、Node.js 24.18.0、JSDOM 26.1.0、esbuild 0.28.2。

测试将真实 App、AuthProvider、Login、AuthenticatedApp、Layout、业务页面和 ReactDOM 编译后挂载到 JSDOM。认证 fetch 与业务 XMLHttpRequest 访问本地 HTTP 夹具，共享 CookieJar；断言读取实际 DOM、URL、认证状态和缓存状态。这是 DOM 集成验证，浏览器布局、绘制和 Lighthouse 数据由相应独立记录说明。

## 正常运行

在 `frontend` 目录执行：

```powershell
node --test tests/authRouting.test.mjs
npm run typecheck
git diff --check
```

结果：DOM 集成测试 **8/8 通过**，进程正常退出，退出码 **0**，最近一次测试耗时 **4132 ms**；TypeScript 检查及 diff 检查退出码均为 **0**。

覆盖行为：

- 匿名访问实例、任务和未知深链，显示登录页并将 URL 替换为 `/login`。
- 错误密码显示 FastAPI 错误，保留匿名状态。
- 登录进入 `/`，退出回 `/login`，已缓存 Router 再次登录时 DOM 与 URL 一致。
- 已登录实例直链保留查询参数，任务直链通过带 Cookie 的实际 XMLHttpRequest 加载任务详情。
- 普通用户访问 `/users` 回到总览，菜单不显示用户管理。
- 延迟的初始 `/auth/me` 响应不覆盖新登录及退出状态。
- 账号切换清除缓存、终止在途读请求、清理任务提交 Map，新账号使用独立缓存键。

测试使用真实 AntD，并为 JSDOM 补充 matchMedia、ResizeObserver 和 MessageChannel 生命周期管理。每项测试在 finally 中卸载 root、关闭 HTTP 连接和服务器、关闭 MessagePort 及 JSDOM window；测试结束停止 esbuild。

编译目录创建于已 realpath 解析的 `frontend/node_modules/.auth-routing-*`。递归清理前再次验证真实绝对路径的父目录及目录名前缀，运行结束无残留编译目录。

## 变异检查

变异仅通过 esbuild onLoad 改写临时编译内容。产品源文件保持原样。

在 `frontend` 目录设置 `AUTH_ROUTING_MUTATION` 后运行对应命令，三个变异的测试进程均按预期退出 **1**：

| 环境变量值 | 命令 | 命中的实际断言 |
| --- | --- | --- |
| `anonymous-route` | `node --test --test-name-pattern='anonymous deep links' tests/authRouting.test.mjs` | 跳过地址归一化后，实际 URL 为 `/instances`，断言期望 `/login`。 |
| `scope-switch` | `node --test --test-name-pattern='account changes' tests/authRouting.test.mjs` | 跳过登录作用域切换后，实际 scope 为 `anonymous`，断言期望用户 1 的作用域。 |
| `cached-login` | `node --test --test-name-pattern='cached Router' tests/authRouting.test.mjs` | 将登录路由错误地导向实例页后，再次登录显示实例页，断言期望总览。 |

每次检查后移除环境变量，再执行正常测试；正常运行结果为上文的 8/8。

## 依赖核对

新增 jsdom 与显式 esbuild 开发依赖，测试脚本保持 `node --test tests/*.test.mjs`。逐项比较 HEAD 与当前 lockfile，原有非开发 package 的 version 和 integrity 均一致。
