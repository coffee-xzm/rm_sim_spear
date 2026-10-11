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

每个模型生成：

```text
generated/urdf/<robot>/manifest.json       # link/joint/mesh/origin/inertial
generated/urdf/<robot>/ue_overrides.json   # UE 视觉替换模板
generated/urdf/<robot>/mujoco.urdf         # 自动移除 MuJoCo 无法解码 mesh 的副本
generated/urdf/<robot>/scene_binding_plan.json # armor/camera/IMU 具体挂点
```

当前 `总装.urdf` 的净化结果会忽略两个 MuJoCo 无法解码的导出 STL：
`新电池_默认_ad15cf6fb7fa599c.stl` 和 `dm_j4310_2ec_默认_5f36196a63bcc568.stl`。
这两个文件仍保留在原始资产目录中；UE 视觉导入仍可按 override 需要使用它们。

## 场景绑定

`ue_overrides.json` 的 `scene_bindings` 是 UE 场景处理入口：

- `armor.link_regex` 默认匹配 `^armor_link.*$`；每个匹配到的 link 对应一个场景装甲 Actor；
- `armor.actor_name_template` 默认生成 `armor_1`、`armor_2` 等场景 Actor 名；
- `armor.material_slot` 指定装甲 StaticMeshComponent 的材质槽；`material_asset` 填 UE 材质资产路径；
- `camera.link_regex` 默认匹配 `^cam.*$`，只选择第一个匹配 link；
- `imu.relative_position_m` 和 `imu.relative_rpy_rad` 指定 IMU 相对相机 link 的安装位姿。

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

场景已经加载、link Actor 和装甲 Actor 已存在时，在 SPEAR 脚本中执行：

```python
from rm_sim_spear.spear_scene_binding import apply_scene_binding_plan, load_plan

plan = load_plan("generated/urdf/总装/scene_binding_plan.json")
link_actors = apply_scene_binding_plan(sp_instance.game, plan)
```

脚本会：

- 如果 link 锚点不存在，自动创建带稳定名称的空 `AActor`；已有同名 Actor 时直接复用；
- 将 `armor_1` ... `armor_4` attach 到四个 `armor_link_*`；
- 如果配置了 `material_asset`，把它设置到指定材质槽；
- 将 `camera_sensor` attach 到 `cam_link_8`；
- 创建 `imu_marker` 并按米制相对位置、弧度相对姿态挂到相机；
- 返回 `{link_name: ue_link_actor}`，供 MuJoCo `xpos/xquat` 推送循环使用。

`ue_overrides.json` 中需要先填入真实的场景 Actor 名称和材质资产路径。`新烧饼底盘2装电器设备` 没有 `cam*` link，生成的计划会带 warning；必须补一个相机 link 或选择另一份 URDF，脚本不会自动改挂点。

生成器只读 URDF 和旁边的 `meshes/`，不会复制或修改原始资产。

## 清单语义

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
