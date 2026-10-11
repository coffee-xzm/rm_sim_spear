# URDF 到 UE5/SPEAR 与 MuJoCo

本文针对 `assets/` 中的两个机器人 URDF，定义同一份结构如何进入 MuJoCo，并在 UE5 中生成可替换的视觉装配体。

## 当前模型

| 文件 | link | joint | 结构特点 |
|---|---:|---:|---|
| `assets/总装_urdf/总装.urdf` | 13 | 12 | 四个连续轮、云台旋转链、四个装甲 link |
| `assets/新烧饼底盘2装电器设备_urdf/新烧饼底盘2装电器设备.urdf` | 9 | 8 | 四个连续轮、四个装甲 link |

两个文件都是装配导出结果：一个 link 可能包含数百个 visual/collision mesh。URDF 中的 STL 作为 MuJoCo 几何输入；UE 视觉可以继续使用 URDF mesh，也可以用 `ue_overrides.json` 替换为已有的 StaticMesh、Blueprint 或空节点。

## 生成清单

```bash
python -m pipeline.urdf_manifest \
  assets/总装_urdf/总装.urdf \
  assets/新烧饼底盘2装电器设备_urdf/新烧饼底盘2装电器设备.urdf \
  --output-root generated/urdf
```

这一步才是 URDF 生成步骤：它写出 `manifest.json`、`ue_overrides.json` 和
供 MuJoCo 使用的 `mujoco.urdf`。生成文件会显式设置 `fusestatic=false`，保留固定
link 的 body 以便 UE 锚点也能获得位姿。`tools/run_spear_sim.py` 不会隐式重写这些文件，
而是读取已经生成的 `mujoco.urdf` 和 `scene_binding_plan.json`，避免运行时覆盖手工
配置的 UE override。

> **⚠️ `ue_overrides.json` 会被这条命令整个覆盖，而它是手工编辑过的。**
> `generated/urdf/总装/ue_overrides.json` 里的 `scene_bindings.robot.mode`、
> `armor.enabled=false`、四个 `armor_link_*` 的 `ue_asset`/`local_transform`、
> `runtime_bindings` 都不是生成物。**换 URDF 时不要直接跑上面那条命令**，按这个顺序：
>
> ```bash
> # 1) 先落到临时目录，拿到生成物
> python -m pipeline.urdf_manifest assets/总装_urdf/总装.urdf --output-root /tmp/gencheck
> # 2) 只装生成物，ue_overrides.json 手工改（唯一需要同步的生成字段是 ignored_meshes）
> cp /tmp/gencheck/总装/manifest.json  generated/urdf/总装/manifest.json
> cp /tmp/gencheck/总装/mujoco.urdf    generated/urdf/总装/mujoco.urdf
> # 3) 用"新 manifest + 保留手工编辑的 overrides"重算绑定计划，diff 无变化再装
> python -m pipeline.scene_binding_plan \
>   generated/urdf/总装/manifest.json generated/urdf/总装/ue_overrides.json \
>   --output /tmp/gencheck/总装/scene_binding_plan.json
> diff generated/urdf/总装/scene_binding_plan.json /tmp/gencheck/总装/scene_binding_plan.json
> ```

每个模型生成：

```text
generated/urdf/<robot>/manifest.json       # link/joint/mesh/origin/inertial
generated/urdf/<robot>/ue_overrides.json   # UE 视觉替换模板（本项目已手工编辑，别覆盖）
generated/urdf/<robot>/mujoco.urdf         # 自动移除 MuJoCo 无法解码 mesh 的副本
generated/urdf/<robot>/scene_binding_plan.json # armor/camera/IMU 具体挂点
```

**当前 `总装.urdf`（2026-10-10 的导出端 v3）已经不需要丢弃任何 mesh**：
`validation.mujoco_incompatible_meshes` 为空，`ue_overrides.json` 的
`ignored_meshes` 也随之清空。旧导出的 `新电池_默认_ad15cf6fb7fa599c.stl` 和
`dm_j4310_2ec_默认_5f36196a63bcc568.stl` 曾经解码失败、只从 `mujoco.urdf` 里被移除
（原始 URDF 与 STL 从未删除）；v3 里这两个 mesh 正常进了模型（实测
`nmesh=210`，两者都在）。

## 场景绑定

`ue_overrides.json` 的 `scene_bindings` 是 UE 场景处理入口：

- `armor.link_regex` 默认匹配 `^armor_link.*$`；每个匹配到的 link 对应一个场景装甲 Actor；
- `armor.actor_name_template` 默认生成 `armor_1`、`armor_2` 等场景 Actor 名；
- `armor.material_slot` 指定装甲 StaticMeshComponent 的材质槽；`material_asset` 填 UE 材质资产路径；
- `armor.enabled=false`（或整个 `"armor": null`）**关闭**这个场景模板绑定：link 仍是
  位姿锚点，但不再克隆/attach 任何场景装甲 Actor。装甲视觉改由 per-link
  `visual_source: ue_asset` 提供时用这一档，否则同一块装甲会被导入一次、再被克隆一次
  （见下文"装甲改用关卡资产"与"关闭冗余的场景装甲克隆"）；关闭时不会产生
  `no links matched armor.link_regex` 警告——绑定器把任何警告当致命错误；
- `camera.link_regex` 默认匹配 `^cam.*$`，只选择第一个匹配 link；
- `imu.relative_position_m` 和 `imu.relative_rpy_rad` 指定 IMU 相对相机 link 的安装位姿。

`mujoco.urdf` 必须保留固定 link body。MuJoCo 默认会合并固定 body；如果生成文件没有
`<compiler fusestatic="false">`，`base_link`、`armor_link_*`、`cam_link_8` 可能不会出现在
`data.xpos/data.xquat`，对应 UE 可视化锚点就不会更新。双视角测试会检查绑定 link 是否
全部出现在 MuJoCo body 集合中，缺失时直接失败并提示重新生成。

先展开成确定的绑定计划：

```bash
python -m pipeline.scene_binding_plan \
  generated/urdf/总装/manifest.json \
  generated/urdf/总装/ue_overrides.json \
  --output generated/urdf/总装/scene_binding_plan.json
```

当前结果：`总装` 匹配 4 个装甲 link 和 `cam_link_8`；`新烧饼底盘2装电器设备` 匹配 4 个装甲 link，但没有 `cam*` link，因此会生成明确警告。没有相机挂点时不会猜测或挂到 `base_link`。

UE Python 导入器的处理顺序应是：

1. 按 link 名创建或查找 link SceneComponent；
2. 找到 `armor[i].actor_name` 对应的场景 Actor，将其 root component attach 到对应 link；
3. 在该 Actor 的 StaticMeshComponent 上执行 `SetMaterial(ElementIndex=material_slot, Material=material_asset)`；
4. 生成/查找相机 Actor，attach 到 `camera.link`；
5. 在相机下创建 IMU marker/component，应用 `relative_position_m` 和 `relative_rpy_rad`；
6. 运行时 SPEAR 只按 `MuJoCoBody=<link_name>` 推送 link 位姿，装甲、相机和 IMU 会随 link 一起移动。

这套处理不要求把装甲或相机改写进 URDF；URDF 只提供结构和坐标系，UE override 提供场景资产选择。

## SPEAR 中执行

在真正启动 SPEAR 运行时前，先用 UE commandlet 检查绑定计划中的 Actor 名称是否
已经存在于目标地图。该检查只读场景，不会创建或修改 Actor：

```bash
RMSIM_LEVEL=/Game/creo_stp \
RMSIM_BINDING_PLAN=generated/urdf/总装/scene_binding_plan.json \
RMSIM_BINDING_REPORT=/tmp/rm_sim_spear_binding_report.json \
<UE5.8>/Engine/Binaries/Linux/UnrealEditor-Cmd \
  ue/rm_sim_spear.uproject -run=pythonscript \
  -script=tools/validate_ue_binding.py -unattended -nop4 -nosplash
```

只有报告中的 `ready` 为 `true` 时，才进入下面的运行时绑定步骤。`creo_stp`
目前是场地地图；若使用总装 URDF，必须先把机器人视觉装配体导入该地图，或切换到
包含机器人 Actor 的目标地图。

场景已经加载、link Actor 和装甲 Actor 已存在时，在 SPEAR 脚本中执行；采用
`spear_sensor` 模式时，传感器 Actor 由这段代码按需创建：

```python
from rm_sim_spear.spear_scene_binding import (
    apply_scene_binding_plan,
    get_scene_camera_capture_component,
    load_plan,
)
from rm_sim_spear.spear_scene_binding import make_scene_pose_synchronizer

plan = load_plan("generated/urdf/总装/scene_binding_plan.json")
capture_components = {}
with sp_instance.begin_frame():
    link_actors = apply_scene_binding_plan(
        sp_instance.game, plan, capture_components=capture_components)
with sp_instance.end_frame():
    pass
capture_component = capture_components["camera"]
pose_sync = make_scene_pose_synchronizer(link_actors, spear.math)
```

脚本会：

- 如果 link 锚点不存在，自动创建带稳定名称的空 `AActor`；已有同名 Actor 时直接复用；
- 当 `armor.enabled` 不为 `false` 且 `armor.clone_per_link=true` 时，把 `armor.actor_path`
  指定的聚合 Actor 作为模板，在启动时克隆到 `RMSim/armor_1` ... `RMSim/armor_4`，再分别
  attach 到四个 `armor_link_*`；模板 Actor 及其子层级会被隐藏，模板本身保持不动；
  装甲视觉来自 per-link `ue_asset` 覆盖时这一段必须关掉（`armor.enabled=false`），
  否则同一块装甲会被渲染两次；
- 这四个绑定项仍把 `Scene/armor` 视为一个整体，仿真位姿只写入四个 link
  锚点，不会把 `Scene/armor` 的子 Actor 分别绑定到 MuJoCo link；运行时复制
  子层级只是为了保留整体的原始相对变换；
- 如果配置了 `material_asset`，把它设置到指定材质槽；
- `camera.mode=spear_sensor` 时，如果 `camera_sensor` 不存在，自动创建
  `BP_CameraSensor`、初始化 `USpSceneCaptureComponent2D`，并将其 attach 到
  `cam_link_8`；采集组件通过 `capture_components["camera"]` 返回；如果未传入该
  字典，也可以随后调用 `get_scene_camera_capture_component()` 查找；
- `camera.mode=cinecamera` 时，按 `actor_path` 复用场景中的
  `CineCameraActor`。它只有普通 `CameraComponent`，不能直接作为
  `SpearRenderPort` 的图像回读源，除非另外挂载 `USpSceneCaptureComponent2D`；
- 创建 `imu_marker` 并按米制相对位置、弧度相对姿态挂到相机；
- 返回 `{link_name: ue_link_actor}`，供 MuJoCo `xpos/xquat` 推送循环使用。

将同步器传给渲染端即可在每个 `begin_frame()` 内应用同一提交点的位姿：

```python
renderer = SpearRenderPort(
    instance=sp_instance,
    capture_component=capture_component,
    apply_snapshot=pose_sync.apply_snapshot,
    set_game_paused=set_game_paused,
    readback_metadata=metadata.readback_metadata,
    bind_frame_metadata=metadata.bind_frame_metadata,
    metadata_enqueue_copy=metadata.enqueue_copy_metadata,
    metadata_reset=metadata.reset,
    on_reset=pose_sync.reset,
    sensor_id="front_camera",
    modality="rgb",
    buffering_mode="SingleBuffered",
)
```

`PoseSynchronizer` 只更新绑定计划中存在的 link；传入 `strict=True` 时会要求
快照中的 MuJoCo body 名称与 UE link 名称完全一致。`Coordinator.reset()` 开启新
`run_id` 后，应调用 `pose_sync.reset()`，再开始下一轮提交。

### 直接启动

上面的初始化和循环已经收拢到 `tools/run_spear_sim.py`。它必须使用 SPEAR 的
Python 环境，并通过 `--user-config` 指向包含 UE 启动方式的配置文件：

配置样例见 `tools/spear_user_config.yaml`。使用前至少修改
`EDITOR_EXECUTABLE`、`EDITOR_UPROJECT`；如果 UE 已经运行，把 `LAUNCH_MODE` 改为
`none`。

使用 `LAUNCH_MODE: editor` 时，先关闭已经打开的本工程 UE 编辑器，让 SPEAR 独占
启动它；如果要连接已经打开的编辑器，则复制配置文件并把 `LAUNCH_MODE` 改成
`none`，不要同时启动第二个编辑器实例。

#### 相机元数据组件：不再需要在 Blueprint 上手工添加

