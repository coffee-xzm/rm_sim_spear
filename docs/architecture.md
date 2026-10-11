# 架构

> 本文定义分层、职责、接口、数据流、同步模型、单位与坐标、URDF 流水线。
> 相关:[`plan.md`](plan.md)、[`decisions.md`](decisions.md)、[`projectile.md`](projectile.md)

---

## 1. 三层与职责

```
┌──────────────────────────────────────────────────────────────────────┐
│  ① UE5 App (SPEAR 插件)                                              │
│     职责:场景编辑 + 渲染 + 多模态真值 + 灯效/材质                     │
│     约束:被同步的物体关闭自身刚体物理(kinematic);不参与物理求解       │
└───────────────▲──────────────────────────────────────────────────────┘
                │ msgpack-RPC(场景/位姿/控制查询)
                │ 共享内存(图像/深度/分割,零拷贝)
┌───────────────┴──────────────────────────────────────────────────────┐
│  ② 编排进程 (Python 3.11)                                            │
│     ├─ SPEAR client   : begin/end frame 事务,取图/推位姿              │
│     ├─ MuJoCo         : 物理真源、URDF 机器人、IMU/接触               │
│     ├─ 规则层 (sim/)  : rune 规则、弹道、命中判定(纯逻辑,可单测)     │
│     └─ TimeSource     : 唯一时间真源;固定步长;run/pause/step          │
└───────────────▲──────────────────────────────────────────────────────┘
                │ ROS2 / DDS(只通信): /clock /tf Imu JointState 指令
┌───────────────┴──────────────────────────────────────────────────────┐
│  ③ 外部算法栈:ROS2 节点(自瞄/打符)+ 上层规划                          │
└──────────────────────────────────────────────────────────────────────┘
```

**为什么这么切**

- UE 的强项是渲染和材质,**弱项是"被程序精确控制"**——SPEAR 恰好补上这一块。
- MuJoCo 的强项是刚体/接触/关节与传感器,**弱项是渲染**。
- 规则是纯逻辑,放在引擎之外才能**确定性单测**(`rm_sim_rune` 6 个单测的价值所在)。

---

## 2. 四条红线的可执行判定

| 红线 | 判定方法 |
|---|---|
| 1. MuJoCo 是物理真源,UE 是镜像 | 把 UE 侧刚体物理全部关掉,仿真行为不变 |
| 2. 规则是纯逻辑第三层 | `sim/` 能**脱离 UE 与 MuJoCo** 编译并单测 |
| 3. 帧来源可证明 | 每个输出或其关联的 `FrameCommit` 带 `run_id + sim_tick + commit_id`;图/位姿/IMU 不允许跨提交点拼接 |
| 4. 通信边界独立 | ROS2 只通过适配层接入;删掉适配层后核心仿真仍可构建和运行 |

> 红线 2/4 与 `rm_sim` 的"core 必须能脱离 UE 编译""删掉 ros2 必须照常构建"同构——把口号变成一次构建实验。

---

## 3. 数据流

### 3.1 控制环(每个仿真提交点)

```
Rules/算法 → 控制量 → MuJoCo(执行器) ──┐
                                      │ mj_step × N
                                      ▼
                              mj_forward()  → 真值(位姿/IMU/接触)
                                      │
              FrameCommit = {run_id, commit_id, sim_tick, t,
                             physics_step, state_revision}
                                      │
                                      │
        ┌─────────────────────────────┼──────────────────────────────┐
        ▼                             ▼                              ▼
  begin_frame: 提交 UE 快照      ROS2: 发 /clock /tf /Imu      (可选)推理
        │
        ▼
  end_frame: read_pixels()  → 图像 + commit_id(共享内存)
```

### 3.2 关键不变量

