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

不要删除备份，直到确认远端仓库已经包含本地 106 个修改对应的资产版本。
如果远端仍是旧版本，应先在 Content 仓库提交/推送这些修改，再更新主仓库的
submodule commit。后续克隆使用：

```bash
git clone <rm_sim_spear-url>
git submodule update --init --recursive
git lfs pull
```
