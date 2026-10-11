# 决策记录

> 每条含**结论、理由、落地方式、验收、回退**。回退方式是硬要求——没有回退方案的决策不算决策。

---

## D1 — 单位制:用米

**结论**:物理真源用 **SI 米**。UE 原生工作单位是 cm,两者**只在边界上转换**。

**理由**

- MuJoCo 官方文档明确 **"MuJoCo does not specify basic physical units"**,单位未定义,只要求内部自洽。它不偏向任何单位。
- 但机器人侧的**一切都是 SI**:URDF 长度是米、惯量是 `kg·m²`、IMU 规格是 SI、真机控制器增益是米制整定值。
- `~/spear` 现有 pipeline 用的是 **UE 单位(cm)**,证据是 `pipeline/generate_mujoco_scene.py` 里 `gravity="0 0 -981"`(9.81 m/s² × 100),且整条导出链**零缩放**(UE 的 `Scale3D` 直接写进 MuJoCo mesh `scale`,transform 直接写进 `pos`/`xyaxes`)。这是**导出保真 + 视觉对齐**的合理选择,但它的前提是"没有 URDF、没有执行器、没有显式惯量"——例子里只有重力驱动的自由刚体,质量被约掉,单位不进入可观测量。
- **引入 URDF 后该前提失效**。最阴的一条是惯量:URDF 给 `kg·m²`,放进 cm 长度模型必须 ×10⁴,漏了的话机器人"能动但像疯了",**且不报错**。阻尼/刚度/effort/增益同样含长度幂次。

**落地**

| 位置 | 处理 |
|---|---|
| UE 场景 → MuJoCo 导出 | 全局 ×0.01;`gravity` 改 `-9.81`;mesh 用统一 `scale` 或导出时缩放 |
| MuJoCo → UE 运行时推位姿 | ×100,与位姿同步走**同一条**路径 |
| URDF 加载 | **零转换**,惯量/质量原样 |
| `pos_offset` 类手写常量 | 由 cm 换算为 m(如 `-14.0` → `-0.14`) |
| `--visual-parity-with-unreal` 的 Y 翻转 | 与单位无关,保留 |

**唯一转换层**:所有换算集中在 `sim/math/`(或渲染侧的 `render/sync/`),**禁止散落**。

**验收**:已知物理量交叉验证。1 m 自由落体 ≈0.452 s;单摆 `T = 2π√(L/g)`。数值对上,单位就没混。

**回退**:若发现 UE→MuJoCo 的批量缩放引入了难以定位的几何误差,可退回"cm 为真源",但**必须**同时提供完整的 URDF 换算器(长度 ×100、惯量 ×10⁴、阻尼/刚度/effort 按阶换算)并加一条"惯量一致性"断言。回退成本高,故优先米制。

---

## D2 — 时间真源与帧来源

**结论**:**MuJoCo 的仿真时间是唯一仿真时钟**。每次对外推进产生一个不可变 `FrameCommit`:
`{run_id, commit_id, sim_tick, sim_time_ns, physics_step, state_revision}`。
`sim_tick` 是对外仿真帧编号,`physics_step` 是内部 MuJoCo 子步编号。UE 事务和异步图像回读必须携带该提交点,另行记录 `ue_frame_id`。

**理由**

- 没有唯一时钟,"同帧对齐"就是空话。`rm_sim` 实测过反面案例:`rm_sim_rune` 用墙钟 `FDateTime::UtcNow()` 打帧时间戳,导致不可复现、无法重放、图像与位姿不在同一条时间轴上。
- MuJoCo 在锁步(step)语义下天然给出"冻结的当前时刻"；SPEAR 则通过暂停 UE 游戏世界、事务内单步并恢复暂停来保证渲染状态不会被编辑器交互偷偷推进。

**落地**

- 一个 `TimeSource`:`sim_tick`、`sim_time_ns`、`step`、`advance(n)`、`paused`。**谁都不许用系统时间作为仿真时间。**
- 每个传感器包引用一个 `FrameCommit`;不要求事务发生在连续 UE 帧,但禁止跨提交点拼接数据。
- ROS2 侧发 `/clock`,消费者用 `use_sim_time:=true`。
- 严格模式启动 UE 游戏世界为 paused 状态,事务内临时解暂停、应用快照、推进目标游戏帧、再暂停;事务用普通 `end_frame()` 收尾。**不用 `end_frame(single_step=True)`**——它把游戏线程阻塞在帧首(早于 Slate 输入与 `GEngine->Tick()`),编辑器会整个窗口冻结,理由与源码位置见 [`architecture.md`](architecture.md) §4.1。UE 应用和编辑器因此保持可交互,但交互不产生新的仿真提交点。

**已知缺口(必须解)**

