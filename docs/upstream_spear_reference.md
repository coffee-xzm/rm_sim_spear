# 上游 SPEAR 怎么做场景与资产（参考，2026-10-10）

上游仓库在本机 `~/spear`。本文记录**它的**做法，用来判断我们该抄什么、不该抄什么。
所有行号都是 `~/spear` 内的相对路径。

## 1. 上游**不**做运行时全场景同步

这件事很容易误解，所以要写清楚：上游把"场景进 MuJoCo"和"运行时同步"拆成两件独立的事。

| | 做什么 | 在哪 |
|---|---|---|
| **离线导出流水线** | 把整个 UE 关卡烘成**生成出来的** MJCF，含静态几何 | `editor/` + `pipeline/` |
| **运行时示例** | 步进 MuJoCo，只把**被过滤出来的一小撮**动态 Actor 位姿镜像回 UE，**单向** | `examples/mujoco_interop/run.py` |

- 运行时只同步**名字匹配的**对象：`examples/mujoco_interop/run.py:29` `name_prefix = "Meshes/05_chair"`，
  `:94` 过滤 UE Actor，`:141` 过滤 MJCF body。方向是 **MuJoCo → UE**：`:196-197` 读
  `body.xpos/xquat`，`:234-239` 调 `K2_SetActorLocationAndRotation`。**没有任何地方从 UE 写 `mj_data.qpos`**，
  所以它不是双向的。
- MJCF 是**独立生成的文件**，不是关卡本身：`run.py:136` `mujoco.MjModel.from_xml_path(args.mjcf_file)`；
  `examples/mujoco_interop/README.md:5` 要求先跑 "our MuJoCo export pipeline for the `apartment_0000` scene"。
  仓库里**一个 `.mjcf` 都没有签入**（`find . -name "*.mjcf"` 为空）。
- 全关卡静态导出确实存在，但在离线流水线里（`docs/importing_and_exporting_assets.md:32-61`）：
  UE 元数据 + 几何 → 运动学树 → 碰撞几何 → `generate_mujoco_scene.py`。
  `pipeline/generate_mujoco_scene.py:60-67` 读 `collision_geometry/scene.json`；`:34-55` 发一个固定模板
  （一块无限 `<geom type="plane">` + light + `<include file="bodies.mjcf"/>`）；动态/静态是**硬编码 hack**：
  `:220-225` `if actor_name.startswith("Meshes/05_chair"): body_type = "dynamic" else "static"`，
  `:252-253` 只有动态根节点才挂 `<freejoint>`。
- **碰撞几何不是 UE 自己的碰撞体**：`editor/export_unreal_geometry.py:29` 按 `relevant_for_level_bounds`
  过滤，`:98-102` 导出数值一致的 OBJ；`pipeline/generate_collision_geometry.py:100-117` 用 `trimesh` 合并后跑
  **COACD 凸分解**；`generate_mujoco_scene.py:294-302` 把那些 OBJ 部件写成 MuJoCo `mesh`/`geom`。
  即：**MuJoCo 的碰撞 = 对 UE 视觉 mesh 做 CoACD 分解**。
- 排除某些 Actor 靠命令行开关：`generate_mujoco_scene.py:22` `--ignore-actors`，
  文档例子用它忽略天花板（`docs/importing_and_exporting_assets.md:57`）。
- 论文也只讲协同仿真，没有"全关卡同步"的承诺：`/tmp/spear_paper.txt:385-389`
  "we query the state of the MuJoCo scene … and we use SPEAR to update the state of a corresponding UE scene."

**对本项目的意义**：上游解决"穿墙"的办法是**离线把场地 StaticMesh 导成静态 MJCF body**，
不是运行时把 UE 关卡读进 MuJoCo。

## 2. 上游**没有**资产管理系统

上游是 **reflection-first，不是 asset-first**：Python 里直接写死 UE 对象路径。

- 写死的对象路径：`examples/getting_started/run.py:29`
  `load_class(uclass="AActor", name="/SpContent/Blueprints/BP_Axes.BP_Axes_C")`；
  `examples/mujoco_interop/run.py:106` `/SpContent/Blueprints/BP_CameraSensor.BP_CameraSensor_C`；
  换关卡同理：`examples/open_level/run.py:55` `OpenLevel(LevelName="/Game/ThirdPerson/Lvl_ThirdPerson", ...)`。
