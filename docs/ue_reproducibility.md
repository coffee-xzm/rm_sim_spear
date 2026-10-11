# UE/SPEAR 可复现环境

`ue/` 不是只靠 `.uproject` 就能独立打开的工程。它依赖 UE 5.8 和 SPEAR
的 Unreal 插件；插件代码在外部 SPEAR 仓库中，不能用 `ue/` 目录里的
Git 记录替代。下面的版本和步骤是本仓库的可复现基线。

## 固定版本

| 依赖 | 固定值 |
| --- | --- |
| Unreal Engine | 5.8 |
| SPEAR 仓库 | `https://github.com/spear-sim/spear.git` |
| SPEAR 分支 | `ue-58` |
| SPEAR commit | `0434b441bb2f1b190a378a8ec9bb955247dfe528` |
| Content 仓库 | `https://github.com/coffee-xzm/rm_sim_content.git` |
| Content 版本 | `3a2cec34107561036ab53083f2ffb3a54264e33c` |
| Rune compatibility plugin | `ue/Plugins/sim/`，与地图一起版本化 |
| Rune plugin 来源版本 | `rm_sim_rune` commit `a18790bfe9dc749665f10572a568625c67bcd72e` |
| SPEAR Python | Python 3.11；由 SPEAR `python/pyproject.toml` 锁定直接依赖 |

SPEAR 的 `main` 分支面向 UE 5.5，UE 5.8 必须使用 `ue-58`。不要用浮动的
`main` 或最新 commit 替代上面的 commit。

## 首次安装

先从主仓库根目录初始化视觉资产：

```bash
git submodule update --init --recursive
git lfs pull
```

把 SPEAR checkout 放在主仓库的同级目录（即 `../spear`），并固定到指定版本：

```bash
git clone --branch ue-58 --recurse-submodules \
  https://github.com/spear-sim/spear.git ../spear
git -C ../spear checkout 0434b441bb2f1b190a378a8ec9bb955247dfe528
git -C ../spear submodule update --init --recursive
```

在 Python 3.11 环境安装 SPEAR 包。官方安装脚本会 `import spear`，因此这一步
必须先完成：

```bash
conda create -n spear-env python=3.11
conda activate spear-env
python -m pip install -e ../spear/python
```

然后从本仓库根目录执行包装脚本：

```bash
python tools/setup_ue_spear.py
```

该脚本会校验 SPEAR commit，修正 `AdditionalPluginDirectories` 为相对路径，
再调用 SPEAR 官方的
`tools/install_plugins_in_external_project.py`。官方脚本负责生成 `ue/Source/`
（没有源码模块时）、补齐主模块对 `SpCore` 的依赖，并把 `/SpContent` 加入打包
清单。

若 SPEAR 放在其他位置，可设置 `SPEAR_DIR=/path/to/spear`；脚本会重新计算
相对路径。不要手工把 `/home/.../spear/...` 这样的绝对路径提交到
`.uproject`。

## 编译和验证

```bash
<UE5.8>/Engine/Build/BatchFiles/Linux/Build.sh \
  rm_sim_spearEditor Linux Development \
  -Project="$PWD/ue/rm_sim_spear.uproject" -NoHotReloadFromIDE
```

打开工程前检查三项：

```bash
git -C ../spear rev-parse HEAD
git submodule status
rg '"Name": "SpCore"|AdditionalPluginDirectories' ue/rm_sim_spear.uproject
```

运行日志应出现 `SpCore` 的启动记录，并加载 `VulkanRHI`。Linux 的目标 RHI
配置写在 `ue/Config/DefaultEngine.ini` 的
`LinuxTargetPlatform.LinuxTargetSettings` 中，项目固定使用
`SF_VULKAN_SM6`；这是 UE 的平台目标配置，不是 SPEAR 的额外插件开关。

地图依赖的插件也必须处于启用状态：`sim` 提供
`/Script/sim.Rune`、`/Script/sim.RuneSceneDirector` 和
`/sim/DA_RuneVisualProfile`；UE 5.8 的 `USDCore` 提供
`/USDCore/Materials/UsdPreviewSurfaceTwoSided`，`USDImporter` 用于保持原始
USD 资产的编辑和导入链路。三者都已写入 `.uproject`，不依赖 `rm_sim_rune` 的
外部路径。`sim` 的可复现来源是上表 commit；仓库内保存的是该 commit 的
`Source/`、`Content/`、`Resources/` 和 `Scripts/` 快照，`Binaries/` 与
`Intermediate/` 由 UE 本地重新生成。

## 渲染基线

`ue/Content/creo_stp.umap` 与 `rm_sim_rune` 的场景资产一致。当前亮度差异来自
工程渲染配置，而不是地图文件。项目已同步 rune 中影响曝光的设置：关闭自动曝光、
扩展默认亮度范围、固定 Local Exposure 的高光/阴影对比度，并使用 SPEAR 的
`r.EyeAdaptation.ExponentialTransitionDistance=0.5`；同时保留 rune 使用的硬件光线
追踪代理和 Substrate 配置。若编辑器仍显示
不同结果，先关闭并重新打开工程，避免使用 `Saved/Config/LinuxEditor/` 中旧的
用户视口曝光状态；`Saved/`、`Intermediate/` 和 `DerivedDataCache/` 都是本机
生成物，不属于复现输入。需要清除本机视口状态时，关闭编辑器后可用可恢复的
方式重命名它：

```bash
mv ue/Saved/Config/LinuxEditor /tmp/rm_sim_spear-LinuxEditor-config
```

这只影响编辑器布局和视口显示，不会修改 `ue/Content` 或运行时地图。
