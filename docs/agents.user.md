# 本机环境事实（agent 每次会话先读）

> 对应上游 `~/spear/docs/agents.user.md` 的定位：只记"这台机器的事实"，不记项目决策（决策在 [`agents.md`](agents.md) 与 [`decisions.md`](decisions.md)）。

## 路径

| 用途 | 路径 |
|---|---|
| 本仓库 | `/home/coffee/rm_sim_spear` |
| SPEAR（上游，`ue-58` 分支） | `/home/coffee/spear` |
| UE 5.8 引擎源码/安装 | `/home/coffee/Linux_Unreal_Engine_5.8.0` |
| UE 项目 | `ue/rm_sim_spear.uproject`（默认地图 `/Game/creo_stp.creo_stp`） |
| UE 内容（git submodule） | `ue/Content` → `github.com/coffee-xzm/rm_sim_content`（Git LFS） |
| 机器人 CAD 源 | `assets/总装_urdf`、`assets/新烧饼底盘2装电器设备_urdf`（STL 是二进制，见 `.claudeignore`） |
| 相关项目（只作验收标准参考） | `~/rm_sim_rune`（`CLAUDE.md` 定义能量机关时序/灯效/环数） |

**引擎源码只在被明确要求时搜索**（上游 `agents.user.md.example` 的规则）。要引用 UE 行为时，先把结论写进 `docs/`，避免下次再翻源码。

## Python 环境

| 解释器 | 版本 | 用途 |
|---|---|---|
| `/home/coffee/miniconda3/envs/spear-env/bin/python` | 3.11.16，numpy 2.0.2，mujoco 3.14.0 | 跑 SPEAR / MuJoCo / 物理仿真（**默认**） |
| `python3` | 3.10.12，numpy 1.26.4 | 离线单测、纯 Python 流水线（`pipeline/*`、`rm_sim_spear/vram.py` 等） |

当前 180 个离线单测全通过（`PYTHONPATH=. python3 -m unittest discover -s tests`，约 7 s）。
**不要**改用 `python3 -m pytest`：系统 pytest 6.x 与用户目录里的 anyio 插件冲突
（`ModuleNotFoundError: No module named '_pytest.scope'`）。

## GPU / 显存

- NVIDIA GeForce RTX 4060 Laptop，**8188 MiB** 总显存；实测 device-local 可用预算约 **5960–6874 MiB**。
- 满画质（Lumen + RT + VSM）在 1440×1080 下必然失败：`Out of memory on Vulkan; MemoryTypeIndex=1, AllocSize=256.000MB` 后 `VulkanMemory.cpp` 直接 `Fatal`，Python 侧只看到后面的 `rpc::timeout`。
- 2× 超采样 @1440×1080 实测峰值 7718 MiB、最低剩余 63 MiB —— 不要重复尝试。
- 画质/显存的取舍与实测数字见 [`rendering.md`](rendering.md)。
- **启动自己的 UE 之前先看谁在占卡**：`~/rm_sim_rune` 的打包产物（进程名 `rm_sim`）在跑时会占
  **≈5.3 GiB**、cook 进程（`UnrealEditor-Cmd ... -run=Cook`）另占数 GiB；两者都在时我们跑不起来。
  `nvidia-smi --query-compute-apps=pid,used_memory,process_name --format=csv` 一眼看清。
- 参照物：`~/rm_sim_rune` 的打包产物是"同地图、同 1440×1080、同 `USceneCaptureComponent2D` 机制、
  单样本"的干净基准（21.4 fps，显存还比我们高）。用
  [`tools/read_rune_frames.py`](../tools/read_rune_frames.py) 就能读它的共享内存出图，
  对照结论与"该抄什么"见 [`rendering.md`](rendering.md) §12。

## 显示与启动

