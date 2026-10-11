# 弹丸设计

> 决策见 [`decisions.md`](decisions.md) D4。本文是对 `~/rm_sim_rune` 现有实现的拆解,以及在新架构下的复用与改造。

---

## 1. 需求

| 项 | 值 |
|---|---|
| 数量 | **≤ 40 颗/帧**(后续可改) |
| 尺寸 | 17 mm 直径 |
| 视觉 | **只要荧光绿**,无额外视觉要求(不需要曳光/枪口焰/火花) |
| 命中输出 | 装甲块 id、径向距离(mm)、**环数 1–10** |
| 精度要求 | 径向 1 mm;1–10 环;命中"被随机点亮"的那一块 |
| 时序 | 2.5 s 窗口、大符"1 秒内击中第二块"、激活失败判定 |

**结论:不需要任何粒子系统/Niagara。** 40 颗的规模下,"弹丸"就是"一个小绿球 + 一条确定性弹道"。

---

## 2. `rm_sim_rune` 的实现拆解

### 2.1 弹丸本体 `ARuneProjectile : AActor`

[`RuneProjectile.cpp`](../../rm_sim_rune/SimUnreal/Plugins/sim/Source/sim/Private/RuneProjectile.cpp)(共 73 行):

| 组件 | 设置 | 作用 |
|---|---|---|
| `USphereComponent`(根) | 半径 **0.85 cm**(= 17 mm 直径),profile `RuneProjectile` | 唯一碰撞体 |
| `UProjectileMovementComponent` | `InitialSpeed = MaxSpeed = 1500` cm/s、`bShouldBounce=false`、**`bSweepCollision=true`**、**`bForceSubStepping=true`**、`MaxSimulationTimeStep=1/120`、`ProjectileGravityScale=1` | 弹道 + 命中 |
| `UStaticMeshComponent` | `/Engine/BasicShapes/Sphere`,**scale 0.017** → 1.7 cm | 视觉 |
| `UPointLightComponent` | 绿 `(0.05, 1.0, 0.15)`,强度 250,半径 40 | 荧光感 |

外加 `InitialLifeSpan = 5.0f`,`bCanEverTick = false`。

**两个关键开关**:

- **`bSweepCollision = true`** → 做的是**扫掠**(连续碰撞),不是"每帧看球心在哪"。这是高速下不 tunneling 的**真正原因**。
- **`bForceSubStepping = true` + `MaxSimulationTimeStep = 1/120`** → 扫掠段被切成 ≤1/120 s 的小段,保证重力积分精度、并让每段扫掠足够短。

### 2.2 碰撞过滤:一对专用通道,`QueryOnly`

[`DefaultEngine.ini`](../../rm_sim_rune/SimUnreal/Config/DefaultEngine.ini):

```ini
+DefaultChannelResponses=(Channel=ECC_GameTraceChannel1,...,Name="RuneProjectile")
+DefaultChannelResponses=(Channel=ECC_GameTraceChannel2,DefaultResponse=ECR_Ignore,...,Name="RuneArmor")
+Profiles=(Name="RuneProjectile",CollisionEnabled=QueryOnly,...,CustomResponses=((Channel="Pawn",ECR_Ignore),(Channel="RuneArmor",ECR_Block)))
+Profiles=(Name="RuneArmor",     CollisionEnabled=QueryOnly,...,CustomResponses=((WorldStatic,ECR_Ignore),(WorldDynamic,ECR_Ignore),(Pawn,ECR_Ignore),(RuneProjectile,ECR_Block)))
```

**两边都是 `CollisionEnabled=QueryOnly`** —— 这是整套设计的关键(见 §3)。

### 2.3 装甲靶 `URuneArmorTargetComponent : UBoxComponent`

[`RuneArmorTargetComponent.cpp`](../../rm_sim_rune/SimUnreal/Plugins/sim/Source/sim/Private/RuneArmorTargetComponent.cpp):

- 扁盒代理:`SetBoxExtent(RadiusCm, RadiusCm, ThicknessCm*0.5)`,即 XY 是圆盘的**外接正方形**,Z 是厚度;
- profile `RuneArmor`,持有 `Rune`(TWeakObjectPtr)、`ArmorIndex`、`TargetRadiusCm`;
- 由 `ARune` 按臂初始化,校验 `ArmorIndex ∈ [0,5)`、`RadiusCm > 0`、`ThicknessCm > 0`。

