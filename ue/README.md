# UE 元数据组件

`ue/` 是本仓库的完整 UE 5.8 工程，入口为 `ue/rm_sim_spear.uproject`。
视觉资产位于独立的 `ue/Content` Git submodule，由 `rm_sim_content` 的 Git LFS
管理；`rm_sim_rune` 只提供迁移源，不是运行时依赖。

SPEAR/Python 运行时绑定器位于
[`rm_sim_spear/spear_scene_binding.py`](../rm_sim_spear/spear_scene_binding.py)，
UE 插件位于 `ue/Plugins/RmSimSpearMetadata/`。

运行时绑定函数必须在 SPEAR 的 `begin_frame()` / `end_frame()` 事务中调用。
首次初始化会创建四个 transient 装甲实例，后续 reset 按稳定名复用，不会每帧
重新 spawn。

`RmSimSpearMetadata` 是一个独立的 SPEAR companion component，不修改
`USpSceneCaptureComponent2D`。它通过同一相机 Actor 上的 SpFunc 队列保存：

- `run_id`
- `commit_id`
- `sensor_id`
- `modality`
- UE 的 `GFrameCounter`

完整的可复现安装步骤见
[`docs/ue_reproducibility.md`](../docs/ue_reproducibility.md)。不要直接双击打开
工程后再猜插件路径；先在仓库根目录运行：

```bash
python tools/setup_ue_spear.py
```

该命令固定 SPEAR `ue-58` 分支的
`0434b441bb2f1b190a378a8ec9bb955247dfe528`，并调用 SPEAR 官方插件安装脚本。

地图沿用了 rune 的 `sim` 插件资产和 USD 材质，因此工程同时保留并启用
`ue/Plugins/sim/`、UE 5.8 的 `USDCore` 与 `USDImporter`。`sim` 插件的
`Binaries/` 和 `Intermediate/` 不属于版本化输入，会由 UE 重新生成。

插件依赖 SPEAR 的 `SpCore`，`.uplugin` 已声明该依赖。当前源码已在
UE 5.8 Linux 的 `rm_sim_spearEditor` 目标中编译通过；构建命令为：

```bash
<UE5.8>/Engine/Build/BatchFiles/Linux/Build.sh \
  rm_sim_spearEditor Linux Development \
  -Project="$PWD/ue/rm_sim_spear.uproject" -NoHotReloadFromIDE
```

工程通过 `AdditionalPluginDirectories` 找到 SPEAR 的 `unreal_plugins/`，由
`tools/setup_ue_spear.py` 根据 SPEAR checkout 位置自动写入相对路径，否则
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

`RmSimSpearReadbackMetadataComponent` 可以放在相机 Blueprint 上（编辑器 `+Add`，类
上带 `meta=(BlueprintSpawnableComponent)`），也可以由 `run_spear_sim.py` 在事务里用
`unreal_service.create_scene_component_for_actor(...)` 补建。两条路都能用，但**注册
SpFunc 的那一步必须由 Python 在事务里显式触发**：

```python
with instance.begin_frame():
    metadata_component = _find_metadata_component(game, camera, name)  # 找到或补建
    metadata_component.Initialize()   # UFUNCTION：attach 内部 SpFuncComponent + 注册四个 SpFunc
with instance.end_frame():
    pass
with instance.begin_frame():
    metadata_component.initialize_sp_funcs()   # SPEAR 发现已注册的函数名
with instance.end_frame():
    pass
```

`Initialize()` 存在的理由见 `docs/urdf_workflow.md`：SPEAR 只通过
`AttachChildren` 找 `USpFuncComponent`，而构造期 `SetupAttachment` 不会建立这个
关系，运行时 `NewObject` 出来的组件因此会被 SPEAR 当成"没有 SpFunc"。

Python 侧配对：

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
