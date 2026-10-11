# URDF 简化方案（2026-10-10 定）

## 0. 一句话

**「简化 URDF」不是把它变小，是按消费者拆开**：MuJoCo 不需要视觉，UE 不需要物理。
现在一份 `mujoco.urdf` 同时喂两边，于是两边都在读自己用不上的东西。

## 1. 现状（全部实测）

| 事实 | 数值 |
|---|---|
| links / joints | 13 / 12（4 轮 `continuous`、2 云台 `revolute`、6 `fixed`） |
| `visual` / `collision` | **955 / 955** |
| **`collision` 是不是 `visual` 的副本** | **13/13 link 完全一致**——`geometry` 与 `origin` 逐字段相同，零例外 |
| 唯一源网格 | 210 个 / **2,157,746** 三角形 |
| 烘平后（`gltf_skinned`） | **16,786,420** 三角形 / 602 MB（重复倍率 **7.78×**） |
| `mujoco.urdf` 实际 geom | 952 `visual` + 952 `collision` + 5 = **1909** |
| `simple_collision=True` 之后参与碰撞的 | **5** |
| 全量资产导入 | 峰值常驻 **23.3 GB → 被 OOM 杀掉** |
| 蒙皮路径帧时间 | **10.05 ms**（关动态阴影）/ 16.45 ms（开）vs 现状 **23.54 ms** |

### 三条最重要的观察

1. **`collision` 是 `visual` 的机械副本。** 955 比 955，13/13 link 逐字段相同。
   这是 CAD→URDF 导出器（SW2URDF 一类）的典型行为，`<collision>` **一点信息都不携带**。
2. 所以 `mujoco.urdf` 的 1904 个 mesh geom 里，**一半是纯冗余**；另一半就算留着，
   也会被 `_apply_simple_collision` 全部打成 `contype = conaffinity = 0`。
3. **7.78× 的重复是蒙皮网格的固有代价，不是 bug。** 蒙皮里一个顶点只属于一根骨骼，
   所以"4 个轮子"在烘平的网格里必然就是 4 份顶点。

## 2. 目标：三个产物，三个消费者

| 产物 | 消费者 | 它需要 URDF 的什么 | 不需要什么 |
|---|---|---|---|
| **`physics.urdf`**（→ 组装成 `scene.mjcf`） | MuJoCo | 运动学树、关节类型/轴/限位、惯性、**碰撞体** | 952 个视觉 mesh |
| **`visuals.glb`** | UE 渲染 | **只剩外观**（骨骼 = link 的位姿由 MuJoCo 给） | 惯性、关节限位、传动、碰撞体 |
| **`manifest.json` + `scene_binding_plan.json`** | 两端 + 人 | **稳定名字**（跨端主键） | — |

"简单"的标准对三者完全不同：MuJoCo 要**能算**，UE 要**投影像素对**，人要**名字稳**。

## 3. 五步

### 第 1 步：给 MuJoCo **设计一套碰撞体**（最大的一刀）

⚠️ **这一步在 2026-10-10 晚被修正过。** 初版写的是"删掉视觉、留下碰撞"，
那是错的：**留下来的那 955 个 CAD 零件凸包恰恰是错的碰撞体**。
业界共识是 `<collision>` 和 `<visual>` 是**两套独立几何**，碰撞体要人设计——
见本文档 §6「业界一般怎么做」的四条来源。

现在 1909 个 geom，1904 个不参与任何计算，但 MuJoCo 编译时仍要读全部顶点、算 AABB、占内存。

**做法**：保留现有 `wheel_cylinder()` 的推导（它需要轮 body 的 mesh 顶点来定半径；
这是本项目已经做过的一次"人设计碰撞体"），再补上其余 link，然后**把 955 个 mesh 全部丢掉**：

```
assets/.../总装.urdf (955 visual + 955 collision，二者逐字段相同)
   ↓  pipeline（离线，一次性）
physics.urdf  = 惯性 + 关节 + 按 link 设计的碰撞原语
                 base_link   → box（底盘）
                 link_1..4   → 4 个 cylinder（由 wheel_cylinder() 推出）
                 link_5/6/7  → box / sphere（云台 + NUC + 相机）
                 armor_link_*→ box（装甲板）
   ↓  加载 + 追加场地静态 body
scene.mjcf    = 一个 MJCF（<include> 分工），MuJoCo 直接读
```

**预期**：`ngeom` 从 **1909 → ~15**，`mesh_vert` 从 1900+ 个 mesh 降到 0。

**判据必须重新定**（因为碰撞集变了，旧的接触表**本来就该变**）：
- 不变：`nq`/`nv`/`nu`、惯性、关节轴/限位
- **要变**：静止接触数（8 → 更多）、`base_link z`（73.413 mm → 变，因为底盘终于有碰撞体了）
- 新判据：**四个轮子必须在 ±1 mm 内同时接触地面**（沿用
  `tests/test_mujoco_physics_options.py::test_wheels_touch_the_plane_instead_of_sinking_through_it`），
  且**没有任何非轮子 link 接触地面**——这一条正是现在 `simple_collision=False` 会失败的地方
  （`floor <-> link_6 dist = −0.243 mm`，机器人靠 NUC 盖板站着）

