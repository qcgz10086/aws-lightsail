# T05 返修独立复审

**产品验证通过；正式交付证据待核，T06暂未放行。** 绑定产品HEAD `33db3e4f6b04653b93d60e9e88f53d11d785ec94`，tree `9d4dd144c4fc3c6d426fd03c835b63099f5b47fd`，父提交 `bcb2da62c8c2c1fd171c1c4673165c1a2d27969d`，项目BASE `234d1a91e8e24934d2730b4a5a88c8205ac1f649`。正式docs-only交付HEAD尚未提供，因此完整T05交付此刻为“证据不足”，不等于产品返修失败。

## 范围与方法

从指定Git提交导出原始字节到本报告目录`source/`，独立构建到`frontend-build/`，依赖复用T00固定node_modules。核实BASE为祖先；返修只改CreateInstance、Dashboard和V1.0的T06代理验收条款。整个T05 BASE→HEAD仅frontend及计划修改，backend/tools/部署零变更。本次不冒称重跑后端已审T04；最终集成重新全量覆盖。

复现脚本位于`.audit/T05-preparation/verify_candidate.py`、`probe_query_cache.mjs`、`probe_api_cache.mjs`、`browser_probe.mjs`、`browser_catalog_probe.mjs`、`compare_bundle.mjs`。浏览器使用独立生产构建、Chrome157.0.8103.79、本机合成API，拒绝非本机来源；创建接口返回400拦截，区域启用仅fixture。没有真实AWS、产品代码修改、合并、推送、部署、子代理或定时后台监控。

## 独立结果

| 检查 | 结果 / 证据 |
|---|---|
| Git身份、导出与命令 | `independent-verification.json`，所有命令exit0 |
| Node缓存测试 | 9/9，`cache-tests.log` |
| TypeScript / Vite生产构建 | 通过，`typescript.log` / `build.log` |
| 缓存确定性额外探针 | 4/4，`query-cache-probe.json` |
| 真实client.ts Axios适配器 | 4/4，`adapter/result.json` |
| 原浏览器场景 | 11/11，`browser/browser.json`，pageErrors0 |
| 扩展目录/状态场景 | 12/12，`browser-catalog-poll/browser-catalog.json`，errors0；含最初10项及2个启用/轮询用例 |
| 渲染核对 | `browser-catalog/dashboard-stale.png`：范围、过期/不完整提示、错误及最近成功时间可见 |

## 原阻断关闭

- **F01 / 高 / 已关闭：** 凭证或区域变化先清除下游表单、目录、平台和ready scope。慢未预热凭证、目录失败、缓存切回及地区目录失败均不能提交旧选择。探针主动dispatch原生form submit绕过按钮，实际POST仍为0；新目录完成后显式重选只发新credential/region/blueprint/bundle及该镜像platform。onFinish还核对creds、regionsCredentialScope、readyCatalogScope和成员关系，不只依赖disabled。
- **F02 / 高 / 已关闭：** regionIssue持有credentialId/catalogSeq；status、enable、poll均校验credential+region+目录代次，切换即时使旧代次无效。旧A DISABLED响应不再显示于B，未发错误B/us-east-1启用；旧A enable成功不清B目录；10秒定时poll已进入transport后切B，迟到响应也不清新选择。正常同scope启用后可加载并提交WINDOWS目录，证明没有通过禁用全部行为掩盖问题。
- **F03 / 中 / 已关闭：** Dashboard保留scopes并展示失效范围、错误、最近成功时间。stale旧1有明确不完整提示；no_permission冷失败和region=null目录失败均显示“—”及错误，不冒充0；同响应实例页对照保持正确。

其它回归：冷快照轮询到业务数据、慢流量不阻塞实例、总览→实例额外实例HTTP0、账号GET晚到、实例操作/流量同步/账单刷新旧回调、目录迟到、退出登录旧auth/me、首次登录竞态、指标week→month迟到均通过。

## 性能及非阻断建议

同依赖静态闭包，`bundle-comparison.json`：BASE入口1,732,502B/gzip535,739；候选入口786,999B/gzip258,437。Dashboard闭包930,297B/gzip306,683；全JS总量1,751,343B/gzip557,371。初载显著减小，但共享787k chunk警告仍在、全JS总量略增，作为非阻断建议。没有将这些字节结果充当浏览器P95、容器镜像或内存验收。

## 待验证与最终集成保留

正式交接、可重跑runner、产品/证据文件索引、Git原始字节hash与交付HEAD尚待核验；收到后再作T05最终结论和T06门禁决定。Linux、生产容器、CPU/内存、固定mock同环境配对P95、长期清理与T06持久任务仍未在本轮验证。

另有**继承高严重问题**登记T06/最终集成必修：`backend/app/services/lightsail.py:588`建机默认AZ子步骤get_regions漏proxy_url。独立证据`.audit/T05-bcb2da6/create-proxy-probe.json`，实际工厂第二client配置direct，transport0。它未由T05引入，未混进前端修复；V1.0已补T06专项验收要求。此记录不表示该后端缺陷已修复或可用于最终项目放行。

**当前F01–F03产品返修通过，完整交付暂为证据不足；T06等待交付核验。**
