# 复用与不继承

> 原则:**引用两个弃案的"内容"(需求、教训、验收、领域逻辑、资产),不继承它们的"实现选型"。**
> 输入:`~/rm_sim`(AirSim 合并路线)、`~/rm_sim_rune`(rune 原型)。

---

## 1. 一句话

- `~/rm_sim`:想法和教训值钱,**选型不要**(它把力气花在"自己造一个 AirSim 替代品")。
- `~/rm_sim_rune`:**纯逻辑 + 单测 + UE 视觉资产**是原型里质量最高的部分,整块拿;**结构债不要**。

---

## 2. 从 `~/rm_sim` 取

### 2.1 需求与验收标准

- **"产物是带时间轴的传感器流,不是一堆图片"** 这条定位。
- 视觉仿真的验收:外部程序按 ~100 Hz 拿到图 + 对应 `commit_id` 的位姿;端到端延迟预算。
- 传感器验收:可在配置里声明、按名寻址、带噪声模型与**时间戳**。
- 控制验收:外部脚本驱动载具完成一条轨迹;**人为注入延迟后闭环行为按预期劣化**。

### 2.2 教训(与实现无关,必须继承)

| # | 教训 | 我们的落地 |
|---|---|---|
| L1 | **时间戳只有一个来源,且必须是仿真时间**。`rm_sim_rune` 用墙钟 `FDateTime::UtcNow()` 打帧时间戳是**已确认的缺陷**(不可复现、无法重放、来源配对失去意义) | 红线 3;`decisions.md` D2 |
| L2 | **来源可证明**是硬指标;AirSim 自己注释"做不到严格同帧" | 锁步 + `commit_id` 元数据 |
| L3 | **图像/深度不上 DDS** | 红线 4 |
| L4 | 订阅侧**单槽 latest 会静默丢样本**,高频 IMU 必须小容量 SPSC 环 + 消费线程 drain | `architecture.md` §5 |
| L5 | QoS 必须**逐通道显式声明**;必须读 `ROS_DOMAIN_ID` | D3 |
| L6 | 新增"一种东西"**不许**改 dispatch 的 if-else(反面案例:AirSim `SimHUD::createSimMode()` 的 private 非虚硬编码链) | 注册表 dispatch |
| L7 | **数据驱动场景描述**,不手工绑 level actor 图 | `architecture.md` §6.3 |
| L8 | 传感器是**注入式真值的纯读者**,同一份代码 headless / 引擎内都能跑 | 传感器设计 |
| L9 | **时间所有权是封闭集合**:墙钟 / 自走 / 外部按拍 / 外部完全接管。未知外部时钟应被关进这个枚举 | `TimeSource` 留 `External` 缝 |
| L10 | **外部权威失联必须有降级路径**并上报状态,绝不永久挂住 | 若做 HIL 时适用 |
| L11 | "契约单真源 + 机械校验"的思路(协议头带版本 + `static_assert`) | 数据 schema / idl |

### 2.3 反面案例(明确不学)

AirSim 的四个硬伤,我们靠选型规避:

| 硬伤 | 我们的规避 |
|---|---|
| 图像走 msgpack + PNG + **同步请求-响应 RPC** | SPEAR 的共享内存 + 异步回读 |
| 回读用未公开的 `RHICmdList.ReadSurfaceData`,render thread 上,游戏线程**自旋等待** | SPEAR 的 `FRHIGPUTextureReadback` + 三种缓冲 |
| 图像与位姿**不做来源配对** | `commit_id` + 回读元数据 |
| 新增 SimMode 必须 fork 上游 | 注册表 |

---

## 3. 从 `~/rm_sim` **不取**(选型)

| 不取 | 原因 |
|---|---|
| **Cosys-AirSim 作为依赖** | 渲染路径与同帧对齐是硬伤,而 SPEAR 已更好;它还会带来供应链/引擎升级/扩展点三类持续成本 |
| **自研 `transport/`(协议 + 共享内存 + seqlock)** | SPEAR 的 RPC + 共享内存已覆盖 |
| **"UE 插件内 DDS 直发,不链 rclcpp"** | 该选型的前提是"ROS2 活在被 UE 链接的 C++ 进程里";本架构客户端是独立 Python 进程,**前提消失** |
| **UE Chaos 当机器人物理真源** | 物理统一交给 MuJoCo,避免双头 |
| **`core/` + `modules/` 的自研内核分层** | 那是为了脱离 AirSim 而生的;我们的"引擎无关层"是 `sim/`(规则/时间/弹道/数学),范围小得多 |
| **手工 level actor 绑定图** | 用数据驱动场景描述 + 按名索引 |

> 注意:**"不依赖 AirSim" ≠ "不抄 AirSim 的思想"**。它的 `UpdatableObject` 生命周期(reset 先于 update)、注入式 `GroundTruth`、三个正交 API + 按名索引、引擎/内核汇合点的划分,仍是有价值的**设计参考**。

---

## 4. 从 `~/rm_sim_rune` 取

### 4.1 纯逻辑 + 单测(**质量最高的部分,整块拿**)

| 组件 | 说明 |
|---|---|
| `RuneRuleStateMachine.{h,cpp}` | 规则状态机(未激活/正在激活/已激活/激活失败),含约 286 行测试 |
| `RuneMotionModel.{h,cpp}` | 转速模型(解析解 vs 1000 步积分的**帧率无关性**测试) |
| `RuneHitMath.{h,cpp}` | 环边界、NaN 拒绝;可改成引擎无关版本 |
| `RuneAutoHitScheduler.{h,cpp}` | 自动命中调度 |
| `RuneVisualRules.{h,cpp}` | 视觉意图映射 |
| `RuneTypes.h` / `RuneSnapshot.h` | 类型与快照 |
| `RunePlayerController::ComposeShotTransform` | 发射变换,已有单测 |

