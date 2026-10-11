# Agent 指引：SPEAR 编程模型 + 本项目硬规则

> 本文是 `~/spear/CLAUDE.md` 与 `~/spear/docs/agents*.md` 针对本项目的适配版。上游原文是权威定义，本文只做两件事：把与本项目相关的部分具体化，并记录**每条规则对应我们已经踩过的哪个坑**（第 9 节有汇总表）。
> 相关：[`agents.user.md`](agents.user.md)（本机环境）、[`agents.style_guide.md`](agents.style_guide.md)（代码风格）、[`rendering.md`](rendering.md)（采集链路与示例工程对比）、[`architecture.md`](architecture.md)（分层决策）。

---

## 1. 像素优先（最高优先级，等价于上游的 CRITICAL 规则）

上游 `agents.mcp_usage.md` 第 3 行的原文要求：**永远不许用 actor 名字来解析"用户在看什么/指的是什么"；必须从渲染出的像素出发**，先在像素上定位，再用分割图反查 actor/component。违反这条即视为任务失败。

本项目没有 MCP 的 `get_viewport_data`，落地方式：

1. 每轮涉及视觉/绑定/位姿的判断，先出图：`tools/run_physics_sim.py`（固定机位，落 `frame_%04d.png`）或 `tools/test_urdf_views.py`（主视角 + 绑定相机）。
2. 用 `read_image` 真的看图，而不是读日志里的 tag/RPC 返回值。
3. 需要"这个像素是谁"时，用 SpFuncs 的 `segmentation_id_image` + `world_position` / `depth_meters` 反查（契约见 [`spear_metadata.md`](spear_metadata.md)）；不要靠 `find_actors_by_name` 猜。

**推论（同样重要）**：任何"绑定成功 / 几何正确 / 机器人落地了"的结论，都不能只用 tag、`geometry_check.ok`、RPC 返回值或 `trace.csv` 数字来支撑——必须有一张能看出该结论的图。我们正是用 `geometry_check.ok` 宣布过成功，而画面里机器人悬在半空。

## 2. 帧事务 `begin_frame` / `end_frame`

- 每个 `with instance.begin_frame():` 后面**必须恰好跟一个** `with instance.end_frame():`；不得嵌套、不得乱序、不得连续两个 `begin`。
- `end_frame(single_step=True)` 会让游戏线程推进到下一帧开头**然后等下一笔事务**;阻塞点在帧首 `OnBeginFrame`,早于 Slate 输入和 `GEngine->Tick()`,所以**一开它就整个编辑器冻住**。本项目**不用它**;严格模式只做"暂停 + 事务内解暂停/暂停",事务用普通 `end_frame()` 收尾([`architecture.md`](architecture.md) §4.1)。`instance.step(num_frames=N)` 用于让渲染/TAA/物理稳定。
- **所有 UFUNCTION/RPC 调用都必须在事务内**。事务外调用会污染 SPEAR 的帧状态。
- `instance.get_editor()` 只在编辑器世界有效；standalone（`-game`）下调用它是错误，只能用 `instance.get_game()`。

## 3. 采集路径（本项目认定的唯一正确做法）

- 传感器一律用 `/SpContent/Blueprints/BP_CameraSensor.BP_CameraSensor_C` 上的 `USpSceneCaptureComponent2D`：
  `CaptureSource="SCS_FinalColorLDR"`、`bAlwaysPersistRenderingState=True`，尺寸/FOV 用 `viewport_service.align_camera_with_viewport(...)` 对齐。注意这条路径的 FOV 来自 **viewport**（standalone `-game` 下是 UE 默认 90°，相当于 1/2.3" 传感器上的 2.8 mm 镜头），**不是**机器人相机的镜头，而且从不设置景深；物理镜头模型（焦距/传感器宽高/光圈/对焦距离 → `FOVAngle` + UE CircleDoF）在 [`lens.py`](../rm_sim_spear/lens.py) 与 [`rendering.md`](rendering.md) §13。`--view bound`（`cam_link_8` 上那台相机）自 2026-10-08 起默认套 `ROBOT_CAMERA_LENS`（8 mm + 1/1.8"，4:3 下 47.8° + CircleDoF）；`fixed`/`main` 调试机位仍用 viewport 投影，`--robot-lens` 可强制套上，`--lens-fstop`/`--lens-focus-cm` 覆盖光圈与（仍属假设的）对焦距离。