`run_spear_sim.py` 走 `SpearRenderPort`，要求相机上有 `bind_frame_metadata` /
`enqueue_copy_metadata` / `readback_metadata` / `reset_metadata` 四个 SpFunc。这四
个由 `URmSimSpearReadbackMetadataComponent` 提供；**组件不在 `BP_CameraSensor` 上
也能工作**——由 `_find_metadata_component()` 在事务里补建，再由 Python 调用它的
`Initialize()` 完成注册（2026-10-10 修好之前，这条补建路径是坏的）。

无头编辑器实测的对照证据（`{"event": "metadata_component", ...}`）：

| 查询 | 结果 |
|---|---|
| 建之前，相机 actor 上的 `USpFuncComponent` 数量 | 20 |
| 建之后，同上 | **21**（说明 `sp_func_component` 确实建出来了） |
| `get_children_components_for_scene_component(刚建的 metadata 组件)` | **`[]`** |
| 同一个查询，父组件换成 Blueprint 造的 `USpSceneCaptureComponent2D` | `[UStaticMeshComponent, UDrawFrustumComponent, USpFuncComponent]` |

原因在 SPEAR 侧：`getSpFuncComponent()` **只认直接 attach 子节点**
（`UnrealUtils.h` 的 `getChildrenComponentsByType<USpFuncComponent>(component, false)`
→ `AttachChildren`）。而 `UnrealUtils::createSceneComponentInsideOwnerConstructorByClass`
建 `sp_func_component` 时用 `CreateDefaultSubobject` + `SetupAttachment`——UE 的
`SetupAttachment` 只写 `AttachParent`，真正的 `AttachChildren` 接线发生在
`AttachToComponent`（源码注释：*"SetupAttachment should only be used to initialize
AttachParent and AttachSocketName for a future AttachToComponent. Once a component is
registered you must use AttachToComponent."*）。owner 作为 class default object 构造
（也就是 Blueprint）时，actor 注册会替它接上；运行时 `NewObject` 出来的组件没人接，
于是 SpFuncComponent 建了却不在 `AttachChildren` 里，SPEAR 认为该组件没有任何 SpFunc。

**修法（已落地）**：给组件加 `UFUNCTION` 的 `Initialize()` / `Terminate()`（与上游
`USpSceneCaptureComponent2D::Initialize()` 同形），`Initialize()` 里先
`SpFuncComponent->AttachToComponent(this, KeepRelativeTransform)` 再注册四个 SpFunc；
`BeginPlay()` 也调它，所以 Blueprint 放置的用法照旧。Python 侧在事务里显式调用。

无头编辑器实测对照（同一轮同一次查询）：

| 查询 | 修前 | 修后 |
|---|---|---|
| 相机 actor 上的 `USpFuncComponent` 数量（建之前 → 建之后） | 20 → 21 | 20 → 21 |
| `get_children_components_for_scene_component(metadata 组件)` | **`[]`** | `[USpFuncComponent]` |
| `{"event": "metadata_sp_funcs", "available": ...}` | `[]` | 四个全有 |

Blueprint 造的 `USpSceneCaptureComponent2D` 一直是
`[UStaticMeshComponent, UDrawFrustumComponent, USpFuncComponent]`，作为对照。

#### 编辑器路径上修掉的六处（2026-10-10）

这条路径此前**从未跑通过**——metadata 回读链路只有 `run_spear_sim.py` 在用，
`run_physics_sim.py` 直接 `read_pixels()`、不碰 metadata，所以一直没暴露。实测依次
暴露并修掉：

| 位置 | 症状 | 原因 |
|---|---|---|
| `_get_game_world()` | `get_game()` 抛 `Unexpected frame state: request_begin_next_frame` | `EditorRequestBeginPlay()` 之后的 `end_frame(single_step=True)` 把帧状态钉在 `request_begin_next_frame`，而随后 `get_game()` 的 worker-thread world 查询只接受 `idle`。改用普通 `end_frame()`（与 [`test_urdf_views.py`](../tools/test_urdf_views.py) 同款做法，那边早就有注释说明） |
| `_find_metadata_component()` | `UnrealUtils.h:1410 ERROR: Input vector is empty`，随后一长串 `In an error state` | 单数 `get_component_by_class`/`get_component_by_name` 走 `UnrealUtils::toItem`，**查询未命中时是硬断言**并污染引擎错误状态，补建回退根本执行不到。改成复数 `get_components_by_class`/`get_components_by_name`（返回可能为空的 vector） |
| `metadata_component.initialize_sp_funcs()` | `Entry points that execute on the game thread are only allowed in "with begin_frame()" ...`，帧状态 `idle` | 它是 game-thread 入口，必须在事务里调用；原来是裸调用在 `end_frame()` 之后。改为放进事务 |
| `game.get_unreal_object(uclass="UGameplayStatics")` | 同上的 game-thread 断言 | 构造 class default object 句柄也是 game-thread 入口，原来也在事务外。移进事务 |
| `capture_component` 没初始化 | 循环里 `read_pixels` 退化成属性访问：`TypeError: 'UnrealObject' object is not callable` | `apply_scene_binding_plan()` 按设计用 `initialize=False` 返回相机组件；`read_pixels`/`enqueue_copy` 要等 `Initialize()` 才注册。补上"先对齐投影 → `Initialize()` → `initialize_sp_funcs()`"（上游注释里的顺序） |
| `SpearRenderPort._consume_readback()` | `readback_metadata()` 在 `end_frame()` **之后**被调用，game-thread 断言 | `readback_metadata` 也是 SpFunc，只能在 `begin_frame`/`end_frame` 块内调用。把配对移进 `end_frame` 临界区 |

验证：`tools/run_spear_sim.py --frames 8 --low-vram --no-mujoco-viewer --save-dir ...
--trace ...` 在
`xvfb` 无头编辑器下 **exit 0**，产出 8 组 `commit_NNNNNNNN.bin/.png`（512×512 RGBA，
1 MiB/帧）+ `trace.csv` 8 行（`max_body_speed` 非零、`wheel_1_angle` 递增）。
`ue_frame_id` 每笔事务前进 3–4 帧（52→55→59→63→67→71→75→79）——这正是"事务之间
不保证连续 UE 帧"的观测证据，也说明去掉 `single_step` 之后游戏线程确实在自由跑
（`single_step=True` 下会被钉成逐帧）。

```bash
# 交互（编辑器窗口里看，Ctrl-C 停）；MuJoCo viewer 默认就开，不用加参数
DISPLAY=:1 conda run --no-capture-output -n spear-env python -u tools/run_spear_sim.py \
  --user-config /path/to/user_config.yaml \
  --forever --drive-wheels 6

# 出图（固定帧数，落 PNG + trace）；无头必须显式关掉 viewer
conda run --no-capture-output -n spear-env python -u tools/run_spear_sim.py \
  --user-config /path/to/user_config.yaml \
  --plan generated/urdf/总装/scene_binding_plan.json \
  --model generated/urdf/总装/mujoco.urdf \
  --no-mujoco-viewer \
  --frames 100 \
  --save-dir /tmp/rm_sim_spear_frames
```

**MuJoCo viewer 默认开着（2026-10-10 起）。** 它是交互入口，而且**碰撞体只在这个窗口里
看得见**——UE 画的是视觉网格，`simple_collision` 那 4 个轮柱 + 1 个底盘盒在 UE 里根本不
存在。所以 `run_spear_sim.py` 用 `argparse.BooleanOptionalAction`，默认 `True`，
`--no-mujoco-viewer` 才是关闭。

无头必须显式关，两条路都量过：

| 环境 | 结果 |
|---|---|
| 没有 `DISPLAY` | `launch_passive` 在 C 层 `exit(1)`，只留 `ERROR: could not initialize GLFW`；**Python 接不到**，所以工具改成调用前预检 `DISPLAY`/`WAYLAND_DISPLAY`，缺失时 `SystemExit` 并提示补 `--no-mujoco-viewer` |
| `xvfb-run -n 99` | 预检放行，窗口**真的开了**（`is_running()` 为真、`sync()` 有效），但退出时挂死：`X Error of failed request: GLXBadDrawable (X_GLXSwapBuffers)`，只能超时杀掉 |

`tools/run_physics_sim.py` 的 `--mujoco-viewer` 仍是 opt-in（它的无头吞吐基准不想要
GUI），两边默认值不一致是**故意的**。

**在 viewer 里默认看不到碰撞体，这是刻意的。** URDF 导入器把 `<collision>` 放进 group 0、
`<visual>` 放进 group 1，而 MuJoCo 默认两组都画，于是 955 个 CAD 碰撞壳叠在 955 个视觉网格
上，关节和轮子全被盖住。`MujocoDriver.hide_collision_geometry()` 把所有碰撞几何搬进
**group 3**（MuJoCo 约定的"只见碰撞"组）。要看碰撞体：viewer **左侧 Rendering 面板勾
Geom Group 3**；地面留在 group 0，是参考面不是碰撞壳。纯显示改动，接触数不变
（实测 `ncon=8`、8 个地面接触，全在四个轮柱上）。

**运行时会看到的两件"看起来像坏了"的事：**

1. **循环结束时画面定格，但编辑器还是活的。** 每个事务末尾都会
   `SetGamePaused(True)`，循环退出后没人再解暂停，所以 UE 游戏世界停在最后一个
   提交点上——**画面不动正是设计行为**，不是卡死（编辑器可点就是证据）。结束时脚本
   会打一行 `{"event": "loop_done", "paused_at_exit": true, ...}`。要一直动就用
   `--forever`，想停再 Ctrl-C。
2. **不带 `--drive-wheels` 时机器人纹丝不动。** 生成的 URDF 只描述机器人本身：
   实测 `nq=6 nv=6 **nu=0**`，`base_link` **没有关节**（焊在 world 上），也没有地面。
   所以 MuJoCo 在跑（`sim_time_s` 在涨）但底盘在数学上不可能移动，只有四个轮子和云台
   在自由摆动。加 `--drive-wheels <rad/s>` 之后模型变成 `nq=13 nv=12 nu=4`，多出一个
   free joint、四个速度执行器和一块地面。

**只有一个 SPEAR 实例能跑。** SPEAR 客户端连的是配置里那个固定 RPC 端口，第二个
实例连上的会是第一个的 server，SPEAR 自己会拒绝：

```
ERROR: ... get_current_process_id returned 43292 but the PID of the process we just
launched is 44294. The Unreal Editor might be open already ...
```

所以跑 `run_spear_sim.py` 之前要先确认没有别的 Unreal Editor 在跑（`pgrep -x UnrealEditor`）。
另外脚本**从不调用 `instance.close()`**，退出后编辑器会留下来——这既是"跑完还能继续
看"的原因，也是下一次运行会撞端口的原因。

这个脚本启动 `spear.Instance`，在首个事务中完成 link 锚点、相机和 IMU
绑定（装甲段为空，装甲视觉是装甲 link 锚点的子件），然后每帧执行 MuJoCo 推进、link 位姿同步和 SPEAR 图像回读。图像先以原始
`.bin` 保存，同时在回读数据是标准 `uint8` 的 RGB/RGBA 缓冲时写出同名 `.png`；
每帧输出中的 `image_readback`、`image_bytes`、`raw_path` 和 `png_path` 可用于核对
是否确实完成了读图。若不传 `--save-dir`，图像仍会被回读并参与帧配对，但只保留在
内存中。

`--user-config` 的 `SPEAR.LAUNCH_MODE` 可以是 `game`（已打包的 UE 游戏）或
`editor`（UE Editor + 项目路径）。仓库中的 `rm_sim_spear/cli.py` 不启动 UE，
只用于离线核心测试。

### 双视角 NumPy 验收

在开始写控制逻辑前，可以用下面的脚本同时检查主视角和 URDF 绑定相机：

```bash
conda run --no-capture-output -n spear-env python -u tools/test_urdf_views.py \
  --user-config tools/spear_user_config.yaml \
  --plan generated/urdf/总装/scene_binding_plan.json \
  --model generated/urdf/总装/mujoco.urdf \
  --frames 1 \
  --output-dir /tmp/rm_sim_spear_views \
  --display
```

脚本在首个 SPEAR 事务中创建四个 `RMSim/armor_*` 整体克隆，并绑定
`cam_link_8`；随后每帧把两路图像读成 NumPy 数组。输出目录包含：

```text
main_view_000001.npy/.png       # 当前主视口
bound_camera_000001.npy/.png    # cam_link_8 上的绑定相机
comparison_000001.png           # 左右拼接预览
```

`--display` 需要图形桌面；没有桌面时去掉它，仍会保存 NumPy 和 PNG 文件。