**副作用（是好事）**：`--mujoco-viewer` 里能看到的就是碰撞体。
调试物理本来就该看碰撞体，外观是 UE 的事。

**但这个"看碰撞体"2026-10-10 之前是强制的，而且看不成**：URDF 导入器把 `<collision>` 放进
**group 0**、`<visual>` 放进 **group 1**，而 MuJoCo 默认 `geomgroup = [1,1,1,0,0,0]`——
**两组都画**。于是 955 个视觉网格上又叠了 955 个 CAD 碰撞壳，加上生成的那 5 个碰撞体，
关节/轮子/云台全被盖住（用户原话："看不到里面的关节，甚至盖过了轮子"）。
现在 `MujocoDriver.hide_collision_geometry()` 把所有碰撞几何搬进 **group 3**（MuJoCo 约定的
"只见碰撞"组），默认画面就是机器人本体；要看碰撞体在 viewer 左侧 Rendering 面板勾上
Geom Group 3。**地面留在 group 0**，它是参考面不是碰撞壳。纯显示改动，接触一个没变。

### 包围盒是不是太大了？

量过了，**不大**：`shell_base_link` = **584 × 608.2 × 190.5 mm**，放在 base_link 的
(141, −261.9, 48.6) mm。去掉保险杠/包围/挡板这些外圈件重算，"核心"也只有
571 × 608 × 188 mm——**差 2 %，缩小不了**。这就是机器人真实占地（RMUC 上限 600×600×500）。

而且**它不是支撑**：静置时盒子底面在世界 z = 73.478 − 46.65 = **+26.8 mm**，悬在空中；
`ncon = 8`，8 个接触**全在地面↔四个轮柱**上，每个轮子 2 个。轮子才是接地的那个。

### 第 2 步：接受蒙皮的 7.78×，靠第 3 步解决

考虑过"躯干用蒙皮、4 个轮子用挂在骨骼上的独立 `StaticMeshComponent`"（可共享网格）。
**不采用**：§18 已实测"每帧代价跟着**改变变换的组件数**走"，而轮子是**一直在转**的——
给它们独立组件正好回到那个坑。蒙皮网格用一个 primitive 带 13 个 link 恰恰是它的优势。

### 第 3 步：按投影像素减面

工具已有：[`pipeline/decimate_meshes.py`](../pipeline/decimate_meshes.py)。

- **改 210 个源文件就等于改 16.79M 的输出**（7.78× 放大）。其中 **10 个源网格占 93.8%**，
  第一刀就砍它们。
- **那个 8 mm 轴承是 79,680 三角形、被用了 160 次 = 整台机器人的 76%。**
  在 512×384 下 1 px ≈ 1.17 mm，**它整体只有 10 px 宽**——减它没有任何视觉代价。
- 预算是**投影像素数**：`--pixel-size-m`（默认 1.2 mm）× `--tris-per-pixel`（默认 4.0）。
- 误差**实测闭环**：两个表面各采 5 万点、双向 `RaycastingScene` 量距离，超差就把预算 ×4 重试。
- **只重写 `links[].visuals`**，一个字都不碰 `links[].collisions`（有单测钉住这条）。
- **诚实的地方**：QEM 有地板。`新电池` 从 714,188 怎么也只能减到 4.1 万，目标给 200 也一样；
  工具写 `attempts` 数组如实报告"够不到"。

### 第 4 步：定一套生产资产，接进主入口

现在 Content 里有 **4 套**蒙皮资产，**没定过哪套是生产用的**：

| 资产 | mesh 大小 |
|---|---|
| `RMSim/rmsim_skinned_small/SkeletalMeshes/zongzhuang_skinned.uasset` | 137 MB |
| `RMSim/zongzhuang_lod1px/SkeletalMeshes/` | 58 MB |
| `RMSim/proxy_2000/SkeletalMeshes/zongzhuang_proxy_mid.uasset` | 39 MB |
| `RMSim/proxy_200/SkeletalMeshes/zongzhuang_proxy_tiny.uasset` | 1.6 MB |

- 定一个唯一命名，在本文档里钉住"生产用哪一个"。
- **`tools/run_spear_sim.py` 里现在一个 `skeletal` 字样都没有**——蒙皮只接在
  `tools/run_physics_sim.py --pose-bones`。**那 13.5 ms 还没拿到手。**
- 所以：`run_spear_sim.py` 加 `--pose-bones` 并**默认开启**。

### 第 5 步：验收判据（照渲染预算的规矩，不允许只看数字）