- **必须把后处理拷到采集组件**：`_copy_viewport_post_process(component, viewport_desc)`（关卡后处理体积）或 `_copy_player_camera_post_process(game, component)`（`PlayerCameraManager` 的混合结果）。只 spawn 不拷后处理，曝光/环境光就与编辑器视口不一致。
- 采集前设置 `r.Streaming.FramesForFullUpdate=0`、`r.Streaming.FullyLoadUsedTextures=1`，否则脚本相机里薄几何/贴图停在低 mip，看起来是"虚线 + 糊"。
- 初始化后先 warm 3–4 帧再存第一帧（上游注释：让 TAA 累积信息，"the rendered image will look better"）。
- 读像素走 `component.read_pixels()`（SpFuncs）。**不要再走 `SpearRenderPort`**：它要求 `bind_frame_metadata`/`readback_metadata`（`run_id`/`commit_id`/`ue_frame_id`），standalone 下没有 metadata 组件，必然失败（[`spear_metadata.md`](spear_metadata.md)；实现见 `rm_sim_spear/` 与 `docs/risks.md`）。
- 显存是硬约束：`r.Streaming.FullyLoadUsedTextures=1`、超采样、多路 SceneCapture、Lumen/RT/VSM 互相抢预算。**低显存预设只能用于"跑通逻辑"，不能用于画质验收**：关掉 Lumen/RT/VSM 后，合并 CAD 场景在脚本相机里近似全黑（实测灰度均值 9.4/255），细线也会因为贴图停在低 mip、内部分辨率被压而断成虚线。另注意 `r.ScreenPercentage` 对 SceneCapture **无效**（UE 5.8 直接给 `FLegacyScreenPercentageDriver(1.0)`，见 [`urdf_workflow.md`](urdf_workflow.md)），它只影响视口；把它写进低显存预设换不来捕获侧收益。取舍与实测数字见 [`rendering.md`](rendering.md)。

## 4. 名字解析

- `find_actors_by_name` / `getStableName` 匹配的是 **stable name**，不是 label：standalone `-game` 下 stable name 就是 UObject 名；编辑器/PIE 下是 `resolveStableName`（label + folder path）。实现见 `~/spear/cpp/unreal_plugins/SpCore/Source/SpCore/UnrealUtils.cpp:1280`。
- 因此同一个 actor 在 `-game` 与 PIE 下可能"找不到"或"找到别的"。跨模式不要假设一致。
- 脚本 spawn 的 actor 必须 `unreal_service.set_stable_name_for_actor(actor=..., stable_name=...)`，否则之后只能按 class/tag 找。

## 5. 物理与坐标（本项目约定）

- MuJoCo 是物理真源，UE 只是可渲染镜像（UE 侧刚体物理关掉，仿真行为不变）。
- 单位：MuJoCo 用米，UE 用厘米，只允许在边界上 `×100` / `×0.01`。
- 坐标：`p_ue_cm = 100 · diag(1,−1,1) · p_mj_m`，`R_ue = D · R · D`；MuJoCo 自己的地面是**模型原点处一块不可见的平面**，而关卡地面实测在 z ≈ −137.9…−140.5 cm（俯视网格，见 [`rendering.md`](rendering.md) 第 10 节），所以 `world_offset_cm` **默认 `0 0 -140`**——它等价于"关卡地面在哪"。填 −160 机器人埋进地面约 20 cm；而且关卡地面本身不是平面（实测 −124…−140.5），单一 offset 只能是折中。
- 刚体推进参数：`drive_gain ≤ 5`、`joint_armature ≥ 0.05`；再陡就会出现 `WARNING: Nan, Inf or huge value in QACC`，trace 里表现为 `z` 在 −9059 cm 与地面高度之间跳变。任何 drive 参数改动后先跑离线单调性检查（`tests/test_mujoco_physics_options.py` 的模式）。
- 落地判据是"轮子接触地面后的 z"，不是"看起来像站着"：本项目实测静止时 base_link 在平面之上 ≈7.3 cm（`z ≈ −132.7 cm`，地面 −140，轮半径 6.01）。`simple_collision` 的轮子圆柱必须按**滚动面**取半径，不能拿 `geom_aabb` 并集（那是每个 geom 各自的坐标系，会退化成"最大的单个零件"，本项目是 55.5 mm 侧板而不是 60.1 mm 滚子）——见 `mujoco_driver.wheel_cylinder`。

## 6. 验证与交付

- **改动之后必须重新渲染并看图**（上游："Verify by rendering … Don't declare success from metadata alone."）。
- 工具产物必须落盘并打印/记录路径：`trace.csv`（数值）+ `frame_%04d.png`（画面）。验收时两者一起看。
- 不删已有的诊断日志（上游明令）；要加日志就加，别把之前排障用的 cvar 回读、warning 计数删掉。
- 新发现（cvar 语义、坐标偏移、显存峰值、命名不一致、某个 UE 版本的行为）写进 `docs/`，不要只留在会话记忆里。项目文档滞后于代码时，先补文档再改下一处。

## 7. 与本项目无关的上游规则

上游 `agents.mcp_usage.md` 里有一部分是 **MCP 沙箱专属**，我们的 CLI 工具不适用：

