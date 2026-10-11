"""VRAM budget checks, sampling, and Unreal crash diagnosis.

The 8 GiB laptop GPU in this project cannot hold the editor viewport, the PIE
world, the merged CAD scene and two SceneCapture targets at once.  UE does not
degrade gracefully here: a failed transient allocation on the render thread is
a hard crash (``VulkanMemory.cpp``), and the Python side only observes a later
``rpc::timeout``.  These helpers exist so a tool can *predict* that failure
instead of describing it afterwards.

Nothing in this module talks to SPEAR; it only reads ``nvidia-smi``, Unreal log
text and process state, so it can be unit tested offline.
"""

from __future__ import annotations

import csv
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "LOW_VRAM_CVARS",
    "CrashSignature",
    "DeviceHeap",
    "MemoryBudget",
    "PreflightDecision",
    "UeLogWatcher",
    "VramConsumer",
    "VramSampler",
    "StaleProcess",
    "VramSnapshot",
    "crash_signature_names",
    "device_local_budget_mb",
    "evaluate_preflight",
    "find_crash_signatures",
    "find_ue_processes",
    "largest_failed_allocation_bytes",
    "kill_processes",
    "low_vram_ini_args",
    "parse_device_memory_heaps",
    "parse_memory_budget",
    "query_vram",
    "query_vram_processes",
    "resolve_ue_log_path",
]


#
# Renderer preset for a memory-constrained run.
#
# DefaultEngine.ini enables Lumen, ray tracing, virtual shadow maps and mesh
# distance fields for the whole merged CAD map.  Measured runs show the
# device-local heap sitting within ~100 MiB of the driver budget, so the
# renderer footprint, not the SceneCapture targets, decides success.
#
# The overrides travel as ``-ini:Engine:[SystemSettings]:<cvar>=<value>``
# command-line args, which the engine applies while loading config, i.e.
# before the default map is loaded.  ``-ExecCmds`` is NOT early enough: a run
# log shows the engine executing it after ``MAP LOAD FILE=...creo_stp.umap``,
# so the map-load peak is unaffected.
#
LOW_VRAM_CVARS: tuple[tuple[str, object], ...] = (
    ("r.RayTracing", 0),
    ("r.DynamicGlobalIlluminationMethod", 0),
    ("r.ReflectionMethod", 0),
    ("r.Shadow.Virtual.Enable", 0),
    ("r.GenerateMeshDistanceFields", 0),
    ("r.Lumen.DiffuseIndirect.Allow", 0),
    ("r.LumenScene.SurfaceCache.AtlasSize", 1024),
)

# Two levers used to sit in the tuple above and were removed on 2026-10-08
# because neither paid for itself:
#
#   ("r.Streaming.PoolSize", 512), ("r.Streaming.LimitPoolSizeToVRAM", 1)
#       -3 MiB against the project default of 1000 MB (docs/render_budget.md
#       section 3.2), while pinning thin lines and alpha-tested detail to a low
#       mip, which is one of the documented causes of dashed geometry.
#
#   ("r.ScreenPercentage", 50)
#       Never reaches a SceneCapture: the engine hands those
#       FLegacyScreenPercentageDriver(1.0) and ignores the cvar
#       (docs/urdf_workflow.md, docs/risks.md).
#
# The remaining entries are the ones that actually move the peak; keep them.


def low_vram_ini_args(
    cvars: tuple[tuple[str, object], ...] = LOW_VRAM_CVARS,
) -> dict[str, object]:
    """Render LOW_VRAM_CVARS as UE ``-ini:Engine:[SystemSettings]:...`` args."""
    return {f"ini:Engine:[SystemSettings]:{name}": value for name, value in cvars}


#
# nvidia-smi
#


@dataclass(frozen=True)
class VramSnapshot:
    name: str
    total_mb: int
    used_mb: int
    free_mb: int

    def as_dict(self) -> dict[str, object]:
        return {
            "gpu": self.name,
            "total_mb": self.total_mb,
            "used_mb": self.used_mb,
            "free_mb": self.free_mb,
        }


@dataclass(frozen=True)
class VramConsumer:
    pid: int
    process_name: str
    used_mb: int