- 真实 X display 是 `:1`（不是 `:0`）。所有带窗口的 UE 进程都加 `env DISPLAY=:1`。
- UE 可执行：`/home/coffee/Linux_Unreal_Engine_5.8.0/Engine/Binaries/Linux/UnrealEditor`（编辑器）与同目录 `UnrealEditor-Cmd`（commandlet）。
- standalone 用 `-game`；此模式下**没有编辑器世界、没有 metadata 组件**，`instance.get_editor()` 会断言失败（见 [`agents.md`](agents.md) 第 2、3 节）。

### 同一时刻只能有一个 SPEAR 实例（2026-10-10 实测）

SPEAR 客户端连的是配置文件里那个**固定 RPC 端口**，所以第二个实例连上的是第一个的
server，SPEAR 自己会拒绝并**顺手杀掉它刚启动的那个新编辑器**：

```
ERROR: engine_globals_service...get_current_process_id returned 43292 but the PID of
the process we just launched is 44294. The Unreal Editor might be open already ...
```

判据：`run_spear_sim.py` 在 2 秒内就以 `instance.py:884` 的 `assert False` 退出（正常
启动编辑器要几十秒）。所以：

- 人和 agent **不能同时**跑 `run_spear_sim.py` / `run_physics_sim.py` / `test_urdf_views.py`
  的 UE 路径——不只是抢显存，是抢端口。
- `run_spear_sim.py` **从不调用 `instance.close()`**，退出后编辑器留在原地（这也是"跑完
  还能接着看"的原因）。下一次运行前先 `pgrep -x UnrealEditor`，有就说明端口被占。
- 遗留编辑器会一直占着 ~5.5 GiB。确认不是别人的 cook 之后再按 PID 收掉。

### agent 跑无头、人用 `:1`（2026-10-10 约定）

同一张 8 GiB 卡上，agent 的迭代运行不要占真实显示器，也不要和人的开发抢进程：

- **agent 侧一律无头**：`xvfb-run`（显式 `-n <n>`，别用 `-a`：本机 `-a` 丢过 X server，
  见 [`risks.md`](risks.md)），不传 `--display`，**并且一律给 `tools/run_spear_sim.py`
  加 `--no-mujoco-viewer`**。存下来的帧和 `trace.csv` 就是证据。
  `tools/run_spear_sim.py` 的 MuJoCo viewer 默认是开的（它是交互入口，而且碰撞体只在
  那个窗口里看得见）；`tools/run_physics_sim.py` 仍是 opt-in，不传即可。
- **为什么无头必须显式关**（2026-10-10 实测，两条路都试过）：
  - **没有 `DISPLAY`**：`launch_passive` 抛的是 `mujoco.FatalError`，而它由 C 层直接
    `exit(1)`，只留一行 `ERROR: could not initialize GLFW`——Python 侧 `try/except`
    **永远接不到**。所以 `run_spear_sim.py` 改成在调用前预检 `DISPLAY`/`WAYLAND_DISPLAY`，
    缺失时直接 `SystemExit` 并提示补 `--no-mujoco-viewer`。
  - **`xvfb-run` 下 `DISPLAY` 是有的**，预检放行，窗口**真的会开**（`is_running()` 为真、
    `sync()` 有效），但进程退出时会挂住：`X Error of failed request: GLXBadDrawable
    (X_GLXSwapBuffers)`，只能靠超时杀掉。所以"有 DISPLAY 就安全"是错的，无头必须显式关。
- **人侧走真实显示**：交互式自由相机用 `tools/view_pose.sh`（默认 `DISPLAY=:1`，驱
  `tools/run_physics_sim.py --interactive --view fixed`，**物理照常推进**）。
  `tools/view_pose.sh --headless` 是同一入口的无头分支（有帧预算，跑完落盘）。
  冻结姿态那个观察器是另一回事：`tools/test_urdf_views.py --continuous --display`，
  它不调用 `physics.step()`（[`urdf_workflow.md`](urdf_workflow.md) 的"自由相机"一节有对照表）。
- 两边都**不要盲杀 UE 进程**：`pgrep -a -x UnrealEditor` 里可能是别人正在跑的 cook
  （`UnrealEditor-Cmd ... -run=Cook`，见下节）。`tools/view_pose.sh` 发现已有进程就直接
  报错退出；`tools/test_urdf_views.py --kill-stale-editor` 在 `LAUNCH_MODE: "editor"` 下
  按 `ue/rm_sim_spear.uproject` 限定作用域，但仍然会连 cook 一起杀——先看清再动手。

