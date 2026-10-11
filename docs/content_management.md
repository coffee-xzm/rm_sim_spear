# UE Content 管理

`ue/` 是主仓库中的完整 UE 工程。`ue/Content` 是独立的
`rm_sim_content` Git submodule，由该仓库的 Git LFS 管理 `.uasset`、`.umap`、
纹理等二进制文件；主仓库只记录 Content 的 submodule commit。

`rm_sim_rune/SimUnreal/Content` 只作为迁移和恢复来源，不是运行时依赖。

首次迁移旧资产时可以使用：

```bash
python tools/sync_rune_content.py
```

正式接入时，先备份当前本地副本，再将 Content 注册为 submodule：

```bash
mv ue/Content /tmp/rm_sim_spear-content-backup
git submodule add https://github.com/coffee-xzm/rm_sim_content.git ue/Content
git -C ue/Content lfs pull
git add .gitmodules ue/Content
```

不要删除备份，直到确认远端仓库已经包含本地的 20 处改动对应的资产版本
（`git -C ue/Content status --porcelain` 实测：10 个 `M` + 10 个 `??`）。
其中 10 个新增的 `SM_MERGED_*.uasset` 目前仍是 **untracked**，而 `creo_stp.umap`
已被修改并把它们摆进关卡：只做 `git submodule update` 会丢掉这些网格，关卡里的
`SM_MERGED_*` Actor 就会引用到不存在的资产。如果远端仍是旧版本，应先在 Content
仓库提交/推送这些修改（含 `SM_MERGED_*` 与 `creo_stp.umap`），再更新主仓库的
submodule commit。后续克隆使用：

```bash
git clone <rm_sim_spear-url>
git submodule update --init --recursive
git lfs pull
```

---

## 场地的资产构成（2026-10-10 实测）

### 场地**已经用 Merge Actors 合并过了**，源资产还在

2026-10-10 我一度写"场地没有合并、还是 2891 个 body"，**这是错的**。实情：

| | 数值 |
|---|---|
| 合并后的场地资产 | **8 个**：`SM_MERGED_Actor_422/423/424/445`、`SM_MERGED_StaticMeshActor_2183/2601/3019/3156`，共 **98.4 MB** |
| 另外 2 个 | `SM_MERGED_blender_for_ue5_node_22/231`（2.5 MB）是**机关/能量机关**，不是场地 |
| 合并前（Datasmith CAD 导入） | `RMUC2026/Geometries/` **2891 个 `.uasset`，1.597 GB —— 占 Content 的 95%** |
| `blender_for_ue5/StaticMeshes/` | 270 个，45 MB |
| 最大单个源资产 | `RMUC2026/Geometries/飞镖发射井_body1.uasset` = 94.07 MB |

判据（四条，缺一不可）：

1. `rm_sim_spear/spear_scene_binding.py:115-116` 的注释直接写着：
   *"**Merge Actors removes the source UObject but commonly preserves its name as
   `SM_MERGED_<source>`**"* —— 所以 `SM_MERGED_Actor_445` 就是关卡里那个 `Actor_445` 合并来的。
2. `git -C ue/Content status --porcelain`：10 个 `SM_MERGED_*` 是 `??` **untracked**，
   `creo_stp.umap` 是 ` M` **modified** —— 关卡被改过，用来摆这些合并资产。
3. **`creo_stp.umap` 的 sha256 与大小都变了**：`asset_manifest.json`（2026-07-18，合并前生成）
   记的是 **11,781,223 字节**，现在是 **3,490,882 字节（−8.3 MB，−70%）**。
   关卡缩小这么多，正是"几千条 actor 记录被 8 个合并 actor 取代"。
4. `asset_manifest.json` 里 **3414 条全部字节匹配、磁盘上一条不少** —— 源资产**没有被删**，
   只是很可能变成了孤儿。

源资产是 Datasmith CAD 导入的，一个 CAD body 一个 StaticMesh：命名 `<装配名>_body<N>.uasset`，
材质引用 `/Game/RMUC2026/Materials/References/M_DatasmithCAD`。

### 关键区分：**合并的是 actor，不是三角形**

**Merge Actors 把 N 个 mesh 烘成 1 个 mesh 资产，三角形总数一个不少。**
它降的是 **actor 数 / draw call 数** 和关卡体积（本例 −8.3 MB）。

所以"STP 面数太多"这个问题**合并没有解决**，只是从 2891 个资产搬进了 10 个大资产。
真正被合并解决的是 actor 数与 draw call。**三角形账要重新量，而且现在量的是这 10 个资产。**

### 由此产生的两个待定论问题

**(1) 这 10 个资产是不是 Nanite？**

