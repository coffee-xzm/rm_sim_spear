#!/usr/bin/env bash
#
# Watch the robot move, from a camera you fly.
#
# This drives tools/run_physics_sim.py --interactive, i.e. the real simulation
# loop: physics steps every frame, and the capture camera is steered by the
# keyboard instead of by --camera-standoff-cm.  For a frozen pose (binding and
# self-intersection checks) use tools/test_urdf_views.py --continuous instead;
# that one never calls physics.step().
#
# Two audiences, two display paths -- one entry point so nobody has to remember
# the difference (docs/urdf_workflow.md "自由相机：人看 :1，agent 跑无头"):
#
#   tools/view_pose.sh                # VISIBLE: real X display, OpenCV window
#   tools/view_pose.sh --headless     # HEADLESS: Xvfb, bounded run, PNGs on disk
#
# The split is not fussiness: the camera is driven by keyboard events read from
# the OpenCV window, so that window has to be on a display the operator can see
# and focus.  A `--display`-style window under xvfb renders fine and receives no
# keys at all.  Agents run headless (saved frames and trace.csv are their
# evidence) and leave the real display to whoever is developing.
#
# Keys, once the OpenCV window has focus:
#   W/S forward/back   A/D strafe   E/Q up/down
#   I/K pitch          J/L yaw
#   X or Esc quit      Ctrl-C also works
#
# Everything that is not a flag below is forwarded verbatim to
# tools/run_physics_sim.py, e.g.:
#   tools/view_pose.sh --drive-wheels 0            # let it just stand there
#   tools/view_pose.sh --settle-frames 200         # settle longer before driving
#   tools/view_pose.sh --width 1440 --height 1080
#   tools/view_pose.sh --view main                 # viewport-aligned instead of fixed
#   tools/view_pose.sh --camera-speed-cm 10 --camera-turn-deg 2
#
# This script never kills Unreal processes.  If one is already alive SPEAR would
# attach to the wrong RPC server, so it refuses and prints what is running; stop
# it yourself, or forward --kill-stale-editor to let the Python tool do a
# project-scoped SIGKILL (tools/spear_user_config.yaml sets LAUNCH_MODE
# "editor", so the scope is ue/rm_sim_spear.uproject, not your other work).
#
# Env overrides: RM_SIM_PYTHON, RM_SIM_DISPLAY (visible, default :1),
# RM_SIM_XVFB_DISPLAY (headless, default :99).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${RM_SIM_PYTHON:-/home/coffee/miniconda3/envs/spear-env/bin/python}"
VISIBLE_DISPLAY="${RM_SIM_DISPLAY:-:1}"
XVFB_DISPLAY="${RM_SIM_XVFB_DISPLAY:-:99}"
HEADLESS=0
PASSTHROUGH=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --headless) HEADLESS=1; shift ;;
        --screen)   VISIBLE_DISPLAY="${2:?--screen needs a value, e.g. :1}"; shift 2 ;;
        -h|--help)  sed -n '2,43p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)          PASSTHROUGH+=("$1"); shift ;;
    esac
done

[[ -x "${PYTHON}" ]] || {
    echo "ERROR: python not executable: ${PYTHON} (RM_SIM_PYTHON overrides)" >&2
    exit 1
}

# A stale UnrealEditor is the one failure worth failing fast on: SPEAR attaches
# to whichever RPC server is already listening rather than starting a second one.
running_ue="$(pgrep -a -x 'UnrealEditor' 2>/dev/null || true; pgrep -a -x 'UnrealEditor-Cm' 2>/dev/null || true)"
if [[ -n "${running_ue}" ]]; then
    echo "ERROR: an Unreal process is already running; SPEAR would attach to it:" >&2
    echo "${running_ue}" >&2
    echo "Stop it first, or forward --kill-stale-editor for a project-scoped SIGKILL." >&2
    exit 1
fi

# Visible mode flies an unbounded robot; headless mode gets a frame budget so it
# terminates without a keyboard, and reads the pixels back off disk afterwards.
# Both defaults are overridable because the passthrough comes last.
MODE_ARGS=()
if [[ "${HEADLESS}" -eq 1 ]]; then
    MODE_ARGS=(--frames 120 --save-images sync)
else
    MODE_ARGS=(--interactive)
fi

ARGS=(
    "${ROOT_DIR}/tools/run_physics_sim.py"
    --user-config "${ROOT_DIR}/tools/spear_user_config.yaml"
    --plan "${ROOT_DIR}/generated/urdf/总装/scene_binding_plan.json"
    --model "${ROOT_DIR}/generated/urdf/总装/mujoco.urdf"
    --view fixed
    --width 1024 --height 768
    "${MODE_ARGS[@]}"
    "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}"
)

cd "${ROOT_DIR}"
if [[ "${HEADLESS}" -eq 1 ]]; then
    # `-n` rather than `-a`: docs/risks.md records `xvfb-run -a` losing its X
    # server mid-launch, which surfaces as a fatal X11 IO error inside UE.
    echo "headless: Xvfb ${XVFB_DISPLAY}, no window (frames + trace.csv to --output-dir)"
    exec xvfb-run -n "${XVFB_DISPLAY#:}" env SDL_VIDEODRIVER=x11 \
        "${PYTHON}" -u "${ARGS[@]}"
else
    echo "visible on DISPLAY=${VISIBLE_DISPLAY}; focus the OpenCV window for the keys"
    exec env DISPLAY="${VISIBLE_DISPLAY}" SDL_VIDEODRIVER=x11 \
        "${PYTHON}" -u "${ARGS[@]}"
fi
