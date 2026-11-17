# T00 容器原始证据与制品索引

本地 `container-verification.json` 是复核摘要投影：包含两批次身份、资源和首访页面数值、四个镜像 ID、原始文件/归档 SHA256 及 14 项本机工具指纹复核。完整逐样本 API、页面阶段、资源窗口、Docker inspect、构建日志和输入文件指纹保存在下面的 Instabox 归档，未全部复制到 Windows。本地摘要不能替代原始文件复核。

## 保存位置

- Instabox 机器：`m_agqymokr64sggvjgxypiphnoke`，名称 `instabox`。
- 持久磁盘目录：`/data/lightsail-t00-revision1`；该目录为本轮任务独立目录。
- 原始 BASE：`container-base-delivery/`，run_id `8cc5dfcd-2b69-4dc6-a45f-fbd787faa7ab`。
- 原始候选：`container-candidate-delivery/`，run_id `963b5736-2736-4a15-b44b-2e65fc5c5d34`。
- 完整复核脚本：`verify_evidence.py`，SHA256 `0a1661b3f8a6510839bdab7b4d696a6103f38fd04e37344c466e5c43f4202d33`；与本地同名脚本字节一致。

```text
/data/lightsail-t00-revision1/container-evidence-delivery.tar.xz
/data/lightsail-t00-revision1/production-images-delivery.tar
/data/lightsail-t00-revision1/container-verification.json
```

| 文件 | 字节数 | SHA256 |
|---|---|---|
| container-evidence-delivery.tar.xz | 270572 | 8d1b0a38d49bbe03217c06b334202c883c9ba88c65627d1c202be507f902f5aa |
| production-images-delivery.tar | 177861120 | ba0c678b597c1845a5bea299de5e9ccf047c456ae43ec6bbdc49ce188f83583b |
| container-verification.json（远端完整版本） | 30225 | 72b99ad749a7ecd8c00deb420ebf708d941f834f164b7338ebe392e8e469f267 |

原始采样 SHA256：BASE `9b0852201b78d3fa9f00087f2592f9d54ac23e68f51f91f7ffb2091dc6ab9146`；候选 `8e3a7b7dc616b1295ff6aa6e1f490af74a83003ce0ee9543b44083bf234d4557`。分别有 3806 / 3669 条采样。镜像归档的字节数是归档存储量，不能替代镜像未压缩逻辑大小 677126001 字节。

## 复核方法

通过 Instabox 获取机器状态；若暂停，先恢复。可用 Instabox 的 read_file 读取完整复核 JSON、run/summary/environment/images/image-inspect/cleanup 文件；原始证据 tar.xz 小于 read_file 的 1 MiB 上限。较大镜像归档用 Instabox 文件复制能力下载。不要删除机器或本目录，复核所需原始文件尚在持久磁盘中。

在机器上执行以下只读证据复核（脚本写出新的相同口径复核 JSON，不重新运行工作负载）：

```bash
cd /data/lightsail-t00-revision1
sha256sum container-evidence-delivery.tar.xz production-images-delivery.tar
/data/lightsail-t00-revision1/driver-venv/bin/python verify_evidence.py containers /data/lightsail-t00-revision1
```

该脚本独立用 median / nearest-rank 复算 60 组延迟统计；用原始 JSONL 加 summary.windows 的实际起止采样复算 8 个资源窗口；核对每次 240 API / 180 页面、零错误、completed、退出 0、run_id 一致和清理无错误；核对 58 个产品文件、14 个工具文件一致，以及镜像 Config、RootFS、Size、架构、OS 和 Created 一致。未用测量工具的统计函数代替复核。

镜像索引 ID 不同，分别保留；docker inspect 同时显示不同的构建 Ref/CreatedAt 标识。完整 inspect 文件位于每个运行目录，不能只用标签认定内容相同。镜像归档包含四个标签对应制品；只需审读时不必运行镜像。

测量结束后额外用 --network none / --read-only 的一次性容器核对版本：两个后端镜像均输出 Python 3.12.16，前端镜像输出 nginx/1.27.5。驱动环境的 Python 3.12.2 不是后端镜像解释器版本。该额外探针记录在本地摘要 image_runtime_probe 中，未回写此前原始归档。

## 运行与导出边界

本机拥有完整 Git Worktree。Instabox 先克隆公开 BASE，再导入原审计候选提交及必要树对象供定向导出；目标 backend/frontend 树和所有产品文件与 BASE 一致。Instabox 该副本未导入候选的全部无关 docs/tool blob，不能当作完整候选仓库进行代码审计。目录导出以 `commit:backend` 和 `commit:frontend` 读取精确目标树，避免无关文档缺失阻止构建。archive 哈希是该次导出制品哈希，tree 导出时间会影响 tar 字节；源码身份由完整提交及逐文件内容哈希共同证明。

基础镜像摘要、固定 Dockerfile、构建日志保存在每批次的 base-images.json、images.json、image-inspect.json 和 build-*.log。镜像从原生产 Dockerfile 构建，运行时挂载只读 fixture/psutil、一次性 SQLite 和临时 Nginx 统计配置。测量期仅两个容器，内部网络无对外端口；客户端走宿主机本机转发与浏览器来源代理。结束清理本次容器、网络及临时运行目录，保留镜像和审计证据。

早期失败/诊断目录亦留在任务目录，未计入最终统计。原生旧批次留在 Windows Worktree 的 `.baseline-runs/t00-revision1-diagnostics/`；正式本地结果仅用 `native-delivery/`。
