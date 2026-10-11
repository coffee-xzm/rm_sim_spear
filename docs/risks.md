# 风险与环境约束

> 相关:[`plan.md`](plan.md)、[`decisions.md`](decisions.md)

---

## 1. 本机环境实测约束

| 项 | 实测 | 影响 |
|---|---|---|
| OS | Ubuntu **22.04** | 对应 ROS2 **Humble** |
| ROS2 | 已装 `/opt/ros/humble` | — |
| ROS2 的 `rclpy` | 在 `/opt/ros/humble/local/lib/python3.10/dist-packages`,**仅 Python 3.10** | **不能直接在 SPEAR 的 3.11 env 里 import** |
| SPEAR | `requires-python = "~=3.11"`;`spear-env` 是 3.11.16 | — |
| MuJoCo | **未安装** | 阶段 0 需装 |
| UE | 目标 **5.8**;`~/spear` 当前 checkout 是 `ue-58` 分支(`main` 对 5.5) | 版本对齐 |
| GPU | **RTX 4060 Laptop，heap 8188 MiB**;驱动 memory budget extension 给出的 device-local 可用 budget 实测只有 **6358–6736 MiB**(桌面合成器等占 0.8–1.2 GiB) | 见 R13;启动显存预算比"8 GiB"少 1.4–1.8 GB |
| X 显示 | 真实会话在 `DISPLAY=:1`(可直连);`xvfb-run -a` 偶发丢失 X server,会让 UE 在启动阶段以 `Fatal X11 IO Error` 退出 | 该失败现由日志签名 `x11_fatal` 立即报告;关键运行优先用真实 display 或显式 `-n` 的 Xvfb |
| 版本交集 | **ROS2 主流发行版没有一个对应 Python 3.11**(Humble/Iron = 3.10,Jazzy/Rolling = 3.12) | 见 [`decisions.md`](decisions.md) D7 |

**结论**:ROS2 与 SPEAR 的 Python 版本冲突通过独立 Humble adapter 隔离,不再阻塞核心进程。

---

## 2. 风险登记

### R1 — 物理与渲染的几何漂移 ★

| | |
|---|---|
| **风险** | UE 的可视网格与 MuJoCo 的碰撞几何不一致 → 感知学到的几何与物理不符(sim2real gap 的主要来源) |
| **影响** | 高。这是"感知仿真"的核心正确性 |
| **缓解** | 单一描述源 → 两侧同源生成;**几何共注册断言**(同一 link 的 visual/collision 变换一致);CI 校验 |
| **指标** | 两边的 link 变换与包围盒在容差内一致 |

### R2 — 时间戳缺口 ★

| | |
|---|---|
| **风险** | SPEAR 的 `read_pixels()` 不带帧号/时刻,配合 0/1/2 帧缓冲延迟,图像来源无法证明 |
| **影响** | 高。数据集与闭环都会错 |
| **缓解** | 在 enqueue/readback 链路绑定 `commit_id`、`ue_frame_id` 和 `sim_tick`;严格采集只消费有元数据的图像 |
| **指标** | 图/位姿/IMU 能按 `commit_id` 无歧义配对 |

### R3 — 位姿同步带宽

| | |
|---|---|
| **风险** | 多机器人 × 多 link × 高频 = 大量 RPC(如 6 机器人 × 20 link × 100 Hz ≈ 1.2 万次/s) |
| **影响** | 中高。会吃掉帧时间 |
| **缓解** | 40 对象内用 `send_async`;超过则加**批量 UFUNCTION**(扁平 float 数组,一次应用全部 transform);只同步"可见且变化"的对象 |
| **指标** | 单帧同步耗时占帧预算 < 10% |

### R4 — 确定性

