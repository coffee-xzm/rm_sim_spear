# `assets/rmuc26` 评测

对 Blender 插件 `blender_mujoco_terrain` 导出的整套 MJCF 场地做的实测评估。

- 资产：`assets/rmuc26/{rmuc26.json, mjcf/scene_rmuc26.xml, meshes/*.obj, meshes/rmuc26.hfield}`
- 生成器：`~/.config/blender/5.2/extensions/user_default/blender_mujoco_terrain/`
- 生成日志：`assets/rmuc26/.blender_mujoco_terrain_logs/`
- 复现脚本：`tmp/rmuc26/`（未入库）
- 配置图：`docs/figures/rmuc26_collision_vs_visual.png`

## 一、能不能加载

可以。`MjModel.from_xml_path` 0.51 s（`scene_rmuc26.xml` 里
`<include file="robot.xml"/>` 指向的文件不存在，评测时把这行去掉）。

| 项 | 值 |
|---|---|
| geom | 180（1 plane + 1 hfield + 178 mesh） |
| 顶点 / 面 | 1,748,817 / 1,208,031 |
| 坐标系 | UE 世界坐标，米，Z 上，无轴取反；x ∈ [2, 31]，y ∈ [-8, 8] |
| hfield | 283 行 × 512 列，size = 14.505 × 8.002 × 2.011 × 0.2，格距 **5.67 cm** |
| 地面高度 | -1.8509 … +0.1605 m（自洽：`pos_z = height_min`，`size_z = height_range`） |

`<worldbody>` 里有一个 `type="plane"` 的 `floor` 位于 z = 0，比场地地面高约
1.45 m。它是预览用的临时地面，接入机器人时必须删掉或关掉 contype，
否则机器人会站在一块看不见的平面上。

## 二、几何本身是对的

hfield 的源网格 `rmuc26_visual.obj`（150k 面）就是真实场地：八角形中央平台、
外圈场地、四角结构都在。hfield 对它的复现：

| 指标 | 值 |
|---|---|
| 中位误差 | **0.2 mm** |
| p90 | 46 cm |
| > 5 cm | 37 %（抬高 19.5 % / 偏低 17.7 %） |
| > 20 cm | 23 % |

平地上几乎精确到 0；误差全部集中在立面边缘一圈（hfield 无法表达竖直面和悬空）。
这是高度场的固有局限，不是错误。

## 三、碰撞体是坏的

把 304,672 个碰撞三角形和 1,191,563 个视觉三角形各自从 z = +20 m 向下投射，
在 5 cm 栅格上比较（1168×648 图，左上=视觉真值，右上=碰撞体）：

| 指标 | 碰撞体 vs 视觉 |
|---|---|
| 中位误差 | **2.024 m** |
| p90 | 2.539 m |
| > 5 cm | 81.9 % |
| **碰撞体高于真值 > 5 cm（幽灵）** | **73.5 %** |
| 碰撞体低于真值 > 5 cm（缺失） | 8.5 % |

去掉 `static_SM_MERGED_Actor_424` 那一块之后：

| 指标 | 去掉 424 凸包 |
|---|---|
| 中位误差 | **0.4 mm** |
| p90 | 0.52 m |
| > 20 cm | 24 % |

也就是说：**整个场地的碰撞精度由一块几何决定**。

## 四、根因

`SM_MERGED_Actor_424` 有 **1,572,696 个源面**，`collision_mode = "CONVEX"`，
被压成 **1 个凸包，272 个面，凸包体积 749.75 m³**，世界 AABB
= 26.6 × 13.9 × 2.86 m —— 几乎等于整个场地。它是一块实心板，悬在地面上方。

MuJoCo 接触实测（场地中心 x=16.505, y=0）：

| 小球位置 | 与 `Actor_424_collision_000` 的穿透 |
|---|---|
| 真实地面高度 z = -1.351 | **-0.571 m** |
| z = 0.0 | **-1.136 m** |
| z = 0.55 | **-0.587 m** |
| z = 1.15 | 无接触 |