**处置**:搬进 `sim/rules/`,去 UE 宏;修掉已知小缺陷——`RuneMotionModel::AdvanceTime` 返回内部可变状态的引用、`RuneTypes.h` 的 `EFlowEffectState` 死代码、几处硬编码 5(见 4.3)。

**为什么直接搬而不是重写**:只有直接搬,**原测试照跑**才能作为"抽象没被搬坏"的证据。

### 4.2 UE 视觉资产

| 组件 | 说明 |
|---|---|
| `URuneVisualProfile`(DataAsset) | 数据资产模式的视觉配置,**值得推广到其他领域** |
| `Rune.cpp` 的材质槽 machinery | `CollectVisualSlots` / `ApplyMaterialToSlots` / 原材质恢复 |
| `BP_Rune` / `DA_RuneVisualProfile` 及 `SimUnreal/Content` | 能量机关的视觉与灯效 |
| `~/rm_sim_rune/SimUnreal/Content/`(约 1.8 GB,LFS) | 一次性迁移到本项目 `ue/Content/` 的 RM 场景与 UE 资产来源 |

> 灯效/材质只有 UE 能做,这是感知仿真的核心资产。

### 4.3 弹丸设计

见 [`projectile.md`](projectile.md)。要点:UE `QueryOnly` 扫掠球 + 盒状靶代理 + 纯函数环数 + 专用碰撞通道。

### 4.4 真机接口形状

`~/sp_vision_25` 的相机和云台字段可作为语义参考,但不再承诺 1:1 复刻其 ABI 或 `steady_clock` 时间类型。仿真对外以标准 ROS2 消息和版本化自定义 IDL 为准(见 `architecture.md` §7)。

### 4.5 需求文档

`~/rm_sim_rune/CLAUDE.md` —— 能量机关的完整时序与灯效要求(1/3π rad/s、`spd=a·sin(ωt)+b`、2.5 s 窗口、大符两组、1–10 环、径向 1 mm、17 mm 弹丸)。作为验收标准。

### 4.6 打包经验

`package_linux.sh` 的 UAT `BuildCookRun` + ZenServer 处理 + 用 `jq` 改写 `Saved/Cooked/.../ue.projectstore` 里的 `.zenserver.hostname`——血泪经验,直接继承。`check_assets.sh` 的资产 SHA256 校验思路也值得保留。

---

## 5. 从 `~/rm_sim_rune` **不取**(结构债)

| # | 债 | 我们的做法 |
|---|---|---|
| E1 | 相机是 Pawn 的属性,只有一台,绑 `GetFirstPlayerController()` 视角 | 相机与观察者解耦,多相机可声明 |
| E2 | IMU 由玩家视点差分合成,**不是刚体传感器** | 真刚体采样(MuJoCo 原生 IMU 传感器) |
| E3 | 采集与 IMU 分居两处,彼此不共享任何东西 | 统一传感器抽象,同一时间轴 |
| E4 | 分辨率/采集频率是编译期常量 | 配置化 |
| E5 | 手工绑定的 level actor 图,manifest 无消费者 | 数据驱动场景描述 + 校验 commandlet |
| E6 | "恰好 5 臂 / 2 rune / 1 director" 在三处各自强制 | 去硬编码,数量由场景描述决定 |
| E7 | 模式切换有两条路径,状态可失同步 | 单一入口 |
| E8 | `ReadPixels` 每帧阻塞游戏线程 | SPEAR 异步回读 + 缓冲 |
| E9 | 发布者侧没有测试 | 两端都有测试 |
| E10 | SDK CMake 反向依赖 UE 插件目录 | 引擎无关的 `sim/` 独立构建 |
| E11 | 文档与协议不一致(README 写 1280×720,头文件 1440×1080) | 单一真源 |
| E12 | 领域代码里画 debug 几何 | debug 与逻辑分离 |
| E13 | `AdvanceTime` 返回内部可变状态的引用 | 返回值语义 |

---

## 6. 逐组件处置总表

| 来源 | 组件 | 处置 |
|---|---|---|
| `rm_sim_rune` | rune 纯逻辑 + 6 单测 | **[搬]** 进 `sim/rules/`,修小缺陷 |
| `rm_sim_rune` | 弹丸碰撞设计 + 环数 + 单测 | **[搬+改]** 见 `projectile.md` |
| `rm_sim_rune` | UE 材质槽 machinery / `URuneVisualProfile` / Content | **[搬]** |
| `rm_sim_rune` | 打包脚本经验 | **[搬]** |
| `rm_sim_rune` | 相机/IMU/场景绑定/模式切换 | **[重写]** |
| `rm_sim` | "带时间轴的传感器流"定位、验收标准 | **[搬]** 作为需求 |
| `rm_sim` | 时间戳/同帧/无损订阅/QoS 等教训 | **[搬]** 作为红线 |
| `rm_sim` | ADR 的写法(含回退方式) | **[学]** |
| `rm_sim` | transport / AirSim / DDS 直发 / Chaos 物理 / 自研内核 | **[弃]** |
| `~/sp_vision_25` | `io/camera`、`io/gimbal` 字段语义 | **[参考]** ROS2 适配层不复刻其 ABI |
| `~/spear` | 渲染/共享内存/帧事务/多模态 GT | **[基座]** |
| 全新 | 单位转换层、时间真源、URDF 流水线、弹丸编排 | **[新写]** |
