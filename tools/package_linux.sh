#!/usr/bin/env bash
#
# Package ue/rm_sim_spear.uproject into a cooked Linux standalone, so SPEAR can
# drive a shipped build instead of `UnrealEditor -game`.
#
# Why this exists (docs/rendering.md section 12): ~/rm_sim_rune ships exactly
# this shape (package_linux.sh -> /tmp/rm_sim_package/Linux/rm_sim.sh) and it
# delivers the same 1440x1080 single-sample capture at 21.4 fps with a 3.2 GB
# RSS, while our `UnrealEditor -game` path pays for the editor binary, the
# editor world and the editor's runtime shader machinery (11.4 fps). SPEAR
# supports the packaged executable natively: SPEAR.LAUNCH_MODE="game" plus
# SPEAR.INSTANCE.GAME_EXECUTABLE (see ~/spear/python/spear/instance.py:656-760),
# which tools/run_physics_sim.py exposes as `--game-executable`.
#
# Usage:
#   tools/package_linux.sh                    # -build -cook, then stage/pak/archive
#   tools/package_linux.sh --skipbuild        # only cook + stage
#   tools/package_linux.sh --resume           # reuse an existing cook, only stage
#   UE_ENGINE_ROOT=/path/to/UE_5.8 tools/package_linux.sh
#
# Output: ${ARCHIVE_DIR:-build/package}/Linux/rm_sim_spear.sh
#
# The archive is a long-lived deliverable, so it defaults to the repo's
# git-ignored build/ tree rather than /tmp -- a system tmp cleanup deleted a
# finished 1.6 GB package once already (2026-10-06).  RM_SIM_ARCHIVE_DIR
# overrides it; the cooked data itself lives in ue/Saved/Cooked (also durable)
# and only the UAT scratch home stays in /tmp (RM_SIM_BUILD_HOME to move it).
# Our ZenServer's own data dir (the reusable DDC, ~1.7 GB per full cook) is
# durable too: build/zen, RM_SIM_ZEN_DIR to move it.
#
# The Zen server is what has bitten us before: `-NoZenAutoLaunch` makes UAT use
# an already-running server, so if the one this script starts dies halfway the
# cook stops making progress and *looks* alive for hours. We therefore always
# start (and trap-kill) our own server, keep its log, and refuse to start if
# something already owns the port.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${ROOT_DIR}/ue/rm_sim_spear.uproject"
ENGINE_ROOT="${UE_ENGINE_ROOT:-/home/coffee/Linux_Unreal_Engine_5.8.0}"
TEMP_HOME="${RM_SIM_BUILD_HOME:-/tmp/rm_sim_spear_build_home}"
ARCHIVE_DIR="${RM_SIM_ARCHIVE_DIR:-${ROOT_DIR}/build/package}"
# Deliberately NOT 8558: that is the port every UnrealEditor auto-launches its
# own ZenServer on (docs/rendering.md section 12.10), and the port is the one
# resource this script shares with an editor opened from the desktop entry --
# the data dir and the ZenServerLaunch lock are already isolated by TEMP_HOME.
# A foreign server on 8558 makes the editor's own launch fail to bind, which it
# retries for up to 30 s per attempt ("Zen 卡死"); 38958 stays out of the
# 8558-8560 probe range and out of the way of the user's editor.
ZEN_PORT="${RM_SIM_ZEN_PORT:-38958}"
# The Zen data dir is the derived-data cache the cooker reuses across runs (the
# "Compiling global *changed* shaders" pass only recompiles what is missing), so
# it is a long-lived artifact, not scratch: it defaults into the repo's
# git-ignored build/ tree.  A full cook leaves ~1.7 GB here, and /tmp gets wiped
# by the system (2026-10-06: a wipe deleted a finished 1.6 GB package).  Only
# UAT's scratch HOME stays in /tmp.  RM_SIM_ZEN_DIR moves it.
ZEN_DIR="${RM_SIM_ZEN_DIR:-${ROOT_DIR}/build/zen}"
ENGINE_ZEN_SERVER="${ENGINE_ROOT}/Engine/Binaries/Linux/zenserver"
ZEN_SECURITY_CONFIG="${ZEN_DIR}/Install/security-config.json"
ZEN_LOG="${ZEN_DIR}/zen-package.log"
RESUME=0
DO_BUILD=1
DO_COOK=1