1. **一次提交只对应一个快照**——`sim_tick` 是对外仿真帧编号,内部可以包含 N 个 `mj_step`;`begin_frame`/`end_frame` 是同一 UE 帧的两端,但不假设事务连续发生在连续 UE 帧。
2. **观测必须带来源**——图像回读可以延迟,但必须返回产生它的 `run_id`、`commit_id`、`ue_frame_id`、`sensor_id` 和 `modality`；同一提交的多路传感器分别配对,由 `CompositeRenderer` 汇聚。
3. **被同步物体在 UE 侧无物理**。
4. **图像默认不经过 DDS**;兼容模式必须显式启用并单独测量带宽与延迟。

---

## 4. 同步模型

### 4.1 游戏暂停与编辑器交互

SPEAR 的暂停语义是**暂停 UE 游戏世界**,不是关闭 UE 应用：游戏逻辑 Tick、UE 物理推进和碰撞检测停止；UE 进程、窗口、编辑器、视口渲染和用户输入仍保持响应。因此可以在事务之间查看状态、移动观察视角和编辑场景。

严格单步时,UE 游戏世界初始处于 paused 状态。每次事务只执行一次“临时解暂停 → 应用本次状态 → 运行目标游戏帧 → 再暂停”。事务之间不保证连续 UE 帧,但不会因为编辑器交互自动推进仿真逻辑。

#### 4.1.1 上游论文口径:两条**互斥**的路

SPEAR 论文（[`2607.06701v1.pdf`](2607.06701v1.pdf) §3 Programming Model,及 Fig. 4 的 "External physics simulator" 栏）把"事务之间不推进仿真"讲成**两个替代方案**,不是两个可以叠加的开关：

| | 事务之间的游戏线程 | 编辑器 |
|---|---|---|
| 暂停 + 事务内 `unpause()` → 改状态 → `pause()`（论文推荐,Fig. 4 的 AirSim 栏） | 自由推进 | **保持可交互**——"remains responsive to user input at all times" |
| `end_frame(single_step=True)`（论文里对应 CARLA 同步模式,Fig. 4 的 CARLA 栏） | 推进到下一帧开头,**然后等下笔事务** | 冻结 |

论文原话（§3）：