### 常用命令

```bash
# 导入 URDF 视觉网格 + 建 link 锚点（编辑器 commandlet，幂等）
env DISPLAY=:1 /home/coffee/Linux_Unreal_Engine_5.8.0/Engine/Binaries/Linux/UnrealEditor-Cmd \
  /home/coffee/rm_sim_spear/ue/rm_sim_spear.uproject \
  -run=pythonscript -script=/home/coffee/rm_sim_spear/tools/import_urdf_visuals.py \
  -unattended -nop4 -nosplash

# 物理推进 + 逐帧落图（MuJoCo 是物理真源；机位默认相对已施加位姿的 base_link）
# 默认就是 1440x1080 单样本：~/rm_sim_rune 的打包产物在同样的地图/分辨率/采集机制下单样本就干净，
# 且整进程显存比我们还高（21.4 fps vs 我们 11.4 fps）。消细线断裂的三种手段只在"必须贴地掠射"时用
# （实测见 docs/rendering.md 第 7、11、12 节）：
#   --supersample 2            空间超采样，143 ms；3x/4x 不要再考虑（4x 是 2 GB 连续分配撞墙）
#   --average-frames 8 --jitter-pixels 0.5   时域抖动+平均，保持锐度（断裂 20 -> 5，帧时间 x3.5）
#   --downsample-filter gaussian --downsample-sigma-px 0.6   重建期高斯，最连续但最软
# 注意：对"已经采样完"的成图做 --blur-sigma-px 不能消混叠（断裂只从 20 降到 16，锐度减半）
env DISPLAY=:1 PYTHONPATH=. /home/coffee/miniconda3/envs/spear-env/bin/python -u \
  tools/run_physics_sim.py --frames 160 --settle-frames 20 --drive-gain 5.0 \
  --joint-armature 0.05 --drive-wheels 6 --view fixed --width 1440 --height 1080 \
  --vram-poll-seconds 0.5 --output-dir /tmp/rm_sim_spear_physics

# 吞吐模式：出图每帧 11 ms（512x384）～20-24 ms（1440x1080），对比原始链路的 108 ms
#   --buffering triple  流水线回读，省掉 read_pixels() 每帧 ~22 ms 的 flush（图像延后 3 帧，
#                       工具按 _BUFFERED_LAG 自动配对，输出与 single 逐像素等价）
#   --sync-pose         位姿改用直接 UFunction。实测两种方式一样快（21.7 vs 22.6 ms），
#                       它不是性能杠杆，只影响同一事务内写入/读取的可见性（render_budget.md §15）
#   --pose-every N      每 N 帧下发一次位姿（默认 1 = 每帧）。注意：2026-10-09 前这个默认值是坏的
#                       （`iteration % 1 == 1` 恒假），当时的"位姿很便宜"结论全部作废，见 §15
#   --save-images none|async   PNG 编码 1440x1080 一张 22-39 ms，比主循环还慢，必须出主循环
#                       或改用共享内存发布；每帧出图时它是吞吐上限，不是仿真
#   --publish-shm NAME  每帧写进 /dev/shm/NAME 的环形缓冲（实测 0.6-0.7 ms/帧、消费者 83 fps @512x384）。
#                       任意进程用 tools/read_shared_frames.py 消费（只需 python3+numpy/cv2，不占 GPU）：
#                         python3 tools/read_shared_frames.py --list
#                         python3 tools/read_shared_frames.py --output /tmp/frame.png
#                         python3 tools/read_shared_frames.py --fps-seconds 3
# 实测与拆解（含阶段列、地板、MuJoCo 速率）：docs/render_budget.md 第 13 节
env DISPLAY=:1 PYTHONPATH=. /home/coffee/miniconda3/envs/spear-env/bin/python -u \
  tools/run_physics_sim.py --frames 40 --settle-frames 10 --view fixed \
  --world-offset-cm 0 0 -140 --drive-gain 5.0 --joint-armature 0.05 --drive-wheels 6 \
  --buffering triple --sync-pose --save-images none \
  --width 512 --height 384 --window-size 512 512 --output-dir tmp/perf_throughput

# 读 ~/rm_sim_rune 正在跑的打包产物（1440x1080 单样本参照系），不花我们的显存：
# 共享内存 /dev/shm/rmsim_camera_v2 是它对外发布的最终图像，UDP 39001 可以改它的相机俯仰/偏航
python3 tools/read_rune_frames.py --output /tmp/rune_now.png --fps-seconds 6 --imu
python3 tools/read_rune_frames.py --camera 0,-25,0 --output /tmp/rune_down.png   # 之后记得 --camera 0,0,0 还原

# 打包成 cooked standalone（抄 rune 的交付形态；SPEAR 原生支持 LAUNCH_MODE=game）
# 会自己起 Zen + 看门狗重启 + trap 收尸；另一个 UnrealEditor* 在跑时直接拒绝启动（--force 越过）
tools/package_linux.sh                     # -build -cook + stage/pak/archive
tools/package_linux.sh --resume --force    # cook 已落盘但 UAT 报错时，直接 stage（6 秒出包）
# 产物：build/package/Linux/rm_sim_spear.sh（长期产物放仓库 build/，不要再放 /tmp——
# 2026-10-06 一次系统 tmp 清理把 1.6 GB 成品删掉过；RM_SIM_ARCHIVE_DIR 可改）：
#   脚本自己的 Zen 数据目录（可复用 DDC，一次全量 cook 约 1.7 GB）同样落在 build/zen
#   （RM_SIM_ZEN_DIR 可改），只有 UAT 的临时 HOME 留在 /tmp（RM_SIM_BUILD_HOME 可改）
#   --rpc-timeout-seconds 20 是必需的：打包版从 pak 流式加载 + PSO 首次创建会让默认 2.0 s 超时
env DISPLAY=:1 PYTHONPATH=. VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/nvidia_icd.json \
  /home/coffee/miniconda3/envs/spear-env/bin/python -u \
  tools/run_physics_sim.py --game-executable build/package/Linux/rm_sim_spear.sh \
  --rpc-timeout-seconds 20 --frames 160 --settle-frames 20 --drive-gain 5.0 --joint-armature 0.05 \
  --drive-wheels 6 --view fixed --world-offset-cm 0 0 -140 --width 1440 --height 1080 \
  --vram-poll-seconds 0.5 --output-dir tmp/packaged
# 实测（同机位同参数背靠背）：RSS 6.60 GB -> 3.62 GB（-45%），frame_ms 中位 91.2 -> 87.1 ms，
# 显存没变便宜（footprint 2673 -> 3073 MiB，口径噪声 ±200）

# 机器人不出现在画面里时，先查绑定而不是猜：打印锚点/视觉网格的世界坐标、缩放、可见性
env DISPLAY=:1 PYTHONPATH=. /home/coffee/miniconda3/envs/spear-env/bin/python -u \
  tools/run_physics_sim.py --frames 2 --settle-frames 1 --view fixed \
  --width 256 --height 256 --dump-binding --output-dir /tmp/rm_sim_spear_dump

# 离线单测
PYTHONPATH=. python3 -m unittest discover -s tests
```