def query_vram(timeout_seconds: float = 5.0) -> VramSnapshot | None:
    """Return current GPU memory usage, or None when nvidia-smi is unusable."""
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    line = completed.stdout.strip().splitlines()
    if not line:
        return None
    parts = [part.strip() for part in line[0].split(",")]
    if len(parts) < 4:
        return None
    try:
        total, used, free = (int(float(parts[1])), int(float(parts[2])), int(float(parts[3])))
    except ValueError:
        return None
    return VramSnapshot(name=parts[0], total_mb=total, used_mb=used, free_mb=free)


def query_vram_processes(timeout_seconds: float = 5.0) -> tuple[VramConsumer, ...]:
    """Return per-process GPU memory usage, largest first (best effort)."""
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if completed.returncode != 0:
        return ()
    consumers: list[VramConsumer] = []
    for line in completed.stdout.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            consumers.append(
                VramConsumer(pid=int(parts[0]), process_name=parts[1], used_mb=int(float(parts[2])))
            )
        except ValueError:
            continue
    consumers.sort(key=lambda consumer: consumer.used_mb, reverse=True)
    return tuple(consumers)


@dataclass(frozen=True)
class StaleProcess:
    """A running Unreal process that a new launch could collide with."""

    pid: int
    cmdline: str

    def as_dict(self) -> dict[str, object]:
        return {"pid": self.pid, "cmdline": self.cmdline}


def find_ue_processes(
    project_file: str | os.PathLike[str] | None = None,
    process_iter: object | None = None,
) -> tuple[StaleProcess, ...]:
    """Find running UnrealEditor processes, optionally for one project.

    SPEAR's launcher connects to whichever server is already listening, so a
    process left behind by a crashed run makes the next launch attach to the
    wrong instance (observed as a PID-mismatch assertion) while still holding
    gigabytes of VRAM.  ``process_iter`` is injectable for offline tests.
    """
    if process_iter is None:
        try:
            import psutil
        except ImportError:
            return ()
        process_iter = psutil.process_iter
    wanted = None if project_file is None else str(project_file)
    found: list[StaleProcess] = []
    try:
        iterator = process_iter(["pid", "name", "cmdline"])
        for process in iterator:
            try:
                info = process.info
                name = info.get("name") or ""
                if "UnrealEditor" not in name:
                    continue
                cmdline = " ".join(info.get("cmdline") or [])
                if wanted is not None and wanted not in cmdline:
                    continue
                found.append(StaleProcess(pid=int(info["pid"]), cmdline=cmdline))
            except Exception:  # noqa: BLE001 - process may exit while iterating
                continue
    except Exception:  # noqa: BLE001 - a process-table race must not break the run
        pass
    return tuple(found)


def kill_processes(pids: tuple[int, ...] | list[int], timeout_seconds: float = 10.0) -> tuple[int, ...]:
    """SIGKILL the given PIDs, returning those still alive afterwards."""
    import signal

    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            continue
    if not pids:
        return ()
    deadline = time.monotonic() + timeout_seconds
    alive = list(pids)
    while alive and time.monotonic() < deadline:
        alive = [pid for pid in alive if os.path.exists(f"/proc/{pid}")]
        if alive:
            time.sleep(0.1)
    return tuple(alive)


#
# Unreal log parsing
#


@dataclass(frozen=True)
class DeviceHeap:
    """One line of the engine's on-failure ``Device Memory`` dump."""

    index: int
    size_mb: float
    used_mb: float
    used_percent: float
    device_local: bool
    primary: bool


@dataclass(frozen=True)
class MemoryBudget:
    """One row of the engine's ``Memory Budget Extension`` table."""

    heap_index: int
    usage_percent: float
    usage_mb: float
    budget_mb: float
    size_mb: float


_HEAP_RE = re.compile(
    r"^(?:.*?)(?P<index>\d+):\s*Flags\s+0x[0-9a-fA-F]+\s*-\s*Size\s+(?P<size_bytes>\d+)\s*"
    r"\((?P<size>[\d.]+)\s*MB\)\s*-\s*Used\s+(?P<used_bytes>\d+)\s*"
    r"\((?P<used_pct>[\d.]+)%\)(?P<flags>.*)$"
)
_BUDGET_RE = re.compile(
    r"^(?:.*?)HEAP\s+(?P<index>\d+)\s*\|\s*(?P<usage_pct>[\d.]+)%\s*/\s*(?P<usage>[\d.]+)\s*MB\s*"
    r"\|\s*(?P<budget>[\d.]+)\s*MB\s*\|\s*(?P<size>[\d.]+)\s*MB\s*\|"
)
_OOM_RE = re.compile(r"Out Of Memory, trying to allocate (?P<bytes>\d+) bytes")
_ALLOC_RE = re.compile(
    r"Failed to allocate Device Memory, Requested=(?P<kb>[\d.]+)Kb MemTypeIndex=(?P<type>\d+)"
)
_VULKAN_OOM_RE = re.compile(
    r"Out of memory on Vulkan; MemoryTypeIndex=(?P<type>\d+), AllocSize=(?P<mb>[\d.]+)MB"
)
_FATAL_RE = re.compile(r"Fatal error: \[File:(?P<file>[^\]]+)\] \[Line: (?P<line>\d+)\]")
_BREADCRUMB_RE = re.compile(r"Breadcrumbs '(?P<name>[^']+)'")
_X11_FATAL_RE = re.compile(r"Fatal X11 IO Error(?:: (?P<detail>.*))?")
_INIT_FATAL_RE = re.compile(r"LogInit: Error: Fatal (?P<detail>.*)")