即地面上方 0–1.15 m 的整个空间都在凸包内部 —— 机器人放在场上任何位置都会
嵌进实体里约 0.5–1.1 m。

另一个 `static_SM_MERGED_blender_for_ue5_node_22`（1.88 × 1.87 × 3.10 m，
2.879 m³ 单凸包）也压住中心，穿透 0.30–0.37 m。

**7 个场地物体全部是单凸包，没有一个走 CoACD。** CoACD 只用于地形的 4 个
悬空切出块（共 157 块）。

## 五、为什么必然如此（插件设计如此，不是 bug）

`job.py:626` 和 `terrain_scene_worker.py:415`：

```
source_kind = "CUTOUT" if (role == "STATIC" and "mujoco_terrain_cutout_mode" in obj) else "OBJECT"
collision_mode = "CONVEX"
if source_kind == "CUTOUT": ... SURFACE 或 COACD
...
if source_kind == "OBJECT" and collision_mode != "CONVEX":
    raise ValueError(f"scene manifest {label} OBJECT only supports CONVEX collision")
```

`README.zh-CN.md:102-103` 明确写了：

> 两种模式只用于 Cube 切出的额外碰撞体。手动加入的普通额外碰撞体和所有可移动
> 碰撞体均使用单凸包。

所以：**手动加进“额外碰撞体”列表的对象一律是单凸包**，插件没有凸性检查，
也不给 UI 选项（`panels.py:124` 只在 `mujoco_terrain_cutout_mode` 存在时才显示
模式下拉框）。

## 六、坐标系差一个 y 取反

用 **Unreal 自己测的 `actor.get_actor_bounds()`**（`scene.json` 的
`world_bounds`，与任何摆放约定无关）对比 `assets/rmuc26` 的 `origin`，9 个对象
**y 全部相反**，x/z 完全一致：

| 对象 | UE world_bounds y (m) | addon origin y (m) |
|---|---|---|
| SM_MERGED_blender_for_ue5_node_22 | `[+0.717, +2.583]` | -1.6500 |
| SM_MERGED_Actor_422 | `[+0.567, +1.763]` | -0.8029 |
| SM_MERGED_Actor_423 | `[+1.526, +2.722]` | -2.4858 |
| SM_MERGED_StaticMeshActor_2183 | `[+5.230, +5.752]` | -5.4917 |
| SM_MERGED_StaticMeshActor_2601 | `[-2.558, -1.981]` | +2.2430 |
| SM_MERGED_StaticMeshActor_3156 | `[+1.029, +2.219]` | -1.6243 |
| SM_MERGED_Actor_424 | `[-5.338, +8.587]` | -8.1638 |

网格本身也是同一个结论 —— `blender_for_ue5_node_22` 两份导出的世界包围盒：

| | 世界 y (m) |
|---|---|
| Unreal 关卡（`actor.get_actor_bounds()`） | `[+0.717, +2.583]` |
| `assets/rmuc26` | `[-2.583, -0.717]` |

原因是 Blender 右手系、Unreal 左手系，两者互转必然要翻一个轴。**场地本身也不
对称**：镜像后 24.3% 的地面格点移动超过 20 cm，9.8% 超过 50 cm（最大 3.07 m）。

### 但这不意味着导入时要取反 —— 位姿桥已经翻了

上面整段比的是"文件 vs Unreal"。可把场景送进 Unreal 的不是文件，是
[`to_unreal_pose`](../rm_sim_spear/pose_sync.py)，而它用的正是同一个反射：

```python
ue_cm = 100 * diag(1, -1, 1) * p_mj + world_offset_cm
```

也就是说 **文件里 y 已经取反，恰好就是桥要的约定**。导入时再取反就是翻第二次。

用桥自己的约定去解 `world_offset_cm`（每个物体反解一个偏移，看它们是否收敛）：