- "禁止 import / 禁止使用 `os`、`sys`、`pathlib`"、"禁止自己 `spear.Instance()`"、"必须用 `spear.log`" —— 这些约束来自 MCP 工具的 exec 命名空间。我们的 `tools/*.py` 是独立进程脚本，用 `print`/JSON 事件写 stdout、用 pathlib 处理路径是正确做法。
- 我们的 `agents.user.md` 记录本机命令与路径；上游的 `agents.user.md.example` 只是模板。

## 8. 每次会话的最小检查清单

1. 读 [`agents.user.md`](agents.user.md)：路径、显存、运行命令、进程注意事项。
2. 判断"用户在看什么/改什么"之前，先出图并看图（第 1 节）。
3. 写代码前读 [`agents.style_guide.md`](agents.style_guide.md)；改 `~/spear` 里的文件前，额外读上游 `~/spear/docs/agents.style_guide.md`。
4. 结论必须能落到"图 + 数值 + 可复现命令"三件套上。

## 9. 规则 ↔ 已发生问题的对照表

| 上游指示 | 我们踩过的坑 | 若早遵守会省掉什么 |
|---|---|---|
| 永远从渲染像素出发，禁止用 actor 名字解析用户所指（`agents.mcp_usage.md:3,77-83`） | ① `findActorsByName("SM_MERGED_blender_for_ue5_node_22")` 找不到 → 回退 `runtime_spawn` → rune 被渲染两遍；该 actor 在 `-game` 下 UObject 名其实是 `StaticMeshActor_998`。② 机关绑定按 `armor_tune_node_4` 这类 UObject 名匹配，关卡里 label 是 `前哨站_body36_2/37_2/38_2/398_2` → 绑错/绑不上。③ 用户报"只有一个 UE 默认圆球"、"机器人在半空中"、"左半边比右半边清晰"时都在用名字和元数据推断 | 至少 4 轮返工；重复渲染的 rune；错误机关 actor 残留 |
| "Verify by rendering. Don't declare success from metadata alone."（`agents.mcp_usage.md:88`） | 用 `geometry_check.ok`、tag 清单和 RPC 返回值宣布"绑定成功"，而画面里机器人悬空、帧近全黑 | 一轮就要看到的"悬空 + 黑帧" |
| 每次用户新 prompt 先重新取视口数据（`agents.mcp_usage.md:64`） | 反复假设"用户看到的是我上次那个视角"，用户不得不重复描述（"现在我看自己创建的…"） | 澄清往返 |
| 场景元数据不可信：方向/距离从像素算（`agents.mcp_usage.md:84-86`） | "远一点虚线"、"机器人半空中"两轮都在猜相机/几何而不是量 `world_position`/`depth` | 两轮 |
| `spawn_actor(rotation=...)` 会与类默认旋转**复合**（如 `ASpotLight` 默认 `Pitch=-90`）（`agents.mcp_usage.md:121`） | 相机/灯朝向反复对不上（我们后来改成显式 `K2_SetActorRotation`） | 朝向调试 |
| 每个 `begin_frame` 恰好一个 `end_frame`；RPC 必须在事务内（`agents.md:7`） | 事务外调用 `_set_camera_look_at` → `_validate_frame_state_for_game_thread_work: assert False`，整帧状态被污染，只能重启进程 | 一次进程级失败 |
| standalone 只有 `get_game()`，`get_editor()` 是错误（`agents.md:6`） | `_get_game_world` 在 `-game` 里调 `get_editor()` → `assert False` | 一次启动失败 |
| spawn 后设置 stable name（`agents.mcp_usage.md:136-139`） | 运行时 spawn 的回退 actor 没有 stable name，后来按名字找不到，落到原点、重复渲染 | 重复 actor |
| 不删诊断日志（`agents.style_guide.md:60`） | 排障用的 cvar 回读/warning 计数被当成噪音忽略过 | 定位显存/崩溃的线索 |
| 面向其他人的知识写进 `docs/`，不进个人记忆（`~/spear/CLAUDE.md:12`） | STL→OBJ 的 Y 镜像、stable name 语义、`r.ScreenPercentage` 对 SceneCapture 无效、−160 cm 地面偏移、显存预算等事实在会话里反复重新推导；`docs/risks.md`/`implementation.md` 一度落后于代码 | 多处重复调研 |
| 新建通用工具函数后立刻把相关旧函数收敛为薄封装（`agents.style_guide.md:43`） | 采集路径分叉成三套（main/bound/fixed），只有一套拷了后处理，导致"同一场景三套画质" | 画质不一致 |
| `.claudeignore`：不读/不改二进制与生成树（`~/spear/CLAUDE.md:10`） | 在 `.uasset`/Content 路径上猜名字、猜目录结构；`ue/Content` 是 submodule 且已有 21 处本地改动 | 无谓的搜索与误改风险 |