- **同会话 A/B**，`trace.csv` 的 `frame_ms` 中位数，512×384，`--save-images none --no-capture`
- **逐像素比图**（`r.Nanite=0` 那次的教训：先看数字再验图才敢下结论）
- `min_free_mb ≥ 1024`
- **记录导入峰值 RSS**，不允许再 OOM
- `bone_vs_actor` 校验 `max_delta_cm == 0.0`，13/13 匹配

## 4. 明确不做

| 不做 | 为什么 |
|---|---|
| **为了渲染去改 URDF 的 `<collision>`/joint** | URDF 是 MuJoCo 的输入、是物理真源。渲染侧只改 `manifest.json` 的 `links[].visuals` |
| **删 joint 来"简化"** | 物理侧一个都不能删。云台那 2 个 joint 该修 zero pose，不是删 |
| **手工减面覆盖 `collisions`** | `wheel_cylinder` 的半径/轴向来自轮 body 的 mesh 顶点，砍了就量错（§20.6） |
| **给场地 mesh 当碰撞体** | MuJoCo 只做凸包（[`urdf_workflow.md`](urdf_workflow.md) 有实测），墙/凹槽会被填平 |
| **删只有 1 mm 的零件来省帧时间** | §20.4：关掉阴影后，13k 到 1.2M 三角形在帧时间噪声里。减面只为**能导入**和**资产体积** |

## 5. 顺序与依赖

```
第 1 步（物理瘦身）—— 独立，风险最低，先做
第 3 步（减面）    —— 独立，离线，可与第 1 步并行
第 4 步（接线）    —— 依赖第 3 步产出的资产；这是拿到 13.5 ms 的那一步
第 2 步            —— 已决定不做
第 5 步            —— 每步之后都要做
```

**最小可用路径**：第 1 步 + 第 4 步（用现有 `proxy_200` 那套 1.6 MB 的资产先把管线跑通），
拿到基准后再做第 3 步把质量补回去。

## 6. 业界一般怎么做（2026-10-10 调研）

### 骨架是同一条，三代工具都一样

```
CAD (SolidWorks / Onshape / Fusion)
  │ ① 导出：视觉全分辨率 + 碰撞简化        ← 第一刀在导出时，不在导入后
  ▼
URDF (sw2urdf / onshape-to-robot)
  │ ② 清理 + 减面：去重顶点、修法线、三角化、Decimate
  ▼
视觉网格 + 碰撞网格（两套独立文件）
  │ ③ 碰撞：原语 > 凸包 > 凸分解（VHACD / CoACD）
  ▼
Gazebo / MuJoCo / Isaac Sim
```

### Blender 用不用？—— ROS/Gazebo 圈是标准动作