| | |
|---|---|
| **风险** | MuJoCo tick、UE 事务帧和 GPU 回读帧不是同一序列;Niagara/动画还可能使用变步长 |
| **影响** | 中高。数据集与回归测试依赖它 |
| **缓解** | 核心状态只按 MuJoCo tick 推进;事务带 `commit_id`;严格模式使用暂停 + 事务内解暂停/暂停(**不用** `single_step`——它阻塞游戏线程,会冻结编辑器,见 [`architecture.md`](architecture.md) §4.1);**禁止**用墙钟作为仿真时间 |
| **指标** | 核心状态和事件日志在相同输入下逐字段一致;图像按像素误差阈值验证 |

### R5 — MuJoCo 侧的限制

| | |
|---|---|
| **风险** | URDF 支持不完整(mimic/transmission);mesh 只吃 OBJ/STL;大场景性能 |
| **影响** | 中。RM 场地小,大场景风险低;URDF 转换是确定的工程量 |
| **缓解** | 统一的 URDF→MJCF 转换步骤 + 校验;必要时手写 MJCF |
| **指标** | 目标机器人 URDF 全部可加载且关节行为正确 |

### R6 — 高频控制 vs 渲染帧率

| | |
|---|---|
| **风险** | 1 kHz 的控制环被塞进 30–90 Hz 的渲染帧事务 |
| **影响** | 中高。会毁掉控制仿真 |
| **缓解** | 明确频率分工(见 `architecture.md` §4.4);高频环放 MuJoCo/算法侧;渲染帧只做同步 |
| **指标** | 控制环周期稳定,不随渲染抖动 |

### R7 — 弹丸

| | |
|---|---|
| **风险** | 高速穿模、确定性、命中精度 |
| **影响** | 中。**扫掠已解决 tunneling** |
| **缓解** | 独立 ProjectileSubsystem 使用 `QueryOnly` 扫掠球;命中输出 `HitEvent`;环数纯函数 + 单测 |
| **指标** | 见 [`projectile.md`](projectile.md) §6 |

### R8 — UE 版本与资产迁移

| | |
|---|---|
| **风险** | UE5.8 升级;`ue/Content/` 约 1.8 GB(LFS);材质/后处理重做 |
| **影响** | 中高。工期风险 |
| **缓解** | 先只迁 rune 子集,不动全量;里程碑式验证渲染 |
| **指标** | 目标场景在 UE5.8 下渲染正确 |

### R9 — ROS2 环境与进程边界

| | |
|---|---|
| **风险** | 见 §1;ROS2 adapter 与 SPEAR 不在同一进程 |
| **影响** | 中。有明确兜底 |
| **缓解** | D7 |
| **指标** | 标准 ROS2 节点可收发时钟、传感器和控制消息,QoS 能匹配 |

### R10 — 许可证

| | |
|---|---|
| **风险** | RM 场地/机器人模型版权;MuJoCo(Apache-2.0)、SPEAR(MIT)、ROS2(Apache-2.0) 各自合规 |
| **影响** | 中。影响发布 |
| **缓解** | 资产来源登记;发布前做合规清单;不自研大段受版权限制的资产 |

### R11 — "双时间源"漂移

| | |
|---|---|
| **风险** | UE 动画/Niagara/弹丸使用自身帧时间,与 MuJoCo sim time 不是同一时钟 |
| **影响** | 中高。表现为"偶尔对不上" |
| **缓解** | 只有 MuJoCo 状态进入核心快照;UE 独立效果只带 `ue_frame_id`;跨边界只用 `commit_id` 关联 |
| **指标** | MuJoCo/UE 镜像按同一 `state_revision` 对齐;独立弹丸不改变核心状态 |

### R12 — 多机器人

| | |
|---|---|
| **风险** | 命名冲突、传感器混淆、性能 |
| **影响** | 中(v1 单机可延后) |
| **缓解** | 按名索引;命名空间;实体注册表 |

### R13 — 显存预算与启动崩溃 ★