**画面里的"没有材质的圆球"**：那是引擎 `ADefaultPawn` 的占位球（`/Engine/EngineMeshes/Sphere`，
无材质，`bOwnerNoSee` 只对玩家视口隐藏，场景捕获照画），不是关卡道具。`run_physics_sim.py`
默认 `--hide-pawn-mesh` 把它隐藏掉；`--no-hide-pawn-mesh` 可复现。详见
[`rendering.md`](rendering.md) §8.4。四块装甲板目前**没有导入也没有绑上**（绑定对画面贡献 0 像素，
克隆落到锚点下方 1.28 m），修法与证据见 [`rendering.md`](rendering.md) §8.3。

**观察机位**：默认机位已改为 `rm_sim_spear/view_pose.py` 的 200/170/30 cm（离地约 170 cm、
俯角 −35°、距离 2.44 m、机器人约 188×134 px），掠射虚线随之消失；旧的 155/60/20（离地 60 cm、
俯角 −14°）可用 `--camera-standoff-cm 155.56 --camera-height-cm 60 --camera-aim-height-cm 20` 复现做 A/B。
地面线与地面纹理在 1.5–2.5 m 机位下才可读（实测 `tmp/height_ladder/`、[`rendering.md`](rendering.md) §12.6），
而显存几乎不变（3968→3974 MiB）。

