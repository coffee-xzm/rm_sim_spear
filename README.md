# rm_sim_spear

RM(RoboMaster)场景下的**感知与控制仿真**。定位是"**带时间轴的传感器流 + 可闭环的控制仿真**",不是"一堆图片",也不是又一个 UE 物理仿真器。

本仓库是**方案与计划文档**,代码骨架随后按 `docs/plan.md` 的阶段落地。

---

## 一句话架构

```
UE5 (SPEAR 插件)          ← 场景编辑 + 渲染 + 多模态真值
      ↑ msgpack-RPC / 共享内存
编排进程 (Python 3.11)     ← MuJoCo 物理 + 机器人 + 传感器 + 唯一时间真源
      ↑ ROS2 / DDS (独立适配层,只做通信)
外部算法栈                 ← 自瞄 / 打符 / 规划
```

- **SPEAR 为基座**:它提供帧内事务编程模型、异步相机回读、共享内存零拷贝、多模态 GT。它是**渲染端 + 场景操纵层**,不是物理引擎。
- **UE5 是场景编辑器 + 渲染端**:灯效/材质/后处理只有 UE 能做。
- **MuJoCo 是物理真源 + 机器人侧**:URDF、接触、关节、IMU 等传感器。
- **ROS2 只做通信**:使用独立标准 adapter,不引入 `ros2_control`,不让任何 ROS2 组件拥有物理时钟。

---

## 已定的四件事

| # | 决策 | 文档 |
|---|---|---|
| 1 | **单位制用米**。UE 原生 cm,只在边界上 ×0.01 / ×100 | [`docs/decisions.md`](docs/decisions.md) D1 |
| 2 | **单一时间真源**(MuJoCo sim time),所有时间戳由它派生 | [`docs/decisions.md`](docs/decisions.md) D2 |
| 3 | **ROS2 只用于通信**,图像默认走共享内存并提供标准兼容模式 | [`docs/decisions.md`](docs/decisions.md) D3 |
| 4 | **弹丸沿用 `rm_sim_rune` 的做法**(UE `QueryOnly` 扫掠球 + 盒状靶代理 + 纯函数环数) | [`docs/projectile.md`](docs/projectile.md) |

---

## 四条设计红线

1. **MuJoCo 是物理真源,UE 是可渲染镜像。**
   判定:把 UE 侧的刚体物理全部关掉,仿真行为不变。
2. **规则是纯逻辑第三层**,不依赖 UE、不依赖 MuJoCo。
   判定:能脱离两者单测。
3. **所有仿真数据都能追溯到同一个 sim clock。**
   判定:每个包或其关联的 `FrameCommit` 带 `run_id + sim_tick + commit_id`,禁止跨提交点配对。
4. **ROS2 不进入核心和 UE 进程。**
   判定:删掉 ROS2 adapter 后,仿真与 SPEAR 取图照常运行。

`ue/` 是独立的 UE 5.8 工程，入口为 `ue/rm_sim_spear.uproject`。视觉资产按 UE
约定放在独立的 `ue/Content` Git submodule 中，由 `rm_sim_content` 的 Git LFS
管理。首次接入可从本机 rune 原型工作树同步约 1.8 GB 资产（不会复制源目录
的嵌套 Git 元数据）：

```bash
python tools/sync_rune_content.py
```

同步前可用 `--dry-run` 查看文件数量和总大小。`pipeline.AssetCatalog` 优先读取
本地 `ue/Content/`，旧环境未同步时才回退到 `~/rm_sim_rune/SimUnreal/Content/`。

---

## 文档

| 文件 | 内容 |
|---|---|
| [`docs/plan.md`](docs/plan.md) | 目标、边界、红线、路线图、决策表 |
| [`docs/architecture.md`](docs/architecture.md) | 分层、职责、数据流、同步模型、单位与坐标、URDF 流水线 |
| [`docs/decisions.md`](docs/decisions.md) | 决策记录(含**回退方式**)与待定项 |
| [`docs/projectile.md`](docs/projectile.md) | 弹丸设计(基于对 `rm_sim_rune` 实现的拆解) |
| [`docs/reuse.md`](docs/reuse.md) | 从 `rm_sim` / `rm_sim_rune` **取什么、不取什么** |
| [`docs/risks.md`](docs/risks.md) | 风险登记 + 本机环境实测约束 |
| [`docs/implementation.md`](docs/implementation.md) | 当前可运行核心、运行方式与测试 |
| [`docs/spear_metadata.md`](docs/spear_metadata.md) | SPEAR 图像回读来源绑定契约 |
| [`docs/urdf_workflow.md`](docs/urdf_workflow.md) | `assets/` 中 URDF 到 MuJoCo/UE override 的清单与流程 |
| [`docs/content_management.md`](docs/content_management.md) | UE 工程与 Content 的 Git LFS 管理方式 |
| [`docs/ue_reproducibility.md`](docs/ue_reproducibility.md) | UE 5.8、SPEAR 固定版本、插件安装与渲染基线 |
| [`rm_sim_spear/spear_scene_binding.py`](rm_sim_spear/spear_scene_binding.py) | SPEAR 运行时装甲、相机和 IMU 场景绑定 |
| [`ue/README.md`](ue/README.md) | 可复制到 UE 工程的元数据 companion component |

---

## 与两个弃案的关系

- [`~/rm_sim`](../rm_sim) —— **引用它的"内容"(需求、教训、验收标准),不继承它的实现选型**(不自研 transport、不链 AirSim、不在 UE 内直发 DDS、不让 UE Chaos 当机器人物理真源)。
- [`~/rm_sim_rune`](../rm_sim_rune) —— **纯逻辑 + 6 个单测 + `SimUnreal/Content` UE 视觉/材质资产**是原型里质量最高的部分,整块复用;**手工 actor 绑定、相机绑 Pawn、视点差分 IMU 等结构债不继承**。

详见 [`docs/reuse.md`](docs/reuse.md)。