`ue/Config/DefaultEngine.ini:75-79` 写着 *"this scene's meshes are not Nanite"*，
`r.Nanite=0`。但 [`render_budget.md`](render_budget.md) §4 与 §4.5 **自己标了这个结论的来源**：

> 「"本场景网格不是 Nanite"是从**画面逐像素等价**推断的，没有查过资产上的
> `nanite_settings.enabled`：网格本就不是 Nanite 时这个 +10 ms 只能是噪声；是 Nanite 资产时关掉
> 就是改走 fallback 网格（画面几乎不变、帧时间上涨）。两种情况结论相反，所以不动 `r.Nanite`。」

而且 `r.Nanite=0` 的 **+10 ms 至今是开放项**。这是一个**一次性可定论**的实验：
dump 这 10 个合并资产的 `nanite_settings.enabled` / 三角形数 / LOD 数。

- 若已经是 Nanite → 现在是被 `r.Nanite=0` 降级到 fallback 网格，应开着。
- 若不是 → **给静态场地批量开 Nanite 比手工减面省事得多**，且正好是 Nanite 的目标场景
  （代价是磁盘与导入时间，要单独评估）。

**(2) 那 2891 个源资产是不是孤儿？** 合并工具**默认不会删除源资产**，而它们**字节全都没变、
1.597 GB 还躺在仓库里**。关卡可能只合并了一部分。**"没人引用"必须证明，不能猜。**

⚠️ **量对象要选对**：`inspect_arena_assets.py` 查的是**文件夹**，
在场地已经合并之后，它量的是"源资产有多大"，**而不是"渲染要花多少"**。
这两个数现在差 1.6 GB。所以有了第二个工具。

### 两个工具，量的是两件不同的事

| 工具 | 量什么 | 什么时候用 |
|---|---|---|
| [`tools/inspect_arena_assets.py`](../tools/inspect_arena_assets.py) | **文件夹**：每个资产的 Nanite / 三角形 / LOD / 碰撞 / 估算压缩体积 | 判断"这批源资产值多少"、"该不该删"、"该不该开 Nanite" |
| [`tools/inspect_level_references.py`](../tools/inspect_level_references.py) | **关卡**：实际摆了哪些 mesh、各多少个实例、**实际放置的三角形总数**，并给出孤儿资产清单 | 判断"渲染真正花钱的是哪些"、验证"哪些 uasset 已经不再使用" |

两个都走 **AssetRegistry 的 tag 字符串**，`load_asset` 都不做：
`UStaticMesh::GetAssetRegistryTags`（`Engine/Source/Runtime/Engine/Private/StaticMesh.cpp`）
已经发布了 `Triangles`、`Vertices`、`LODs`、`Materials`、`CollisionPrims`、`CollisionComplexity`、
`NaniteEnabled`、`NaniteTriangles`、`EstTotalCompressedSize`、`EstNaniteCompressedSize`、
`DistanceFieldSize`。所以只读、不可能改坏资产。

```bash
# 场地实际用了哪些 mesh + 孤儿资产清单（会 load_map，但只读不保存）
UnrealEditor-Cmd ue/rm_sim_spear.uproject -run=pythonscript \
  -script=tools/inspect_level_references.py -unattended -nop4 -nosplash

# 文件夹普查（换目录/换输出/先小样本试都是 RMSIM_ARENA_* 环境变量）
UnrealEditor-Cmd ue/rm_sim_spear.uproject -run=pythonscript \
  -script=tools/inspect_arena_assets.py -unattended -nop4 -nosplash
```

`inspect_level_references.py` 刻意**分开报**两件事，因为它们经常被混为一谈：

- **合并后的 actor 数**（`actor_count`）—— Merge Actors 真正降的东西。
- **实际放置的三角形总数**（`placed_triangles` = mesh 三角形 × 实例数）——**Merge Actors 不动这个数**。
  如果它和"文件夹普查的合计数"对不上，说明关卡只合并/只摆了一部分，或者源资产确实成了孤儿。

### 现有资产清单类工件（三份，各有用途，不要混）

| 文件 | 是什么 | 谁生成 |
|---|---|---|
| `ue/Content/asset_manifest.json` | Content 全量清单（路径 + 字节数 + sha256），**出处证明**用 | 场地内容自带（2026-07-18，UE 5.8.0） |
| `ue/Plugins/sim/Scripts/rune_scene_manifest.json` | 机关的场景描述，用 `Scene/rune1/fan1_axis` 这类**稳定路径**定位 actor | 本项目手写 |
| `generated/urdf/总装/{manifest.json,ue_overrides.json,scene_binding_plan.json,mujoco.urdf}` | 机器人的声明式绑定 + 物理模型 | `pipeline/` 生成 |