SPEAR 侧用户配置（含 `EDITOR_EXECUTABLE`/`EDITOR_UPROJECT`）在 `tools/spear_user_config.yaml`；实际运行时常复制到 `/tmp/rm_sim_spear_user_config.yaml` 再改。

渲染预算开关（`r.Nanite=0`、光追按需、VSM 页数 1024、Lumen surface cache 1024、`r.Lumen.TraceMeshSDFs=0`；GI+反射保持 Lumen）已经写进 `ue/Config/DefaultEngine.ini` 的 `[SystemSettings]`，改动前先读 [`render_budget.md`](render_budget.md)。

`--world-offset-cm`：**实测竞技场地面在 z ≈ −137.9 … −140.5 cm**（俯视采样 `sp_world_position_` 网格，见
[`rendering.md`](rendering.md) 第 10 节），所以用 **−140**，现在也是 `tools/run_physics_sim.py` 的
**默认值**（2026-10-09 晚改；`tools/run_spear_sim.py` 2026-10-10 也改成 −140 —— 它之前一直是 `0 0 0`，
help 里还写着过时的 "creo_stp's floor is at z=-160"。之前只有本文档写了 −140，代码默认一直是 −160——那来自合并 rune Actor 的 Z，
不是机器人脚下那块地面——于是不带开关的调用包括本文档那条主命令都把机器人埋进地面 20 cm。判据：隐藏
机器人的像素差在 −160 时只有 260 px（几乎看不见），−140 时 12084 px。另见
[`rendering.md`](rendering.md) 第 10.1 节，含修前后的掠射机位对照图）。机器人"悬空/
埋进地面/画面里看不见"先用 `--dump-robot-bounds`、`--hide-tag` 和 `world_position` 模态量，不要用 actor
名字或单个 actor 的 Z 推断。

### 鼠标捕获：**关着**（2026-10-10 修定）

**人的调试路径是"编辑器 + PIE 暂停，然后直接在画面里点选 actor"**，所以指针必须留在桌面手里。
开着捕获时症状正是"一点视口鼠标就被抢走，选不中物体"——因为 `Capture` 的语义就是
*点一下视口才开始捕获*。

`-game`/PIE 窗口**一激活**就会抓指针：`FSceneViewport::OnWindowActivated` 在
`bShouldCaptureMouseOnActivate`（来自 `UInputSettings::bCaptureMouseOnLaunch`）或"永久捕获"模式下调用
`AcquireFocusAndCapture`，`FLinuxApplication::UpdateMouseCaptureWindow` 随后 `SDL_CaptureMouse(true)`
并（除非 `-norelativemousemode`）切到相对鼠标模式 —— 桌面指针就此失灵。

正确值（**不要改回 stock**）：

| 位置 | 必须的值 | 覆盖范围 |
|---|---|---|
| `ue/Config/DefaultInput.ini` | `bCaptureMouseOnLaunch=False`、`DefaultViewportMouseCaptureMode=NoCapture`、`DefaultViewportMouseLockMode=DoNotLock` | 编辑器 / PIE / `-game` / 所有工具 |
| `tools/spear_user_config.yaml` 的 `COMMAND_LINE_ARGS` | `norelativemousemode: null`、`DefaultViewportMouseCaptureMode: "NoCapture"` | SPEAR 拉起的所有进程 |

