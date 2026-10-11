#!/usr/bin/env python3
"""Install the pinned SPEAR plugins into this repository's UE project."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


SPEAR_REPOSITORY = "https://github.com/spear-sim/spear.git"
SPEAR_BRANCH = "ue-58"
SPEAR_COMMIT = "0434b441bb2f1b190a378a8ec9bb955247dfe528"


def run(*command: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spear-dir",
        default=os.environ.get("SPEAR_DIR"),
        help="SPEAR checkout; defaults to ../spear relative to this repository",
    )
    parser.add_argument(
        "--project-dir",
        default=None,
        help="UE project directory; defaults to this repository's ue directory",
    )
    args = parser.parse_args()

    repository_root = Path(__file__).resolve().parents[1]
    project_dir = Path(args.project_dir or repository_root / "ue").resolve()
    spear_dir = Path(args.spear_dir or repository_root.parent / "spear").resolve()
    uproject_path = project_dir / "rm_sim_spear.uproject"

    if not (spear_dir / ".git").exists():
        raise SystemExit(
            f"找不到 SPEAR checkout: {spear_dir}\n"
            f"请使用: git clone --branch {SPEAR_BRANCH} --recurse-submodules "
            f"{SPEAR_REPOSITORY} {spear_dir}"
        )
    if not uproject_path.exists():
        raise SystemExit(f"找不到 UE 工程: {uproject_path}")

    actual_commit = run("git", "rev-parse", "HEAD", cwd=spear_dir)
    if actual_commit != SPEAR_COMMIT:
        raise SystemExit(
            "SPEAR 版本不匹配:\n"
            f"  需要: {SPEAR_COMMIT} ({SPEAR_BRANCH})\n"
            f"  当前: {actual_commit}\n"
            f"请执行: git -C {spear_dir} checkout {SPEAR_COMMIT}"
        )
    plugins_dir = spear_dir / "cpp" / "unreal_plugins"
    if not plugins_dir.is_dir():
        raise SystemExit(
            f"SPEAR 子模块不完整，缺少: {plugins_dir}\n"
            f"请执行: git -C {spear_dir} submodule update --init --recursive"
        )

    with uproject_path.open(encoding="utf-8") as stream:
        uproject = json.load(stream)
    relative_plugins_dir = os.path.relpath(plugins_dir, project_dir).replace(os.sep, "/")
    additional_dirs = [
        entry
        for entry in uproject.get("AdditionalPluginDirectories", [])
        if "unreal_plugins" not in entry.replace("\\", "/")
    ]
    additional_dirs.append(relative_plugins_dir)
    uproject["AdditionalPluginDirectories"] = additional_dirs
    with uproject_path.open("w", encoding="utf-8") as stream:
        json.dump(uproject, stream, indent=4)
        stream.write("\n")

    installer = spear_dir / "tools" / "install_plugins_in_external_project.py"
    try:
        run(sys.executable, str(installer), "--external-project-dir", str(project_dir), cwd=spear_dir)
    except subprocess.CalledProcessError as error:
        raise SystemExit(
            "SPEAR 官方安装脚本执行失败。请确认当前 Python 环境已执行 "
            f"`python -m pip install -e {spear_dir / 'python'}`，并重新运行本命令。"
        ) from error

    # The official installer appends the primary module when it creates Source/.
    # Normalize that list so running this wrapper repeatedly is idempotent.
    with uproject_path.open(encoding="utf-8") as stream:
        uproject = json.load(stream)
    modules = []
    seen_module_names = set()
    for module in uproject.get("Modules", []):
        name = module.get("Name")
        if name in seen_module_names:
            continue
        seen_module_names.add(name)
        modules.append(module)
    if "rm_sim_spear" not in seen_module_names:
        modules.append(
            {
                "Name": "rm_sim_spear",
                "Type": "Runtime",
                "LoadingPhase": "Default",
                "AdditionalDependencies": ["SpCore"],
            }
        )
    uproject["Modules"] = modules
    with uproject_path.open("w", encoding="utf-8") as stream:
        json.dump(uproject, stream, indent=4)
        stream.write("\n")
    print(f"已安装 SPEAR {SPEAR_COMMIT} 到 {project_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