### 调试记录：绑定失败与细线断续

#### `armor_link_1` 不是 UE 场景 Actor

`armor_link_1` 来自 URDF/MuJoCo 的 link 名称，是运行时位姿同步的坐标系；它不要求
在 UE Outliner 中预先存在。场景中实际需要存在的是 `armor_1` 或
`Scene/armor` 模板 Actor。绑定器找不到 link 锚点时会创建 transient 的空 Actor，
再把装甲克隆 attach 到这个锚点。找不到模板 Actor 才是场景配置错误，应检查绑定计划
中的 `actor_name` 和 `actor_path`。

若日志出现 `cannot attach ... which is static to it`，说明模板的根或子 Actor 仍是 `Static` mobility。运行时绑定器会递归把**解析出的机器人视觉 Actor 及其子层级**改为 `Movable`（`_make_actor_tree_movable()`），再执行 attach；这是 PIE/独立运行内存中的运行时状态，不会保存回关卡资产。

不要把“URDF 中有该 link”与“UE 中已有同名 Actor”混为一谈。SPEAR 的单个 Actor
查找接口在零匹配时可能把 EngineService 置为 error；绑定器使用安全的映射查询，
再决定创建 link 锚点还是报告模板缺失。

#### 低分辨率细线变成点列

该现象首先要区分为渲染采样问题，而不是 SDK 共享内存或 PNG 回读问题。若主视角和
绑定相机的原始 NumPy 图像同时出现断续，问题发生在 UE 捕获之前。

Rune SDK 的基线（`rm_sim_rune/SimUnreal/Plugins/sim/Source/sim/Private/RunePawn.cpp`）是：
`USceneCaptureComponent2D` + `SCS_FinalColorLDR`、`bCaptureEveryFrame=false`、
`bCaptureOnMovement=false`、`bAlwaysPersistRenderingState=true`，render target 为
`RTF_RGBA8_SRGB`（`bForceLinearGamma=false`），`InitAutoFormat(1440, 1080)`
（`rmsim_protocol.h` 的 `ImageWidth/ImageHeight`）——**捕获目标分辨率与输出分辨率
1:1，从不缩小**；每次 `CaptureScene()` 前把玩家相机的 `PostProcessSettings` 和
`PostProcessBlendWeight` 原样拷给捕获组件。它**不做超采样**。

`tools/test_urdf_views.py` 使用同一条基线：`SCS_FinalColorLDR`、保留
`bAlwaysPersistRenderingState`、同步视口后处理、预热渲染历史、每次提交后设置
`bCaptureGpuNextRender`，并且默认 `--supersample 1.0`（渲染目标 = 输出分辨率）。
`--max-internal-pixels` 只在需要**削减超采样**时生效，永远不会把目标降到 `1.0×`
以下：低于输出分辨率的 target 再在 CPU 侧放大，正是远处细线断成点列的原因。实测
出现过 main `632x474 = 0.44×`、bound `1155x866 = 0.80×`，随后 `cv2.INTER_AREA`
放大到 `1440x1080`。运行时日志 `[3/6] internal capture: ...` 打印实际比例，低于请求的
`--supersample` 时标注 `budget-limited`。

实测对照（同一条 1440×1080 独立运行命令，只差内部目标尺寸）：旧默认渲染
`main 632x474 (0.44x)`，远处蓝色场地线断成一串色块、白色场地线断成虚线；
新默认渲染 `main 1440x1080 (1.00x)`（日志原文
`[3/6] internal capture: main 1440x1080 (1.00x), bound 1440x1080 (1.00x), output 1440x1080`）
后同一位置的蓝线是连续实线、白线连续，只有原本就是虚线样式的那几段仍是虚线。
两路输出都是 `[1080, 1440, 4] uint8`，`--verify-armor-links` 仍为 `all_ok=true`。

需要比 rune 更好的细线连续性时用真超采样：

```bash
--supersample 2 --max-internal-pixels 6500000   # 1440×1080 → 2880×2160 RGBA8 ≈ 25 MB/target
```

注意 render target 本身只多占几十 MB，但**后处理/TSR 瞬时缓冲随目标像素数增长**：本机
独立运行 1440×1080 双视角顺序捕获实测，`1.00×` 峰值 6672 MiB、最小空闲 1110 MiB，
`2.00×` 峰值 7718 MiB、最小空闲 63 MiB（两次都不加 `--keep-expensive-rendering`，
即脚本在运行时关掉了 Lumen/光追/VSM）。也就是说真超采样多花约 1 GB 瞬时显存，
空闲余量几乎归零，只有在桌面显存确实空闲时才用；日常验收用 `1.00×`。

`r.ScreenPercentage` 不影响捕获：引擎对不继承主视口分辨率的 SceneCapture 直接用
`FLegacyScreenPercentageDriver(InResolutionFraction = 1.0)` 并关闭
`EngineShowFlags.ScreenPercentage`（`SceneCaptureRendering.cpp`），SPEAR 也没开
`bRenderWithMainViewResolution`。所以 `--low-vram` 里曾经设的 `r.ScreenPercentage=50` 只作用
于编辑器/游戏视口，不会破坏保存的图像（该 cvar 已于 2026-10-08 从预设移除，见下文）。

修改 `CaptureSource` 或目标分辨率后必须重新初始化 `USpSceneCaptureComponent2D`
（`_set_capture_resolution()` 会先 `Terminate()` 再 `Initialize()`），否则已有 render
target 不会改变。

#### 显存预算（实测，2026-10）

UE 在失败时会打印自己的 `Device Memory` dump 和 `Memory Budget Extension` 表。实测本机
（RTX 4060 Laptop 8188 MiB）可用预算为：

| 现象 | heap Size | 驱动预算 budget | 引擎自身占用 | 结果 |
|---|---|---|---|---|
| 两次启动崩溃 | 8188 MiB | **6358 MiB** | 6134 MiB | 256 MiB 连续申请失败 |
| 两次通过（预设未生效） | 8188 MiB | **6736 MiB** | ~6.6 GB（nvidia-smi 峰值 7419–7496 MiB） | 正常出图 |

所以"8 GiB 显存"不是可用额度：可用额度是 **约 6.36–6.74 GiB，随桌面上其它进程浮动**。
本文件此前"同样参数有时成功、有时在启动阶段失败"（`500000` 成功、降到 `300000` 反而
失败）就是这 ~380 MiB 浮动造成的，和内部像素预算无关。

崩溃机制（来自两次崩溃的 UE 日志，如
`ue/Saved/Logs/rm_sim_spear-backup-2026.10.05-14.03.34.log`；该目录会轮转）：渲染线程在
`FRDGBuilder::Execute()` → `FRHITransientResourceHeapAllocator::CreateTextureInternal`
→ `FVulkanTransientHeap::FVulkanTransientHeap` → `AllocateBufferMemory` 中申请
**256 MiB 连续块**，失败即 `Out Of Memory, trying to allocate 268435456 bytes`
（`VulkanMemory.cpp:3114`）。崩溃位置是 PIE 世界最初几帧
（`Breadcrumbs 'RDGExecute_RenderThread'`、`Frame 16`），发生在脚本 `[3/6]` **之前**。
因此：

- `--max-internal-pixels` 是**超采样/回读预算，不是显存总闸**：它只削减
  `--supersample`，且不会把捕获目标降到输出分辨率以下（详见上一节）。
  `1440x1080` RGBA8 目标 6.2 MB、`2×` 目标 24.9 MB，而场景本身峰值约 6.6 GB。
  继续降低它降不下启动峰值。
- 在 PIE 内用 `set_cvar` 关 Lumen/光追/VSM 也**赶不上**地图加载峰值：`-ExecCmds` 是
  在 `MAP LOAD FILE=...creo_stp.umap` **之后**才执行的（实测日志中两条记录的行号）。
- 有效做法是在**配置加载阶段**改渲染配置，即下面的 `--low-vram`。

#### `--low-vram`：在关卡加载前落低显存预设

`--low-vram` 把预设开关以 `-ini:Engine:[SystemSettings]:<cvar>=<value>` 传给
`UnrealEditor`，在配置加载阶段生效（日志中可见 `LogConfig: Set CVar [[r.RayTracing:0]]`）。
当前预设共 7 条：关闭 `r.RayTracing`、`r.DynamicGlobalIlluminationMethod`、
`r.ReflectionMethod`、`r.Shadow.Virtual.Enable`、`r.GenerateMeshDistanceFields`、
`r.Lumen.DiffuseIndirect.Allow`，并把 `r.LumenScene.SurfaceCache.AtlasSize` 降到 1024。

> 2026-10-08 起预设**不再**设置 `r.Streaming.PoolSize=512` / `r.Streaming.LimitPoolSizeToVRAM=1`
> 与 `r.ScreenPercentage=50`（原为 10 条）：前两者在同口径实测里只把峰值移动 −3 MiB
> （[`render_budget.md`](render_budget.md) §3.2），代价是把细线/alpha 细节压到低 mip；后者对
> SceneCapture 完全无效（见上一节）。下面这张对照表是**改动前**（10 条版本）的实测，数字保留作历史证据。

同一条 `1440×1080` 命令的实测对比：

| 运行 | 预设 | nvidia-smi 峰值 | 最小空闲 | 结果 |
|---|---|---|---|---|
| 对照 | 无 | 7419 MiB | — | 出图（当次预算 6736 MiB） |
| `-ExecCmds`（太晚） | 只影响 PIE 内 | 7496 MiB | — | 出图 |
| `--low-vram` ×3 | `-ini:[SystemSettings]` | **4271–5263 MiB** | **2518–3511 MiB** | 出图（当次预算 6722–6870 MiB） |
| 独立运行 `-game`（无预设，保留 Lumen/光追/VSM） | 无 | **5984–6087 MiB** | **1694–1798 MiB** | 出图（当次预算 6752–6774 MiB） |

峰值下降约 **2.2–3.1 GB**，空闲余量从百 MiB 级升到 2.5–3.5 GB 级。注意 `--low-vram` 改变光照、
反射和阴影质量，只适用于**几何与绑定验收**；渲染保真验收要在解决预算后用独立运行
（见下）或更大显存的机器。

合并 Actor 后默认保持纹理流式加载，不再把整张 CAD 地图的所有纹理一次性推入显存。
`--fully-load-textures` 的显存代价取决于上下文：PIE + 双 SceneCapture 时强制全量纹理曾在
SceneCapture 预热阶段让一次 128 MiB transient 申请失败；而 2026-10 的独立 `-game` 单捕获实测
里，1440×1080 下全量纹理只比流式多 **20 MiB**（见 [`render_budget.md`](render_budget.md) 第 3 节），
512×512 下则在噪声范围内。结论：独立单捕获路径可以把 `--fully-load-textures` 当默认开，
用它换细线清晰度；PIE/多捕获调试仍要保持流式。纹理侧的常规额度开关是
`r.Streaming.PoolSize`（项目默认 1000 MB），`--low-vram` 曾把它降到 512 MB，实测对峰值几乎无影响
——正因如此，2026-10-08 起该预设不再覆盖它。

#### 该用的验收命令

512×512 场景绑定调试：

```bash
xvfb-run -a env SDL_VIDEODRIVER=x11 \
  conda run --no-capture-output -n spear-env python -u tools/test_urdf_views.py \
  --user-config tools/spear_user_config.yaml \
  --plan generated/urdf/总装/scene_binding_plan.json \
  --model generated/urdf/总装/mujoco.urdf \
  --width 512 --height 512 \
  --low-vram \
  --output-dir /tmp/rm_sim_spear_views
```

1440×1080 双视角（低显存预设 + 顺序捕获）：

```bash
xvfb-run -a env SDL_VIDEODRIVER=x11 \
  conda run --no-capture-output -n spear-env python -u tools/test_urdf_views.py \
  --user-config tools/spear_user_config.yaml \
  --plan generated/urdf/总装/scene_binding_plan.json \
  --model generated/urdf/总装/mujoco.urdf \
  --frames 1 --width 1440 --height 1080 \
  --sequential-capture --robot-focus --low-vram \
  --output-dir /tmp/rm_sim_spear_views_1440
```

两个视角都按输出分辨率（`1440×1080`）渲染，这就是默认行为：`--supersample 1.0`
且 `--max-internal-pixels 0`（不设上限）。要做比 Rune SDK 更强的细线连续性，加
`--supersample 2 --max-internal-pixels 6500000`（代价见上一节：目标只多几十 MB，
但瞬时后处理缓冲多约 1 GB）。

