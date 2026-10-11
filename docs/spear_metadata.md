# SPEAR 回读来源契约

现有 SPEAR 的 `read_pixels()` 只返回数组，无法证明图像属于哪个提交点。因此严格模式需要在 SPEAR 扩展侧增加一层元数据绑定。

## 单缓冲

`SpearRenderPort` 在 `begin_frame()` 内按以下顺序调用：

```text
bind_frame_metadata({run_id, commit_id})
apply_snapshot(snapshot)
end_frame(single_step=True)
read_pixels()
```

扩展保存本次绑定的提交身份，并在读回 bundle 中返回：

```json
{
  "metadata": {
    "run_id": "...",
    "commit_id": 12,
    "ue_frame_id": 481,
    "sensor_id": "front_camera",
    "modality": "rgb"
  }
}
```

## 双/三缓冲

业务帧调用 `metadata_enqueue_copy(snapshot)`，将提交身份和当前 UE 帧一起写入 GPU 回读槽；预热帧只调用无身份的 `enqueue_copy()`。每个回读槽必须保存自己的元数据，不能在 `read_pixels()` 时读取“当前”提交身份。

双缓冲预热 1 帧，三缓冲预热 2 帧。预热回读直接丢弃，不进入 `Coordinator` 的待配对表。

`reset_metadata()` 必须在 reset 后的第一个 SPEAR `begin_frame()` 内调用，清空
UE companion component 中上一 run 的元数据队列；`SpearRenderPort` 会延迟执行这个回调，避免在事务外调用 SpFunc。

真实 capture component 的 GPU pending readback 不属于 companion 元数据队列。
如果底层组件在 reset 时仍保留这些槽位，必须给 `SpearRenderPort` 注入
`capture_reset()`，在其中清空或重新 `Terminate/Initialize` capture component。
该回调在 `SpearRenderPort.reset()` 中调用，发生在下一轮预热之前。双/三缓冲实例
已经预热后再次 reset 而没有这个回调会直接报错，不会复用旧的 GPU 槽位。

## 校验规则

- `run_id` 必须匹配当前运行；reset 后旧图像必须拒绝。
- `commit_id` 必须能在待配对快照中找到且只能消费一次。
- `ue_frame_id` 只表示 UE 帧来源，不参与仿真时间计算。
- `sensor_id` 和 `modality` 标识同一提交下的具体传感器流；每个 `SpearRenderPort` 必须校验它们。
- 图像、位姿和传感器消息的仿真时间来自同一个 `FrameCommit`。

这层扩展只负责标识回读来源，不改变 UE 游戏暂停语义，也不让 UE 物理成为仿真真源。