[《Blender Workflow for Cleaning CAD Meshes for ROS 2 / Gazebo》](https://mohammadrobot.github.io/ROS/Blender-CAD-Meshes/)
把步骤写死了：

| 步 | 操作 | Blender 快捷键 |
|---|---|---|
| 1 | 去重顶点 | `M` → By Distance |
| 2 | 修法线（翻面的面在 Gazebo 里会消失） | `Shift+N`，`Alt+N` 翻选中的 |
| 3 | 三角化 | `Ctrl+T` |
| 4 | **视觉减面** | Decimate modifier，**ratio 0.2–0.4** |
| 5 | Apply Transform（不 apply 会导致导入尺寸/朝向错） | `Ctrl+A` |
| 6 | **碰撞再减一遍** | 复制一份，Decimate **ratio 0.05** |

它还给了**预算表**和一句直接命中我们的话：

| Mesh | 推荐面数 |
|---|---|
| Visual | **< 50k faces** |
| Collision | **< 5k faces** |

> "**Keep your visual and collision meshes separate.** This gives better performance and more
> stable physics than reusing one heavy mesh for both."

> "Unstable collisions → tiny CAD details in collision mesh → **remove screws/threads/fillets
> from collision mesh**"

而且有人专门为"SolidWorks → URDF → 减面"写了 Blender 批量减面脚本
（[`blender_urdf_visualizer`](https://github.com/mcevoyandy/blender_urdf_visualizer)）：
*"Often when I'm generating URDFs from SolidWorks I need … decimate the meshes so they are a
reasonable file size."*

**但 Blender 不是唯一的路**，它主要属于 ROS/Gazebo 圈。同样的活在别处换个工具：

| 栈 | 视觉简化 | 碰撞 |
|---|---|---|
| ROS/Gazebo | Blender Decimate / MeshLab | Blender 手动 + VHACD |
| **MuJoCo** | Open3D `simplify_quadric_decimation` | **原语优先（胶囊 > 圆柱）** + `pyvhacd` |
| Isaac Sim | 导入器处理 | **导入时选**（见下） |
| 我们 | `pipeline/decimate_meshes.py`（Open3D） | **无** ← 缺口在这 |

### 离我们最近的一个真实案例

[`ldr-urdf-tools`](https://github.com/LD-Robots/ldr-urdf-tools)（Onshape → ROS2/Gazebo）的账：

| | 三角形 | 体积 |
|---|---|---|
| Onshape 原始导出 | 6,426,130 | 306 MB |
| 视觉（20% 减面） | ~1,285,000 | ~61 MB |
| 碰撞（凸包） | ~210,000 | ~10 MB |

它还有一步**我们完全没有**：`strip_collision.py` —— 删掉内部子装配（电机、PCB、支架）的
`<collision>`，理由是 *"slow down physics without adding value"*。

### Isaac 把"简化"产品化成了导入器的一个下拉框

IsaacLab 的 [`MeshCollisionPropertiesCfg`](https://github.com/isaac-sim/IsaacLab/blob/main/source/isaaclab/isaaclab/sim/schemas/schemas_cfg.py)
列出全部可选（`mesh_approximation_name`）：

| 选项 | 说明 |
|---|---|
| `none` | 直接用三角网格 |
| `boundingCube` / `boundingSphere` | 包围盒 / 包围球 |
| `convexHull` | 凸包（**默认**） |
| `convexDecomposition` | 凸分解，可配 `max_convex_hulls` / 体素分辨率 |
| `meshSimplification` | 网格简化 |
| `sdf` | 符号距离场 |

这张表就是全景图：**"碰撞怎么简化"在这个领域早就是产品化的选项，不是手工活**。

### 导出端的杠杆（我们一直没用）

[SW2URDF](https://github.com/ros/solidworks_urdf_exporter) 在 SolidWorks 里就有
**per-link 的 `STLQualityFine` 开关**（默认 **Coarse**）：每个 link 单独选精度。
另有 `swSTLComponentsIntoOneFile = true`（一个 link 一个 mesh）。
另外 `onshape-to-robot` 是 Onshape 侧的等价物。

**所以第一刀应该在 SolidWorks 导出时，而不是在导入之后。** 我们现在是从别人已经导好的
STL 开始做的，这一层用不上——但下次重导时要用上。

### 我们和共识的差距：就一条，但是在根上

**共识：`<collision>` 和 `<visual>` 是两套独立的几何。**
**我们：`collision == visual`，955 比 955，13/13 link 逐字段相同。**

所有来源都在反对我们这件事——包括 SW2URDF 自己在导出时就把 collision 的质量单独设。
这解释了我们已经量到的两个现象：

1. 为什么 `simple_collision=False` 会让机器人**靠 NUC 盖板站着**
   （`floor <-> link_6 dist = −0.243 mm`）——955 个 CAD 零件的凸包拼起来根本不是一台机器人
2. 为什么 `mujoco.urdf` 有 1904 个 mesh geom —— 952 visual + 952 collision，后者是前者的机械副本

## 7. 导出端 v2 复核（2026-10-10 晚，`assets/总装_urdf/总装.urdf`）

用户在导出端换了一版：**每个零件给一个 `<collision><box>`**。复核结论：方向完全对，
但**这套 box 不能整体当碰撞集**。

### 7.1 好的部分（六条）

| # | 事实 | 判据 |
|---|---|---|
| 1 | **13/13 link 现在 `collision ≠ visual`** | 955 `<visual><mesh>` + 955 `<collision><box>` |
| 2 | **导出端就把三角形砍到 1/11** | log `exported_triangles=192606`（旧：210 个唯一网格 2,157,746；烘平后 16,786,420） |
| 3 | `<box size>` 用的是 URDF 正确约定（**全长**，米） | AM02 装甲 = `0.141` m = 141 mm，对 |
| 4 | **今天什么都没坏** | `simple_collision=True` 仍复现基线：73.389 mm（基线 73.413）、33 044 步/s（基线 33 846）、8 接触、只在 `world` |
| 5 | `<inertial>` 缺 4 个**不是这次引入的** | `link_5/6/7/cam_link_8`，与旧 URDF 完全相同 |
| 6 | **923/955 个 box 尺寸正确** | 见下表 |

### 7.2 一个问题，但很局部：32 / 955 个 box 过大

每个 link 的碰撞集最低点 vs 其 CAD 最低点（都在 link 坐标系里量，**不需要配对**）：

| link | box 数 | 低于 CAD 的 | CAD 最低 mm | box 最低 mm | 超出 mm |
|---|---|---|---|---|---|
| base_link | 345 | 0 | −46.65 | −46.65 | 0.00 |
| **link_1** | 128 | **8** | −56.44 | **−110.70** | **+54.26** |
| **link_2** | 128 | **8** | −56.44 | −110.67 | +54.23 |
| **link_3** | 128 | **8** | −56.44 | −110.70 | +54.26 |
| **link_4** | 128 | **8** | −56.44 | −110.67 | +54.23 |
| link_5 | 45 | 0 | −53.57 | −53.54 | −0.03 |
| link_7 | 53 | 0 | −67.50 | −67.50 | 0.00 |

**只有 4 个轮 link 有问题，各 8 个，全是同一批零件**（每个轮 link 这 4 种各 2 份）：

| 零件 | 它得到的 box（mm） |
|---|---|
| `全向滚子安装` | 117.5 × 109.3 × 117.5 |
| `全向滚子侧板` | 110.9 × 154.8 × 110.9 |
| `全向滚子安装_2` | 111.6 × 154.8 × 111.6 |
| `n_120mm全向轮垫块` | 72.1 × 98.0 × 72.1 |

**原点是对的（与最近网格偏差 ≤ 1.3 mm），尺寸不对**——这些 box 约等于**整个轮组**的尺寸
（155 mm 那一维就是轮 + M3508 电机的宽度）。所以像是这几类零件取到了**装配体/参考几何的包围盒**
而不是自己的（**这是推测，未证实**）。

### 7.3 实测后果

| 碰撞集 | base_link 静止 z | 接触数 | floor 接触 | 步速 |
|---|---|---|---|---|
| URDF 自带 955 box | **123.600 mm** ❌ | **709** | 9 | 2 266 步/s |
| `simple_collision=True`（4 圆柱） | 73.389 mm ✓ | 8 | 8 | **33 044 步/s** |
| 混合：923 好 box + 轮子圆柱 | 63.847 mm | **719** | 6 | 2 343 步/s |
| 旧 URDF，全 CAD mesh | 79.343 mm | 114 | — | 3 467 步/s |

**两个结论**：

1. **整体用 955 个 box 不行**：8 个过大的 box 把整车顶高 50 mm，同时制造 ~700 个内部接触。
2. **"保留好 box + 轮子用圆柱"也不行**：还是 719 个接触、比纯圆柱慢 14×。
   根因是 **444 个 box 互相重叠**——相邻 CAD 零件的包围盒必然相交，于是产生大量内部接触。
   这正是 [`ldr-urdf-tools`](https://github.com/LD-Robots/ldr-urdf-tools) 要写 `strip_collision.py`
   的原因（*"slow down physics without adding value"*）。

**所以 box 是"对的素材"，但不能整体用。要做的是从里面挑十几个**：
4 轮圆柱 + 底盘 box + 4 装甲板 box + 云台 box。业界就是这么做的
（`< 500 三角形/link`、strip 内部件）。

### 7.4 如果 `<collision>` 用**视觉网格**而不是 box？（同一版 URDF，2026-10-10 实测）

问题来自用户："把全向轮改成圆柱、导出时不选碰撞盒而选视觉网格会怎样"。做法：把每个
`<collision>` 的 **`<origin>` 和 `<geometry>` 一起**换成对应 `<visual>` 的（按 index 配对，
13 个 link 都是 1:1 且同序），写成 `/tmp` 下的一份变体再量。

| 碰撞集 | base_link 静止 z | 接触数 | floor 接触 | **谁在地面上** | 步速 |
|---|---|---|---|---|---|
| `box`（现在的导出） | **123.600 mm** ❌ | 709 | 9 | `link_1/2/4`（**只有 3 个轮**） | 2 232 步/s |
| **`mesh`（视觉网格）** | **73.414 mm** ✅ | 109 | 11 | **`link_1/2/3/4`（四个轮全中）** | 3 993 步/s |
| 原语圆柱（`simple_collision`） | 73.393 mm ✅ | **8** | 8 | 四个轮，各 2 个 | **34 133 步/s** |

**结论：用视觉网格当碰撞，落地姿态是对的**——73.414 mm 与原语圆柱的 73.393 mm 只差
**0.02 mm**，而且四个轮都真的踩在地上。box 之所以不行，是因为它**比零件大**（§7.2 那 32 个更是
离谱），而**凸包比 box 紧**，所以网格反而贴得更准。

**但代价还在**：

- **接触数 109 而不是 8** —— 剩下 ~100 个是**相邻零件凸包互相重叠**产生的内部接触。几何简化
  减不掉这个，因为凸包还是互相相交。
- **步速 3 993 vs 34 133，慢 8.5×** —— 网格-网格/网格-平面走支撑函数，原语走解析解。
- **接触数（而不是 box/mesh 的选择）才是成本主因**：box 2 232 步/s ≈ mesh 3 993 步/s，
  而原语 34 133 步/s。

**所以"全向轮改圆柱"这个想法对，但正确的表达是"导出成 `<cylinder>` 原语，不是圆柱网格"**：

- URDF 本身就有 `<collision><geometry><cylinder radius=... length=.../></geometry></collision>`，
  MuJoCo 会映射成 `mjGEOM_CYLINDER`。碰撞就**长在 URDF 里**，不再依赖 `simple_collision` 这个补丁。
- 原语是解析接触：**34 133 步/s、8 个接触**，比圆柱网格好 8.5×。
- 而且如果 CAD 里那个轮**本来就是圆柱**，导出器可以直接读出半径/长度，不用再靠
  `wheel_cylinder()` 的"取 95% 最大半径那一圈顶点"这类启发式。

**推荐**：轮 → `<cylinder>`，底盘/装甲/云台 → `<box>`，**内部零件根本不给碰撞体**。
这也是行业做法（§6）。

> **方法论教训（第五次同类）**：这个实验第一版是**错的**，差点得出相反结论。
> 我一开始只把 `<geometry>` 换成视觉的网格，**没有换 `<origin>`**——而导出器给 box 的
> `<origin>` 是 OBB 坐标系，**955 个里有 955 个与对应 visual 的 origin 不同**。
> 于是网格被放在了 box 的位置上，量出 106.614 mm、floor 接触全在 `base_link`（"底盘着地、四轮悬空"），
> 看着像个真结论。补上 `<origin>` 后是 73.414 mm、四轮着地。
> **换几何必须连变换一起换**——判据是"13/13 origin 不同"这个数早就在 §7 里量过了，只是没往这上面想。

### 7.5 第三个想法也不行：**从包围盒推导碰撞体**（2026-10-10 实测）

"每个 link 一个包围盒"是很自然会想到的省事办法。**不行。**
4 个轮用圆柱 + 其余每个 body 一个 bbox（`base_link`、`link_5`、`link_7`，共 **8 个碰撞体**）：

| 碰撞体 | 全长（mm） |
|---|---|
| `base_link` | 590.9 × 621.7 × 190.5 |
| `link_5` | 203.4 × 195.6 × **224.9** |
| `link_7` | 93.2 × 255.5 × 286.0 |
| 4 × 轮圆柱 | r=60.25, half=15.15 |

结果：**`base_link z = 187.830 mm`，floor 接触全在 `link_5`** —— 机器人**架在云台上**。

原因：**一个 link 的包围盒包含它里面所有零件的扫掠体积**，`link_5` 的盒子（225 mm 高）伸到轮子
下面。所以**碰撞体是"设计"出来的，不是"算"出来的** —— 这正是业界把它做成
导入器下拉框（§6 的 Isaac 那张表）或 Blender 里手工留一份 `*_collision` 的原因。

### 7.6 四种"自动得到碰撞集"的尝试，只有一种成立

| 做法 | 碰撞体数 | base_link z | ncon | 步速 |
|---|---|---|---|---|
| 导出 955 box | 956 | 123.600 ❌ | 709 | 2 232 |
| 导出 955 **mesh** | 956 | **73.414** ✅ | 109 | 3 993 |
| 每 link 一个 bbox | 8 | **187.830** ❌ | 8 | 26 136 |
| **4 轮圆柱（`simple_collision`）** | 5 | **73.393** ✅ | **8** | **34 133** |
| 目标：4 圆柱 + 手工挑的底盘/装甲/云台 box | ~10 | ~73.4 | <20 | ~30 000 |

**结论**：轮子可以自动（圆柱，已解决），**但"壳"（底盘/装甲/云台）必须手工指定**。
而且**内部零件在导出端就不该有 `<collision>`** —— 那一条只能在上游做，下游没法可靠地猜。

### 7.7 导出端 v3：加了 SolidWorks envelope（2026-10-10 实测）

导出端第二次改：**每个零件加 envelope**，碰撞几何改成**视觉网格**。

| 指标 | v2（box） | v3（envelope + mesh） |
|---|---|---|
| `exported_triangles` | 192 606 | **280 190** |
| `topology_non_watertight_meshes` | **145** | **5** ✅ |
| `topology_warning_meshes` | 145 | 5 |

**envelope 的真正价值在网格质量**：非水密网格从 **145 → 5**。这对任何需要"封闭实体"的
下游都有用（凸包行为、体积/惯量计算、CoACD）。

**碰撞语义**：`<visual>` 和 `<collision>` 都是 mesh，955 / 955。

**物理结果**（4 轮圆柱不受影响）：

| 碰撞集 | base_link 静止 z | 接触数 | floor 接触 | 步速 |
|---|---|---|---|---|
| 955 网格（v3 现状） | **73.414 mm** ✅ | 109 | 11，四轮都有 | 4 105 步/s |
| 4 轮圆柱 | 73.597 mm ✅ | **8** | 8，四轮各 2 | **32 546 步/s** |

**v3 的落地姿态是对的**（73.414），但 101 个多余接触和 8× 的步速代价还在——
和 §7.4 的结论一致：**问题不是 box 还是 mesh，是碰撞体的"数量"。**

### 7.8 实现：`shell_bodies`（2026-10-10 落地）

`Simple_collision` 原来只留 4 个轮子圆柱，**底盘完全没有碰撞体**（能穿墙）。
新增 `PhysicsOptions.shell_bodies`：给列出的 body **各加一个 box**（该 body 的网格 AABB），
其余仍然全部关掉。实测三种配置：

| 配置 | 碰撞体数 | base_link z | 接触数 | 步速 |
|---|---|---|---|---|
| `()` 只有轮子 | 5 | 73.597 mm | 8 | 32 546 |
| **`("base_link",)`（默认）** | **6** | **73.597 mm** | **8** | **26 972** |
| `("base_link","link_5","link_7")` | 8 | **116.689 mm** ❌ | 7 | 26 075 |

- **`("base_link",)` 是甜点**：落地姿态和只有轮子时**完全一样**（73.597、8 个接触），
  多花 ~17% 步速买到"底盘撞得到墙"。`base_link` 的 CAD 最低点是 −46.65 mm，
  而轮子到 −56.44 mm，所以它不可能承重。
- **加 `link_5`/`link_7` 会坏**：机器人**架在云台上**（116.689 mm）。
  它们的 AABB 伸到轮子下面——`link_5` 是 203×196×225 mm。

**cloud 台碰撞体被 `link_5`/`link_7` 的 zero pose 卡住**：等 §"资产的底是云台"那条修完
（云台姿态摆正、AABB 抬到轮子之上），才应该把它们加进 `shell_bodies`。

落地位置：

- `rm_sim_spear/mujoco_driver.py`：`DEFAULT_SHELL_BODIES = ("base_link",)`；
  `_apply_simple_collision` 增加 shell box 分支（对没有网格的 body **报错而不是静默跳过**）
- `tools/run_spear_sim.py` / `tools/run_physics_sim.py`：新增 `--shell-bodies`
- `tests/test_mujoco_physics_options.py`：两条新测试钉住"一个 shell body 恰好一个 box"与"名字打错要报错"

### 7.9 云台为什么不需要碰撞体，以及"只靠限制"还不够（2026-10-10 实测）

**拥有者决定**：底盘要碰撞体，云台不要——云台只遵循 URDF 的角度限制。

URDF 里确实有，MuJoCo 也确实吃进去了（`jnt_limited = 1`）：

| 关节 | URDF `<limit>` | 角度 |
|---|---|---|
| `link_5_joint`（yaw） | lower −0.30, upper 0.75 | −17.2° … +43.0° |
| `link_7_joint`（pitch） | lower −1.40, upper 0.30 | −80.2° … +17.2° |

**限制守得住**：静置后 `link_5` 停在 −0.3023、`link_7` 停在 +0.3002 —— 都**贴在限制上**，
没有越界（超出 ~2.3e-3 rad 是 MuJoCo **软约束**的正常过冲，不是限制失效）。已由
`test_gimbal_has_no_collider_and_stays_inside_its_urdf_limits` 钉住。

⚠️ **但"只靠限制"有一个后果**：云台**没有任何东西托着**，重力把它压到限制上，
而那个姿态下云台 + 相机**穿到地面以下 30 mm**：

| 云台状态 | 最低的 body | `link_5` 最低世界 z | `link_7` 最低世界 z |
|---|---|---|---|
| **垂到限制上（当前）** | **`link_7`，−29.87 mm** ❌ | −28.3 mm | **−29.9 mm** |
| **按 CAD 姿态按住** | `link_3`（轮子），−0.27 mm ✅ | **+21.6 mm** | **+64.3 mm** |

原因不是限制不够严，而是**限制的范围很宽**（pitch 允许 −1.4…+0.3），而 CAD 的工作姿态
（`link_5 = 0.8840`、`link_7 = −0.8444`，正是 `view_sw_pose.py` 里那两个值）**落在限制内部**。
重力把它从工作姿态推到 +0.30 那一端，于是相机插进地里。

**两条修法**（都还没做，见 [`urdf_workflow.md`](urdf_workflow.md) 的"资产的底是云台"）：

1. **把云台按住**：加 actuator 或 equality，让 `link_5`/`link_7` 停在 CAD 姿态。
   实测这样最低点是轮子的 −0.27 mm，**完全正确**。
2. **收紧限制**：把 URDF 的 `<limit>` 收到 CAD 工作姿态附近。但这会限制云台的实际行程。

**注意**：地面不透明，所以"相机在地里"在画面上只表现为**云台不见了**——不量就不会发现。

### 7.10 两件家务

1. ~~**`meshes/` 里有 251 个 STL，但 URDF 只引用 210 个** → 41 个是上一版导出的残留~~
   → **2026-10-10 已清掉**：删了 41 个 `.stl` + 41 个 `.stl.stamp`，剩 210 + 210，
   与 URDF 引用集合**逐名相等**（`comm` 两个方向都是空）。删除前对每个目标做了
   `realpath` 前缀校验，确认都在 `assets/总装_urdf/meshes/` 里面。
   复核判据（清掉前）：
   ```bash
   ls assets/总装_urdf/meshes/*.stl | wc -l                                   # 251
   grep -o 'filename="meshes/[^"]*"' assets/总装_urdf/总装.urdf | sort -u | wc -l  # 210
   grep -o 'filename="meshes/[^"]*"' assets/总装_urdf/总装.urdf | wc -l            # 1910 = 955 visual + 955 collision
   ```
2. ~~`generated/urdf/总装/` 下的 `manifest.json` / `mujoco.urdf` / `scene_binding_plan.json`
   还是从旧 URDF 生成的~~ → **2026-10-10 已重新生成**，实测结果：

   | 项 | 旧生成物（10-05） | 重新生成后 |
   |---|---|---|
   | `manifest.json` | 1 626 658 B | 1 626 559 B |
   | `mujoco.urdf` | 671 992 B | 674 039 B |
   | `validation.mujoco_incompatible_meshes` | `["新电池_默认_...", "dm_j4310_2ec_..."]` | **`[]`** |
   | `ue_overrides.json` 的 `ignored_meshes` | 那两个 | **`[]`** |
   | `scene_binding_plan.json` | 4988 B | **逐字节不变**（diff 为空） |
   | link / joint / visual mesh 结构 | 13 / 12 / 955 | **完全相同**（结构化 diff 只差 validation 那一项） |

   载入验证（`MujocoDriver`，`simple_collision` + `shell_bodies=("base_link",)`）：
   `nbody=14 ngeom=1916 nmesh=210`，碰撞 geom 6 个（4 轮 + 1 底盘盒 + 1 地面），
   静置 `base_link z = 73.478 mm`、`ncon=8`、qpos 全有限。v3 里那两个曾经解码失败的
   mesh 现在都在模型里。

   **踩到的坑**：`pipeline.urdf_manifest` 会把 `ue_overrides.json` 整个覆盖掉，而
   `generated/urdf/总装/ue_overrides.json` 是**手工编辑过**的（`armor.enabled=false`、
   四个 `armor_link_*` 的 `ue_asset`/`local_transform`、`runtime_bindings`）。所以正确
   顺序是"生成到临时目录 → 只装 `manifest.json`/`mujoco.urdf` → 手工同步唯一那个生成
   字段 `ignored_meshes` → 用新 manifest + 保留的 overrides 重算 `scene_binding_plan.json`
   并 diff"。命令写在 [`urdf_workflow.md`](urdf_workflow.md) 的"生成清单"一节。

### 7.11 复现

```bash
# 两个 probe 都在 tmp/ 下，纯 MuJoCo，不碰 UE
PYTHONPATH=. /home/coffee/miniconda3/envs/spear-env/bin/python tmp/probe_box_collision.py
PYTHONPATH=. /home/coffee/miniconda3/envs/spear-env/bin/python tmp/probe_box_diag.py
PYTHONPATH=. /home/coffee/miniconda3/envs/spear-env/bin/python tmp/probe_box_extent.py
```

### 7.12 场地**不在** MuJoCo 里，这就是还陷在地里的原因

> **2026-10-10 更新：已经做了，2026-10-11 换了来源。** 场地现在来自 Blender
> `blender_mujoco_terrain` 插件导出的整份 MJCF 世界；本仓库自己那套
> （`pipeline/arena_scene.py` → `arena.json`）因为和机器人处于互为镜像的坐标系而删除。
> 坐标约定的实测判据见 [`rmuc26_asset_review.md`](rmuc26_asset_review.md)。
> 下面这一节保留为**问题陈述**，说明为什么原来会陷——它仍然是"平面代替场地"为什么不行
> 的最好说明。

**没有转，一次都没有。** 证据是生成的模型本身：

```bash
grep -c "<geom" generated/urdf/总装/mujoco.urdf   # 1910 = 955 visual + 955 collision，全是机器人
```

`mujoco.urdf` 里**一个场地几何都没有**。MuJoCo 侧的全部"世界"是
`PhysicsOptions` 运行时加的两样东西：

| 加的东西 | 是什么 |
|---|---|
| `floor` | 一个 **`mjGEOM_PLANE`**，`size=[5,5,0.1]`——数学上**无限大且绝对平**的一张面，在模型 z = 0 |
| `free_base` | 给 `base_link` 一个自由关节，让它能被重力/接触/驱动作用 |

也就是说 `--world-offset-cm 0 0 -140` **不是**"场地在 MuJoCo 里的位置"，而是"把 MuJoCo 那张
无限平面塞进 UE 世界时，贴在哪个高度"。而 UE 的场地地面是 **−137.9 … −140.5 cm**（
[`rendering.md`](rendering.md) §10）——它**不是一个平面**，是一整块有厚度的、随位置起伏的
网格。用一张平面去顶它，必然有的地方差 1–3 cm：**这就是画面里还嵌在地里的原因**，
和 URDF、和包围盒都无关。

要根治只有用户在会话开头提的那条路：**把场地也做成 mesh 交给 MuJoCo 做碰撞**，然后
`world_offset_cm` 整个消失（模型和 UE 用同一套坐标）。这条一直挂在待办上
（[`content_management.md`](content_management.md) 的 `SM_MERGED_*` 已经是合并好的一整块，
本来就适合做静态碰撞体）。