命令行那道在 ini **之后**应用（`GameViewportClient.cpp` 构造函数），所以它是更强的一道；两处都要留。
`ue/Config/DefaultInput_test.ini` 也保留着同一组值，可作对照。

回归守卫：[`tests/test_mouse_capture_config.py`](../tests/test_mouse_capture_config.py)
（"the project must not re-enable UE's pointer capture"）。2026-10-10 之前这两条测试一直是失败的，
因为配置被改回过捕获模式；改回来即恢复绿色。**注意 `tmp/config.yaml` 是 SPEAR 从 user config
展开生成的**（`TEMP_CONFIG_FILE`/`TEMP_DIR`），不要去手改，重启 SPEAR 会重写。

历史：2026-10-09 首次改成不捕获 → 2026-10-10 核对时发现被改回并如实记录 → 同日按"人要在 PIE 里
点选物体"的需求再改回不捕获，并留下上面的测试。

agent 侧不要依赖鼠标——无头运行（`xvfb-run`）根本没有可点的窗口，交互相机一律走
`tools/view_pose.sh` 的键盘路径（`W/S/A/D/E/Q` + `I/J/K/L`，见
[`urdf_workflow.md`](urdf_workflow.md) 的"自由相机"一节）。键盘路径不受捕获设置影响。

**自动化复现的限制**：这些运行都是 `-windowed` 且没有被点击，窗口不会自我激活，所以"被抓住"的瞬间用
X 探针（`XFixesGetCursorImage` + `XGrabPointer`）抓不到；判据是上面两处的配置值、引擎命令行本身，
以及那条回归测试。

## 进程与仓库注意事项

- UE 进程可能残留：启动前 `pgrep -af UnrealEditor`。残留会让 SPEAR 报 `get_current_process_id returned <pid A> but ... <pid B>`；此时杀掉旧进程再重试（不要并发起两个 UE）。
  **注意匹配面很宽**：任何 `UnrealEditor*` 进程都会挡住启动，**包括别人（或自己）正在跑的 cook**
  （`UnrealEditor-Cmd ... -run=Cook`）——症状是 `spear/instance.py:870 _initialize_client` 直接 `assert False`，
  和"残留进程"长得一样。2026-10-06 晚第一次撞上：E1/E2 跑完后起了打包 cook，随后带 `--hide-tag` 的两次
  运行全部 `rc=1`。要么等 cook 结束，要么先杀 cook。
- `ue/Content` 是 submodule，目前有本地改动（10 个 `M` + `?? RMSim/` + 10 个 `?? SM_MERGED_*`）。其中 **8 个 `SM_MERGED_Actor_*`/`SM_MERGED_StaticMeshActor_*` 是 Merge Actors 合并出来的场地**（`creo_stp.umap` 也因此从 11.78 MB 缩到 3.49 MB），另 2 个 `SM_MERGED_blender_for_ue5_node_*` 是机关。**不要执行 `git submodule update`/`git checkout` 之类会丢弃它们的分支操作** —— 丢了这些合并资产，关卡就会引用到不存在的东西。详见 [`content_management.md`](content_management.md)。
- 父仓库 `main` 分支**还没有任何 commit**（所有文件都是 staged 状态）。因此 `git stash`、`git checkout .`、`git reset --hard` 都可能不可预期地丢工作，除非用户明确要求。
- 用 `nvidia-smi` 采样显存时可参考 `rm_sim_spear/vram.py`（预检、崩溃签名、残留进程检测都已实现并有单测）。
- **cook 会"假活"**：`BuildCookRun` 只是让 UAT 去连一个已有的 Zen server（`-NoZenAutoLaunch=<host[:port]>`），
  server 一死 cook 就不再前进，但 `UnrealEditor-Cmd` 进程仍活着、日志仍重复最后一行。
  判据：`ss -ltnp | grep -E '8558|38958'` 有人监听吗 + 日志里 `Cooked packages N` 是否在涨。
  2026-10-06 19:00 的 rune cook 就是这样卡在 `391/3777` 三十多分钟。本仓库用
  [`tools/package_linux.sh`](../tools/package_linux.sh)（自己起 Zen + `trap` 收尸 + 端口占用即拒绝启动）；
  它的 Zen 端口是 **38958**（`RM_SIM_ZEN_PORT` 可覆盖），**刻意避开编辑器自己的 8558**。
  看进度：`grep -a -o "Cooked packages [0-9]*" <uat log> | tail -1`；shader 编译阶段会长时间停在
  `Compiling global changed shaders for platform 'Linux'`，那是正常的（首次 SM6 全量编译）。