- 叫"registry"的跟资产无关：`python/spear/services/world_registry_service.py:14` `get_world_descs()` 管的是
  UE **world**；`python/spear/services/async_loading_service.py:50-53` 只是包了 UE 自己的 `IAssetRegistry`
  用来等异步加载完成（`is_asset_registry_idle`，`:116-121`）。**没有 SPEAR 自己的资产 key/ID。**
- 没有 scene config / catalog / manifest。唯一的 YAML 是
  `python/spear/config/default_config.{spear,sp_core,sp_services}.yaml`（运行时配置，不是场景）。
- **场景描述 = 流水线吐出来的一串 `scene.json`**，逐级累积 `pipeline_info`：
  `unreal_metadata/scene.json`（`editor/export_unreal_metadata/export_scene.py:31-39`）
  → `kinematic_trees/scene.json`（`generate_kinematic_trees.py:47-52`）
  → `collision_geometry/scene.json`（`generate_collision_geometry.py:37-42`）
  → `mujoco_scene/{meshes,bodies,main}.mjcf`（`generate_mujoco_scene.py:75-92`）。
  这是最接近"清单"的东西。
- **关键约定：stable name。** `python/spear/utils/editor_utils.py:233-245` 定义为
  `actor.get_folder_path()` + `actor.get_actor_label()`（需要时再加 `:unreal_name`）；
  `editor/modify_scene_add_stable_name_components.py:42-48` 能把 `sp_stable_name_component`
  盖进关卡，让名字在打包/换关卡后仍然稳定。
  **stable name 就是 UE ↔ MuJoCo 的连接键**：`generate_mujoco_scene.py:246`
  `body_name = f"{actor_name}:{kinematic_tree_node_name}"`，
  运行时按 `f"{ue_actor_name}:StaticMeshComponent0"` 反查（`mujoco_interop/run.py:236`）。
- 内容布局约定：工程内容 `cpp/unreal_projects/SpearSim/Content/SPEAR/Scenes/apartment_0000/...`；
  插件内容 `cpp/unreal_plugins/SpContent/Content/{Blueprints,Meshes,Materials,…}`；
  逻辑路径见 `docs/getting_started.md:202-209`；关卡表在 `maps/spear.csv`；
  内容路径 ↔ 文件系统路径的换算在 `python/spear/utils/pipeline_utils.py:203`。
  导入外部内容是**批量改名工具**，明确限制 "/Game only"（`pipeline/import_external_content.py:27-43`）。

## 3. 我们已经比上游多什么

| | 上游 | 我们 |
|---|---|---|
| 机器人的声明式绑定 | 无 | `manifest.json`、`ue_overrides.json`、`scene_binding_plan.json`、`mujoco.urdf`（见 [`urdf_workflow.md`](urdf_workflow.md)） |
| 读回身份契约 | 无 | [`spear_metadata.md`](spear_metadata.md) |
| 内容版本管理 | 全在仓库里（MIT/CC0） | `ue/Content` 独立 Git-LFS submodule（见 [`content_management.md`](content_management.md)） |
| 场地 → MuJoCo | **有**离线流水线（CoACD） | **无** |

## 4. 值得抄的四条

1. **stable name 当跨端主键。** 我们的 `mujoco.urdf` body 名、绑定计划 link 名、UE Actor label
   必须能一一对上，并且**用同一套规则生成**，而不是各自拼字符串。
2. **给场地加一个导出阶段**，对齐 `pipeline/generate_collision_geometry.py` 的位置：
   导出场地 StaticMesh → 简单体（墙/围栏用 box，别动 CoACD）→ 作为**静态** body/geom 追加进 MJCF，
   再照 `generate_mujoco_scene.py` 的样子组装 `main.mjcf`。
3. **生成物即构建产物，永不手改**，并像上游 `pipeline_info` 那样把
   "UE Actor → body 名"的映射记进 `scene_binding_plan.json`/`manifest.json`。
4. **按场景分目录、逐级可重跑**（`arena/unreal_metadata`、`arena/collision_geometry`、`arena/mujoco_scene`），
   而不是一个脚本一把梭。

**同时记住这个不对称**：上游只有 MuJoCo → UE 单向。只要 MuJoCo 是唯一真源，
场地就保持静态、只有机器人 body 镜像回 UE —— 这正是上游的模型，也是我们该保持的。