for arg in "$@"; do
    case "${arg}" in
        --resume)    RESUME=1; DO_BUILD=0; DO_COOK=0 ;;
        --skipbuild) DO_BUILD=0 ;;
        --skipcook)  DO_COOK=0 ;;
        --force)     FORCE=1 ;;
        -h|--help)   sed -n '2,48p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "Usage: $0 [--resume] [--skipbuild] [--skipcook] [--force]" >&2; exit 2 ;;
    esac
done

[[ -f "${PROJECT}" ]] || { echo "ERROR: missing project ${PROJECT}" >&2; exit 1; }
[[ -x "${ENGINE_ZEN_SERVER}" ]] || {
    echo "ERROR: engine ZenServer missing or not executable: ${ENGINE_ZEN_SERVER}" >&2
    echo "Set UE_ENGINE_ROOT to a valid Unreal Engine installation." >&2
    exit 1
}
command -v jq >/dev/null || { echo "ERROR: jq is required" >&2; exit 1; }

# Two cooks do not fit in 32 GB here: each cooker grows past 6 GB and a full SM6
# shader pass spawns ~29 ShaderCompileWorkers at ~0.5 GB each, which is what
# starves the ZenServer (see the watchdog below). SPEAR also cannot attach to a
# game world while any UnrealEditor* process is alive.
if [[ "${FORCE:-0}" != "1" ]]; then
    # Match the executable name, not the whole command line: ``pgrep -f`` also
    # matches the shell that invoked this script whenever its own arguments
    # happen to mention UnrealEditor, which made the guard refuse to start.
    # Linux truncates comm to 15 characters, so UnrealEditor-Cmd shows up as
    # ``UnrealEditor-Cm``.
    # ``|| true`` keeps ``set -e`` happy: pgrep exits 1 when nothing matches.
    running_ue="$(pgrep -a -x 'UnrealEditor' 2>/dev/null || true; pgrep -a -x 'UnrealEditor-Cm' 2>/dev/null || true)"
    if [[ -n "${running_ue}" ]]; then
        echo "ERROR: another Unreal process is running; cooks compete for RAM and SPEAR cannot attach:" >&2
        echo "${running_ue}" >&2
        echo "Finish or kill it first, or re-run with --force." >&2
        exit 1
    fi
fi

mkdir -p "${TEMP_HOME}/.config" "${TEMP_HOME}/.cache" "${ARCHIVE_DIR}" "${ZEN_DIR}"

# Refuse to reuse a port we do not own: UAT would talk to a server that is not
# ours and silently stop making progress (observed 2026-10-06 with a rune cook).
if ss -ltn 2>/dev/null | grep -q ":${ZEN_PORT} "; then
    echo "ERROR: port ${ZEN_PORT} is already in use; set RM_SIM_ZEN_PORT to a free port" >&2
    exit 1
fi

ZEN_ARGS=(
    --port "${ZEN_PORT}"
    --data-dir "${ZEN_DIR}/Data"
    --http asio
    --quiet
    --owner-pid "$$"
)
if [[ -f "${ZEN_SECURITY_CONFIG}" ]]; then
    ZEN_ARGS+=(--security-config-path "${ZEN_SECURITY_CONFIG}")
fi

ZEN_PID=""
WATCHDOG_PID=""

zen_start() {
    "${ENGINE_ZEN_SERVER}" "${ZEN_ARGS[@]}" >>"${ZEN_LOG}" 2>&1 &
    ZEN_PID=$!
}