## 从桌面打开编辑器 / ZenServer

- 桌面入口在 `~/.local/share/applications/`：原本的 `UnrealEngine.desktop` 只写引擎路径、**没有项目参数**，
  所以会走"项目浏览器 → `SwitchProject` 重启"两个进程；**ZenServer 被那个短命的启动器进程持有**
  （`--owner-pid <启动器>`），启动器一退出 server 就被收走，而编辑器还在用它 → 看起来"Zen 卡死"。
  本仓库新增 `rm_sim_spear.desktop`：直接 `UnrealEditor <uproject>`（单进程）+ 补 `http_proxy`
  （gnome-shell 的环境里没有代理，交互 shell 里有，这一条差别就是启动期那 ~30 s HTTPS 重试的来源）。
  分析、证据和 `no_proxy=...,::1,[::1]` 的必要性见 [`rendering.md`](rendering.md) §12.10。
  验证过（libcurl 7.81 实测 `Trying ::1:8558... Connected`）：`no_proxy` 里的 `::1`/`[::1]` 确实让
  Zen/DDC 走直连，不会被丢给 Clash。
  **入口的 `Name` 字段就是任务栏/程序菜单里显示的程序名**：`Name=RM Sim SPEAR (总装)` 会让
  GNOME 把它显示成"RM Sim SPEAR (总装)"（用户 2026-10-08 问的"程序名怎么变了"就是这个，改 `Name=` 一行即可）。
  UE 自己的窗口标题与它无关：日志里是 `rm_sim_spear - 虚幻编辑器`，来自 `DefaultGame.ini ProjectName=rm_sim_spear`。
  另外**旧入口会留两个 UnrealEditor 常驻**：项目浏览器进程实测 3.8 GB RSS + 532 MiB 显存，而真正干活的
  编辑器是 6.8 GB RSS + 3080 MiB 显存 —— 两者合计 10.6 GB，是"打开工程很卡"的常见来源。
- **看编辑器自己的日志不用碰 `ue/Saved/**`**（那是 `.claudeignore` 的硬规则）：gnome 拉起的进程 stdout
  被 journald 收走了，`journalctl --user` 里按 cgroup 标签就能读到完整启动日志，例如
  `journalctl --user --since "2026-10-08 12:50" | grep -aE "UnrealEngine.desktop\[|LogStall|Total time"`。
  直接（SPEAR）拉起的进程标签是 `UnrealEditor[pid]`。
- 排查用：`journalctl --user --since today | grep "UnrealEngine.desktop\["`（cgroup 标签能区分
  "启动器进程"与"真正的编辑器进程"）；Zen 自己的日志在
  `~/.config/Epic/UnrealEngine/Common/Zen/Data/logs/zenserver*.log`（每次启动一个 `.N` 文件，
  有 `log ending` 就是优雅收尾）。
- **编辑器开着时不要跑 cook / commandlet**：同一个 HOME 下所有 Unreal 进程抢
  `FSystemWideCriticalSection("ZenServerLaunch")`（`~/.config/Epic/ZenServerLaunch`），
  编辑器会打印 `still waiting for other process to complete startup/recovery` 并一直等。

## cook 落盘清单 + "打开工程变卡"怎么查（2026-10-08 实测）

一次成功的 cook（`Cooked packages 1451` / `Took 505.21s to run UnrealEditor-Cmd, ExitCode=0`）**只写派生数据**：