每次运行默认带三道显存闸门（可用 `--min-free-vram-mb 0`、`--vram-poll-seconds 0` 关闭）：

- **preflight**：`nvidia-smi` 空闲显存低于 `--min-free-vram-mb`（默认 1200 MiB）时直接
  中止并列出占用显存的进程，避免在别的 GPU 任务仍在跑时启动编辑器。
- **采样**：每 `--vram-poll-seconds`（默认 1 s）逐行 flush 写
  `--vram-csv`（默认 `<output-dir>/vram.csv`，列 `time,used_mb,free_mb`），崩溃也能保留；
  结束时打印 `peak_used_mb`、`min_free_mb` 和 `device_local_budget_mb`（后者从 UE 日志的
  budget extension 表解析）。判断一次运行是否安全看 `min_free_mb`，不是平均值。
- **崩溃检测**：`--ue-log` 默认取 `<project>/Saved/Logs/<project>.log`；脚本在运行期间
  解析 `Out Of Memory`、`Failed to allocate Device Memory`、`Fatal error`、
  `Fatal X11 IO Error` 等签名，并监测 UE 进程是否存活。命中即打印
  `{"event": "ue_crash_detected", ...}` 与日志尾部并立即退出（退出码 4），不再等 RPC
  超时；`--no-fail-fast` 保留旧的等待行为。已实测：运行中 `kill -9` UnrealEditor 后该
  路径立即报 `engine_process_exited`，而不是等 30 s 超时。

上面两段用 `xvfb-run -a`：本机 `xvfb-run -a` 曾偶发丢失 X server，UE 会在启动阶段以
`Fatal X11 IO Error` 退出（此时 Python 侧只会看到连接重试）。该情况现在会立刻被报成
`x11_fatal` 并退出码 4；关键验收可改用真实显示（`DISPLAY=:1`）或显式
`xvfb-run -n <display>`，避免 `-a` 的显示号竞争。

脚本还会在 `[3/6]` 回读运行时 cvar（`[3/6] runtime cvar check: r.RayTracing=0`），
避免"以为改了、其实被 IConsoleManager 优先级静默拒绝"。

两个视角现在都按输出分辨率渲染，因此不再需要"给绑定相机单独加预算"。参考命令
（顺序捕获保证两个 render target 不同时存活）：

```bash
xvfb-run -a env SDL_VIDEODRIVER=x11 \
  conda run -n spear-env python -u tools/test_urdf_views.py \
  --user-config tools/spear_user_config.yaml \
  --plan generated/urdf/总装/scene_binding_plan.json \
  --model generated/urdf/总装/mujoco.urdf \
  --frames 1 --width 1440 --height 1080 \
  --sequential-capture --robot-focus \
  --output-dir /tmp/rm_sim_spear_views_1440_bound
```

此前"绑定相机把预算从 `300000` 提到 `1000000` 后细线连续性明显改善"的观察，
来自两个**都低于 1.0×** 的目标相比：main `300000` → `0.44×`，bound `1000000`
→ `0.80×`。所以当时的结论要修正为：断续的根因是两路都在欠采样再于 CPU 侧放大，
而不是"输出尺寸无关"。`--max-internal-pixels` 现在只削减 `--supersample`，不会再
低于 `1.0×`；需要更强连续性时显式做真超采样（`--supersample 2`）。
启动峰值仍由 `--low-vram` 在配置加载阶段决定，顺序捕获只保证两个 render target
不同时存活。

改回 1:1 或真超采样后，剩余断续应主要来自几何别名、曝光或线材质。脚本在顺序捕获
重新启用绑定相机后默认丢弃 4 帧，让 `bAlwaysPersistRenderingState` 对应的 TAA 历史
先稳定下来；可用 `--capture-warmup-frames 0` 关闭，或增加该值做诊断。

##### 自由相机：人看 `:1`，agent 跑无头

有两个不同的东西，别混：**会动的仿真 + 你飞的相机**，和**冻结姿态 + 你飞的相机**。

| 要什么 | 用什么 | 每帧 `physics.step`？ |
|---|---|---|
| 看机器人真的掉下去/驱动/翻滚，同时自由飞 | `tools/view_pose.sh` → `run_physics_sim.py --interactive` | 是 |
| 看绑定/限位/穿模对不对（一帧冻结姿态） | `test_urdf_views.py --continuous --display` | 否（`tools/test_urdf_views.py:1201`） |

`tools/view_pose.sh` 把 `run_physics_sim.py --interactive` 连同本项目的
config/plan/model 包成一条命令，并**区分"给人看"和"给 agent 跑"两种显示路径**：

```bash
tools/view_pose.sh                # 可见：真实显示（默认 DISPLAY=:1），OpenCV 窗口
tools/view_pose.sh --headless     # 无头：Xvfb :99，没有窗口，跑 120 帧后停并落盘
```

这个区分不是洁癖：相机靠 `cv2.waitKey` 从 OpenCV 窗口读键盘事件，窗口必须在
操作者**看得见、点得着**的显示器上。`xvfb-run ... --display` 那种写法窗口开在虚拟
display 上——画面照渲染，按键永远收不到。所以 agent 侧一律 `--headless`（存下来的帧
和 `trace.csv` 就是它的证据），真实显示器留给写代码的人。

脚本之后的参数原样转发给 `run_physics_sim.py`，所以它自己的选项都还能用：

```bash
tools/view_pose.sh --drive-wheels 0                    # 只站着，不驱动
tools/view_pose.sh --settle-frames 200                 # 落地后再等一会儿才驱动
tools/view_pose.sh --width 1440 --height 1080
tools/view_pose.sh --view main                         # 用视口对齐机位而不是固定机位
tools/view_pose.sh --camera-speed-cm 10 --camera-turn-deg 2
tools/view_pose.sh --screen :2                         # 换显示器
```

脚本**不会**杀 Unreal 进程：发现已有 `UnrealEditor` 就直接报错退出（SPEAR 会连到
那个已经在监听的 RPC server，而不是新起一个）。要自己决定时，把
`--kill-stale-editor` 透传下去，由 Python 侧做**按工程限定**的 SIGKILL
（`tools/spear_user_config.yaml` 是 `LAUNCH_MODE: "editor"`，作用域是
`ue/rm_sim_spear.uproject`，不会波及别的工程）。

窗口获得焦点后：`W/S` 前后、`A/D` 左右、`E/Q` 上下、`I/K` 俯仰、`J/L` 转向，
`X`/`Esc` 退出（`Ctrl-C` 也行）。按键处理复用 `test_urdf_views` 的
`_move_debug_camera`，`(position, target)` 与它的 `{location, pitch, yaw}` 之间的换算
在 [`rm_sim_spear/view_pose.py`](../rm_sim_spear/view_pose.py) 的 `free_camera_state` /
`free_camera_pose`（有单测）。相机只在按键后重新对准（`camera_dirty`），所以不按键时
每帧的 RPC 开销和固定机位一样。

`--interactive` 只允许 `--view fixed` / `--view main`：绑定相机挂在 `cam_link_8` 上，
飞它就没有"机器人自己看到什么"这个判据了。它也**没有帧预算**——一直跑到你按退出；
要固定帧数就显式给 `--frames N`（无头测试就是这么跑的）。默认还会把
`--save-images` 压成 `none`，否则无界运行会把 `--output-dir` 写满；想要图就显式
`--save-images sync`。没有可用显示时它直接报错退出，而不是在等一个永远不来的按键。

冻结姿态那个观察器（`test_urdf_views.py --continuous --display`）仍然可用。它没有
`run_physics_sim.py` 那一层包装，所以命令要自己写全，**并且必须开在真实显示上**
（`--display` 的窗口要在你面前；加 `xvfb-run` 就等于自断键盘）：

```bash
env DISPLAY=:1 SDL_VIDEODRIVER=x11 \
  conda run --no-capture-output -n spear-env python -u tools/test_urdf_views.py \
  --user-config tools/spear_user_config.yaml \
  --plan generated/urdf/总装/scene_binding_plan.json \
  --model generated/urdf/总装/mujoco.urdf \
  --continuous --display --robot-focus \
  --width 1024 --height 768 --continuous-fps 10 --no-save
```

它启动时显示主视角，按 `1` 切回主视角、按 `2` 显示绑定相机、按 `Tab` 在两者之间切换
（切换时终止当前目标、释放渲染状态再初始化另一个，两路不会同时占显存）。**它的按键
只驱动主视角**——绑定相机属于机器人本身，所以显示 `bound` 视角时按键画面不动，此时它
打一行 `free-camera keys move the main view; press 1 to see it`。要两路并排加
`--dual-view`。agent 侧用无头版本时去掉 `--display` 并加 `--no-save`，
`--frames` 之外的东西都不该依赖键盘。

如果要在 1440×1080 下采集一帧，脚本会自动启用顺序捕获；也可以显式写出：

```bash
xvfb-run -a env SDL_VIDEODRIVER=x11 \
  conda run -n spear-env python -u tools/test_urdf_views.py \
  --user-config tools/spear_user_config.yaml \
  --plan generated/urdf/总装/scene_binding_plan.json \
  --model generated/urdf/总装/mujoco.urdf \
  --frames 1 --width 1440 --height 1080 \
  --sequential-capture \
  --robot-focus --output-dir /tmp/rm_sim_spear_views_1440
```

顺序捕获会先释放主视角 SceneCapture，再采集绑定相机，两个目标不会同时占用显存。
这比降低帧率有效：显存申请发生在 render target/瞬时 Vulkan buffer 创建阶段，帧率
只影响时间间隔，不改变单次峰值。绑定计划阶段也不会再提前初始化绑定相机的 render
target；初始化延迟到主视角目标创建之后，避免在 `[3/6]` 同时保留一个尚未使用的第二目标。
即使这样，编辑器 PIE 仍要为编辑器视口、PIE 世界、场景纹理和渲染瞬时缓冲预留显存，
因此日志中可能出现 `Failed to allocate Device Memory` 的小块重试。只要随后出现
`event: ready` 和 `event: frame`，就表示 Vulkan 回退分配成功；若在 `[3/6]` 长时间卡住，
先看 `--vram-csv` 的 `min_free_mb`，再用 `--low-vram` 重跑，而不是继续降低内部像素预算。

### 编辑器与独立运行

`tools/spear_user_config.yaml` 默认使用 `LAUNCH_MODE: editor`：SPEAR 启动
`UnrealEditor`，进入 PIE，再绑定机器人视觉 Actor（见下）和 `Scene/armor`。这种模式
便于调试关卡 Actor，但会同时保留编辑器视口、PIE 世界和两个 SceneCapture，显存峰值最高。

脚本提供独立模式；它在 2026-10 已实测可用，是 1440×1080 及之后验收的推荐路径（显存
余量比编辑器 PIE 多约 1.4 GB）：

```bash
env DISPLAY=:1 \
  conda run --no-capture-output -n spear-env python -u tools/test_urdf_views.py \
  --standalone --frames 1 --width 512 --height 512 \
  --kill-stale-editor \
  --user-config tools/spear_user_config.yaml \
  --plan generated/urdf/总装/scene_binding_plan.json \
  --model generated/urdf/总装/mujoco.urdf
```

该选项向 `UnrealEditor` 传入 `-game`：直接加载 `/Game/creo_stp` 游戏世界，不启动编辑器
PIE，也不保留编辑器视口和编辑器世界。实测（2026-10）这条路已经可用，不需要先
Cook/打包，代价是仍然使用编辑器可执行文件、直接读项目里的未 Cook 内容：

- 关卡 Actor 在游戏世界里存在，link 锚点和它们的装甲子件照常建立。装甲模板组是
  `Scene/armor/前哨站_body36_2`、`_body37_2`、`_body38_2`、`_body398_2`（编辑器实测
  位置 `[125.2, -1.4, -128.5]`）；它现在**只作关卡里的调参组存在**，不再被运行时克隆
  （`scene_bindings.armor.enabled=false`，见上文"关闭冗余的场景装甲克隆"）。
- 机器人视觉 Actor 走运行时加载：用 `robot_visual.asset_path`（缺省按 `actor_name`
  推导为 `/Game/<actor_name>`）加载 `UStaticMesh`，spawn 一个 movable
  `AStaticMeshActor` 并挂到 `base_link`；`ready` 事件里会报告
  `robot_visual_source: "runtime_spawn"`。**注意当前 override 指向的
  `SM_MERGED_blender_for_ue5_node_22` 是能量机关的合并网格，不是总装机器人**
  （见文末"当前进度与剩余问题"）。
