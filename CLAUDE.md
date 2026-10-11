# Agent 指引

本文件刻意保持轻量。细节放在 `docs/` 下，作为单一事实来源，`CLAUDE.md` 只做指针（沿用 `~/spear/CLAUDE.md` 的组织方式）。

- [`docs/agents.md`](docs/agents.md) — SPEAR 编程模型 + 本项目硬规则。**每次会话必读。**
- [`docs/agents.user.md`](docs/agents.user.md) — 本机环境事实（路径、显存、命令、进程注意事项）。**每次会话必读。**
- [`docs/agents.style_guide.md`](docs/agents.style_guide.md) — 写代码或审 diff 前读。
- [`docs/rendering.md`](docs/rendering.md) — 采集链路、与 SPEAR 示例工程的画质/流畅度对比、显存取舍。
- [`docs/render_budget.md`](docs/render_budget.md) — 显存与帧时间的实测杠杆、分级降载路线（改渲染参数或场景/材质前先读）。
- 项目既有文档（[`docs/architecture.md`](docs/architecture.md)、[`docs/decisions.md`](docs/decisions.md)、[`docs/urdf_workflow.md`](docs/urdf_workflow.md) 等）与 `agents*.md` 冲突时，前者为准：它们是被用户批准的决策。

`.claudeignore` 与 `.gitignore` 相互独立，不要同步。其用途是把 agent 挡在大型二进制/第三方/生成树之外（`ue/Content`、`ue/**/Binaries`、`*.uasset`、`assets/**/*.stl`、缓存目录）。即使工具本身不强制 `.claudeignore`，也不要读、不要改这些路径里的文件；需要了解资产内容时，走 Unreal Editor 的 Python API 或本项目 `tools/` 里的检查脚本。

对其他开发者或后续 agent 有用的知识，写进已入库的 `docs/`，不要留在会话记忆里。个人偏好（沟通语言、本机路径）才放 `docs/agents.user.md`。

上游权威原文（本项目只做适配，不改它的含义）：

- `~/spear/CLAUDE.md`
- `~/spear/docs/agents.md` — SPEAR 编程模型
- `~/spear/docs/agents.mcp_usage.md` — 像素优先、MCP 工具、渲染与空间推理
- `~/spear/docs/agents.style_guide.md` — 代码风格
- `~/spear/docs/agents.user.md.example` — 本机路径类偏好的模板