缺的是**把场地纳进同一套**：一张 `arena_manifest.json`（UE 资产路径 ↔ 稳定名 ↔ 是否 Nanite ↔
是否参与物理），以及场地在 MuJoCo 里的静态表示。上游的做法见
[`upstream_spear_reference.md`](upstream_spear_reference.md)。

## 误删恢复（2026-10-10 实测通过）

`RMUC2026/Geometries/` 被误删过一次，**3.7 秒完整恢复，逐字节可验证**。

```bash
cd ue/Content
git status --porcelain RMUC2026/Geometries/   # 先确认删除是 unstaged (' D') 还是 staged ('D ')
git checkout -- RMUC2026/Geometries/          # unstaged → 从 index 恢复
```

两个决定成败的细节：

1. **删除必须是 unstaged（` D`）。** 那样 index 完好，`git checkout -- <path>` 直接从 index 取。
   **若删除已被 `git add`（`D `），`git checkout -- <path>` 救不回来**，要用
   `git restore --source=HEAD --worktree --staged <path>`。
2. **LFS 对象必须在本地缓存里。** submodule 的 git dir **不在** `ue/Content/.git`，
   要用 `git rev-parse --git-path lfs` 找（本例是
   `/home/coffee/rm_sim_spear/.git/modules/ue/Content/lfs`，实测 **1.8 GB / 3447 个对象**，
   够还原整个 1.597 GiB 的场地）。缓存被 `git lfs prune` 清过就只能联网
   `git lfs pull` 从 `rm_sim_content.git` 取。

**验证恢复是否完整（别跳过）**：

```bash
# 1) git 侧必须干净
git status --porcelain RMUC2026/          # 应为空
# 2) 逐文件对 git HEAD 里的 LFS pointer —— 这才是硬标准
git show HEAD:RMUC2026/Geometries/<file>.uasset   # oid sha256:... / size N
sha256sum RMUC2026/Geometries/<file>.uasset       # 必须与 oid 一致
# 3) 收尾
git lfs fsck                               # 应输出 Git LFS fsck OK
```

⚠️ **不要拿 [`asset_manifest.json`](../ue/Content/asset_manifest.json) 当恢复的判据。**
它是 2026-07-18 的快照，已落后于 submodule 的后续提交。实测 2891 条里 2890 条 sha256 匹配，
唯一不匹配的 `前哨站_body257.uasset` 是**清单陈旧而非恢复失败**：

| | size | sha256 |
|---|---|---|
| git HEAD 的 LFS pointer | 19,358 | `bdbd4384…c2e4` |
| 恢复后的文件 | 19,358 | `bdbd4384…c2e4` ✓ |
| `asset_manifest.json` | 75,368 | `d0e40b58…` ✗ |

该文件在清单生成之后被 commit `3a2cec3 添加装甲板等内容` 改过。清单适合查"少了什么"，
判"对不对"要用 git。

---

## 外部依据（2026-10-10 调研，供上面几条决策引用）

### CAD → UE：Datasmith 是唯一受支持的路，Linux 可用

- STEP(AP203/214/242) 直接导入，**Linux 官方支持**（macOS 不支持）：
  <https://dev.epicgames.com/documentation/en-us/unreal-engine/datasmith-supported-platforms>。
  **本机已核对**：`DatasmithCADImporter.uplugin` 的 `SupportedTargetPlatforms: [Win64, Linux]`；
  `CADKernelSurface`/`CADInterfaces`/`CADTools`/`ParametricSurface` 的 `PlatformAllowList` 都含 Linux；
  `Engine/Binaries/ThirdParty/TechSoft` 在。只有 Alias `.wire`/OpenNurbs 是 Win64-only。
  注意 `EnabledByDefault: false`，且**自建引擎会失去 CAD**（不可再分发）——我们用的预编译 Linux 版没问题。
- **Interchange 还没接管 CAD**（官方说是 "future releases"）。
- **熔接参数可调**：Chord Tolerance、Max Edge Length（仅 CADKernel）、Normal Tolerance、
  Stitching（none/heal/sew）、Geometric/Stitching Tolerance。
- **可以在不重开 CAD 文件的前提下对单个资产重新熔接（retessellate）**：
  <https://dev.epicgames.com/documentation/en-us/unreal-engine/retessellating-cad-geometry-in-unreal-engine>。
  **这是比"重导 STP"便宜得多的一根杠杆**：面数只需在导入后调，不必回到 CAD。
- **没有"导入时合并所有 body"这个选项** —— 一个 CAD body 一个 StaticMesh 是 Datasmith 的固有行为，
  与本文实测一致。

### Nanite：Linux 可用，但**约束是池不是三角形**