# There is no fatal error when Zen dies: the cooker keeps running and keeps
# retrying oplog commits, so the cook looks alive for hours while making no
# progress. It happened twice on 2026-10-06 (this project and ~/rm_sim_rune),
# both times with ~25 GB of 32 GB resident. Restarting it is what un-wedges the
# cook, so do that automatically.
zen_watchdog() {
    while :; do
        sleep 15
        if ! kill -0 "${ZEN_PID}" 2>/dev/null; then
            echo "watchdog $(date -Is): ZenServer ${ZEN_PID} is gone; restarting" >>"${ZEN_LOG}"
            zen_start
        fi
    done
}

zen_start

cleanup() {
    [[ -n "${WATCHDOG_PID}" ]] && kill "${WATCHDOG_PID}" 2>/dev/null || true
    [[ -n "${ZEN_PID}" ]] && kill "${ZEN_PID}" 2>/dev/null || true
    [[ -n "${WATCHDOG_PID}" ]] && wait "${WATCHDOG_PID}" 2>/dev/null || true
    [[ -n "${ZEN_PID}" ]] && wait "${ZEN_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

sleep 3
if ! kill -0 "${ZEN_PID}" 2>/dev/null; then
    echo "ERROR: ZenServer failed to start; see ${ZEN_LOG}" >&2
    exit 1
fi
zen_watchdog &
WATCHDOG_PID=$!
echo "ZenServer pid ${ZEN_PID} on port ${ZEN_PORT}, watchdog pid ${WATCHDOG_PID} (log: ${ZEN_LOG})"

UAT_COMMON_ARGS=(
    BuildCookRun
    "-project=${PROJECT}"
    -noP4
    -platform=Linux
    -clientconfig=Development
    "-NoZenAutoLaunch=127.0.0.1:${ZEN_PORT}"
    -utf8output
    -stdout
)

run_uat() {
    env \
        HOME="${TEMP_HOME}" \
        XDG_CONFIG_HOME="${TEMP_HOME}/.config" \
        XDG_CACHE_HOME="${TEMP_HOME}/.cache" \
        "${ENGINE_ROOT}/Engine/Build/BatchFiles/RunUAT.sh" "${UAT_COMMON_ARGS[@]}" "$@"
}

if [[ ${DO_BUILD} -eq 1 || ${DO_COOK} -eq 1 ]]; then
    BUILD_ARGS=()
    [[ ${DO_BUILD} -eq 1 ]] && BUILD_ARGS+=(-build) || BUILD_ARGS+=(-skipbuild)
    [[ ${DO_COOK} -eq 1 ]]  && BUILD_ARGS+=(-cook)  || BUILD_ARGS+=(-skipcook)
    run_uat "${BUILD_ARGS[@]}"
fi

# UAT names the cook store after the project; do not hardcode it (rune's is
# ue.projectstore, ours may differ).
PROJECT_STORE="$(find "${ROOT_DIR}/ue/Saved/Cooked/Linux" -maxdepth 1 -name '*.projectstore' -print -quit 2>/dev/null || true)"
if [[ -z "${PROJECT_STORE}" || ! -f "${PROJECT_STORE}" ]]; then
    echo "ERROR: no cooked project store under ue/Saved/Cooked/Linux" >&2
    exit 1
fi
PROJECT_STORE_TMP="$(mktemp "${PROJECT_STORE}.XXXXXX")"
jq '.zenserver.hostname = "127.0.0.1"' "${PROJECT_STORE}" >"${PROJECT_STORE_TMP}"
mv "${PROJECT_STORE_TMP}" "${PROJECT_STORE}"
echo "Rewrote .zenserver.hostname in ${PROJECT_STORE}"

run_uat -skipbuild -skipcook -stage -pak -archive "-archivedirectory=${ARCHIVE_DIR}"

echo
echo "Packaged build:"
find "${ARCHIVE_DIR}" -maxdepth 3 -name "*.sh" -print
echo "Drive it with tools/run_physics_sim.py --game-executable <that .sh>"