- 编辑器模式与独立模式的绑定结果一致：`binding_pose_coverage=1.0`、
  `visual_binding_render_ready=true`；`armor_clone_mesh_components` 在关闭克隆后应为空
  （旧记录是 `[4,4,4,4]`）。

独立模式跑 1440×1080 的实测显存（未加 `--low-vram`，即保留地图的 Lumen/光追/VSM
项目设置）：峰值 5984 MiB、最小空闲 1798 MiB、当次 budget 6752 MiB。相比编辑器 PIE
（不加载低显存预设时峰值 7419 MiB）省下约 1.4 GB，这正是 `--low-vram` 之外的另一条
拿回余量的路径。

推荐命令（窗口本身按 1440×1080 显示，绑定相机也按 1440×1080 采集并显示）：

```bash
env DISPLAY=:1 \
  conda run --no-capture-output -n spear-env python -u tools/test_urdf_views.py \
  --user-config tools/spear_user_config.yaml \
  --plan generated/urdf/总装/scene_binding_plan.json \
  --model generated/urdf/总装/mujoco.urdf \
  --frames 1 --width 1440 --height 1080 --window-size 1440 1080 \
  --sequential-capture --robot-focus --standalone \
  --display --display-ms 5000 \
  --verify-armor-links --kill-stale-editor \
  --output-dir /tmp/rm_sim_spear_standalone_1440
```

- `--window-size W H` 覆盖 `resx/resy`，让 UE 窗口（而不只是输出文件）按该分辨率显示。
- `--display` 用 OpenCV 窗口把采集结果按 `--display-ms` 显示在 `DISPLAY` 上；没有桌面时
  去掉它，PNG/NumPy 仍然会写盘。
- `--robot-focus-position X Y Z`（默认 `250 -300 180`，UE cm）把主视角诊断相机放到指定
  位置看向世界原点；装甲 link 实测在 `X∈[-14,42] cm, Y∈[1,57] cm, Z≈2–7 cm`，用
  `--robot-focus-position 115 -125 62` 可以得到含四个装甲挂点的近景。
- 启动时**默认就会落在机器人绑定点上**：`creo_stp` 没有 PlayerStart，默认 Pawn 和 player
  camera 生成在世界原点，而机器人视觉也绑在原点。实测 `robot_visibility` 报告
  `player_pov_cm=[0,0,0]`、`robot_visual_cm=[0,0,0]`、`distance_cm=0.0`——相机在视觉体
  内部，所以"默认视角里看不到东西"是位置问题，不是亮度问题。距离小于 100 cm 时
  脚本会自动改用机器人视角并打印 `auto_robot_focus`；要保留原始默认视角用
  `--keep-default-view`。注意看到的**内容**取决于 `robot_visual` 指向哪个资产：
  当前指向能量机关合并网格，因此看到的是机关而不是总装机器人。
- 自由视角按键作用于**脚本自己的采集相机**，所以 OpenCV 窗口里飞到哪里，存下来的
  `main_view_*.png` 就是哪里。UE 窗口本身：SPEAR 的 `SpPlayerController` 打开了
  `bShouldPerformFullTickWhenPaused` 和 `bIsCameraMoveableWhenPaused`，但实测在 `-game` +
  世界暂停时，`get_current_viewport_desc()` 的 POV 恒为 `(0,0,0)`，且把默认 Pawn 传送走
  也不会跟着变（`ViewTarget.Target` 是 `APlayerController` 而不是相机 Actor）。因此在
  脚本里**不要依赖实时 viewport POV**；需要看哪里就用 `--display` 的窗口驱动采集相机，
  或用 `--robot-focus-position` 显式指定。
- `--verify-armor-links` 在采集结束后用**扰动位姿**做绑定验证：把 `armor_link_1..4` 和
  `base_link` 分别沿 Z 平移 5/10/15/20/25 cm，再比较每个克隆与其**自己**锚点的相对
  位移。被测对象若不跟随、或跟错了 link，相对位移就会变化而被判失败。实测结果
  `anchor_motion_cm=5/10/15/20/25`、`offset_change_cm=0.0`、`all_ok=true`。
- `--kill-stale-editor` 在启动前清掉残留的 `UnrealEditor`。残留进程不只是占显存：
  SPEAR 会连上已经监听的旧实例，导致 PID 校验失败（实测遇到过）。不加此参数时脚本
  会检测到残留进程并直接退出，提示重跑。

`LAUNCH_MODE: game` + `GAME_EXECUTABLE` 指向真正的打包可执行文件仍是完全脱离编辑器的
形态；`-game` 用的是编辑器可执行文件和项目原始内容，因此在"不需要 Cook"的同时保留了
编辑器运行库的开销。两者不冲突：`--standalone` 用于当前的日常验收，打包路径按
Cook/Stage 记录推进。

`scene_binding_plan.json` 的 `robot_visual` 项把机器人视觉 Actor 绑定到 URDF 的
`base_link` 锚点；四个 `RMSim/armor_*` 克隆和 `cam_link_8` 仍按各自 link 同步。
当前 override 里的 `actor_name` 是 `SM_MERGED_blender_for_ue5_node_22`，`actor_path`
为 `null`；`Scene/body` 只是导入场景里的**空组织 Actor**（编辑器实测：0 个子 Actor、
0 个 `UStaticMeshComponent`），不能把它当成机器人视觉。`Scene/armor` 相反是真实模板
（4 个 `前哨站_body*` 子 Actor，各 1 个网格组件）。

场景经过 UE Merge Actors 后，导入时的组织 Actor 可能只剩空壳，合并后的 StaticMesh
会成为独立 Actor。此时应在 `ue_overrides.json.scene_bindings.robot` 中把该合并 Actor
指定为 `actor_name`；绑定器仍自动遍历它当前的附着层级，计划中不再需要保存每个旧导入
子 Actor 的名字。装甲模板也按同样规则优先使用当前仍存在的子 Actor，失效时回退到实时
附着层级。
`Scene/armor` 是一个完整模板，必须克隆四份后分别挂到四个 `armor_link_*`；不能把
四个模板子 Actor 直接当成四个 link 绑定。双视角测试的 `ready` 输出还会报告
`binding_pose_coverage` 和 `robot_visual_pose_applied`，不能只看到空锚点就认为整车
已经渲染。

验收时同时查看 `main_view_000001.png`、`bound_camera_000001.png` 和对应 `.npy`，
不要只判断拼接预览。正常结果应有 `event: frame` 且两路数组都是
`[512, 512, 4] uint8`。退出阶段偶尔出现 `SharedMemory.__del__` 的
`BufferError` 属于 SPEAR 进程清理告警，不应与捕获阶段的 `EngineService` error
混淆；后者才表示事务或场景绑定失败。

`ue_overrides.json` 中需要先填入真实的场景 Actor 名称和材质资产路径。`新烧饼底盘2装电器设备` 没有 `cam*` link，生成的计划会带 warning；必须补一个相机 link 或选择另一份 URDF，脚本不会自动改挂点。

生成器只读 URDF 和旁边的 `meshes/`，不会复制或修改原始资产。

## 清单语义

### Cook/独立运行踩坑记录（2026-10）

#### 已确认的问题

- PIE 运行会同时保留编辑器视口、PIE 世界、场景纹理和 SceneCapture。可用 device-local
  预算只有约 6.36–6.74 GiB（heap Size 8188 MiB），PIE 首帧渲染时
  `VulkanMemory.cpp:3114` 的 256 MiB transient allocation 失败会使 Unreal 先崩溃，
  随后 SPEAR 才报告 `rpc::timeout` 或 `Unexpected frame state: error`。后两个是崩溃后的
  连带症状。启动前落 `--low-vram` 预设后，同一条 1440×1080 命令峰值从 7419 MiB 降到
  4271–5263 MiB、最小空闲 2518–3511 MiB；不带预设时能否通过取决于当时桌面占走多少
  显存（实测两次通过、两次崩溃）。
- `armor_link_1` 等名称来自 URDF/MuJoCo body，不要求它们预先作为 UE Actor 存在。
  真正必须存在的是机器人视觉 Actor、`Scene/armor` 整体模板和相机资源；缺少 link
  锚点时绑定器可以创建空锚点。
- UE Merge Actors 后，导入时的组织 Actor 可能只剩空壳，实际网格变成独立的
  `SM_MERGED_*` Actor。因此绑定计划必须允许显式指定合并 Actor，并在运行时扫描当前
  附着层级，不能依赖旧的硬编码子 Actor 列表。指定合并 Actor 时**必须确认它属于机器人
  而不是场景装饰**：`SM_MERGED_blender_for_ue5_node_22` 的坐标 `[0, 165, -160]` 与
  `Scene/rune1/*`、`Scene/rune1_001/*` 完全重合、资产同在 `/Game/blender_for_ue5/`
  （能量机关导入树），它是一个绑错的例子。
- `Scene/armor` 是一个整体模板，但 URDF 有四个装甲 link。正确语义是复制整体四份，
  分别挂到 `armor_link_1` 到 `armor_link_4`，不能把模板的四个子 Actor 当成四个 link。
- 直接使用 `--skipiostore` 进行传统 Pak staging 时，如果 `Saved/Cooked/Linux/.../Metadata`
  中残留 `zenfs.manifest`，UAT 会报 “A Zen file manifest must not exist when staging
  without IoStore”。这表示上一次 Zen Cook 的中间文件与本次 legacy staging 混用，
  不是 URDF 或场景绑定错误。
- 首次 IoStore Cook 已完成编译和 Cook，但在 UnrealPak 结束后 Zen 服务退出，最终
  `AutomationTool ExitCode=1`，且 archive 目录没有有效产物。因而不能把该次运行当作
  独立运行成功的证据。

#### 已选择的方案

- 场景绑定调试继续使用编辑器 PIE；默认流式纹理、单视角和顺序捕获，启动显存由
  `--low-vram`（配置加载阶段的 `-ini:Engine:[SystemSettings]:` 预设）控制。
  `--max-internal-pixels` 只是超采样上限，默认 `0`（不设上限）、且不会把捕获目标
  降到输出分辨率以下；不要用它当显存闸门。不要用帧率代替显存控制，帧率不会降低
  单次 transient allocation 峰值。
- 1440×1080 及之后的验收优先用独立运行 `--standalone`（`UnrealEditor -game`）：不启动
  编辑器视口和 PIE 世界，直接加载 `/Game/creo_stp`，机器人视觉用运行时资产加载补齐
  （`robot_visual_source=runtime_spawn`）。实测可保留 Lumen/光追/VSM（
  `--keep-expensive-rendering`），1440×1080 双视角峰值 6087 MiB、最小空闲 1694 MiB。
- `LAUNCH_MODE: game` + 打包可执行文件仍是最终形态；它需要 Cook/Stage 产出完整内容，
  当前仍受 Zen manifest/Zen 服务生命周期影响，按下面的记录单独推进。`-game` 路径不需要
  Cook，是现阶段可复现的独立运行验收方式，但不是发布形态（仍带编辑器运行库）。
- 传统 Pak 重试前必须删除 `ue/Saved/Cooked/Linux`、`ue/Saved/StagedBuilds/Linux`
  和旧 archive，确保没有遗留 `zenfs.manifest`；使用 `-skipiostore -nozenstore`。
  若要使用 IoStore，则保留 Zen 流程并把 Zen 服务错误单独排查，不能与 legacy staging
  参数混用。

#### 当前进度与剩余问题

- URDF 生成、scene binding、四份装甲克隆、相机绑定和 512×512 流式单帧/连续捕获
  已通过；1440×1080 顺序捕获在带 `--low-vram` 预设时实测通过三次（峰值
  4271–5263 MiB、最小空闲 2518–3511 MiB）。离线单元测试当前 95 项通过
  （含 26 项显存/崩溃诊断测试、10 项捕获分辨率测试）。
- 当前已加入 `/Game/creo_stp` Cook 地图配置；`binding_pose_coverage=1.0`、
  `visual_binding_render_ready=true` 是运行时验收条件。
- **已知的绑定错误（2026-10）**：`ue_overrides.json` 曾把 `robot_visual` 指向
  `SM_MERGED_blender_for_ue5_node_22`。**该条已过时**：`generated/urdf/总装/ue_overrides.json`
  现在是 `mode: link_hierarchy`，运行时按 `MuJoCoBody=<link>` tag 绑定导入的 9 个 link 视觉
  （`robot_visual_source = imported_link_hierarchy`），不再引用合并 Actor。