```
=== 原样（不取反）===
SM_MERGED_blender_for_ue5_node_22   [-1650.5  +162.4  +0.0]
SM_MERGED_Actor_422 ...             [-1650.5  +162.4  +0.0]   ← 8/9 完全一致
SM_MERGED_Actor_445                 [-2115.2  +686.9   +43.9]  ← 地形，插件重切过
-- 中位偏移 [-1650.5 +162.4 +0.0]   残差 0.0 cm

=== 取反 ===
只有 3/9 一致，其余散布到 ±9 m
```

所以**默认不取反**（`arena_scene_mirror_y = False`）。工具
[`tools/check_arena_scene.py`](../tools/check_arena_scene.py) 会直接给出该用哪一
个，不要靠推断：

```bash
python tools/check_arena_scene.py --scene <scene.xml>
```

> **踩过的坑**：这个工具第一版用自己写的中位公式解偏移，而不是调用
> `to_unreal_pose`，于是在 y 上和桥差了一个符号——镜像结论和偏移符号**同时**反了。
> 照它跑出来场地偏了 3.2 m。现在它直接调用桥函数，这类漂移不可能再发生；
> `tests/test_check_arena_scene.py` 断言的是"推荐值经桥接落回 level"，而不是
> "取反/不取反"。

另外整个导出相对本仓库坐标系偏移 **(+16.505, -1.6243, 0) m**——插件的
`rmuc26_frame` body 就写在这个位置，它就是插件记录下来的关卡原点。

### 本仓库自己那套场地已经删除

之前这个仓库还有第二条路：`tools/export_arena_geometry.py` 把关卡几何导出来，
`pipeline/arena_scene.py` 把它做成"高度场 + 850 个盒子"写成
`generated/arena/mujoco_scene/arena.json`。2026-10-11 整条删掉了，理由是它与机器人
**互为镜像**：

| | 坐标系 |
|---|---|
| 机器人（URDF，`p_ue = D @ p_urdf`） | `D·UE` |
| 本文这份插件导出 | `D·UE` ✓ |
| ~~`arena.json`~~ | `UE`（463 个 placement 对 9 个 actor 的 `world_bounds`：原始坐标 9/9 命中，**过一次桥只剩 4/9**）|

位姿桥是 `ue_cm = 100·diag(1,−1,1)·p + offset`，所以 `arena.json` 那份经桥进 UE 是
镜像的，和关卡差约 2.8 m。两套只可能留一套，留了与桥一致、并且**精确**命中的这一套。

删掉的：`pipeline/arena_scene.py`、`generated/arena/`、`docs/arena_sync.md`、
`tests/test_arena_scene.py`、`--arena` / `--arena-dir` / `--arena-visual` 三个旗标，
以及 3.4 GB 只喂给那条管线的中间几何。保留
`tools/export_arena_geometry.py` —— 它产出的 `build/arena/unreal_metadata/scene.json`
是本文所有对拍用的真值。

> **顺带修掉一个静默陷阱**：删旗标之后 `--no-arena` 仍然能被 argparse 接受，因为它
> 是 `--no-arena-scene-mirror-y` 的合法缩写，会悄悄把镜像开关关掉。两个工具的 parser
> 现在都设了 `allow_abbrev=False`，删掉的旗标一律报错。

## 七、怎么接进来

驱动已经支持：`PhysicsOptions.arena_scene` 直接把整份 MJCF 当世界，机器人 attach
进去。工具侧对应 `--arena-scene`，它是**唯一**的场地来源。

```bash
/home/coffee/miniconda3/envs/spear-env/bin/python tools/run_spear_sim.py \
  --user-config tools/spear_user_config.yaml \
  --arena-scene assets/rmuc26/mjcf/scene_rmuc26.xml \
  --arena-scene-drop static_SM_MERGED_Actor_424_collision_000 \
  --arena-scene-spawn-m 16.5050 1.6243 -1.2565 \
  --world-offset-cm -1650.5 162.43 0 \
  --forever
```