> A practical advantage of this approach, as opposed to blocking the game thread between transactions (e.g., CARLA's synchronous mode), is that the UE application remains responsive to user input at all times. This interactivity is especially useful when controlling simulations running inside the Unreal Editor, because the editor can be used as a powerful visual debugging tool to navigate around the simulation environment and inspect the game state, even in-between transactions when the simulation is paused. Alternatively, if desired, the user can explicitly prevent the game thread from advancing in-between transactions by calling `end_frame(single_step=True)`, which instructs the game thread to advance to the beginning of the next frame and wait for the next transaction.

论文自己的 MuJoCo 协同案例（Fig. 7,对应 Fig. 4 最右栏）用的正是**前者**,而且连暂停都不需要——外部物理真源接管后 `disable_ue_physics()`,每个 `step()` 只用普通的 `with end_frame()`。

#### 4.1.2 本项目的选择:暂停模式,**不用** `single_step`

**本项目选前者。** 严格模式：UE 游戏世界启动即 paused；每次事务 `unpause()` → 应用快照 → （世界推进恰好一帧）→ `pause()`；事务用普通 `end_frame()` 收尾。事务横跨一帧（`begin_frame` 的工作在帧首 `OnBeginFrame`、`end_frame` 的工作在**同一帧**帧尾 `OnEndFrame`），所以"帧首解暂停、帧尾暂停"恰好让游戏世界走一帧。

**为什么不能选 `single_step=True`**：它的阻塞点在游戏线程的帧**最开头**,早于 Slate 输入和 `GEngine->Tick()`：

- `Service.h:35` 把 `beginFrame()` 挂在 `FCoreDelegates::OnBeginFrame` 上（`~/spear/cpp/unreal_plugins/SpServices/Source/SpServices/Service.h`）；
- `EngineService::beginFrame()` → `WorkQueue::run()` → `io_context_->run()` 一直阻塞到下一笔事务 schedule 出 `reset()`（同目录 `WorkQueue.h`）；
- `OnBeginFrame.Broadcast()`（`LaunchEngineLoop.cpp:5682`）与 `OnEndFrame.Broadcast()`（`:6127`）之间依次是 `GEngine->UpdateTimeAndHandleMaxTickRate()` → `BeginFrameRenderThread` → Slate 输入轮询 → `GEngine->Tick()` → `FSlateApplication::Tick()`（`~/Linux_Unreal_Engine_5.8.0/Engine/Source/Runtime/Launch/Private/LaunchEngineLoop.cpp`）。

所以 `single_step=True` 期间游戏线程卡在帧首,Slate、世界 Tick、渲染全都走不到——编辑器是**整个窗口冻结**,不是"仿真暂停但还能飞"。（这一条目前是**源阅读结论**,还没有在 `:1` 上实测确认；确认方法：让脚本跑着循环，同时点编辑器菜单。见 [`agents.user.md`](agents.user.md)。）

这里的 UE 游戏暂停与 MuJoCo 的 `TimeSource.paused` 是两个边界：严格模式下二者都只在一次提交事务中推进；编辑器交互期间二者都不产生新的仿真提交点。

> **迁移状态（2026-10-10）**：`rm_sim_spear/render.py` 与 `tools/run_spear_sim.py`（编辑器/PIE 路径）已改用普通 `end_frame()`。`tools/run_physics_sim.py` 的内联事务**仍**传 `single_step=True`——它是无头吞吐基准，`--save-images`/`_BUFFERED_LAG`/`end_ms` 那套成本模型（[`render_budget.md`](render_budget.md) §13/§14）就建立在这个口径上，而且无头路径不需要编辑器可交互。两条路径的 `read_pixels()` 语义与缓冲 lag 配对**尚未**逐帧对拍验证。

### 4.2 自由跑 vs 锁步

| | 实时运行(可选) | 严格单步 |
|---|---|---|
| 谁推进仿真时间 | 驱动器按墙钟调度 | 只有显式 `step_frame()` 才动 |
| UE 游戏世界 | 可不暂停 | 事务之间暂停 |
| 渲染帧 ↔ 仿真时刻 | 不保证一一对应 | 由 `commit_id` 明确对应,不要求连续 UE 帧 |
| Python 慢了 | 实时模式允许落后或丢帧 | 只延迟下一次提交,不改变轨迹 |
| 编辑器交互 | 有 | **一样有** |
| 适用范围 | 观察效果、性能测量 | 数据集、回归和闭环正确性 |

默认只把严格单步作为正确性模式；实时运行是单独的观测/调试模式,不能用于严格回放验收。

### 4.3 驱动器骨架

```python
while running:
    command_batch = commands.for_next_frame()
    for _ in range(physics_steps_per_frame):
        apply_control(command_batch)
        mj_step(model, data)
    mj_forward(model, data)
    commit = make_commit(data)    # run_id/tick/t/state_revision
    with instance.begin_frame():
        unpause()                 # 帧首:让游戏世界只在本帧推进
        push_snapshot(commit)     # 米→cm 的转换只在这里发生
    with instance.end_frame():    # 普通收尾,不传 single_step
        enqueue_capture(commit)
        pause()                   # 帧尾:事务之间世界不推进
    publish_observations(commit)
```

### 4.4 频率分工

| 环 | 目标频率 | 宿主 |
|---|---|---|
| 物理积分 / 云台内环 | 1 kHz | MuJoCo |
| 自动瞄准感知 | 100–200 Hz | UE 渲染 + 推理 |
| 渲染 / 大闭环 | 30–90 Hz | SPEAR |
| ROS2 小消息 | 50–200 Hz | 编排进程 |
| 图像 / 深度 | 按帧 | SPEAR 共享内存;正确性基线使用单缓冲,性能模式可选双/三缓冲 |

> **对外仿真帧率 ≠ MuJoCo 物理频率。** `physics_steps_per_frame` 只是把多个固定物理子步批量归入一个对外提交点；它不是通用常数。严格闭环应优先设为 1，只有明确需要 1 kHz 物理、100 Hz 观测时才设为 10，并记录在 `FrameCommit.physics_step`。

### 4.5 上游编程模型要点（背景）

以 [`2607.06701v1.pdf`](2607.06701v1.pdf) §3–§4 为准，本项目依赖四条保证：

1. **事务内的顺序与位置**：`begin_frame` 里指定的 UE 工作在**帧首**执行，对应的 `end_frame` 工作在**同一帧帧尾**执行，全部在游戏线程上按 Python 里的书写顺序执行。这是"同一帧内确定性、副作用立即可见"的来源。
2. **不保证连续帧**：事务保证"在一帧内"，但**不保证**相邻两个事务落在相邻两帧。所以仿真推进必须自带暂停/单步语义，不能假设"上次事务之后只过了一帧"。
3. **一次只允许一笔未完成事务**：上一笔 `begin_frame`/`end_frame` 的工作还没做完时，进入下一个同类上下文会阻塞。这是防止 Python 线程跑到游戏线程前面的**唯一**闸门，而且它是**有界**的——只等到上一笔做完就放行，不是 `single_step` 那种"停到下一笔为止"。
4. **全异步时游戏线程可完全不被阻塞**：每个 UE 操作都有 `async` 变体返回 future；只用异步操作时，只要 Python 侧 `[end, begin, execute]` 比一个 UE 帧窄，UE 就按原生帧率跑（论文 Fig. 5 的稳态）。注意异步是"对 server 线程/游戏线程异步、对 Python 线程同步"。

架构上：客户端在 Python 进程，服务端是 UE 插件里的 **server 线程**，两者用 rpc-lib + nanobind 走 TCP；server 线程把 begin/end 工作分别放进两个线程安全队列，由游戏线程在帧首/帧尾抽干（论文 §4）。论文的协同案例（Fig. 7）与本项目同形——外部物理真源接管后先 `disable_ue_physics()`，每个 `step()` 把外部位姿写进 UE，用**普通** `end_frame()` 取观测。

> **不要把 `send_async` 和这件事混起来。** 决定编辑器是否被冻住的是 `single_step`，不是同步/异步调用：本项目实测每帧十几次调用时两者**一样快**（[`render_budget.md`](render_budget.md) §15）。

---

## 5. 单位与坐标

### 5.1 单位

- **物理真源 = 米**;UE 原生 cm;只在边界转换(见 [`decisions.md`](decisions.md) D1)。
- 唯一转换点:导出 ×0.01、运行时推位姿 ×100。
- **禁止**在 `sim/` 或 `physics/` 内散落换算。

### 5.2 坐标与旋转

三套约定叠加,必须**只有一处**转换层:

| 来源 | 手性 | 轴向 | 旋转表示 |
|---|---|---|---|
| Unreal | 左手 | X 前 / Y 右 / Z 上 | `(Roll, Pitch, Yaw)` 度,父系固定轴,序 roll→pitch→yaw |
| MuJoCo | 右手 | X 前 / Y 左 / Z 上 | 四元数(wxyz)/ 矩阵 |
| URDF / ROS | 右手 | X 前 / Y 左 / Z 上 | 四元数(xyzw) |

要点(均已在 `~/spear` 有实现可参考):

- 手性差异用 `diag([1, -1, 1])` 翻 Y(`--visual-parity-with-unreal`)。
- MuJoCo 四元数 **wxyz** ↔ scipy **xyzw**。
- Unreal 的 roll/pitch 取负、yaw 不变;scipy 与 Unreal 的欧拉约定冲突,推导见 `~/spear/python/spear/utils/math_utils.py` 头部注释。
- **禁止**从"actor forward 向量"推断视觉朝向(网格局部朝向无保证)。

### 5.3 验收

- 1 m 自由落体 ≈0.452 s;单摆 `T = 2π√(L/g)`。
- 匀速物体:"图像位移"与"回传位姿位移"一致,无系统性偏差。

---

## 6. 资产与 URDF 流水线

### 6.1 单一描述源 → 两个目标

```
URDF (+ mesh)
   ├─→ MuJoCo : 物理 + 碰撞 + 关节 + 传感器 (MJCF)
   └─→ UE     : 可视化网格 + 材质 + 骨架
```

**必须几何共注册**:同一 link 的 visual 与 collision 变换一致。否则感知会学到与物理不符的几何(sim2real gap 的主要来源)。

### 6.2 注意

- MuJoCo 的 URDF 支持**有限制**:mimic/transmission 不支持,mesh 只吃 **OBJ/STL**(DAE 需转换)。
- `~/spear` 现有 pipeline 是"UE 场景 → 碰撞几何 + MuJoCo 场景",方向相反;机器人侧需要扩展为"URDF → 两者"。
- 资产结论(参考 `rm_sim` 资产侧):USD 为载体、材质极简化、多材质必须预先拆分、后处理必须在引擎侧构建。

### 6.3 场景描述

UE 工程位于 `ue/`，视觉资产位于独立的 `ue/Content` Git submodule，由
`rm_sim_content` 的 Git LFS 管理。`~/rm_sim_rune/SimUnreal/Content/` 只作为首次
迁移源；场景实例仍由本项目的数据描述和 tag/name 契约解析，不直接继承原型的手工 level actor 绑定。

**不**用手工 level actor 图(`rm_sim_rune` 最大的结构债)。用**数据驱动的场景描述 + 按名/tag 解析 + 程序化 spawn**,并提供一个"场景契约校验"入口(对照 `rm_sim_rune` 的 `ValidateBinding` / `RuneSceneValidationCommandlet` 思路)。

---

## 7. ROS2 对外契约

仿真不再承诺复刻某个真机 SDK 的 ABI。仿真时间由本项目产生,外部算法通过标准 ROS2 消息消费并自行完成时间适配。

| 方向 | 话题/类型 | 语义 |
|---|---|---|
| 出 | `/clock` (`rosgraph_msgs/Clock`) | 当前 `sim_time`;外部节点使用 `use_sim_time` |
| 出 | `/sim/commit` (`sim_msgs/FrameCommit`) | 发布 `run_id/commit_id/sim_tick/physics_step/state_revision` |
| 出 | `/tf`, `/imu/data`, `/joint_states` | 使用标准消息,`header.stamp` 等于该提交的 `sim_time`; IMU/关节按传感器映射启用 |
| 入 | `<ns>/gimbal/command`(自定义 IDL) | 带 `target_tick` 的控制指令,按 tick 排序消费 |
| 出/入 | `<ns>/projectile/*`(自定义 IDL) | 发射请求和独立命中事件 |
| 出 | `<ns>/camera/*` | 默认发布帧元数据;图像走 SPEAR 共享内存,可选 `sensor_msgs/Image` 兼容模式 |

ROS2 运行在独立 adapter 进程（当前为系统 Humble 的 `rclpy` 实现，性能不足时可替换为 `rclcpp`）,核心仿真不依赖 ROS2 Python 版本。adapter 与核心通过版本化 Unix domain socket 交换小消息,大图像复用 SPEAR 共享内存。消息定义、QoS、命名空间和 reset 行为放在 `bridge/idl/`，不从 `sp_vision_25` 的内部结构推导协议。

---

## 8. 可插拔点(留缝,不做)

| 缝 | 形态 | 何时用 |
|---|---|---|
| 外部时钟 | `TimeSource` 支持 `External` 权威(外部调 `advance`) | HIL / 重放 / 未来 Isaac |
| 控制后端 | 注册表 dispatch(禁止 if-else 链) | 换机器人/换控制器 |
| 传感器 | 注入式真值 + 按名索引 | 加 IMU/里程计/相机 |
| 实体 | 按名索引,多实例 | 多机器人 |
| 场景 | 数据驱动描述 | 换场地 |
| 弹丸 | 独立 ProjectileSubsystem;输入快照,输出纯数据 HitEvent | 更换弹道/查询实现 |

> `rm_sim` 的教训:新增"一种东西"**不许**改 dispatch 的 if-else(反面案例是 AirSim `SimHUD::createSimMode()` 的 private 非虚硬编码链)。