@dataclass(frozen=True)
class CrashSignature:
    """A diagnosed failure mode, with the evidence that identified it."""

    name: str
    detail: str
    line_number: int

    def as_dict(self) -> dict[str, object]:
        return {"signature": self.name, "detail": self.detail, "line": self.line_number}


def parse_device_memory_heaps(text: str) -> tuple[DeviceHeap, ...]:
    heaps: list[DeviceHeap] = []
    for line in text.splitlines():
        match = _HEAP_RE.match(line)
        if match is None:
            continue
        flags = match.group("flags")
        heaps.append(
            DeviceHeap(
                index=int(match.group("index")),
                size_mb=float(match.group("size")),
                used_mb=round(int(match.group("used_bytes")) / (1024 * 1024), 2),
                used_percent=float(match.group("used_pct")),
                device_local="DeviceLocal" in flags,
                primary="PrimaryHeap" in flags,
            )
        )
    return tuple(heaps)


def parse_memory_budget(text: str) -> tuple[MemoryBudget, ...]:
    rows: list[MemoryBudget] = []
    for line in text.splitlines():
        match = _BUDGET_RE.match(line)
        if match is None:
            continue
        rows.append(
            MemoryBudget(
                heap_index=int(match.group("index")),
                usage_percent=float(match.group("usage_pct")),
                usage_mb=float(match.group("usage")),
                budget_mb=float(match.group("budget")),
                size_mb=float(match.group("size")),
            )
        )
    return tuple(rows)


def device_local_budget_mb(text: str) -> float | None:
    """Smallest device-local budget reported by the engine, in MiB.

    The primary heap's driver budget is what the renderer can actually use; it
    is well below the heap size on this laptop GPU because the desktop session
    shares the card.
    """
    device_local_indexes = {
        heap.index for heap in parse_device_memory_heaps(text) if heap.device_local
    }
    budgets = [
        row.budget_mb for row in parse_memory_budget(text) if row.heap_index in device_local_indexes
    ]
    if budgets:
        return min(budgets)
    rows = parse_memory_budget(text)
    if rows:
        return rows[0].budget_mb
    heaps = [heap for heap in parse_device_memory_heaps(text) if heap.device_local]
    if heaps:
        return heaps[0].size_mb
    return None


def find_crash_signatures(text: str) -> tuple[CrashSignature, ...]:
    """Classify a UE log tail into the failure modes this project cares about."""
    signatures: list[CrashSignature] = []
    seen: set[str] = set()

    def add(name: str, detail: str, line_number: int) -> None:
        if name in seen:
            return
        seen.add(name)
        signatures.append(CrashSignature(name=name, detail=detail, line_number=line_number))

    for line_number, line in enumerate(text.splitlines(), start=1):
        match = _OOM_RE.search(line)
        if match is not None:
            add(
                "vulkan_transient_oom",
                f"transient allocation of {int(match.group('bytes'))} bytes failed",
                line_number,
            )
            continue
        match = _VULKAN_OOM_RE.search(line)
        if match is not None:
            add(
                "vulkan_oom",
                f"memory type {match.group('type')} exhausted at {match.group('mb')} MB",
                line_number,
            )
            continue
        match = _ALLOC_RE.search(line)
        if match is not None:
            add(
                "vulkan_alloc_failed",
                f"device memory request of {match.group('kb')} KB failed (type {match.group('type')})",
                line_number,
            )
            continue
        match = _FATAL_RE.search(line)
        if match is not None:
            add(
                "fatal_error",
                f"{os.path.basename(match.group('file'))}:{match.group('line')}",
                line_number,
            )
            continue
        match = _X11_FATAL_RE.search(line)
        if match is not None:
            add("x11_fatal", (match.group("detail") or "X server connection lost"), line_number)
            continue
        match = _INIT_FATAL_RE.search(line)
        if match is not None:
            add("init_fatal", match.group("detail"), line_number)
            continue
        if "=== Critical error: ===" in line:
            add("critical_error", "engine reported a critical error", line_number)
            continue
        match = _BREADCRUMB_RE.search(line)
        if match is not None:
            add("breadcrumbs", match.group("name"), line_number)
    return tuple(signatures)


