# UE 元数据组件

`ue/` 是本仓库的完整 UE 5.8 工程，入口为 `ue/rm_sim_spear.uproject`。
视觉资产位于独立的 `ue/Content` Git submodule，由 `rm_sim_content` 的 Git LFS
管理；`rm_sim_rune` 只提供迁移源，不是运行时依赖。

SPEAR/Python 运行时绑定器位于
[`rm_sim_spear/spear_scene_binding.py`](../rm_sim_spear/spear_scene_binding.py)，
UE 插件位于 `ue/Plugins/RmSimSpearMetadata/`。

`RmSimSpearMetadata` 是一个独立的 SPEAR companion component，不修改
`USpSceneCaptureComponent2D`。它通过同一相机 Actor 上的 SpFunc 队列保存：

- `run_id`
- `commit_id`
- `sensor_id`
- `modality`
- UE 的 `GFrameCounter`

插件依赖 SPEAR 的 `SpCore`，`.uplugin` 已声明该依赖。当前源码已在
UE 5.8 Linux 的 `SpearSimEditor` 目标中编译通过；构建副本使用：

```bash
<UE5.8>/Engine/Build/BatchFiles/Linux/Build.sh \
  SpearSimEditor Linux Development \
  -Project=<SpearSim>/SpearSim.uproject -NoHotReloadFromIDE
```

把插件放入目标工程的 `Plugins/` 后，还要确保该工程通过
`AdditionalPluginDirectories` 找到 SPEAR 的 `unreal_plugins/`，否则
`SpCore` 无法解析。

目标工程至少需要在 `.uproject` 中启用：

```json
{
  "Name": "SpCore",
  "Enabled": true
},
{
  "Name": "RmSimSpearMetadata",
  "Enabled": true
}
```

并添加类似下面的搜索目录（路径按机器调整）：

```json
"AdditionalPluginDirectories": [
  "/home/coffee/spear/cpp/unreal_plugins"
]
```

在本工程的相机 Blueprint 上添加 `RmSimSpearReadbackMetadataComponent`。Python 侧创建：

```python
from rm_sim_spear.spear_metadata import SpearMetadataCallbacks

metadata = SpearMetadataCallbacks(metadata_component, "front_camera", "rgb")
port = SpearRenderPort(
    instance=instance,
    capture_component=capture_component,
    apply_snapshot=apply_snapshot,
    set_game_paused=set_game_paused,
    readback_metadata=metadata.readback_metadata,
    metadata_enqueue_copy=metadata.enqueue_copy_metadata,
    bind_frame_metadata=metadata.bind_frame_metadata,
    metadata_reset=metadata.reset,
    sensor_id="front_camera",
    modality="rgb",
    buffering_mode="SingleBuffered",
)
```

双/三缓冲时，`metadata_enqueue_copy(None)` 会写入预热标记；业务帧写入提交身份。
`SpearRenderPort` 会先读取并丢弃预热元数据，再校验业务回读。
