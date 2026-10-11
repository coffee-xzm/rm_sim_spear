# rm_sim_spear 计划

> 状态:**草案 v1**(基于 2026-09 的方案讨论)
> 输入:`~/spear`(基座)、`~/rm_sim` 与 `~/rm_sim_rune`(弃案:引用内容,不继承实现选型)
> 相关:[`architecture.md`](architecture.md)、[`decisions.md`](decisions.md)、[`projectile.md`](projectile.md)、[`reuse.md`](reuse.md)、[`risks.md`](risks.md)

---

## 1. 目标与边界

### 1.1 要什么

| 能力 | 含义 | 判定它成了的标准 |
|---|---|---|
| **感知仿真** | 产出带来源标识和确定延迟的图像流 | 外部程序能按 `commit_id` 配对图像、位姿和 IMU |
| **控制仿真** | 外部算法发指令 → 机器人动 → 传感器回传,闭环可跑 | 标准 ROS2 控制/状态契约跑通 |
| **可复现** | 同一初始状态 + 同一指令序列 → 逐字段一致 | 确定性回放测试通过 |
| **可交互** | 任何时候都能停下来看、手动改场景、单步 | 暂停物理时钟后 UE 仍可自由交互 |
| **机器人可插拔** | 导入各种 URDF 做进一步交互 | 新增一个 URDF 不改架构代码 |

### 1.2 不要什么(v1 明确出界)

- **不做**"第二个物理世界"。刚体物理只有 MuJoCo 一个真源,UE 不参与。
- **不做**独立的数据集导出管线。数据集与实时闭环是**同一条渲染管线**的两种消费方式,开第二条必然导致相机模型分叉。
- **不让 ROS2 进 UE 进程**。ROS2 只通信,不拥有时钟;图像默认走 SPEAR 共享内存,可提供标准 `sensor_msgs/Image` 兼容模式。
- **不做** `ros2_control` 式的仿真循环(`mujoco_ros2_control` 可作参考/验证,不当核心)——它会自己跑仿真,和唯一时间真源冲突。
- **不在 v1 做**多机编队、飞控 HIL、Isaac 协同。但接口要留缝(见 `architecture.md` 的可插拔点)。
- **不为视觉做过度投入**。弹丸只要荧光绿,不需要 VFX。

### 1.3 一条最重要的定位

> **本项目的产物是"带时间轴的传感器流 + 可闭环的机器人交互",不是"一堆图片"。**
> 因此**仿真帧来源、时间戳、可复现**从第一天就是一等公民。

---

## 2. 选型评估摘要

### 2.1 SPEAR 为基座

上游论文:[`2607.06701v1.pdf`](2607.06701v1.pdf) — *SPEAR: A Simulator for Photorealistic Embodied AI Research*（arXiv:2607.06701v1）。编程模型的权威口径在它的 §3,系统实现在 §4,性能数字在 §5。

**它的护城河**:

- **帧内事务模型**:`begin_frame`(帧首下发动作)/ `end_frame`(帧尾取观测),同一帧内确定性、副作用立即可见。事务之间不保证连续 UE 帧;严格模式用**暂停 + 事务内解暂停/暂停**(普通 `end_frame()`,**不用** `single_step=True`——它的阻塞点在帧首、早于 Slate 和 `GEngine->Tick()`,会把编辑器整个冻住,见 [`architecture.md`](architecture.md) §4.1)。
- **自研相机**:三种缓冲(0/1/2 帧延迟)+ GPU 异步回读 + 共享内存零拷贝;多模态 GT(depth / normal / segmentation / material ID / PBR)。论文口径 1080p @73fps。
- **14K UE 函数直接暴露**,UE 只当"编辑器 + 渲染端"完全成立。

**它的弱项(决定架构怎么切)**:

1. 没有物理服务、没有 IMU、没有关节/接触传感器 → 物理必须交给 MuJoCo。
2. 协同是"位姿镜像",不是双向物理耦合;被同步的 UE 物体必须**让位**(关掉自身物理)。
3. 同步 RPC 会阻塞帧 → 高频环不能塞进主循环,必须 `send_async` / `call_async`,或放到 MuJoCo 侧。**注意这是"调用量大"时的结论**:本项目实测每帧十几次调用时两种方式**一样快**(21.7 vs 22.6 ms),`--sync-pose` 不是性能开关(更正记录见 [`render_budget.md` §15](render_budget.md))。
4. **`read_pixels()` 不带时间戳** → 需要自己绑定帧号/时刻,或加一个 SpFunc。
5. UE 版本绑定:当前 checkout 是 `ue-58`(UE5.8);`main` 对 UE5.5。