def largest_failed_allocation_bytes(text: str) -> int | None:
    """Largest allocation the engine reported as failing, in bytes."""
    sizes = [int(match.group("bytes")) for match in _OOM_RE.finditer(text)]
    sizes.extend(int(float(match.group("mb")) * 1024 * 1024) for match in _VULKAN_OOM_RE.finditer(text))
    return max(sizes) if sizes else None


#
# Preflight
#


@dataclass(frozen=True)
class PreflightDecision:
    ok: bool
    reason: str
    snapshot: VramSnapshot | None
    consumers: tuple[VramConsumer, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "vram": None if self.snapshot is None else self.snapshot.as_dict(),
            "consumers": [
                {"pid": c.pid, "name": c.process_name, "used_mb": c.used_mb} for c in self.consumers
            ],
        }


def evaluate_preflight(
    snapshot: VramSnapshot | None,
    min_free_mb: int,
    consumers: tuple[VramConsumer, ...] = (),
) -> PreflightDecision:
    """Decide whether it is safe to launch Unreal at all.

    ``nvidia-smi`` being unavailable is a warning, not a failure: the run may
    legitimately target a machine where only the in-engine budget matters.
    """
    if snapshot is None:
        return PreflightDecision(
            ok=True,
            reason="nvidia-smi unavailable; skipping the free-VRAM gate",
            snapshot=None,
        )
    if snapshot.free_mb < min_free_mb:
        return PreflightDecision(
            ok=False,
            reason=(
                f"only {snapshot.free_mb} MiB free of {snapshot.total_mb} MiB, "
                f"below the {min_free_mb} MiB gate"
            ),
            snapshot=snapshot,
            consumers=consumers,
        )
    return PreflightDecision(
        ok=True,
        reason=f"{snapshot.free_mb} MiB free of {snapshot.total_mb} MiB",
        snapshot=snapshot,
        consumers=consumers,
    )


def format_preflight(decision: PreflightDecision) -> str:
    lines = [f"preflight: {decision.reason}"]
    for consumer in decision.consumers[:5]:
        lines.append(f"  gpu consumer pid={consumer.pid} {consumer.process_name} {consumer.used_mb} MiB")
    return "\n".join(lines)


#
# Sampling
#


@dataclass
class VramSampler:
    """Sample ``nvidia-smi`` in the background and keep the peak.

    Sampling is cheap relative to the failure it explains, and the peak is the
    only number that matters: the engine dies on a single 256 MiB request, not
    on an average.  Rows are flushed as they are taken, because the interesting
    case is a run that never reaches a clean shutdown.
    """

    csv_path: Path | None = None
    poll_seconds: float = 1.0
    samples: list[tuple[float, int, int]] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _handle: object | None = field(default=None, repr=False)

    def start(self) -> None:
        if self.poll_seconds <= 0:
            return
        self._open_csv()
        self._thread = threading.Thread(target=self._run, name="vram-sampler", daemon=True)
        self._thread.start()

    def _open_csv(self) -> None:
        if self.csv_path is None:
            return
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.csv_path.open("w", newline="", encoding="utf-8")
        writer = csv.writer(handle)
        writer.writerow(["time", "used_mb", "free_mb"])
        handle.flush()
        self._handle = handle

    def _run(self) -> None:
        while not self._stop.is_set():
            snapshot = query_vram()
            if snapshot is not None:
                timestamp = time.time()
                with self._lock:
                    self.samples.append((timestamp, snapshot.used_mb, snapshot.free_mb))
                    self._append_csv(timestamp, snapshot.used_mb, snapshot.free_mb)
            self._stop.wait(self.poll_seconds)

    def _append_csv(self, timestamp: float, used_mb: int, free_mb: int) -> None:
        if self._handle is None:
            return
        csv.writer(self._handle).writerow([f"{timestamp:.3f}", used_mb, free_mb])
        self._handle.flush()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    @property
    def peak_used_mb(self) -> int | None:
        with self._lock:
            if not self.samples:
                return None
            return max(used_mb for _, used_mb, _ in self.samples)

    @property
    def min_free_mb(self) -> int | None:
        """Smallest free-VRAM reading seen during the run.

        This is the number that decides whether a large contiguous allocation
        still fits; a run whose minimum sits within a few MiB of zero is one
        unlucky desktop frame away from the VulkanMemory crash.
        """
        with self._lock:
            if not self.samples:
                return None
            return min(free_mb for _, _, free_mb in self.samples)

    def summary(self) -> dict[str, object]:
        return {
            "samples": len(self.samples),
            "peak_used_mb": self.peak_used_mb,
            "min_free_mb": self.min_free_mb,
            "csv": None if self.csv_path is None else str(self.csv_path),
        }


