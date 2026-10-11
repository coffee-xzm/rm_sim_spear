#!/usr/bin/env python3
"""Draw the SPEAR frame-transaction timeline used by docs/render_budget.md section 14.

The picture answers one question: *when* does the work queued inside
``with instance.begin_frame():`` actually run on the Unreal game thread, and who
waits for whom in between.  It is schematic in the sub-millisecond sense (the
phase widths are rounded) but the ordering and the two synchronization gates are
taken from the engine source and from the traces in ``tmp/perf/j*/``:

- the game thread drains the BeginFrame queue at ``FCoreDelegates::OnBeginFrame``,
  the first statement of ``FEngineLoop::Tick``, and the EndFrame queue at
  ``OnEndFrame``, after every tick and after the render commands were submitted;
- ``WorkQueue::run()`` blocks the game thread until the worker posts ``reset()``,
  which ``engine_service.execute_frame`` does at the end of the begin block;
- ``single_step=True`` pushes the *next* iteration's BeginFrame entry from
  ``end_frame``, so one Python iteration maps onto exactly one engine frame.

Run:

    python3 tools/make_frame_timeline.py --output docs/figures/frame_timeline.png
"""

from __future__ import annotations

import argparse
import pathlib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Rectangle

# The engine frame floor measured in tmp/perf/j1_empty (512x384, triple, no save).
FRAME_MS = 11.4

COLOR_GT = "#cfe3f7"
COLOR_GT_EDGE = "#4a7fb5"
COLOR_WORKER = "#d8ecd4"
COLOR_WORKER_EDGE = "#4f8a4a"
COLOR_WAIT = "#e8e8e8"
COLOR_BEGIN = "#2e8b3d"
COLOR_END = "#8e5aa8"
COLOR_POST = "#2a6fb0"
COLOR_HL = "#f6d98a"


def _box(ax, x, y, w, h, label, facecolor, edgecolor, *, fontsize=9, hatch=None, zorder=3):
    if w <= 0:
        return
    ax.add_patch(Rectangle((x, y), w, h, facecolor=facecolor, edgecolor=edgecolor,
                           linewidth=1.1, hatch=hatch, zorder=zorder))
    if label and w > 1.0:
        ax.text(x + w / 2.0, y + h / 2.0, label, ha="center", va="center",
                fontsize=fontsize, zorder=zorder + 1)