### 2.4 命中处理 `OnHit`

[`RuneProjectile.cpp:50-73`](../../rm_sim_rune/SimUnreal/Plugins/sim/Source/sim/Private/RuneProjectile.cpp):

```
bProcessedHit 去重 → 立即关碰撞
Cast<URuneArmorTargetComponent>(OtherComp)
   → Rune->HandleArmorHit(Target->GetArmorIndex(), Hit.ImpactPoint, ShooterTeam)
SetLifeSpan(0.05)
```

`ARune::HandleArmorHit` 再用靶组件的 transform 与半径调用环数计算,然后交给 `RuleState.HandleHit(ArmorIndex, RadiusMm, Ring)`。

> **要点**:`Hit.OtherComp` **直接给出"是哪一块装甲"**。这是 UE 碰撞查询天然提供、而 Niagara 碰撞事件负载给不了的东西(`FNiagaraCollisionEventPayload` 里没有 actor/component)。

### 2.5 环数:纯函数 `RuneHitMath::Calculate`

[`RuneHitMath.cpp`](../../rm_sim_rune/SimUnreal/Plugins/sim/Source/sim/Private/RuneHitMath.cpp)(28 行):

```
Local    = TargetTransform.InverseTransformPosition(ImpactPoint)
RadiusCm = sqrt(Local.X² + Local.Y²)      // 装甲板局部 XY 平面就是环盘
RadiusMm = round(RadiusCm * 10)
if RadiusMm > TargetRadiusMm: 无效
Ring     = clamp(10 - floor(RadiusMm / (TargetRadiusMm/10)), 1, 10)
```

带 NaN 与非正半径防护。**有单测**([`RuneHitMathTests.cpp`](../../rm_sim_rune/SimUnreal/Plugins/sim/Source/sim/Private/Tests/RuneHitMathTests.cpp)):0→10 环、5 mm→9、44→2、50→1、51→无效,以及镜像靶、法向偏移、NaN、零/负半径。

### 2.6 发射 `ComposeShotTransform`

[`RunePlayerController.cpp:18-22`](../../rm_sim_rune/SimUnreal/Plugins/sim/Source/sim/Private/RunePlayerController.cpp):

```
ComposeShotTransform(CameraWorldTransform, ShotFrameFromCamera) = ShotFrameFromCamera * CameraWorldTransform
```

即"枪口相对相机的固定偏移"。有单测([`RuneProjectileTests.cpp`](../../rm_sim_rune/SimUnreal/Plugins/sim/Source/sim/Private/Tests/RuneProjectileTests.cpp))验证相机局部前向偏移被旋转到世界系。`Fire()` 里还有 `MinimumFireIntervalSeconds` 节流与 `AlwaysSpawn`。

---

## 3. 为什么它在新架构里成立(不违反红线 1)

> **两边都是 `QueryOnly`,意味着 UE 在这里没有做任何刚体物理仿真。它只提供空间查询结构。**

- 弹丸飞行是 `UProjectileMovementComponent` 的**解析式固定步长积分 + 扫掠**,不是刚体动力学(无质量、无接触力、无约束求解);
- 装甲靶是 `QueryOnly` 的 kinematic 盒子,自己不参与仿真;
- 所以 UE 扮演的是"**碰撞查询服务**",不是"第二个物理世界"。

这与"MuJoCo 负责刚体/接触/机器人"**不冲突**:弹丸是独立的**碰撞查询子系统**,不是 MuJoCo 刚体。UE 暂停仿真时弹丸继续飞行也不改变 MuJoCo 核心状态;命中结果通过数据事件交给规则层。

**相对于自己写 `mj_ray` 的优势**:

1. `Hit.OtherComp` 直接定位装甲块;`mj_ray` 只给 geom id,还要自己维护映射并同步装甲位姿;
2. 扫掠 + 子步进已调好;
3. 环数数学已是纯函数且有单测。

---

## 4. 复用 / 改造清单

### 4.1 直接照搬

