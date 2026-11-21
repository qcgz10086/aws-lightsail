# 冷登录首屏优化

本轮针对 PageSpeed 报告中的首屏 CSS 阻塞、未使用 JavaScript 和认证请求依赖链优化登录入口。

| 登录页资源 | 优化前 8e1a7c7 | 优化后 |
| --- | ---: | ---: |
| 必需 JavaScript 原始大小 | 262,303 B | 152,993 B |
| JavaScript gzip 6 | 89,253 B | 50,023 B |
| 首屏外链 CSS 请求 | 1 | 0 |

gzip JavaScript 减少 39,230 B，约 44%。1,472 B 的初始 CSS 内联到构建 HTML，保留原 CSS 资产供后续依赖使用。登录前只下载 React、登录表单和轻量认证代码；React Router、Axios、后台布局及业务页面在认证成功后加载。

认证请求使用原生 fetch，保留 HttpOnly Cookie、120 秒超时、取消、错误提示及同会话 401 处理。认证和后台查询共用同一个缓存作用域；切换账号清除缓存和任务提交记录。首屏查询预取在动态加载 API client 后再次确认会话和取消状态。

已登录直链保留原路径及查询参数。匿名访问后台地址转到 `/login`，登录后进入总览；退出及再次登录时，界面和路由保持一致。

验证结果：前端测试 34/34 通过，包含 10 项认证、8 项真实 DOM 集成、7 项 CSS 构建及 9 项缓存测试；TypeScript 与生产构建通过；产物预算 5 项检查通过。DOM 测试实际挂载 App、AuthProvider、ReactDOM、Router 和 AntD，连接本地 HTTP 夹具。变异检查确认地址归一化、再次登录导航和会话隔离错误会触发断言失败。

证据：

- `evidence/cold-login-before.json` 与 `cold-login-after.json`：入口资源和预算。before 的 JS 来自原入口，CSS 统计采集于内联插件已合入的中间构建；表中原 CSS 请求数依据旧部署 HTML 及 PageSpeed 报告。
- `evidence/cold-login-dom.md`：DOM 场景、正常退出、变异检查和依赖版本核对。
- `evidence/cold-login-browser-blocked.md`：浏览器执行环境记录。Chrome、Edge 启动失败，内置浏览器超时，本轮未取得新的 FCP、LCP 或 PageSpeed 分数。

认证请求仍在应用入口执行后发起，入口下载字节已减少。PageSpeed 报告中的 LCP 渲染延迟需要在可用浏览器环境重新测量。