SPEAR 的 `read_pixels()` **只返回数组,没有帧标识或时间戳**(见 `SpSceneCaptureComponent2D.cpp` 的 `registerFunc("read_pixels", ...)`)。配合 0/1/2 帧的缓冲延迟,"这张图属于哪个 `t`"必须自己绑:

- **方案 A(必须)**:在 `enqueue_copy` 时绑定 `run_id`/`commit_id`/`ue_frame_id`,`read_pixels` 返回图像与元数据。不能只在读取时查询当前时间。`SpearRenderPort` 的 `readback_metadata` 必须返回这三个字段，适配器按 `run_id + commit_id` 查找待配对快照。
- 暂停期间允许编辑器继续渲染,但交互画面不自动冒充新的仿真观测;严格采集只消费带有效 `FrameCommit` 的图像。

**验收**:
- 图 / 位姿 / IMU 可以按 `commit_id` 配对,且 `sim_tick` 单调递增;
- 同一初始状态 + 同一指令序列跑两次,逐字段一致(确定性回放)。

**回退**:若无法绑定 readback 元数据,严格数据集模式暂停交付,不退回墙钟或隐式按数组顺序配对。

---

## D3 — ROS2:标准独立适配层

**结论**:ROS2 只承载**通信**,运行在独立 adapter 进程。不引入 `ros2_control`,不让 ROS2 拥有物理时钟;仿真发布 `/clock`、`/sim/commit` 和带 `sim_time` 的标准消息。图像默认走 SPEAR 共享内存,另提供 `sensor_msgs/Image` 兼容模式。

**理由**

- `~/rm_sim` 的结论是"UE 插件不链 `rclcpp`/`rcl`/`rmw`,直接打 Fast DDS"。**这个选型的前提在本架构里消失了**——SPEAR 的客户端是独立 Python 进程,没有 UE 链接约束,`rclpy` 成本最低、生态最好。所以**不继承**该选型。
- 但继承它的教训(与实现无关):
  - 图像**不上 DDS**(变长序列化最贵;`sensor_msgs/Image` 不带相机位姿,会强制跨通道 join);
  - 订阅侧**单槽 latest 会静默丢样本**,高频 IMU 必须小容量 SPSC 环 + 消费线程 drain;
  - QoS 逐通道显式声明(默认 RELIABLE 匹配一切但会背压);
  - 必须读 `ROS_DOMAIN_ID`;
  - 云台角是**高频关节信号**,帧里只放"曝光时刻的快照",两者不是一回事。

**落地**

- 进出 DDS 的只有**小消息**:`/clock`、`/tf`、`Imu`、`JointState`、`Odometry`、自定义指令。
- 控制和传感器使用标准消息:`rosgraph_msgs/Clock`、`tf2_msgs/TFMessage`、`sensor_msgs/Imu`、`sensor_msgs/JointState`、`nav_msgs/Odometry`;用 `sim_msgs/FrameCommit` 承载提交元数据,领域控制使用版本化自定义 IDL。
- 订阅侧按 `target_tick` 排序,仿真核心按 tick 消费;ROS 线程不直接调用 `mj_step`。

**验收**:**把 ROS2 adapter 整块删掉,核心仿真与 SPEAR 取图照常运行。**

**回退**:若某通道确需低频 latest 语义,可退化为单槽,但**必须显式标注为有损**,且不得作为算法输入。

---

## D4 — 弹丸:独立碰撞查询子系统

**结论**:**ProjectileSubsystem 与 MuJoCo/规则核心解耦**。它可以在 UE 中继续飞行,但只通过数据接口产生 `HitEvent`;UE `QueryOnly` 扫掠球 + 盒状装甲靶代理 + 纯函数环数。命中事件不会直接改变 MuJoCo 状态。

**理由**(完整拆解见 [`projectile.md`](projectile.md))

- `rm_sim_rune` 的弹丸**不是刚体物理仿真**:弹丸与装甲靶两边都是 `CollisionEnabled=QueryOnly`,`UProjectileMovementComponent` 做的是解析式固定步长积分 + **扫掠**,没有质量/接触力/约束求解。所以 UE 在这里是"**碰撞查询服务**",**不违反红线 1**。
- 它比我曾建议的 `mj_ray` 更省:`Hit.OtherComp` **直接给出是哪一块装甲**;扫掠+子步进已调好;环数数学已是纯函数且有单测。
- **扫掠**解决 30 m/s 的 tunneling(`bSweepCollision=true`),子步进(`bForceSubStepping=true`,`MaxSimulationTimeStep=1/120`)保证重力积分精度。

**落地**