- **当前真正的绑定错误：四块装甲板没有导入，也没有绑上**。`tools/import_urdf_visuals.py` 的
  `DEFAULT_SKIP_LINKS` 跳过 `armor_link_1..4`（理由"装甲是关卡聚合模板的运行时克隆，导入会重复渲染"），
  而计划克隆的 `Scene/armor` → `armor_tune_node_4` 是 CAD 调参节点聚合体，克隆后落在锚点下方约 1.28 m
  （埋进地板）。同物理帧 A/B 实测：绑定装甲与 `"armor": []` 的画面差 **0 像素**。
  真正的装甲几何在 URDF 里（每个 `armor_link_*` 一个 `RoboMaster_装甲模块_AM02_2019_*.stl`，
  四块正好绕底盘四面）。修法见 [`rendering.md`](rendering.md) §8.3。
- 画面里那个"没有材质的圆球"**不是**关卡道具、也不在 `blender_for_ue5` 机关树里，而是引擎
  `ADefaultPawn::MeshComponent0`（`/Engine/EngineMeshes/Sphere`，无材质，`bOwnerNoSee=true`，
  只对玩家自己的视口隐藏、场景捕获照画）。早先"编辑器全量扫描没有任何 Actor 使用
  `/Engine/BasicShapes/Sphere`"的扫描查错了资产路径，因此没有得到结论。证据与修法见
  [`rendering.md`](rendering.md) §8.4。
- 尚未完成的是一次真正可复现的 Cook 后独立进程验收。当前主要阻塞点是 Cook 输出
  仍受 Zen manifest/Zen 服务生命周期影响；获得有效 executable、Pak 和地图后，需用
  `--standalone --frames 1 --width 512 --height 512` 验证机器人、四份装甲和
  `cam_link_8` 均能在无编辑器进程中绑定。
- 远处细线断续的根因已定位：捕获目标分辨率低于输出分辨率（实测 main `632x474 =
  0.44×`、bound `1155x866 = 0.80×`）再在 CPU 侧放大。默认已改为 1:1
  （`--supersample 1.0`、`--max-internal-pixels 0`），预算不再允许低于 `1.0×`；
  需要更强连续性时显式 `--supersample 2 --max-internal-pixels 6500000` 做真超采样
  （实测峰值 6672 MiB → 7718 MiB、最小空闲 1110 MiB → 63 MiB）。
  1:1/真超采样之后仍残留的断点才需要结合材质、曝光和相机姿态判断。

`manifest.json` 是引擎无关的结构契约：

- `link.name` 是 MuJoCo body 名、UE link 节点名和运行时映射键；
- `joint.parent/child/type/axis/origin` 驱动 MJCF 和 UE 层级；
- `visuals` 记录视觉 mesh 及其相对 link 的 origin；
- `collisions` 记录 MuJoCo 碰撞候选；
- `inertial` 保留 URDF 的 kg、kg·m² 参数；
- `validation.missing_meshes` 必须为空才能进入导入阶段。
- `validation.mujoco_incompatible_meshes` 记录 MuJoCo 解码失败的 STL；它们只从生成的 `mujoco.urdf` 中移除，原始 URDF 和 STL 不会被删除。

## UE visual override

`ue_overrides.json` 只改变 UE 的显示装配，不改变 MuJoCo 的 body、joint、质量、惯量或 collision：

```json
{
  "links": {
    "link_5": {
      "visual_source": "ue_asset",
      "ue_asset": "/Game/Robots/Gimbal/BP_GimbalYaw",
      "local_transform": {
        "xyz_m": [0, 0, 0],
        "rpy_rad": [0, 0, 0]
      },
      "runtime_bindings": ["camera_mount"]
    },
    "cam_link_8": {
      "visual_source": "placeholder",
      "ue_asset": null,
      "runtime_bindings": ["SPEAR_Camera"]
    }
  }
}
```

支持三种来源：

- `urdf`：使用 URDF visual mesh；
- `ue_asset`：使用 UE 内容库中的 StaticMesh 或 Blueprint；
- `placeholder`：只生成 SceneComponent，供相机、雷达、IMU 等运行时挂载。

UE Editor Python 导入器应为每个 link 创建稳定节点，并写入 `RobotLink=<link_name>` 和 `MuJoCoBody=<link_name>` 标签。override 由导入器解释；JSON 本身不会自动同步。

**实现状态（2026-10-06 晚）**：`pipeline/visual_override.py` + `tools/import_urdf_visuals.py --overrides`
（默认 `generated/urdf/<robot>/ue_overrides.json`，可用 `RMSIM_IMPORT_OVERRIDES` 覆盖）已经把这个契约实现了。
在此之前**只有生成端会写这些键、没有任何代码读它**，所以"URDF 视觉应被场景资产覆盖"一直没生效。

导入器行为：

- `visual_source` 缺省 `urdf` → 照旧导入 URDF mesh；`placeholder` → 只留 anchor；`ue_asset` → 按 `ue_asset` 放资产。
- **非 `urdf` 的 override 优先于 `--skip-links`**（skip 列表的作用是压掉该 link 的 URDF mesh，不是压掉 override），
  所以 `armor_link_*` 既留在默认 skip 里、又能拿到场景资产。
- `ue_asset` 可以是**一个路径**，也可以是**数组**；数组元素是路径或 `{"asset", "xyz_m", "rpy_rad", "label"}`。
  每个条目缺 transform 时依次回退：条目自带 → `local_transform` → **该 link 第一个 URDF visual 的 origin**。
  最后这条意味着"只换 mesh、不动位姿"不需要写任何数字。
- `xyz_m` / `rpy_rad` 是 **link 坐标系下的 URDF 米与 RPY**，和 URDF visual origin 同一套约定；导入器仍用
  `ue_transform()` 做 Y 镜像转换。资产必须是 StaticMesh（放不进 Blueprint，需要时把条目拆成多个 StaticMesh）。
- 每个 override 资产也会被打上 `RMSimRobot=<robot>` + `RMSimLink=<link>`，所以 `--hide-tag RMSimLink=<link>`
  的像素 A/B 依然可用；`MuJoCoBody=` 仍只在 anchor 上（运行时绑定要求唯一）。
- 注意：`ue_overrides.json` 由 `pipeline/urdf_manifest.py` **生成**，手工填写的 override 在重新生成 manifest 时
  会被覆盖回去，重生成后需要重跑或保留一份手改副本。

**本项目的实例：`armor_link_*` 用关卡自带的灯条装甲模块**

关卡里的 `Scene/armor` **是一整组**：组根 actor（label `armor`，独立运行时的 UObject 名 `armor_tune_node_4`）
+ 4 个挂在上面的子 actor（label `前哨站_body{36,37,38,398}_2`）。它没有"单个资产"形态，所以 override 用
**数组**列出这 4 块 mesh（板 `前哨站_body36` / `body398`，材质 `armor_outpost` / `armor_outpost_001`；
灯条 `body37` / `body38`，材质 `red_light`）。

**先证明 link 侧没问题（2026-10-06 晚）**：URDF 里每个 `<link>` 前面有 SolidWorks 写的 CAD frame
（`<!--swurdf-link-v1 ... frame=<3x4 行主序> -->`）。把它取逆乘到 `base_link` 的 CAD frame 上，再和
`link_9..12_joint` 的 `origin` 比：4 个装甲 link 的 **rpy 与 xyz 完全相同（差 0.00°）**。也就是说
`armor_link` 的坐标系就是 CAD 里的那个坐标系，**没有丢旋转**；要动的是装甲自己的摆放。

**装甲摆放：组根相对装配 + 手摆角度烘焙进子件（2026-10-06 晚，已定稿）**

最终值（4 个 link 相同，写入 `ue_overrides.json` 的 `local_transform`）：

```text
xyz_m   = ( -0.303615, -0.035528, -1.383381 )
rpy_rad = ( -1.596976, 0.261799, 0 )      # roll -91.5°, pitch +15°
```

它 = 组根相对装配（rel loc (6.478, 3.553, −141.482) cm、rot roll −91.5°）**再叠一个 pitch**。
这个 pitch 来自用户在编辑器里手摆的结果：他把每个 `armor_link_*` 的 pitch 从 **−75°（URDF 推出值）
改成 −90°**，也就是 delta = 纯 pitch +15°。

**父节点必须改回去**：`armor_link_*` 是 MuJoCo/URDF 真源（运行时每帧覆写），锚点上的手动旋转既不可复现
也留不住。所以把 delta 搬到子件上：

```text
R_c' = R_a^-1 @ R_a' @ R_c        t_c' = R_a^-1 @ R_a' @ t_c
Y = R_a^-1 @ R_a' = 纯 pitch +15°（父节点保持不动，子件绕 link 原点做同样的刚性变换）
```

`tmp/bake_armor_anchor_rotation.py` 就是做这件事的（用引擎自己的 `MathLibrary.get_forward/right/up_vector`
取旋转矩阵，不依赖任何 FRotator 符号约定），它把结果直接写进 `ue_overrides.json`。
跑完再导入一次，锚点就回到 URDF 值。

**回读验证**（读 `tmp/armor_anchor_readback.py`，它同时打印锚点保存值、URDF 推出值、子件相对值——
只比子件相对值是看不出父节点被动过的，这是上一轮的教训）：

| 项 | 结果 |
|---|---|
| 4 个 `armor_link_*` 锚点相对旋转 | `(0, −75, …)`，与 URDF 推出值**完全一致** |
| 4 个 link 的子件相对变换 | loc `(−30.36, 3.55, −138.34)` cm、rot `(−91.5, −15, 0)`（UE 侧 pitch 符号与 URDF 相反，见下） |
| 导入 | `placed_visuals = 967`、`placement_error_count = 0`、`saved_level = true` |

**符号约定提醒**：URDF rpy 的 pitch 与 UE FRotator 的 pitch 符号相反（URDF +15° ↔ UE −15°；URDF 75° ↔ UE −75°），
roll 相同。所以 override 里写 `+0.261799`，关卡里读回是 `−15`——两者描述的是同一个旋转。

**踩过的坑（别再犯）**：判断角度必须用**方向量**（法向/薄轴/正反面），不能只看 pivot 坐标或包围盒尺寸——
这些 STL 的 pivot 离几何 141 cm，且楔形件的包围盒轴不等于它的面朝向。

**关闭冗余的场景装甲克隆（2026-10-06 晚）**

装甲视觉既然已经由 per-link `ue_asset` 覆盖导入到 link 锚点下，`Scene/armor` 的
`clone_per_link` 就只剩副作用了：它在运行时把整组 CAD 调参节点（`armor_tune_node_4`
及其 4 个子件）复制 4 份挂到 `armor_link_*` 上，而那一组的实际位置在**地板下约 1.7 m**。
所以 `generated/urdf/总装/ue_overrides.json` 里显式关掉它：

```json
"armor": {
  "enabled": false,
  "reason": "armor_link_* 的视觉由 links 里的 ue_asset override 提供，运行时不再克隆 Scene/armor",
  ...其余键保留，作为"这条路以前怎么配"的记录...
}
```

改完必须重新展开计划（`pipeline/scene_binding_plan.py` 读的是 `ue_overrides.json`，
运行时读的是计划）：

```bash
PYTHONPATH=. python -m pipeline.scene_binding_plan \
  generated/urdf/总装/manifest.json generated/urdf/总装/ue_overrides.json \
  --output generated/urdf/总装/scene_binding_plan.json
# 总装: armor=0, camera=True   → 无 warning
```

结果与代价：

| 项 | 结果 |
|---|---|
| 计划 `armor` | `[]`，`warnings` 为空（关闭是**故意**的，不再报 `no links matched armor.link_regex`） |
| 计划 `links` | 仍是 13 条，含 4 个 `armor_link_*`——锚点照旧每帧被位姿同步器驱动，装甲视觉是锚点的子件 |
| 画面 | 与"保留克隆"相比 **0 像素差**（同物理帧 A/B，克隆那 4 份本来就埋在地板下、不在视野内） |
| `Scene/armor` 现状 | 不再被绑定器隐藏（`hide_template` 属于克隆路径），作为关卡里的调参组原样留着，和场景其他内容一样 |

`tests/test_scene_binding_plan.py` 用 4 个用例锁住这个语义：`enabled: false` 关闭、
`"armor": null` 关闭、整个键缺失时保留旧默认（4 条）、以及**出厂文件**必须展开成
`armor == []` 且 4 个装甲 link 都在 `links` 里。

## MuJoCo 与 UE 的运行时同步

```text
URDF → MJCF body/link
URDF → UE link 节点（或 override 节点）
             ↓ 相同 link name
MuJoCo mj_step()
  data.xpos/body_id + data.xquat/body_id
             ↓
SPEAR begin_frame()
  设置 UE link 世界位姿
SPEAR end_frame()
  读取图像和真值
```