| 项 | 说明 |
|---|---|
| 通道/profile 对(`RuneProjectile` / `RuneArmor`,均 `QueryOnly`) | 原样写进新工程 `DefaultEngine.ini` |
| 17 mm 球 + `bSweepCollision` + `bForceSubStepping` + `MaxSimulationTimeStep` | "不 tunneling"的核心,**别改** |
| 扁盒靶 + `TargetRadiusCm` / `ThicknessCm` | 靶的几何约定 |
| `RuneHitMath::Calculate` + 其单测 | 改成引擎无关版本后进 `sim/rules/`,单测照跑 |
| `ComposeShotTransform` + 其单测 | 发射变换约定 |
| 一次性命中去重 + 立即关碰撞 + 短 LifeSpan | 防重复计入规则 |

### 4.2 必须改造

| # | 项 | 改造 |
|---|---|---|
| 1 | **事件来源** | 弹丸运动可使用 UE `DeltaTime`;但命中事件必须带 `shot_id`、`target_id`、相对命中点、`ue_frame_id` 和关联 `commit_id`,不能假装是 MuJoCo 物理结果 |
| 2 | **40 并发** | 改成**预分配对象池**(只改 transform + 可见性),不做每帧 spawn/destroy,避免注册开销与 GC 抖动 |
| 3 | **绿色** | 用 **Unlit + Emissive** 材质,弃用点光(点光在 17 mm 球上读不出来,40 个还显著增加渲染开销) |
| 4 | **初速** | 原型是 1500 cm/s(15 m/s);按真机规格设(17 mm 一般 ~30 m/s → 3000 cm/s) |
| 5 | **从 SPEAR/Python 发射** | 做成 **Blueprint**(`BP_RuneProjectile`)由 `unreal_service.spawn_actor(uclass=...)` 生成;或加一个 `UFUNCTION` 包装的发射入口 |
| 6 | **荧光绿在最终图像里** | 相机读 `final_tone_curve_hdr`,会过 tone curve(和可能的 bloom)。若弹丸还要被感知算法看到,Emissive 值要给到 tone mapping 后仍饱和,并渲染一张验证 |

### 4.3 已知近似(知道即可,不必改)

- 靶是**盒子**不是圆盘:四角落半径外 → 判无效(正确);但打在**侧面(厚度)**上的点投影到 XY 后可能落在半径内被判有效。板薄时可忽略。

---

## 5. 两条实现路径

| | **A1:沿用 UE 积分** | **A2:规则层积分,UE 只做扫掠查询** |
|---|---|---|
| 弹道由谁算 | `UProjectileMovementComponent` | `sim/ballistics/`(纯函数,米制,引擎无关) |
| UE 的角色 | 积分 + 查询 | **只做 sphere sweep** |
| 事件语义 | UE 帧/查询时间;与仿真 tick 解耦 | 可选用仿真 tick,但仍通过相同 HitEvent 接口输出 |
| 复用程度 | **几乎全搬** | 搬通道/靶/环数,去掉 movement component |
| 可单测性 | 环数是纯函数;弹道不好单测 | **弹道 + 命中都可单测** |
| 改动量 | 最小 | 中等 |

**建议**:先走 **A1**——改动量最小,能最快确认视觉和相对碰撞位置。A1 的输出只作为独立 `HitEvent`;如果后续需要可复现弹道,再将积分替换为 A2,上层接口不变。

> A2 的实质:弹道在 `sim/ballistics/` 积分,再由 UE 批量执行球扫掠查询。查询必须提供批量 UFUNCTION,不能按每个物理子步发单个 RPC;命中结果统一转换为 `HitEvent`。

---

## 6. 验收

| # | 验收项 |
|---|---|
| 1 | 40 颗/帧无掉帧 |
| 2 | 扫掠不穿模(连发高速弹丸不穿过装甲板) |
| 3 | 环数边界单测全绿(0→10、5 mm→9、44→2、50→1、51→无效、镜像靶、法向偏移、NaN) |
| 4 | 命中"被随机点亮"的装甲块时,规则状态机按预期推进(2.5 s 窗口 / 大符两组 / 激活失败) |
| 5 | 命中事件包含完整来源: `shot_id`、`target_id`、相对命中位置、`ue_frame_id`、`commit_id` |
| 6 | 弹丸独立运行不改变 MuJoCo/规则核心;规则只通过 `HitEvent` 消费命中 |