- 照搬:通道/profile 对(`RuneProjectile`/`RuneArmor`,均 `QueryOnly`)、17 mm 球、扫掠与子步进参数、盒靶 `(Radius, Radius, Thickness/2)`、一次性命中去重、`RuneHitMath` 及其边界单测、`ComposeShotTransform` 及其单测。
- 改造:40 并发 → **预分配对象池**(不再每帧 spawn/destroy);绿色 → **Unlit+Emissive 材质**(不用点光);初速按真机规格设(原型是 15 m/s,真机 17 mm 一般 ~30 m/s);能从 **SPEAR/Python 侧发射**(做成 Blueprint 由 `spawn_actor` 生成,或加 UFUNCTION 入口)。
- 弹丸的 UE 运动时间与仿真时间分开记录。规则只消费带 `shot_id`、`target_id`、相对命中点、`ue_frame_id` 和关联 `commit_id` 的 `HitEvent`。

**验收**:40 颗/帧无掉帧;扫掠不穿模;环数边界单测全绿(0→10 环、5 mm→9、44→2、50→1、51→无效、镜像靶、NaN)。

**回退**:若 UE 积分无法满足命中精度,将轨迹积分替换为 `sim/ballistics/` 的纯函数,ProjectileSubsystem 接口不变。

---

## D5 — 同步模型:提交点 + run/pause/step

**结论**:**UE 游戏世界在严格模式下暂停,编辑器和 UE 应用仍可交互；每个对外仿真帧必须形成一个明确提交点。** 严格模式使用"暂停 + 事务内解暂停/暂停",**不用 `single_step`**。

**理由**

- SPEAR 的 UE 是实时游戏进程；暂停游戏世界后,应用、编辑器、视口和输入仍然工作。所以可以停在一个仿真提交点查看和编辑场景。**这条只在"暂停 + 普通 `end_frame()`"下成立**：`single_step=True` 阻塞的是游戏线程本身（帧首 `OnBeginFrame`,早于 Slate 与 `GEngine->Tick()`），编辑器会整体冻结。上游论文把两者讲成替代方案（`2607.06701v1.pdf` §3）,没有理由把代价最高的那个再叠加上去。
- 二者的真正区别是:**游戏世界是否推进**、**谁推进 MuJoCo 仿真时间**、以及**渲染帧与仿真时刻是否一一对应**。
- **为什么必须区分**:SPEAR 相机有 0/1/2 帧回读延迟。旧图像不是错误,但必须通过 `commit_id` 证明它对应哪个提交点。

**落地**

```
while running:
    command_batch = commands.for_next_tick()
    for _ in range(N):
        apply_control(command_batch)
        mj_step(model, data)
    mj_forward(model, data)
    commit = make_commit(data)
    with begin_frame(): unpause(); push_snapshot(commit)
    with end_frame(): enqueue_capture(commit); pause()
```

> **迁移状态（2026-10-10）**：`rm_sim_spear/render.py` 与 `tools/run_spear_sim.py` 已改用普通 `end_frame()`；`tools/run_physics_sim.py` 的无头吞吐路径仍保留 `single_step=True`（成本模型建立在该口径上）。`read_pixels()`/缓冲 lag 两处口径还没有逐帧对拍。

采集/严格闭环时使用提交点锁步;手动调场景/看效果时允许 UE 自由渲染。弹丸可在 UE 中继续运行,命中事件按独立子系统协议回传。

**验收**:匀速运动物体的"图像位移"与"回传位姿位移"一致(无系统性偏差)。

**回退**:无——提交点和来源标识是地基。

---

## D6 — 帧来源元数据

见 D2 的"已知缺口"。**结论**:阶段 1 必须在提交/回读链路中绑定 `commit_id` 和 `ue_frame_id`。

**验收**:图像、位姿和 IMU 能按 `commit_id` 无歧义配对。

**回退**:暂停严格采集,禁止按数组顺序猜测帧号或使用墙钟。

---

## D7 — ROS2 环境与进程边界

**结论**:使用系统 **ROS2 Humble 独立 adapter 进程**;SPEAR/MuJoCo 进程不加载 `rclpy`。

**理由(本机实测)**

| 项 | 实测 |
|---|---|
| OS | Ubuntu 22.04 → ROS2 **Humble** |
| ROS2 Humble 的 `rclpy` | 在 `python3.10/dist-packages`,**仅 Python 3.10** |
| SPEAR | `requires-python = "~=3.11"`,当前 `spear-env` 是 3.11.16 |
| 结论 | **ROS2 主流发行版没有一个对应 Python 3.11**(Humble/Iron = 3.10,Jazzy/Rolling = 3.12) |