（`--arena-scene-mirror-y` 默认关，这里不需要写。spawn 的 x/y 就是插件写的关卡
原点，z 是落点高度。）

必须用 `spear-env` 的解释器，系统 `python` 没有 `spear` 模块（会
`ModuleNotFoundError: No module named 'spear'`）。`--user-config` 是必填项。

**`--world-offset-cm` 在这里是必需的，不能省**（工具会硬拒绝：漏了直接
`SystemExit`）。插件的导出**有自己的原点**，偏移是唯一能把"MuJoCo 世界"和
"Unreal 关卡"摆在同一处的东西。省掉它：

| 世界 | MuJoCo root | → UE 位置 |
|---|---|---|
| 不给偏移 | `(16.505, 1.624, -1.257)` | **`(1650, -162, -126) cm`** |
| 给 `-1650.5 162.43 0` | `(16.505, 1.624, -1.377)` | `(0, 0, -138) cm` ✓ |

不给偏移的症状是**整个帧率塌掉**：采集相机挂在机器人身上（`cam_link_8`），所以它被
搬到 16.5 m 外另一个位置去渲染，很可能就在周边建筑内部。这也会让 UE 里的机器人和
MuJoCo 里的不在同一个地方。

- `--arena-scene-mirror-y`（默认**关**）：导入时对 y 取反。只对"自己已经翻过 y"
  的导出才需要，别的原因见第六节。打开它会同时翻转所有 placement、所有 mesh 的
  `scale.y`（绕序变了，所以 mesh 惯性退回 `mjMESH_INERTIA_LEGACY`；这里全是静态
  几何，惯性值没人用）以及高度场的行序（高度场的 y 沿行方向，取反就是行倒序；
  直接给 `size_y` 取负会被 MuJoCo 拒绝：
  `size parameter is not positive in hfield`）。
- `--arena-scene-drop`（可重复）：删掉指定 geom。424 那块单凸包必须在列。
- `--arena-scene-spawn-m X Y Z`：必需 —— 别人的导出没有 spawn 块。
- 插件写的 z = 0 预览平面会被自动删除。它**不是**场地地面，实测高出场地
  1.45 m，留着机器人就站在空气上。

实机验证（`总装` URDF，场地中心）：

| 导入方式 | t=0 接触数 | 最深穿透 | 3 s 后 base z |
|---|---|---|---|
| 原样（不取反、保留 424） | 3679 | -0.814 m | -1.983（比地面低 0.53 m） |
| 取反 + 删 424 | 0 | 0 | **-1.292**（地面 -1.377） |
| 取反、保留 424 | 4 | 0 | -1.859（低 0.48 m） |

> 这组数据是**用"取反"这个错误配置**跑出来的，而它看起来是正常的（-1.292 对地面
> -1.377）。原因不难看出：场地中心那块地面接近水平，而且它不自证手性——只看
> "机器人有没有站在地上"，镜像和原样都过关。**判手性只能靠对拍
> `actor.get_actor_bounds()`**，也就是 `tools/check_arena_scene.py` 做的事。
> 这张表现在只用来证明一件事：**424 那块凸包必须删**。

**把机器人初始位置设高解决不了 424 的问题**：那块凸包是实心的，机器人只是落到
它顶上、悬在真实地面以上约 2 m，永远碰不到地面。唯一的办法是把那块 geom 删掉。

## 八、这份导出的可用性小结

- **hfield 是好的**：512×283、5.67 cm 格距、自洽的 size/pos。它现在是**唯一**的
  地面表示，不再是"替换谁"的问题；
- **4 个 COACD 悬空切出块（157 块）**是处理高度场无法表达的悬空的正确做法 ——
  这也是本仓库原来那套"按列填实"做不到的，是选这份导出的主要理由；
- **7 个物体的单凸包**里至少 424 那一个必须删（见上）；其余 6 个在实测里没有
  造成问题，但它们是同样的机制，换关卡时要重新验。