| | |
|---|---|
| **风险** | 本机 device-local heap 是 8188 MiB，但驱动给出的可用 **budget 只有 6358–6736 MiB，随桌面上其它进程浮动**。PIE 世界首帧渲染时一个 256 MiB 连续 transient 分配失败就会让 UE 硬崩(`VulkanMemory.cpp:3114`),而 Python 侧只看到 `rpc::timeout`。同样的参数在预算紧张时崩、宽松时通过,验收因此不可复现 |
| **影响** | 高。启动即崩;此前的缓解手段(`--max-internal-pixels` 只影响 6–25 MB 级的 SceneCapture target,而且现在只用于削减超采样、不再把目标降到输出分辨率以下)量级上就不可能有效 |
| **缓解** | 在**配置加载阶段**落低显存预设(`--low-vram` → `-ini:Engine:[SystemSettings]:`,实测峰值 7419→5263 MiB);启动前空闲显存 preflight 闸门与残留 UE 进程检测(`--kill-stale-editor`);每次运行采样 `peak_used_mb`/`min_free_mb`/`device_local_budget_mb`;解析崩溃签名并探测 UE 进程存活,立即失败而不是等 RPC 超时;**独立运行 `--standalone`(`UnrealEditor -game`)已实测可用**:去掉编辑器视口后 1440×1080 峰值 5984–6087 MiB,可保留 Lumen/光追/VSM |
| **指标** | 1440×1080 顺序捕获:`event: frame` 出现;`min_free_mb ≥ 1024`;`device_local_budget_mb − peak_used_mb ≥ 1024`;日志无 `Out Of Memory` 与 `VulkanMemory.cpp:3114`;`--verify-armor-links` 报告 `all_ok=true` |

---

### R14 — Nanite：池是预分配的，且蒙皮 + SceneCapture2D 会静默冻结动画

| | |
|---|---|
| **风险** | 三条已确认的事实，方向各不相同：① Nanite 的 streaming pool **是引擎 init 时预分配的，`r.Nanite.Streaming.StreamingPoolSize` 故意不是 `ECVF_Scalability`**，运行时改不了（Epic 员工 2026-03 在论坛确认，见 [`content_management.md`](content_management.md) 的外部依据）；② **fallback mesh 在 Nanite 开启时同样常驻 VRAM**，所以开 Nanite 不等于省掉 fallback 那一份；③ **蒙皮网格 + Nanite 在 5.6 有 bug：通过 `SceneCapture2D` 渲染时动画会静默冻结** —— 而本项目的采集链路**正是** `SceneCapture2D`，症状会是"物理在动、图不动"，正是最难查的一类 |
| **影响** | 中高。①②决定"给场地开 Nanite"要按显存预算重新量，不能假设省内存；③一旦误开在机器人蒙皮网格上，会以"静默冻结"的形式破坏验收 |
| **缓解** | **机器人的蒙皮网格保持 Nanite 关闭**（当前 `r.Nanite=0` 也是这个效果，但如果为了场地把它改成 1，必须逐个确认机器人资产是 False）；场地开 Nanite 前先跑 [`tools/inspect_arena_assets.py`](../tools/inspect_arena_assets.py) 拿到 `nanite_share`，再做同会话 A/B（`vram.csv` + `trace.csv`），并记住池是 init 时定的、只能通过**降网格密度**（fallback percent triangles、position precision）或 **HLOD + Nanite off** 来省 |
| **指标** | `r.Nanite=1` 下机器人资产仍报 `NaniteEnabled=False`；采集帧序内 `commit_id` 与图像同步变化（无"物理动图不动"）；同会话 A/B 的 `peak_used_mb` 与 `frame_ms` 都记录在案 |

> 本机引擎已确认 Nanite 在 Linux 可用：`Config/VulkanPC/DataDrivenPlatformInfo.ini` 的
> `[ShaderPlatform VULKAN_SM6] bSupportsNanite = true`（`VULKAN_SM5` 为 false）。
> 官方 Nanite 文档仍写"仅 DX12/SM6"，**已过时**。

---

## 3. 待实测项(不许靠推理)

