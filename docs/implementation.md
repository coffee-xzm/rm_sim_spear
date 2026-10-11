# 当前实现

第一批实现先固定核心契约,不依赖 UE 或 ROS2：

```text
MujocoDriver.step()
  -> FrameCommit + Snapshot
  -> RenderPort.submit(snapshot)
  -> Capture(commit_id)
  -> Observation(snapshot, capture)
```

## 运行

需要 Python 3.11 和 MuJoCo：

```bash
PYTHONPATH=. python -m rm_sim_spear.cli examples/free_fall.xml \
  --frames 10 --steps-per-frame 10 --readback-delay 2
```

每行输出一个提交点。`ready_captures` 里的图像可以晚于提交点返回，但始终通过
`commit_id` 配回原始快照。

## 测试

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

`tools/test_urdf_views.py` 的显存闸门与崩溃诊断集中在
[`rm_sim_spear/vram.py`](../rm_sim_spear/vram.py)：启动前空闲显存 preflight、
`nvidia-smi` 采样（`peak_used_mb` / `min_free_mb`，逐行 flush 以便崩溃后仍有记录）、
UE 日志崩溃签名解析（`Out Of Memory`、`Failed to allocate Device Memory`、
`Fatal error`、`Fatal X11 IO Error`）、以及 UE 进程存活探测（命中即退出，不等 RPC
超时）。这些函数的离线测试在 [`tests/test_vram.py`](../tests/test_vram.py)。
`--verify-armor-links` 的绑定判定在 [`rm_sim_spear/pose_sync.py`](../rm_sim_spear/pose_sync.py) 的
`check_attachment()`：用扰动位姿前后「锚点位移」与「克隆相对锚点位移」两个量判断装甲是否
真的挂在自己的 link 上（离线测试在 [`tests/test_pose_sync.py`](../tests/test_pose_sync.py)）。
独立运行（`UnrealEditor -game`）下机器人视觉由
[`rm_sim_spear/spear_scene_binding.py`](../rm_sim_spear/spear_scene_binding.py) 的
`_spawn_runtime_static_mesh_actor()` 在运行时加载 `/Game/<actor_name>` 资产并 spawn。

UE companion plugin 已用 UE 5.8 Linux `SpearSimEditor` 目标编译通过；
`bridge/idl/sim_msgs` 已在 ROS2 Humble 下用 `colcon` 生成并安装，适配器
用系统 Python 3.10 加载生成消息完成 smoke test。

## 当前边界

- `MemoryRenderer` 是延迟回读测试替身；`SpearRenderPort` 已封装 SPEAR 的暂停、事务和单/双/三缓冲顺序，默认使用 `SingleBuffered` 作为正确性基线。双/三缓冲由 `Coordinator.reset()` 自动预热（直接使用 `SpearRenderPort` 时需显式调用 `prime()`）。单缓冲业务提交必须注入 `bind_frame_metadata(snapshot)`；双/三缓冲业务提交必须注入 `metadata_enqueue_copy(snapshot)`，在 `read_pixels`/`enqueue_copy` 链路关联 `run_id`、`commit_id` 和真实 `ue_frame_id`；`readback_metadata` 返回这三个字段。适配器按 `run_id + commit_id` 配对，不按回读顺序猜测来源。旧的无参数 `enqueue_copy()` 只允许用于预热帧。
- `ProjectileSubsystem` 只处理 UE 或其他引擎上报的相对命中位置，不推进 MuJoCo，也不直接修改规则状态。
- `bridge/codec.py` 是独立 ROS2 adapter 的本地协议；`bridge/idl/sim_msgs` 已提供可生成的 `FrameCommit` 消息包。`bridge.ros2_adapter` 在系统 ROS2 Humble 进程中消费 Unix socket，并发布 `/clock`、`/sim/commit`、`/tf`，IMU/关节流按配置发布；核心侧用 `UnixEnvelopeServer` 和 `UnixBridgePublisher` 接入，不把 ROS2 导入核心。
- `bridge.standard_messages` 已提供 `Clock`、`TFMessage`、`Imu`、`JointState` 的无 ROS 依赖字段映射，adapter 只需将字典填入生成的 ROS 消息对象。
- `bridge.messages.encode_snapshot()` 可把提交元数据、位姿和传感器数组发送到 adapter；图像不进入 JSON envelope，继续使用 SPEAR 共享内存。
- `bridge.transport.UnixEnvelopeChannel` 封装 Unix socket 的分片读写，避免把一次 `send` 当成一次完整消息。
- `pipeline.AssetCatalog` 优先读取本地 `ue/Content/`；只有迁移尚未完成的旧环境才回退到 `~/rm_sim_rune/SimUnreal/Content/`。可用 `python tools/sync_rune_content.py` 导入资产，再通过它校验顶层包名。
- 真实 SPEAR 元数据扩展的调用顺序和 bundle 契约见 [`spear_metadata.md`](spear_metadata.md)。
- `SpearMetadataCallbacks.reset()` 会清空 UE companion component 的旧 run 队列，需传给 `SpearRenderPort(metadata_reset=...)`。
- `SpearRenderPort(capture_reset=...)` 注入真实 SceneCapture 的 GPU 队列清理或 Terminate/Initialize；`reset()` 会在下一轮预热前调用它，避免旧图像跨 run 回读。双/三缓冲实例完成首次预热后再次 reset 若未提供该回调会直接失败。SPEAR 没有统一的 capture reset SpFunc 时必须由 UE 集成提供该回调。
- `Capture` 还携带 `sensor_id` 和 `modality`，多相机/多模态回读不会仅按 `commit_id` 混流。
- `Coordinator` 按 `(sensor_id, modality)` 分别消费同一提交的回读；只有声明的所有流到齐，才释放该提交快照。
- `CompositeRenderer` 将多个独立 SPEAR 相机端口 fan-out 到同一提交，并保留各自缓冲与来源标识。