| 路径 | 大小 | 说明 |
|---|---|---|
| `ue/Binaries/Linux/rm_sim_spear{,.debug,.sym,.target}` | 331+305+207 MB | `-build` 编的**游戏**单文件；`.debug/.sym` 只用于崩溃符号化，`build/package/.../Binaries/Linux/` 里另有一份，删仓库这份只影响下次 `-build` 重链 |
| `ue/Binaries/Linux/libUnrealEditor-SpCore.so`、`SpModuleRules{,Editor}.so` | 小 | `-build` 顺带重链了 **editor 侧**模块（23:27:16；`SpModuleRules*` 是 UBT 读插件 build rules 时加载的模块）。实测**没有**引发编辑器启动重编 |
| `ue/Saved/Cooked/Linux` | 296 MB | cook 产物 + `*.projectstore`（脚本把它的 `zenserver.hostname` 改写成 `127.0.0.1`） |
| `ue/Saved/StagedBuilds` | 1.6 GB | stage 的副本；`build/package` 里还有一份（可删，但 `--resume` 前别删 `ue/Saved/Cooked`） |
| `ue/Saved/Shaders`/`ShaderDebugInfo`/`MaterialStats` | ≈150 MB | cooker 的着色器工作目录 |
| `build/package/Linux/rm_sim_spear.sh` + `rm_sim_spear/` | 1.6 GB | 交付产物 |
| `build/zen/Data` | 1.7 GB | cook 自己的 Zen DDC（端口 38958、`HOME=/tmp/rm_sim_spear_build_home`），与编辑器的 `~/.config/Epic/.../Zen/Data` **完全隔离** |

`ue/Content/creo_stp.umap`(23:12)、`ue/Config/*.ini`、`.uproject` 的 mtime 都**早于** cook（23:18–23:44），
所以"cook 改了内容 / 让编辑器变卡"在文件层面不成立；编辑器启动耗时实测也没变：

```bash
# 每个 UE 进程的启动耗时（编辑器、项目浏览器、-game 都在内）
journalctl --user --since "2026-10-06 18:00" | grep -aE "Engine Initialization\) Total time|LogStall: Startup complete"
```

cook 前 9.4/9.9/10.2/10.6/11.2/11.6/11.6/12.3/16.3/26.0/29.7 s，cook 后 10-07 的 15.7+9.2 s、10-08 的 16.1 s、
今天的 12 s：同一量级，且 `LogStall` 一次主线程卡顿都没报。真正让机器/编辑器"很卡"的是这四条：

1. **cook + stage + pak + 打包版冒烟连在一个窗口里跑**：cook 期间 29 个 ShaderCompileWorker ≈ 25 GB RSS，
   再叠两遍 1.6 GB 拷贝，此时开编辑器必然卡。[`tools/package_linux.sh:96`](../tools/package_linux.sh) 现在
   会先 `pgrep -a -x UnrealEditor` 直接拒绝启动，所以这种重叠不会再有。
2. **旧桌面入口留两个 UnrealEditor 常驻**（见上一节实测内存）。
3. **关卡本身变重**：导入后用 13 个 link 锚点 + 1732 个 StaticMeshActor 代替原来的合并 Actor，
   视口/Outliner 每帧都要为它们付钱。
4. 慢性环境问题：`fs.inotify.max_user_watches=65536`，而引擎目录就有 **69401** 个，编辑器 DirectoryWatcher
   会报 `inotify_add_watch cannot watch folder .../Intermediate/Build/Linux (Out of inotify watches)`
   （`sudo sysctl -w fs.inotify.max_user_watches=524288` 可解）；GUI 会话没有代理，编辑器每 30 s 打一条
   `HTTP request timed out ... generate_204`（一次会话 46 条），用带代理的新入口就没有。

## 用户偏好

- 沟通用中文；代码/docstring 用英文，`docs/*.md` 用中文。
- 汇报时给"结论 + 判据（图/数值）+ 可复现命令"，少讲过程。
- 视觉问题的最终判据是**图**，不是日志（见 [`agents.md`](agents.md) 第 1 节）。