**落地**
1. ROS2 adapter 使用 Ubuntu 22.04 的系统 Humble。当前实现采用独立 `rclpy` 进程，便于直接复用标准消息和 QoS；`rclpy` 仍不进入 SPEAR/MuJoCo 核心进程。若后续性能测试显示 Python 适配层成为瓶颈，再按相同 envelope 契约替换为 `rclcpp`，不改变核心边界。
2. adapter 与核心之间使用版本化 Unix domain socket 控制协议;大图像默认复用 SPEAR 共享内存。
3. QoS 使用 ROS2 标准 profile: `/clock` 使用 `ClockQoS`,传感器使用 `SensorDataQoS`,动态 `/tf` 使用 `tf2_ros` 动态 profile,静态 `/tf_static` 使用 transient-local,控制指令使用 reliable + keep-last(10)。

**验收**:标准 ROS2 节点能收发时钟、传感器和控制消息;QoS 能与 `ros2 topic echo`/标准消费者匹配;核心进程不依赖 ROS2 Python 版本。

**回退**:暂时关闭 ROS2 adapter,核心仿真仍可单独运行。

---

## D8 — 装甲视觉来自 per-link `ue_asset` 覆盖，运行时不再克隆 `Scene/armor`

**结论**:`armor_link_*` 的视觉由 `ue_overrides.json` 的 per-link `visual_source: ue_asset`
提供（关卡自带的灯条装甲模块），并显式关闭 `scene_bindings.armor`（`"enabled": false`）。
不再走"克隆 `Scene/armor` 整组到四个 link"的老路。

**理由(本机实测)**

| 项 | 实测 |
|---|---|
| URDF 自带装甲 mesh | 只有 `/Engine/EngineMaterials/WorldGridMaterial`，没有材质 → 导入后"装甲板没材质" |
| `Scene/armor` 聚合组 | 组根 `armor_tune_node_4` + 4 个子件的 CAD 调参副本，实际位置在**地板下约 1.7 m** |
| 克隆 vs `"armor": []` 的像素差 | **0 像素**（同物理帧，`max |Δbase_link_x| = 0.000000 cm`）→ 克隆只带来语义错误，没有画面收益 |
| 场景资产覆盖后的像素差 | 942 px（URDF 装甲）→ 2021 px（场景组覆盖）→ 视觉确认真实装甲出现在 link 上 |

**落地**
1. `pipeline/visual_override.py` + `tools/import_urdf_visuals.py --overrides` 解释
   `visual_source` / `ue_asset` / `local_transform`，把资产挂到 link 锚点下并打
   `RMSimRobot=<robot>` + `RMSimLink=<link>` 标签。
2. `pipeline/scene_binding_plan.py` 支持 `"armor": null` 与 `"enabled": false` 关闭该段，
   关闭时**不产生** `no links matched armor.link_regex` 警告（绑定器把警告当致命错误）。
3. 装甲 link 仍是位姿锚点：计划里 13 条 `links` 全部交给位姿同步器，装甲只是锚点的子件。
4. 摆放（用户手摆角度烘焙进子件、父节点保持 URDF 值）见 [`urdf_workflow.md`](urdf_workflow.md)。

**验收**:`--verify-armor-links` 改查导入的装甲子件（`RMSimLink=<link>`）在锚点被扰动后
是否保持固定偏移（它的预检也从"计划必须有 `armor` 条目"改成"必须有 `armor_link_*` link"，
否则新语义下正常计划会被直接拒绝）；`tests/test_scene_binding_plan.py` 锁住 `enabled:false` /
`null` / 键缺失三种语义，以及出厂文件必须展开成 `armor == []` 且装甲 link 仍在 `links` 里。
像素 A/B（同机位、同物理、只差克隆）：`frame_0023` 变化像素 `>32` 为 **0**、`mean|d| = 0.061`；
`fed_poses_cm` 四个 `armor_link_*` 逐位一致；峰值显存 3752 → 3823 MiB（噪声）。

**回退**:`"enabled": true`（或删掉该键）后重新展开计划，即回到克隆路径；克隆时代的代码与
`hide_template` 行为都还在。

---

## 待定项(Open)

| # | 待定 | 影响 |
|---|---|---|
| O1 | 目标帧率与分辨率预算(渲染帧率 vs 物理频率的比值 N) | 决定同步、带宽、算力 |
| O2 | 弹丸初速/是否受重力(按真机规格) | 弹道常数 |
| O3 | 机器人 URDF 的来源与网格格式(OBJ/STL/DAE → MuJoCo 只吃 OBJ/STL) | URDF 流水线 |
| O4 | 是否要"下位机在环"(虚拟串口/CAN 回环) | `rm_sim` 的遗留需求 |
| O5 | 多机器人命名空间与场景描述格式 | 阶段 7 |
| O6 | 资产许可(RM 场地/机器人模型的版权) | 发布合规 |