**定位结论**:SPEAR = **渲染端 + 场景操纵层**。

### 2.2 UE5 作为场景编辑器 + 渲染端

RM 的感知难点在灯效、材质、反光。`rm_sim_rune` 里质量最高的资产恰好就是这个(材质槽 machinery、`URuneVisualProfile` DataAsset 模式)。这些只有 UE 能做。

**资产管理**:`ue/` 是本项目 UE5.8 工程，`ue/Content` 是独立的 `rm_sim_content` Git submodule，由其 Git LFS 管理；`~/rm_sim_rune/SimUnreal/Content/` 只作为一次性迁移源。代价是 UE5.8 升级 + 材质/后处理重做。

### 2.3 MuJoCo 作为物理 + 机器人

**选对的理由**:接触/摩擦/关节/执行器成熟;URDF 可加载;**原生 IMU 传感器**(accelerometer / gyro / framequat / velocimeter / force / torque)带噪声模型;Python 原生。

**必须认账的三点**:

1. **单位**:MuJoCo 官方文档明确"does not specify basic physical units"(单位未定义),只要求内部自洽。URDF/ROS 生态是**米**,所以统一用米(见 D1)。
2. **坐标/旋转约定**:Unreal 左手、MuJoCo 右手、URDF/ROS 右手 Z-up;三套约定叠加,必须有且只有一处转换层 + 可执行校验。
3. **弹丸无 CCD**:17 mm 高速弹丸会穿模;解法见 [`projectile.md`](projectile.md)。

### 2.4 ROS2 作为通信

- **不继承** `rm_sim` 的"不链 rclcpp、DDS 直发"选型——那个前提是"ROS2 活在被 UE 链接的 C++ 进程里",而本架构里客户端是独立 Python 进程,前提消失。
- **继承它的教训**(与实现无关):默认图像不上 DDS;订阅侧单槽会静默丢样本(高频 IMU 要 SPSC 环);QoS 逐通道显式声明;读 `ROS_DOMAIN_ID`;时间戳只有一个来源。

---

## 3. 四条设计红线

> 完整论证与可执行判定见 [`architecture.md`](architecture.md)。

**红线 1 — MuJoCo 是物理真源,UE 是可渲染镜像。**
被同步的 UE 物体必须是 kinematic / 关闭物理。判定:关掉 UE 侧刚体物理,行为不变。

**红线 2 — 规则是纯逻辑第三层,不依赖 UE、不依赖 MuJoCo。**
判定:规则模块能脱离两者编译和单测(沿用 `rm_sim_rune` 6 个单测的判据)。

**红线 3 — 所有仿真数据都能追溯到同一个 sim clock。**
判定:每个数据包或其关联的 `FrameCommit` 带 `run_id + sim_tick + commit_id`;禁止用墙钟替代仿真时间。

**红线 4 — ROS2 通过独立适配层接入。**
判定:删掉 ROS2 adapter 后,核心仿真和 SPEAR 取图仍可运行。

---

## 4. 目标分层与目录

```
rm_sim_spear/
├── docs/                    # 本目录:计划、架构、决策、风险
├── sim/                     # ★ 与引擎无关的纯逻辑(红线 2)
│   ├── time/                #   唯一时间真源、固定步长、锁步驱动
│   ├── rules/               #   rune 规则 / 命中判定 / 计分(可单测)
│   ├── ballistics/          #   纯弹道/命中数学(可单测,不含 UE 类型)
│   └── math/                #   坐标系/旋转/单位转换(唯一转换层)
├── physics/                 # ★ MuJoCo 侧
│   ├── model/               #   MJCF 组装、URDF 导入、传感器定义
│   └── driver/              #   step / lockstep / 外部权威时钟
├── render/                  # ★ SPEAR 侧(编排进程)
│   ├── sync/                #   位姿推送、帧↔时刻绑定、对象池
│   └── camera/              #   相机配置、多模态取图
├── bridge/                  # ROS2 adapter + IDL(独立进程)
│   └── idl/                 # 标准消息扩展与版本化自定义消息
├── pipeline/                # 资产:UE 场景 → MuJoCo;URDF → MJCF + UE
├── ue/                      # UE5 工程 + SPEAR 插件扩展(SpFunc / UFUNCTION)
└── tests/                   # 引擎无关的确定性/边界测试
```

**依赖方向**:`sim → (无)`;`physics → sim`;`render → sim`;`bridge → sim`;`ue` 只通过 SPEAR RPC 被 `render` 调用。**没有环**;`sim` 不依赖任何引擎。