def _arrow(ax, xy_from, xy_to, color, *, style="-|>", rad=0.0, lw=1.4, ls="-"):
    ax.add_patch(FancyArrowPatch(xy_from, xy_to, arrowstyle=style, color=color,
                                 mutation_scale=14, linewidth=lw, linestyle=ls,
                                 connectionstyle=f"arc3,rad={rad}", zorder=6))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=pathlib.Path,
                        default=pathlib.Path("docs/figures/frame_timeline.png"))
    parser.add_argument("--dpi", type=int, default=170)
    args = parser.parse_args()

    plt.rcParams["font.family"] = ["Noto Sans CJK JP", "DejaVu Sans"]

    t_begin_nm1 = 0.0                  # OnBeginFrame(N-1)
    t_end_nm1 = 11.2                   # OnEndFrame(N-1)
    t_begin_n = 11.4                   # OnBeginFrame(N)
    t_end_n = 22.6                     # OnEndFrame(N)
    t_begin_np1 = 22.8                 # OnBeginFrame(N+1)
    span = 25.0

    fig, ax = plt.subplots(figsize=(15.5, 8.4))
    ax.set_xlim(-0.3, span)
    ax.set_ylim(-1.7, 6.2)
    ax.axis("off")

    y_gt, h_gt = 3.70, 1.00
    y_fifo, h_fifo = 2.90, 0.45
    y_wk, h_wk = 1.50, 0.90
    y_img, h_img = 0.40, 0.55

    # ---- frame bands -------------------------------------------------------
    for x0, x1, name in (
        (t_begin_nm1, t_end_nm1, "引擎帧 N-1"),
        (t_begin_n, t_end_n, "引擎帧 N"),
        (t_begin_np1, span, "引擎帧 N+1"),
    ):
        ax.axvspan(x0, x1, color="#f4f6f8", zorder=0)
        ax.text((x0 + x1) / 2.0, 5.28, name, ha="center", va="center",
                fontsize=11, color="#5a6672")

    # ---- frame boundaries --------------------------------------------------
    for x in (t_begin_nm1, t_begin_n, t_begin_np1):
        ax.axvline(x, color=COLOR_BEGIN, linewidth=1.6, zorder=2)
    for x in (t_end_nm1, t_end_n):
        ax.axvline(x, color=COLOR_END, linewidth=1.6, zorder=2)
    for x, txt in ((t_begin_nm1, "N-1"), (t_begin_n, "N"), (t_begin_np1, "N+1")):
        ax.text(x, 5.06, f"↓ begin({txt})", ha="left", va="center", fontsize=8.5,
                color=COLOR_BEGIN, zorder=7)
    for x, txt in ((t_end_nm1, "N-1"), (t_end_n, "N")):
        ax.text(x, 5.06, f"end({txt}) ↑", ha="right", va="center", fontsize=8.5,
                color=COLOR_END, zorder=7)

    # ---- game thread -------------------------------------------------------
    _box(ax, t_begin_nm1 + 0.15, y_gt, (t_end_nm1 - t_begin_nm1) - 0.3, h_gt,
         "world tick + 渲染  帧 N-1", COLOR_GT, COLOR_GT_EDGE, fontsize=10)
    _box(ax, t_begin_n + 0.05, y_gt, 0.55, h_gt, "", COLOR_HL, COLOR_GT_EDGE)
    _box(ax, t_begin_n + 0.75, y_gt, (t_end_n - t_begin_n) - 0.9, h_gt,
         "world tick + 渲染  帧 N", COLOR_GT, COLOR_GT_EDGE, fontsize=10)
    _box(ax, t_end_n + 0.05, y_gt, 0.45, h_gt, "", "#e2d2ee", COLOR_END)
    _box(ax, t_begin_np1 + 0.15, y_gt, span - t_begin_np1 - 0.35, h_gt,
         "world tick + 渲染  帧 N+1", COLOR_GT, COLOR_GT_EDGE, fontsize=10)
    ax.text(-0.35, y_gt + h_gt / 2.0, "游戏线程\n(游戏引擎)", ha="right", va="center",
            fontsize=10.5, color="#2f4f6f")

    ax.text(t_begin_n + 0.45, 4.86,
            "⑥ 抽干 BeginFrame 队列 → 应用 iteration i 的位姿",
            ha="left", va="center", fontsize=9.5, color="#8a6d1f", zorder=7)
    ax.text(t_end_n - 0.25, 4.86, "⑦ 抽干 EndFrame 队列",
            ha="right", va="center", fontsize=9.5, color=COLOR_END, zorder=7)

    # ---- FIFO --------------------------------------------------------------
    ax.text(-0.35, y_fifo + h_fifo / 2.0, "FIFO\n帧事务登记", ha="right", va="center",
            fontsize=10.5, color="#2f4f6f")
    _box(ax, t_begin_nm1 + 0.2, y_fifo, 1.3, h_fifo, "End(i-1)", "#e2d2ee",
         "#9aa7b4", fontsize=8.5)
    _box(ax, t_begin_nm1 + 1.7, y_fifo, 1.4, h_fifo, "Begin(i)", "#d9f0dc",
         "#9aa7b4", fontsize=8.5)
    ax.text(t_begin_n + 0.45, y_fifo + h_fifo + 0.12,
            "游戏线程只在帧边界上看这张表，前半段空着也不看",
            ha="left", va="bottom", fontsize=8.5, color="#7a8794")

    # ---- worker ------------------------------------------------------------
    ax.text(-0.35, y_wk + h_wk / 2.0, "worker 线程\n(你的 Python)", ha="right",
            va="center", fontsize=10.5, color="#2f4f6f")

    a0 = t_begin_nm1 + 0.15
    _box(ax, a0, y_wk, 0.5, h_wk, "①", COLOR_WORKER, COLOR_WORKER_EDGE, fontsize=9)
    _box(ax, a0 + 0.55, y_wk, 1.5, h_wk, "② 下发 13 个位姿", COLOR_WORKER,
         COLOR_WORKER_EDGE, fontsize=9)
    _box(ax, a0 + 2.1, y_wk, 1.3, h_wk, "③ 回读", COLOR_WORKER, COLOR_WORKER_EDGE,
         fontsize=9)
    _box(ax, a0 + 3.45, y_wk, t_end_nm1 - (a0 + 3.45), h_wk,
         "④ execute_frame()：等 End(i-1) 被抽干", COLOR_WAIT, "#9aa7b4",
         fontsize=9, hatch="////")
    _box(ax, t_end_nm1, y_wk, t_begin_n - t_end_nm1, h_wk, "", COLOR_WAIT, "#9aa7b4",
         hatch="////")
    ax.text(t_begin_n, y_wk - 0.16, "⑤ end_frame()", ha="center", va="top",
            fontsize=8.5, color="#5a6672")

    b0 = t_begin_n + 0.15
    _box(ax, b0, y_wk, 0.5, h_wk, "①", COLOR_WORKER, COLOR_WORKER_EDGE, fontsize=9)
    _box(ax, b0 + 0.55, y_wk, 1.5, h_wk, "② 下发位姿 (i+1)", COLOR_WORKER,
         COLOR_WORKER_EDGE, fontsize=9)
    _box(ax, b0 + 2.1, y_wk, 1.3, h_wk, "③ 回读", COLOR_WORKER, COLOR_WORKER_EDGE,
         fontsize=9)
    _box(ax, b0 + 3.45, y_wk, t_end_n - (b0 + 3.45), h_wk,
         "④ execute_frame()：等 End(i) 被抽干", COLOR_WAIT, "#9aa7b4",
         fontsize=9, hatch="////")
    _box(ax, t_end_n, y_wk, t_begin_np1 - t_end_n, h_wk, "", COLOR_WAIT, "#9aa7b4",
         hatch="////")

    # ---- image lane --------------------------------------------------------
    _box(ax, t_begin_nm1, y_img, t_end_nm1 - t_begin_nm1, h_img, "图像 N-1",
         "#f7f3e4", "#c8a83c", fontsize=9.5)
    _box(ax, t_begin_n, y_img, t_end_n - t_begin_n, h_img,
         "图像 N  =  iteration i 的位姿 + physics(i) 的状态", "#fdf2d0", "#c8a83c",
         fontsize=9.5)
    _box(ax, t_begin_np1, y_img, span - t_begin_np1 - 0.3, h_img, "图像 N+1",
         "#f7f3e4", "#c8a83c", fontsize=9.5)

    # ---- coupling arrows ---------------------------------------------------
    _arrow(ax, (a0 + 2.7, y_wk + h_wk), (t_begin_n + 0.32, y_gt), COLOR_POST,
           rad=-0.25)
    _arrow(ax, (t_begin_n + 0.32, y_gt), (t_begin_n + 2.4, y_img + h_img), "#c8a83c",
           rad=0.18)
    _arrow(ax, (a0 + 6.0, y_wk + h_wk), (t_end_nm1, y_gt + 0.05), "#9aa7b4",
           style="-|>", lw=1.1, rad=-0.32, ls=(0, (4, 3)))
    _arrow(ax, (t_end_nm1 + 0.15, y_wk + h_wk), (t_begin_n + 0.32, y_gt),
           "#9aa7b4", style="-|>", lw=1.1, rad=0.28, ls=(0, (4, 3)))

    # ---- headline / notes / legend ----------------------------------------
    ax.text(0.0, 5.95,
            "一个 Python 迭代 = 一个引擎帧：worker 在帧 N-1 里把位姿排进队列，游戏线程在帧 N 开头应用（⑥）、"
            "帧 N 末尾收尾（⑦），帧 N 渲染出来的就是这个位姿。",
            ha="left", va="center", fontsize=10, color="#22303c")
    ax.text(0.0, 5.62,
            f"worker 自己只干 ~4 ms（① physics 0.4 + ② 位姿 1.7 + ③ 回读 0.5），其余时间都花在 ④⑤ 两个同步门上等帧边界；"
            f"实测帧周期 {FRAME_MS} ms 就是引擎帧本身。",
            ha="left", va="center", fontsize=10, color="#5a6672")
    ax.text(0.0, 0.02,
            "④ 等的是上一次迭代的 EndFrame 被抽干（发生在 end(N-1)）；⑤ 等的是本次迭代的 BeginFrame 被抽干（发生在 begin(N)）。"
            "这两个门就是 trace.csv 里的 begin_ms 和 end_ms：空事务/直接调用下 begin 10.5 + end 0.2 ≈ 1 帧。",
            ha="left", va="center", fontsize=9.5, color="#5a6672")
    ax.text(0.0, -0.42,
            "为什么不能在调用的一瞬间就执行：只有游戏线程能改 Actor 状态，而它此刻正在 tick/渲染中间，"
            "帧边界是唯一安全且可复现的插入点；一次事务的所有调用也因此作为一批原子生效，渲染看不到“改了一半”的帧。",
            ha="left", va="center", fontsize=9.5, color="#5a6672")

    for x, y, label, color in ((0.0, -1.15, "worker 干活", COLOR_WORKER),
                               (3.4, -1.15, "worker 阻塞等帧边界", COLOR_WAIT),
                               (9.4, -1.15, "游戏线程 tick+渲染", COLOR_GT),
                               (15.8, -1.15, "帧边界上的队列抽干", COLOR_HL)):
        ax.add_patch(Rectangle((x, y), 0.55, 0.32, facecolor=color, edgecolor="#9aa7b4",
                               zorder=3))
        ax.text(x + 0.72, y + 0.16, label, ha="left", va="center", fontsize=9,
                color="#5a6672")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(args.output, dpi=args.dpi, facecolor="white")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