本项目使用米制 MuJoCo 和厘米制 UE：

```text
p_ue_cm = 100 * diag(1, -1, 1) * p_mj_m
R_ue    = diag(1, -1, 1) * R_mj * diag(1, -1, 1)
```

MuJoCo 四元数是 `wxyz`；转换为 Unreal 四元数前先转旋转矩阵并完成 Y 轴镜像。不要直接把 URDF mesh 的 pivot 当作 link 原点；visual origin 和 override 的 `local_transform` 都必须保留。

## 运行前校验

1. 两个 manifest 的 `missing_meshes` 为空；
2. UE 中每个 `MuJoCoBody` 都能找到同名 body；
3. 静止姿态下 base/link 原点与 MuJoCo 对齐；
4. 连续轮的关节轴方向一致；
5. UE 关闭刚体物理后，MuJoCo 轨迹不改变；
6. 先验证原始 URDF visual，再逐项启用 UE override。

当前仓库已实现 URDF 解析、清单生成、MuJoCo 不兼容 mesh 自动忽略和 override 模板；UE Editor Python 的资产创建器以及最终的 SPEAR link 推送仍是下一层适配代码，必须消费这里的 manifest，而不能重新解析两个超大 URDF。

## URDF 视觉的脚本导入与运行时按 link 绑定（2026-10 实测）

上一节描述的"UE Editor Python 导入器"已经落地，机器人视觉不再依赖手工 Merge Actors：

```bash
# 1. 把每个 link 的视觉导入关卡（编辑器 commandlet，不需要 PIE）
UnrealEditor-Cmd ue/rm_sim_spear.uproject -run=pythonscript \
  -script=tools/import_urdf_visuals.py -unattended -nop4 -nosplash
# 2. 切换绑定模式并重新生成计划
#    generated/urdf/总装/ue_overrides.json:
#    scene_bindings.robot = {"link": "base_link", "actor_name": null,
#                            "actor_path": null, "mode": "link_hierarchy"}
PYTHONPATH=. python -m pipeline.scene_binding_plan \
  generated/urdf/总装/manifest.json generated/urdf/总装/ue_overrides.json \
  --output generated/urdf/总装/scene_binding_plan.json
```

导入器（`tools/import_urdf_visuals.py`，可用 `RMSIM_IMPORT_*` 环境变量配置）做的事情：

- 把 manifest 里 **每个唯一 STL 只导入一次**（`/Game/RMSim/<robot>/Meshes/rmsim_mesh_XXX`）。
  这里是共享网格而不是逐 link 合并：四个轮 link 的 128 个 visual 只来自 10 个文件，
  逐 link 合并会把 **16.8M 三角形**烘进关卡，共享唯一网格只有 **210 个网格 / 2.16M 三角形**。
- 在 `creo_stp` 里为每个 link 建一个锚点 Actor（label = link 名，`tags` 含
  `MuJoCoBody=<link>`、`RobotLink=<link>`、`RMSimRobot=<robot>`），按 URDF joint origin 挂成父子链；
  每个 visual 一个 `StaticMeshActor` 挂在锚点下，按 visual origin 定位。装甲 link **不再**
  走运行时克隆：`--skip-links`（默认四个 `armor_link_*`）压掉的是 URDF 自带的、
  没有材质的 STL，装甲改由 per-link `ue_asset` 覆盖放进关卡（见上文"关闭冗余的场景装甲克隆"）。
- **只有 link 锚点带 `MuJoCoBody=` tag**：绑定器按该 tag 查找锚点并对多匹配报错，
  visual 子 Actor 只带 `RMSimLink=`。
- 单位/坐标：网格写 OBJ 时只做米→厘米缩放，**不烘 Y 镜像**——Interchange 的 OBJ
  翻译器导入时自身会做一次 Y 镜像（实测：资产 bounds 与 `D·100·v_urdf` 完全一致，
  误差 0.0）。导入器自带这个自检并写进报告（`geometry_check.ok`），位姿换算用
  `location = D·100·t`、`rotation = D·R·D`（`D = diag(1,-1,1)`）。

运行时（`scene_bindings.robot.mode = "link_hierarchy"`）在 [spear_scene_binding.py](../rm_sim_spear/spear_scene_binding.py) 中：
从计划新增的 `links`（13 条，带 parent/joint/origin）为**每个 link** 解析锚点（先按 stable name，
再按 `MuJoCoBody=<link>` tag，最后才创建空锚点），把 13 个锚点全部交给位姿同步器，
诊断输出 `robot_visual_source: "imported_link_hierarchy"`。装甲段现在为空（视觉随锚点走），
相机和 IMU 路径不变。

实测（2026-10，`creo_stp`）：

| 运行 | 结果 |
|---|---|
| 独立 `-game`，1440×1080 顺序捕获 | `imported_link_hierarchy`、`binding_links` 13 个 link 全部被驱动、峰值 6903 MiB / 最小空闲 879 MiB、无崩溃签名 |
| 独立 `-game` 近景（`--robot-focus-position 95 -115 55`） | 车体与四个带轮辋的轮子可见（总装 URDF 的 `link_1..link_4`） |
| 编辑器 PIE，512×512 + `--low-vram` + `--verify-armor-links`（**克隆时代**的旧记录） | `imported_link_hierarchy`、`armor_clone_mesh_components=[4,4,4,4]`、`visual_binding_render_ready=true`、`all_ok=true`、`offset_change_cm=0.0`；`armor_clone_mesh_components` 现在应为空 |

**`SM_MERGED_blender_for_ue5_node_22` 在 `-game` 找不到的原因（已定案）**：SPEAR 的
`find_actors_by_name*` 匹配的是 **stable name**（`UnrealUtils.cpp:1280-1370`）：独立模式下
stable name = Actor 的 **UObject 名**，PIE/编辑器世界才是"标签 + FolderPath"。而
editor 全量 dump 显示这个 Actor 的 label 是 `SM_MERGED_blender_for_ue5_node_22`、
UObject 名却是 `StaticMeshActor_998`（它由 `Scene/body` 的 UObject 名
`blender_for_ue5_node_22` 经 Merge Actors 得名）。所以按 label 写进 override 的名字
在 `-game` 里永远查不到，绑定器才回退 `runtime_spawn`，并把机关网格渲染了第二遍。
教训：计划里的 `actor_name` 必须写 UObject 名（装甲的 `armor_tune_node_*` 正是如此），
或者改用 `actor_path`/tag/`link_hierarchy` 这类与命名体系无关的定位方式。

### `--instanced-visuals`：把逐 visual 的 Actor 换成锚点上的 ISM 组件（2026-10 实测）

默认导入给**每个 visual 建一个 `StaticMeshActor`**（总装：967 个），挂在 link 锚点下。
`--instanced-visuals` 改成：同一个 link 里**共用同一个 mesh 的那些 visual 合成一个
`HierarchicalInstancedStaticMeshComponent`**，组件加在**锚点 Actor 自己**身上
（总装：244 个组件承载 951 个实例，装甲 4×4=16 个 visual 仍走 `ue_asset` Actor）。

```bash
# 绝对路径 + 参数必须和脚本路径包在同一个引号里：-Script= 只解析一个 token
DISPLAY=:1 UnrealEditor-Cmd ue/rm_sim_spear.uproject -run=pythonscript \
  -script="$PWD/tools/import_urdf_visuals.py --instanced-visuals --report /tmp/inst_report.json" \
  -unattended -nop4 -nosplash -stdout
```

会在报告里给每个 link 加 `instanced_groups` 字段（`base_link` 341 visual → 136 组）。
**去掉该开关重跑即可回退**成逐 visual 的 Actor（导入器先按 `RMSimRobot=` tag 清掉旧对象，
`removed_previous_actors: 980`），不需要手工删关卡里的东西。

实现上有四个只有踩过才知道的绑定细节（都写在 `add_instanced_visuals` 的注释里）：

- 创建组件只有 `SubobjectDataSubsystem.AddNewSubobject` 一条路：`AddComponentByClass` 是
  `ScriptNoExport`，`RegisterComponent` 不是 `UFUNCTION`。而 `SubobjectDataSubsystem` 是
  **`UEngineSubsystem`** → 必须 `unreal.get_engine_subsystem(...)`，用
  `get_editor_subsystem` 会报 `Parameter must be a 'Class' not 'SubobjectDataSubsystem'`。
- `add_new_subobject` 的 C++ 出参 `FText& FailReason` 在 Python 侧被提升成第二个返回值 →
  返回 `(handle, fail_reason)`；`k2_find_subobject_data_from_handle` 同理返回 `(bool, data)`，
  要挑非 bool 的那个（直接取 `[-1]` 会拿到 `FText`，报 `Cannot nativize 'Text'`）。
- `unreal.Transform` 直接吃绑定原生类型（`location` 用 `unreal.Vector`、`rotation` 用
  `quat_to_rotator` 的结果），不要再解包成元组。
- `add_instances` 在这个 build 里参数名没有 `b_` 前缀 → **按位置传**
  `component.add_instances(transforms, False, False, False)`。

**性能结论（重要，别再重复试）**：这个开关**没有**改善帧时间，反而慢了一点
（512×384 驱动工况 22.00 → 23.29 ms）。它把游戏线程的变换写入从 9.46 ms 降到 0.87 ms，
但渲染线程的 `View Visibility` 反涨 2 ms——批处理只降组件数、不降**实例数**。
完整实测、归因与后续杠杆见 [render_budget.md §18](render_budget.md)。


## 骨骼驱动的资产管线（离线 glTF → SkeletalMesh，2026-10 实测）

这一节只讲**怎么把 URDF 变成一个带骨骼的 SkeletalMesh 资产并驱动它**；性能结论（以及它为什么
不划算）在 [render_budget.md §19](render_budget.md)。两者要一起读。

### 为什么不用 FBX

`URDF → FBX` 不是一条省事的路：FBX 是闭源二进制，要 Autodesk SDK 或过一遍 Blender。
glTF 是 JSON + 二进制 buffer，**纯 Python 就能生成**，而 UE 5.8 的 Interchange glTF 导入器
（`Engine/Plugins/Interchange/Runtime/Source/Import/Private/Gltf/InterchangeGltfMesh.cpp:104-218`）
会从 `skins` / `JOINTS_0` / `WEIGHTS_0` 建出真正的 `USkeletalMesh`，骨骼名直接取 glTF 节点名。

关键前提是：**URDF 本身没有蒙皮概念**，它只说"这个 visual 属于这个 link"。所以换格式本身
什么也不解决——要加的是一个 13 骨骼的骨架，加上"每个顶点 100% 绑到一根骨骼"的权重。
这件事我们在离线侧做，因为几何、link 树、visual origin 全都在 manifest 里。

### 三个被源码确认、否则一定会踩的坑

1. **Interchange 的 glTF 只做米→厘米的 ×100 平移，不做 Y-up→Z-up 旋转**
   （`InterchangeGltfTranslator.cpp:617` 调 `ScaleNodeTranslations`，`:141-148` 只乘平移）。
   所以 `.glb` 直接按 UE 轴写即可，不要按 glTF 惯例转 Y-up。
2. **bind pose 来自 `inverseBindMatrices`**（`GLTFReader.cpp:1380-1502`），而蒙皮方程
   `jointMatrix = global(joint) * IBM` 在 bind pose 下必须退化成单位阵——这意味着**顶点必须写在
   骨架根空间**，不能留在 link 局部空间。`pipeline/gltf_skinned.py` 因此把每个 link 的顶点再过一遍
   该 link 的全局 bind 变换。
3. **客户端没有 `unreal` 模块**。运行时由 `as_unreal_object=True` 拿到的对象是
   `spear.UnrealObject` 代理，它转发任意 UFUNCTION，但有两条硬性约定：
   - **参数必须全部用关键字**（代理的生成函数接受 `*args` 然后**静默丢掉**）；
   - **关键字必须和引擎头文件完全一致**。拼错不是 Python 的 `TypeError`，而是服务端失败，
     症状是"之后这个组件上的所有调用都不返回值"（我们观察到的是 `GetNumBones` 返回 `None`，
     最终报成"这个组件没有骨骼"）。

### 步骤