| # | 要测什么 | 怎么测 | 判定 |
|---|---|---|---|
| T1 | ROS2 进程边界 | 系统 Humble 独立 `rclpy` adapter 与核心同时运行 | 标准节点可收发,核心不依赖 `rclpy`;性能不足时可替换为 `rclcpp` |
| T2 | 帧来源方案 | 在 enqueue/readback 链路绑定 `commit_id`/`ue_frame_id`,渲染并读回 | 图/位姿/IMU 可无歧义配对 |
| T3 | 单位一致性 | 1 m 自由落体、单摆周期 | 与解析解一致 |
| T4 | 同步带宽 | 推 N 个对象,测单帧耗时 | 满足帧预算 |
| T5 | 扫掠不穿模 | 30 m/s 弹丸连发打薄板 | 无穿透,命中点正确 |
| T6 | 确定性 | 同初始状态跑两次 | 逐字段一致 |
| T7 | URDF 转换 | 目标机器人 URDF → MJCF + UE | 几何重合、关节正确 |
| T8 | 图像渲染正确性 | 荧绿弹丸在 `final_tone_curve_hdr` 里是否仍是荧绿 | 视觉可辨/可分割 |
| T9 | UE5.8 构建 | SPEAR `ue-58` 下编出可运行工程 | 取到图 |
| T10 | 显存预算与启动峰值 | `tools/test_urdf_views.py --low-vram --vram-poll-seconds 1`(1440×1080 顺序捕获);另跑只关渲染特性/只降纹理池两次做归因 | `event: frame`;`min_free_mb ≥ 1024`;无 `Out Of Memory`/`VulkanMemory.cpp:3114` |
| T11 | 打包形态的独立运行 | Cook/Stage 出 Pak 与可执行文件后,用 `SPEAR.LAUNCH_MODE=game` + `GAME_EXECUTABLE` 跑同一命令 | 无需编辑器运行库即可出图;峰值不高于 `--standalone` 的 6087 MiB |

---

## 4. 风险与阶段的对应

| 阶段 | 主要要消解的风险 |
|---|---|
| 0 | R9(ROS2 环境)、T9、**R13** |
| 1 | **R2、R4、R11、R3**(同步契约,地基) |
| 2 | R1、R5(R1 是最高优先) |
| 3 | R6(频率分工落地) |
| 4 | R8(资产迁移) |
| 5 | R7(弹丸) |
| 6 | R2/R4 的最终验证(数据集可复现) |
| 7 | R12 |

---

## 5. 2026-10 采集链路补充

- 运行期 `console_service.set_cvar("r.PostProcessing.UserSceneTextureDebug", ...)` 在 `UnrealEditor -game` 下会 **SIGSEGV**（该 cvar 触发 MaterialEditor 的刷新回调，而 `-game` 没有编辑器子系统）。后处理类 cvar 必须写进 `ue/Config/DefaultEngine.ini` 的 `[SystemSettings]`，不能运行时下发。
- `--low-vram`（关 Lumen/RT/VSM + 纹理池 512 + `r.ScreenPercentage=50`）只能用于"跑通逻辑"，不能用于画质验收；并且 `r.ScreenPercentage` 对 SceneCapture 本来就无效（UE 强制 `FLegacyScreenPercentageDriver(1.0)`，它只降视口）——**2026-10-08 已把 `r.ScreenPercentage=50` 与 `r.Streaming.PoolSize=512`/`LimitPoolSizeToVRAM=1` 从预设里删掉**（前者对捕获无效，后者峰值只 −3 MiB 却压低 mip），剩下的"关 Lumen/RT/VSM/DF"才是预设真正买到的显存。修复前的固定机位帧灰度均值只有 9.4/255，是"低显存预设 + 未拷后处理 + 机位没对准机器人"三者叠加的结果，单独归因会误判。画质要省显存就降分辨率，而不是关渲染特性。
- 画质/显存的完整对比、已落地的修复、以及"`-game` 下机器人尚未出现在画面中"这一开放项的证据与下一步，见 [`rendering.md`](rendering.md)。