#
# Unreal log watching
#


def resolve_ue_log_path(
    project_file: str | os.PathLike[str] | None = None,
    explicit: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Locate the Unreal log for an editor launch.

    ``-abslog`` is not used by default, so the log lives next to the project
    file in ``Saved/Logs/<project>.log``.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    if project_file is None:
        return None
    project = Path(project_file).expanduser()
    return project.parent / "Saved" / "Logs" / f"{project.stem}.log"


@dataclass
class UeLogWatcher:
    """Tail the Unreal log and report the failure signature as soon as it lands.

    The engine dies on the render thread; the Python client keeps waiting for
    an RPC reply that will never come.  Watching the log is what turns a
    30-second timeout into an immediate, actionable diagnosis.
    """

    log_path: Path | None
    poll_seconds: float = 1.0
    tail_lines: int = 400
    on_failure: object | None = None
    process_probe: object | None = None
    signatures: tuple[CrashSignature, ...] = ()
    tail: tuple[str, ...] = ()
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _armed: bool = field(default=True, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _offset: int = field(default=0, repr=False)

    def start(self) -> None:
        if self.log_path is None:
            return
        # Record the end of the existing log synchronously: anything the engine
        # writes after this call must be seen, even if it crashes immediately.
        try:
            self._offset = self.log_path.stat().st_size if self.log_path.exists() else 0
        except OSError:
            self._offset = 0
        self._thread = threading.Thread(target=self._run, name="ue-log-watcher", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        assert self.log_path is not None
        while not self._stop.wait(self.poll_seconds):
            if self._armed and self.process_probe is not None:
                # A dead engine is the most common reason the client sees an
                # RPC timeout; detect it without waiting for a log line.
                try:
                    alive = bool(self.process_probe())
                except Exception:  # noqa: BLE001 - diagnostics only
                    alive = True
                if not alive:
                    with self._lock:
                        self.signatures = (
                            CrashSignature(
                                name="engine_process_exited",
                                detail="Unreal process is no longer running",
                                line_number=0,
                            ),
                        ) + tuple(
                            signature
                            for signature in self.signatures
                            if signature.name != "engine_process_exited"
                        )
                    if self.on_failure is not None:
                        self.on_failure(self.failure)
                    self._armed = False
                    return
            try:
                size = self.log_path.stat().st_size
                if size < self._offset:
                    # The engine rotated the log file; read the new one fully.
                    self._offset = 0
                with self.log_path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(self._offset)
                    chunk = handle.read()
                    self._offset = handle.tell()
            except OSError:
                continue
            if not chunk:
                continue
            with self._lock:
                text = "\n".join(self.tail) + "\n" + chunk
                self.tail = tuple(text.splitlines()[-self.tail_lines :])
                self.signatures = find_crash_signatures("\n".join(self.tail))
            if self._armed and self.failure is not None and self.on_failure is not None:
                self.on_failure(self.failure)

    def disarm(self) -> None:
        self._armed = False

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    @property
    def failure(self) -> CrashSignature | None:
        fatal_names = {
            "vulkan_transient_oom",
            "vulkan_oom",
            "vulkan_alloc_failed",
            "fatal_error",
            "critical_error",
            "init_fatal",
            "x11_fatal",
            "engine_process_exited",
        }
        with self._lock:
            for signature in self.signatures:
                if signature.name in fatal_names:
                    return signature
        return None

    def tail_text(self) -> str:
        with self._lock:
            return "\n".join(self.tail[-60:])