---

## 5. 阶段路线图

每个阶段以**一个能演示的东西 + 一条可测的验收**结束。

| 阶段 | 一句话 | 验收 |
|---|---|---|
| **0** | 环境与连通:UE5.8 + SPEAR 构建、MuJoCo、ROS2 环境选型 | 能取到一张图;`import mujoco` 通;ROS2 通信通 |
| **1** | **同步契约**(地基):单位/坐标/时间真源/提交点 | 1 m 自由落体 ≈0.452 s;图/位姿/IMU 可按 `commit_id` 配对;匀速物体图像位移与位姿位移一致 |
| **2** | 机器人接入:URDF → MJCF + UE 可视化,几何共注册 | 同一 URDF 两边几何重合、关节角一致 |
| **3** | ROS2 对外契约:时钟、传感器、云台命令 | 标准 ROS2 节点收发并按 `commit_id` 配对 |
| **4** | RM 场景 + rune 规则(移植纯逻辑 + 单测) | 6 个单测全绿;四条激活路径可走 |
| **5** | 弹丸子系统 | 40 颗/帧;扫掠不穿模;环数边界单测全绿 |
| **6** | 感知数据集(多模态 + 来源元数据) | 图/位姿/IMU 按 `commit_id` 配对;可复现回放 |
| **7** | ROS2 adapter + 多机器人 | 删 adapter 后核心照常;标准消息和命名空间隔离 |
| **8**(延后) | 无人机 / HIL | 高频环放在 MuJoCo/SITL 侧 |

---

## 6. 决策点

| # | 决策 | 结论/建议 | 何时必须定 | 文档 |
|---|---|---|---|---|
| **D1** | 单位制 | **米**(物理真源),UE 原生 cm 只在边界转换 | 阶段 0 | [`decisions.md`](decisions.md) |
| **D2** | 时间真源 | **MuJoCo sim time**,单一权威 | 阶段 0 | [`decisions.md`](decisions.md) |
| **D3** | ROS2 定位 | **独立标准适配层**;仿真拥有时钟;图像默认共享内存并提供 ROS2 兼容模式 | 阶段 0 | [`decisions.md`](decisions.md) |
| **D4** | 弹丸实现 | 独立 `ProjectileSubsystem`;UE 只做 QueryOnly 查询,规则只消费 `HitEvent` | 阶段 0 | [`projectile.md`](projectile.md) |
| **D5** | 同步模型 | 每个对外仿真帧生成一个 `FrameCommit`(内部可含 N 个 MuJoCo 子步);严格模式使用暂停 + 事务内解暂停/暂停,不用 `single_step` | 阶段 1 | [`architecture.md`](architecture.md) |
| **D6** | 帧来源 | 回读元数据必须携带 `commit_id`/`ue_frame_id`;不以单缓冲掩盖来源问题 | 阶段 1 | [`decisions.md`](decisions.md) |
| **D7** | ROS2 环境 | **系统 ROS2 Humble 独立进程**优先,不把 Python 版本耦合到 SPEAR | 阶段 0 | [`risks.md`](risks.md) |
| **D8** | 位姿同步带宽 | 40 个对象以内用 `send_async`;更多则加批量 UFUNCTION | 阶段 2 | [`architecture.md`](architecture.md) |
| **D9** | 资产管理 | `ue/Content` 使用独立 `rm_sim_content` submodule + Git LFS；rune 仅作为一次性迁移源 | 阶段 4 | [`content_management.md`](content_management.md) |
| **D10** | URDF 流水线 | URDF 单一描述源 → MuJoCo(MJCF)+ UE(visual) | 阶段 2 | [`architecture.md`](architecture.md) |

---

## 7. 与两个弃案的关系

| | 取 | 不取 |
|---|---|---|
| `~/rm_sim` | 需求定义、验收标准、"时间戳只有一个来源且是仿真时间"等教训、ADR 的写法 | 自研 transport、Cosys-AirSim 作为依赖、UE 内 DDS 直发、UE Chaos 当机器人物理真源、手工 actor 绑定图 |
| `~/rm_sim_rune` | rune 纯逻辑 + 6 单测;UE 资产/材质槽 machinery/`URuneVisualProfile`;弹丸的碰撞设计;真机接口形状(`sp_vision_25`) | 相机绑 Pawn、视点差分 IMU、编译期常量分辨率、手工 level actor 图、"恰好 5 臂"硬编码 |

详见 [`reuse.md`](reuse.md)。