```bash
# 1) 离线：把一个 manifest 的 955 个 visual 合成"一个骨骼 + 一个蒙皮网格"
python3 -m pipeline.gltf_skinned generated/urdf/总装/manifest.json \
  --output /tmp/zongzhuang_skinned.glb --report /tmp/zongzhuang_skinned.json

# 2) 编辑器：导入成 /Game 下的 SkeletalMesh + Skeleton（脚本路径与参数必须整体加引号）
env DISPLAY=:1 <UE>/Engine/Binaries/Linux/UnrealEditor-Cmd <uproject> \
  -run=pythonscript \
  -script="$(pwd)/tools/import_skinned_robot.py --glb /tmp/zongzhuang_skinned.glb \
            --destination /Game/RMSim --name zongzhuang_skinned --report /tmp/import.json" \
  -unattended -nop4 -nosplash

# 3) 运行：加载资产、临时 spawn 一个 UPoseableMeshComponent，每帧按 link 写骨骼
PYTHONPATH=. python tools/run_physics_sim.py ... \
  --pose-bones /Game/RMSim/rmsim_skinned_small/SkeletalMeshes/zongzhuang_skinned
```

导入器会在 `--destination` 下再建一层以 `.glb` 文件名命名的目录，实际路径形如
`/Game/RMSim/<glb 主名>/SkeletalMeshes/<name>`；以 `--report` 里的 `skeletal_meshes` 为准。
Interchange 还会顺手建一个 `PhysicsAsset`——运行时 `spawn_poseable_robot` 会把碰撞和物理都关掉，
否则一个会模拟的蒙皮组件会引入一大块和位姿无关的每帧开销。

### 阶段化：先用一个"代理皮肤"验证机制

全量资产的三角形是 $16.8 \times 10^6$（见下），导入和测量都很贵。所以 `pipeline.gltf_skinned`
有两个只用于阶段推进的开关，让"骨骼有没有生效"可以先在几千个三角形上验证：

| 开关 | 作用 | 实测产物 |
|---|---|---|
| `--max-visuals-per-link N` | 每个 link 只留前 N 个 visual | N=2 → 1.20M 三角形 / 597k 顶点 / 43 MB |
| `--max-triangles-per-visual N` | 丢掉源网格超过 N 三角形的 visual | N=2000 → 358k / 175k / 12.7 MB；N=200 → 13k / 6.7k / 0.7 MB |

这两个开关是**丢件**（整个 visual 消失），只用来先验证"骨骼有没有生效"。要做一份能真正用的
轻量资产用 `pipeline.decimate_meshes`（见下），它是**减面**，形状还在。

### 三角形的账

按 link 合并（也就是蒙皮网格必须的形态）会把共享网格复制到每个 link 上：
全局去重后是 210 个源网格、**2,157,746** 三角形，合并输出 **16,786,420**，即 **7.78×**。
其中 **13,567,232 是四个轮子**（每个 3,391,808，来自同一批 10 个 STL，共 183,312 三角形，
即单个轮子内部就复制了 18.5×）。四个轮子的零件摆位各不相同（同一个 M3508 电机，
`link_1` yaw −0.3446、`link_2` yaw −2.7007），**不能互相复用**。非轮部分只有 3,219,188。

> 早期版本这里写的是 `2,815,252 → 5.96×`。那个数字是"每个 link 内部去重后求和"，一个出现在
> 多个 link 里的网格被重复计了。真正的全局去重量是 2,157,746。7.78× 这个倍率是
> [`render_budget.md`](render_budget.md) §20.1 那张"76% 是一个 8 mm 轴承"表的来源。

顺带一个好消息：955 个 visual 的 manifest 材质**只有 1 种**（全是默认 0.7 灰），所以合成一个网格后
是 1 个 section、1 个 draw call。

### 减面（可选，但全量资产必须做）

16.79M 三角形的 `.glb` 是 602 MB，导入时编辑器峰值常驻内存 23.3 GB，被 OOM killer 杀掉。
所以资产要落地就必须先减面。[`pipeline/decimate_meshes.py`](../pipeline/decimate_meshes.py)
在离线侧把 210 个源 STL 减到 548,395 三角形（30.6×，全体实测最大偏差 1.184 mm ≈ 1 px），
生成的是一个**指向新 STL 的 manifest**，`gltf_skinned` 不需要任何改动：

```bash
python3 -m pipeline.decimate_meshes generated/urdf/总装/manifest.json \
  --output-dir /tmp/dec_1px \
  --pixel-size-m 0.0012 --tris-per-pixel 4 --max-deviation-pixels 1
python3 -m pipeline.gltf_skinned /tmp/dec_1px/manifest.json --output /tmp/zongzhuang_lod1px.glb
```

预算按**投影像素**给（`--pixel-size-m` 是一个像素在机器人表面上的世界尺寸），误差用双向
表面采样**实测**，超差就把预算 ×4 重试。两个必须知道的点：

- **它只重写 `links[].visuals`。** `links[].collisions` 是 MuJoCo 读的，而默认的
  `simple_collision` 路径会用轮子 body 内所有 mesh geom 的联合 AABB 造碰撞圆柱——
  那些定 AABB 六个面的网格（`M3508`、滚子侧板/安装件）**不能减**。
- **减面不省帧时间。** 关掉蒙皮组件的动态阴影之后，13k 到 1.2M 三角形的代价都在噪声里；
  减面真正的收益是**导入可行性**（3.48 GB vs OOM）和**资产体积**（19 MB vs 602 MB），
  以及"必须保留阴影"时买回那 4–7 ms。完整实测见
  [`render_budget.md`](render_budget.md) §20.4。

### 顶点焊接的取舍

STL 是三角汤，一个三角形三个顶点、不共享。不焊接的话 16.8M 三角形要 50M 顶点（约 2.4 GB 的 `.glb`）；
按位置焊接后是 8.37M 顶点（约 400 MB）。`weld_triangles` 目前**只按位置焊接**，法线取面积加权的
平滑法线——对 CAD 零件并不处处正确（硬边应该保持硬），这是后续要补的按角度拆分。
按"位置 + 量化法线"焊接会退回到 33M 顶点，比不焊接还差，因为 STL 每个面片自带独立法线。

### 运行时校验（每次跑都会做）

`spawn_poseable_robot` 在组件没有骨骼时直接抛错，而不是继续跑——否则机器人会停在 bind pose，
而帧时间看起来"非常漂亮"。另外在第一次施加位姿时，`run_physics_sim.py` 会把 13 根骨骼的
世界坐标从引擎里**读回来**，和同一次 `to_unreal_pose()` 写进 link 锚点的坐标逐 link 对比：

```json
{"event": "bone_vs_actor", "read_error": null, "matched": 13, "max_delta_cm": 0.0, "worst_link": "armor_link_1"}
```

13/13 完全一致（delta 0.000 cm）。这条校验只在 `--sync-pose`（锚点真的被驱动）时执行；
在 `--no-pose-sync` 下锚点不动，比较出来的只会是那 139.8 cm 的世界偏移。

### 测量开关

| 开关 | 作用 |
|---|---|
| `--pose-bones <asset>` | 加载蒙皮资产、spawn 一个 `UPoseableMeshComponent`，按 link 写骨骼 |
| `--no-bone-sync` | spawn 蒙皮机器人但**不写骨骼**，把"有一个蒙皮网格"和"驱动一个蒙皮网格"分开 |
| `--skel-no-shadow` | 关掉蒙皮组件的动态阴影投射（它会额外拉起整套 shadow pass） |
| `--no-pose-sync` | 原有的开关：不写 link actor 变换 |

## 资产的"底"是云台，不是轮子（2026-10-09 晚实测）

修碰撞体那条线（[`rendering.md`](rendering.md) 第 10.1 节）时量到一件更靠上游的事：
**这份 URDF 的 zero pose 里，云台整组比轮子还低。**

- `link_5_joint`（yaw）与 `link_7_joint`（pitch）没有任何驱动器，重力下整组比 zero pose 又多转了
  **0.30 rad**；`link_6`/`link_7`/`cam_link_8` 相对 base_link 分别掉了 18.3 / 58.2 / 60.6 mm。
- zero pose 本身就矮：base_link 之上 `link_5` +20.0、`link_6` +16.3、`link_7` +6.25、
  `cam_link_8` +16.94 mm，而装甲顶端才 +68.5 mm——**相机在车体里面**。整机（含 6.01 cm 半径的轮子）
  在侧视投影里就是一摞 12 cm 高的板子（`tmp/sink/simple_silhouette.png`）。
- 结果：`link_6` 的 `nuc盖板` 停在 MuJoCo 地面平面以下 **36.5 mm**，`link_7` 以下 8.0 mm。
  `simple_collision` 不给云台碰撞体，它**直接穿过地面**（地面不透明，画面上只是"云台不见了"）；
  换成全 CAD 碰撞则相反——MuJoCo 的接触表是 `floor <-> link_6 dist = −0.243 mm`，
  **机器人靠 NUC 盖板站住，四个轮子悬空 4.2 mm**。

判据（都不需要 UE）：

```python
# 全 CAD 碰撞下谁在接触地面：mujoco.MjData.contact 里 geom2 的 body 名
# 云台相对 zero pose 掉了多少：mj_forward() 后再 mj_step(3000)，比 xpos 的 z
```

### "干脆让 mesh 自己做碰撞"这条路已经量过了（2026-10-10，`tmp/probe_mesh_cost.py`）

同一个 `PhysicsOptions`（`free_base=True`、4 轮驱动、floor、1 ms 步长），只切 `simple_collision`：

| | `simple_collision=True`（现状） | `simple_collision=False`（全 CAD mesh 碰撞） |
|---|---|---|
| ngeom | 1909 | 1905 |
| **参与碰撞的 geom** | **5**（floor + 4 轮圆柱） | **953** |
| 静止 base_link z | 73.413 mm | **79.343 mm** |
| 静止接触数 | 8（全在 `world`） | **114**（floor 6） |
| 静止接触的 body | 仅 `world` | **`base_link`**、`link_1`、`link_4`、`world` |
| 步进速度 | **33 846 步/s**（0.030 ms/步） | **3 467 步/s**（0.288 ms/步，**≈10×** 慢） |

**结论：换 mesh 碰撞不会停止"陷进地面"，它只是换了谁陷。** `base_link` 反而**升高** 5.93 mm 且接触表里出现
`base_link` —— 机器人是靠 NUC 盖板/云台站在地上的，四个轮子悬空。也就是说 muJoCo 的 mesh 碰撞是**能用**的
（953 个 geom、114 个接触都正常收敛），挡住这条路的不是 mesh 碰撞本身，而是**云台的 zero pose**。

所以顺序是：**先修云台 → 再谈 mesh 碰撞**。反过来做只是把"轮子陷下去"变成"轮子悬空、相机承重"。
代价另外记住：全 mesh 碰撞是 **10× 步进成本**（0.288 ms/步，1 kHz 占实时预算 29%），而且这还没算场地。
1000 步的稳定时间也从 0.030 s 涨到 0.288 s。

### ⚠️ MuJoCo 的 mesh 碰撞是**凸包**，不是 mesh 本身（2026-10-10 实测）

这条决定了"能不能把场地 mesh 直接当碰撞体"，所以单独记。判据是一个**形状无关**的实验
（L 形凹体，凹口在 `x,y ∈ [0.1,0.2]`，该区域在**所有** z 上都是空的；小球从 `(0.15, 0.15, 0.30)`
落下）：

```
mesh verts given=12  faces given=20
model.mesh_vert (as compiled) = 12 vertices
ball settled at z = 0.2296      ← 停在凸包顶面（0.2 + 球半径 0.03），没有穿过凹口
  contact: ballgeom <-> concave
```

即：**MuJoCo 用它自己算的凸包做碰撞**（MJCF 的 `<mesh>` 有 `maxhullvert` 属性就是这件事，
见 MuJoCo XML Reference 的 `asset/mesh` 一节）。

对应到本项目的两条结论：

- **合并后的场地 mesh 不能直接当 collider。** 墙、围栏、凹槽会被凸包填平，场地里到处是隐形斜面，
  机器人会被莫名弹开或卡住。**场地碰撞要用 box/plane 原语，或像上游那样做 CoACD 凸分解**
  （见 [`upstream_spear_reference.md`](upstream_spear_reference.md) 第 4 节第 2 条）。
- 这和机器人侧为什么用轮子圆柱而不是 CAD mesh 是**同一条**理由，不是两个巧合。

结论：**云台的 zero pose 是错的**（或者是"收拢态"被当成了工作态）。两条可能的修法：

1. 运行时把 `link_5_joint`/`link_7_joint` 按 CAD 姿态固定住（加 actuator 或直接 damping/equality），
   机器人就只靠四个轮子承重；
2. 回到导出端，按 CAD 的实际装配姿态重写这两个关节的 zero offset（`<joint><origin rpy=...>`）。

在决定之前不要动 `simple_collision` 的碰撞集：给云台补碰撞体会让"靠盖板站着"从隐形错误变成显性错误。
