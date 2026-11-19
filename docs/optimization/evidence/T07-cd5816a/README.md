# T07 证据结构

产品/测试/工具固定于 `cd5816aa0ab29ca9d692537845687b1bc7060333`。本目录交付文件须按原始字节保存，`.gitattributes` 禁用文本换行转换；索引绑定每个文件大小与SHA256。

- `product-source.tar`：135个 backend/frontend/tools 文件直接来自 `git cat-file blob`。`source-manifest.json`列逐文件SHA，`source-archive-verification.json`重新读取Git blob逐字节核验135/135。
- `local/`：空node_modules的npm ci/test/tsc-build、后端测试、严格前端页面11/目录12/任务7、客户端意图5、前端缓存容量与取消结果。`source-equivalence.json`证明前端子树自64b7dc9不变，后端app自5df7967不变，避免将复用结果写成重新执行。
- `diagnostics.tar.gz`：小样本preliminary-1至4及formal-2代理失败原批的原始日志、browser、环境、镜像和cgroup记录（不包含工具依赖和runtime副本）。
- `formal-1.tar.gz`：64b7dc9两版完整20样本及汇总，用于保留已独立复核但非最终迁移修复版本的历史记录。
- `image-initial-complete.tar.gz`：早期镜像测试与回退，含e805db8缺requirements挂载失败、64b7dc9真实原库升级失败、5df7967修复通过和临时数据库；旧注入探针不能证明v5注入执行，最终工具增加显式断言。
- `final-cd5816a.tar.gz`及`final-contents.json`：最终formal-3两版完整样本、实际镜像294/52项无跳过测试、显式v5注入回退及数据库/回执。关键汇总另复制为final-*.json供直接阅读。

所有AWS凭据、口令和数据库均为一次性固定测试数据。容器输出中出现的fixture配置不得当作生产配置。`service_may_resume=false`是回退演练的预期安全结论：旧库没有任务锁，未知操作仍须核对；旧镜像只在隔离写入门禁下检查页面和只读API。

JSON中的绝对路径指向执行环境当时的位置；日志SHA和Git blob字节是复核依据。cgroup工作集按同次memory.stat的inactive_file计算；峰值为100ms采样峰值。所有20样本保留，失败批不并入成功统计、不删掉慢样本。