- **Linux/Vulkan 可用**：本机 `Config/VulkanPC/DataDrivenPlatformInfo.ini` 的
  `[ShaderPlatform VULKAN_SM6] bSupportsNanite = true`（`VULKAN_SM5` 为 false）。
  官方 Nanite 文档的"仅 DX12/SM6"**已过时**。Deferred + Lumen + VSM 都支持；
  Forward、MSAA、lighting channels、VR stereo 不支持；只支持 Opaque/Masked 材质。
- **预分配池是真问题**：`r.Nanite.Streaming.StreamingPoolSize` **故意不是 `ECVF_Scalability`**，
  只在引擎 init 时重分配，约 1 GB 的 `Nanite.StreamingManager.ClusterPageData`；
  Epic 员工（2026-03）建议**降网格密度**（fallback percent triangles、position precision）
  或用 **HLOD + Nanite off**，而不是缩池：
  <https://forums.unrealengine.com/t/scaling-nanite-streamingpoolsize-vram-usage-via-scalability-settings-or-at-runtime/2712963>。
  **这与本项目实测的 `r.Nanite=0` → −819 MiB 且逐像素等价完全吻合。**
- **fallback mesh 在 Nanite 开启时也常驻 VRAM**，所以开 Nanite 不会省掉 fallback 那份：
  <https://forums.unrealengine.com/t/nanite-fallback-mesh-buffers-vram-residence/2563974>。
- **Nanite 不自动更快**（与 overdraw 导向的 LOD 有反例）：
  <https://forums.unrealengine.com/t/nanite-performance-is-not-better-than-overdraw-focused-lods-test-results-epics-documentation-is-dangering-optimization/1263218>。
- ⚠️ **蒙皮网格 + Nanite 在 5.6 有 bug：经 `SceneCapture2D` 渲染时动画静默冻结**
  （<https://forums.unrealengine.com/t/eu5-6-bug-scene-capture-2d-and-skeletal-mesh-with-nanite-not-get-animate/2666843>），
  5.5+ 还有特定角度低模问题。**我们的采集链路正是 SceneCapture2D ⇒ 机器人蒙皮网格必须保持 Nanite 关闭。**
  已记入 [`risks.md`](risks.md) R14。

### URDF → UE：没有官方导入器，我们这条路有先例

- Epic 在 5.8 **没有官方 URDF importer**。
- 社区主要路径是 RapyutaSimulationPlugins + rclUE（解析 URDF，自带"physics-enabled skeletal
  mesh component-based" Turtlebot3）：<https://github.com/rapyuta-robotics/RapyutaSimulationPlugins>。
  UE4/早期 UE5 年代，**5.8 + Linux 兼容性未验证**，预期要移植。
- **URDF → USD 是维护得最好的路**（Omniverse URDF Importer，一 link 一刚体，可实例化；
  限制：只支持运动学树、无 `planar`、无 `<mimic>`）：
  <https://docs.omniverse.nvidia.com/kit/docs/omniverse-urdf-importer/latest/source/extensions/omni.importer.urdf/docs/Overview.html>。
  UE 侧有 `USDImporter`；glTF 走 Interchange 的 `UInterchangeGLTFTranslator`。
- URDF → FBX（经 Blender）是最常见的实用路径；有专门为此存在的 CLI（`URDF2FBX`，
  明说"because UE cannot load URDF directly"，支持 STL/DAE，`--scale_unit 0.01`，材质去重/改名）。
- **"一个蒙皮网格 + 一 link 一骨骼"是有先例的已知技术**（Rapyuta 的 Skeletal Turtlebot3；
  Blender `urdf_importer`）。**材质会保留**（UE 的 SkeletalMesh 保留多个材质槽/段），
  真正要主动管的是跨 link 的材质去重/合并。限制：没有逐 link 的剔除/LOD/碰撞；
  必须刚性绑定（每个顶点权重 1.0 到唯一一根骨骼），否则会出现剪切。

### 跨端主键：业界已经收敛到我们在做的事

- NVIDIA SimReady 的 Robot Naming 要求小写下划线命名 + **稳定 prim path**
  （`/Carter/base_link`、`/Carter/Joints/left_wheel_joint`）：
  <https://nvidia.github.io/simready-foundation/aif-0.1.0/capabilities/isaac_sim/robot_core/requirements/robot-naming.html>。
- MuJoCo Menagerie 的约定：一个机器人一个目录，**MJCF 的 body/joint 名镜像 URDF 的 link/joint 名**。
- 在 UE 里：Datasmith **保留 CAD 零件名与层级**（外加 metadata）⇒ 场地的天然主键；
  机器人主键 = URDF 的 link/joint 名。本项目已经在这条路上
  （`spear_metadata.md` 把**帧来源**（`run_id`/`commit_id`/`sensor_id`）与**资产身份**刻意分开，
  这是对的）。
