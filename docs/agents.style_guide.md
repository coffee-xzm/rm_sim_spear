# 代码风格（本项目适配版）

> 上游权威原文：`~/spear/docs/agents.style_guide.md`。本文只保留与本项目相关的部分，并显式记录**哪里与上游不同**。总原则仍是上游第一条：**Local Style First** —— 就近代码就是规范；拿不准时，看被改文件的主导写法。

## 1. 通用

- **本地风格优先**：匹配所在文件的命名、空行、调用习惯。
- **最小 diff**：一次只改达成目标所必需的东西；提交前用 `git diff` 自查。
- 优先简单直接的代码，不引入只为"看起来整洁"的辅助函数或临时变量。
- **不删既有诊断日志**（cvar 回读、warning 计数、超时提示）。要加就加。
- 日志噪音保持克制：除非任务需要，不额外加调试输出。
- 新建通用工具函数后，**同一次改动**里把相关旧函数收敛成薄封装（上游 `style_guide:43`）；否则容易出现"同一件事三套实现"，本项目采集路径已经吃过这个亏（见 [`agents.md`](agents.md) 第 9 节）。
- 数学表达式：`*`、`/` 两侧不留空格，`+`、`-` 两侧留空格（`a*b + c`、`x/2.0 + y`）。新代码照此写，尤其是矩阵/变换代码。
- 优先 `diag(...)` 对角矩阵做逐轴缩放，不做逐元素赋值。
- 文件路径统一用 `pathlib.Path`（本项目主流）；不用 `os.path.join` 拼字符串。
- 不新增 `# noqa` / `# type: ignore` / `# pylint: disable`。仓库里已有的 22 处 `# noqa: BLE001` 属于历史写法（多为"进程表竞态/纯诊断"场景），新代码不要模仿：要么修掉根因，要么安静地忽略工具提示。

## 2. Unreal / SPEAR 调用约定（上游强制，本项目同样适用）

- UFUNCTION 一律用**关键字参数 + Unreal 精确大小写**：`comp.SetStaticMesh(NewMesh=...)`、`actor.K2_SetActorLocationAndRotation(NewLocation=..., NewRotation=...)`。
- UPROPERTY 读用 `.get()`，写直接赋值：`actor.RootComponent.get()`、`component.Width = 1024`。
- `as_dict=True` 的字典键与 UFUNCTION 形参名一致，非 void 返回值带 `"ReturnValue"`。
- 枚举用不带命名空间限定的字符串：`"DoubleBuffered"`、`"SCS_FinalColorLDR"`、`"Movable"`。
- 传入的 `FVector`/`FRotator` 字典键大写（`{"X": ..}`），UFUNCTION 返回的键小写（`{"x": ..}`）；上游 `agents.mcp_usage.md:167` 有完整说明。
- `spear.*` 函数也用关键字参数（`variadic` 的 `entry_point_caller.call_on_game_thread` 除外）。
- 改动 `~/spear` 内的文件时，**完整**遵循上游风格：两个版权头、C++ include 分组与注释对齐、`WITH_EDITOR`/`WITH_EDITORONLY_DATA`、IWYU、`Std::containsKey/insert/remove`、`_RenderThread` 后缀等。改本仓库的文件不需要 SPEAR 版权头。

## 3. 本项目 Python 风格（与上游的差异点）

| 主题 | 上游 (`~/spear`) | 本项目 |
|---|---|---|
| 类型注解 | 禁止 | **使用**，并在模块首行 `from __future__ import annotations` |
| docstring | 简短 | 英文 docstring，解释"为什么存在"（对比替代方案） |
| 文档语言 | 英文 | `docs/*.md` 用中文；代码、注释、docstring 用英文 |
| argparse | 不传 `description=`/`help=`，用 `assert args.x in [...]` 代替 `choices=` | **允许并鼓励** `description=__doc__`、`help=`、`choices=`；解析后保留显式校验 |
| 除零/常量 | 优先 `/2.0` 而非 `*0.5` | 同上游 |
| 文件路径 | `os.path.realpath(os.path.join(...))` | `pathlib.Path` |

公共约束（两边一致）：

- 公开 API 用 `__all__` 声明；dataclass 承载结构化数据；`if-return-else-return`（显式 `else`）而不是两条 `return`。
- Python 里 `isinstance` 分派链把最一般/兜底分支放最后。
- 在 `__init__.py` 中按类别、字母序排列导入。

## 4. 命令行工具与日志

- 每个 `tools/*.py` 的 stdout 是**机器可读的 JSON 事件流**：`print(json.dumps({"event": "...", ...}, ensure_ascii=False), flush=True)`。人类可读的补充信息才用普通 `print`。
- **运行子进程前先记录完整命令**（上游 `style_guide:33`）；**写文件后记录输出路径**（`style_guide:34`）。本项目已有：`tools/*` 把 `output_dir` 打进事件里，继续照做。
- 退出码/异常要显式：工具失败时打印带 `"event": "error"` 的 JSON 并 `raise`，不要静默返回 0。
- 诊断信息尽量带上下文（哪个 cvar、哪个 link、哪一帧），便于事后只读日志定位。

## 5. 测试

- 测试放 `tests/test_*.py`，用 `unittest`。**离线可跑**是硬要求：依赖 UE/GPU 的代码不能进默认路径（用 import 守卫或参数跳过）。
- 全量离线套件：

  ```bash
  cd ~/rm_sim_spear && PYTHONPATH=. python3 -m unittest discover -s tests
  ```

- 依赖 MuJoCo 的测试用项目环境：`/home/coffee/miniconda3/envs/spear-env/bin/python`。
- 新增行为先补能失败的测试（或最小复现命令），再改实现；测试名说明"防的是什么回归"，不要叫 `test_1`。

## 6. 文档与引用

- 文档里引用函数/符号/行号前，**先 grep 确认它真的存在**（上游 `style_guide:44`）。不要编造示例名字。
- 引用代码用 `path:line` 或 `path:line-line`，便于跳转核对。
- 文档只写"结论 + 判据 + 复现命令"；把过程性的探索留在测试或工具里，不要留成口述历史。
