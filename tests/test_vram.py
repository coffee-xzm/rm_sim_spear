import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from rm_sim_spear import vram


HEAP_DUMP = """\
LogVulkanRHI: Display: 2 Device Memory Heaps:
LogVulkanRHI: Display:   0: Flags 0x1 - Size 8585740288 (8188.00 MB) - Used 6432086272 (74.92%) - DeviceLocal - PrimaryHeap
LogVulkanRHI: Display:   1: Flags 0x0 - Size 24967756800 (23811.11 MB) - Used 1032755184 (4.14%)
LogVulkanRHI: Display: Memory Budget Extension:
LogVulkanRHI: Display: \t         | Usage                     | Budget          | Size            |
LogVulkanRHI: Display: \t---------|------------------------------------------------------------------|
LogVulkanRHI: Display: \t HEAP 00 |   0.21% /         17.38 MB |       6410.81 MB |       8188.00 MB |
LogVulkanRHI: Display: \t HEAP 01 |   0.18% /         43.29 MB |      23811.11 MB |      23811.11 MB |
LogVulkanRHI: Display: \t HEAP 00 |  68.23% /       5586.69 MB |       6358.19 MB |       8188.00 MB |
"""

X11_DUMP = """\
LogInit: Error: Fatal X11 IO Error: Lost connection to X Server
LogCore: FUnixPlatformMisc::RequestExit(1, <NoCallSiteInfo>)
"""

CRASH_DUMP = """\
LogVulkanRHI: Warning: Failed to allocate Device Memory, Requested=65536.00Kb MemTypeIndex=1
LogVulkanRHI: Warning: Out of memory on Vulkan; MemoryTypeIndex=1, AllocSize=256.000MB
LogCore: === Critical error: ===
LogCore: Fatal error: [File:./Runtime/VulkanRHI/Private/VulkanMemory.cpp] [Line: 3114]
Out Of Memory, trying to allocate 268435456 bytes
LogRHI: Error: Breadcrumbs 'RDGExecute_RenderThread'
"""


class DeviceMemoryParsingTests(unittest.TestCase):
    def test_heaps_keep_device_local_and_primary_flags(self):
        heaps = vram.parse_device_memory_heaps(HEAP_DUMP)
        self.assertEqual([heap.index for heap in heaps], [0, 1])
        primary, host_visible = heaps
        self.assertTrue(primary.device_local)
        self.assertTrue(primary.primary)
        self.assertEqual(primary.size_mb, 8188.0)
        self.assertEqual(primary.used_mb, 6134.12)
        self.assertAlmostEqual(primary.used_percent, 74.92)
        self.assertFalse(host_visible.device_local)

    def test_budget_rows_are_parsed(self):
        rows = vram.parse_memory_budget(HEAP_DUMP)
        self.assertEqual([row.heap_index for row in rows], [0, 1, 0])
        self.assertEqual(rows[0].budget_mb, 6410.81)
        self.assertEqual(rows[2].usage_mb, 5586.69)

    def test_device_local_budget_ignores_host_visible_heap(self):
        # 23811 MiB is the host-visible heap and must never be mistaken for the
        # budget of the 8 GiB card.
        self.assertEqual(vram.device_local_budget_mb(HEAP_DUMP), 6358.19)

    def test_device_local_budget_is_none_without_evidence(self):
        self.assertIsNone(vram.device_local_budget_mb("nothing to see here"))


class CrashSignatureTests(unittest.TestCase):
    def test_all_failure_modes_are_classified(self):
        names = [signature.name for signature in vram.find_crash_signatures(CRASH_DUMP)]
        self.assertEqual(
            names,
            [
                "vulkan_alloc_failed",
                "vulkan_oom",
                "critical_error",
                "fatal_error",
                "vulkan_transient_oom",
                "breadcrumbs",
            ],
        )

    def test_signatures_are_unique_and_carry_evidence(self):
        signatures = vram.find_crash_signatures(CRASH_DUMP + CRASH_DUMP)
        self.assertEqual(len(signatures), len(set(s.name for s in signatures)))
        fatal = next(s for s in signatures if s.name == "fatal_error")
        self.assertEqual(fatal.detail, "VulkanMemory.cpp:3114")

    def test_x11_loss_is_a_fatal_signature(self):
        # Seen live: xvfb-run's X server died mid-startup, the engine exited,
        # and the Python client sat in its connect loop with no diagnosis.
        names = [signature.name for signature in vram.find_crash_signatures(X11_DUMP)]
        self.assertEqual(names, ["x11_fatal"])

    def test_largest_failed_allocation_reports_the_transient_heap_request(self):
        self.assertEqual(vram.largest_failed_allocation_bytes(CRASH_DUMP), 268435456)
        self.assertIsNone(vram.largest_failed_allocation_bytes("all good"))


class PreflightTests(unittest.TestCase):
    def test_unavailable_nvidia_smi_does_not_block_the_run(self):
        decision = vram.evaluate_preflight(None, min_free_mb=1200)
        self.assertTrue(decision.ok)
        self.assertIn("nvidia-smi unavailable", decision.reason)

    def test_contended_gpu_is_rejected_with_its_consumers(self):
        snapshot = vram.VramSnapshot(name="GPU", total_mb=8188, used_mb=7719, free_mb=63)
        consumers = (vram.VramConsumer(pid=13612, process_name="SpearSim", used_mb=5822),)
        decision = vram.evaluate_preflight(snapshot, min_free_mb=1200, consumers=consumers)
        self.assertFalse(decision.ok)
        self.assertIn("63 MiB free", decision.reason)
        self.assertIn("SpearSim", vram.format_preflight(decision))

    def test_free_gpu_passes(self):
        snapshot = vram.VramSnapshot(name="GPU", total_mb=8188, used_mb=983, free_mb=6749)
        self.assertTrue(vram.evaluate_preflight(snapshot, min_free_mb=1200).ok)


class LowVramPresetTests(unittest.TestCase):
    def test_preset_covers_every_expensive_default_engine_setting(self):
        # DefaultEngine.ini turns these on for the whole merged CAD map; the
        # map is loaded before any in-PIE cvar can be set, so each one needs a
        # config-time counterpart.
        preset = vram.low_vram_ini_args()
        for name in (
            "r.RayTracing",
            "r.DynamicGlobalIlluminationMethod",
            "r.ReflectionMethod",
            "r.Shadow.Virtual.Enable",
            "r.GenerateMeshDistanceFields",
        ):
            self.assertEqual(preset[f"ini:Engine:[SystemSettings]:{name}"], 0)

    def test_preset_leaves_the_texture_pool_and_screen_percentage_alone(self):
        # Both were dropped from the preset on 2026-10-08.  PoolSize=512 moved
        # the peak by -3 MiB against the project default of 1000 MB while
        # parking thin lines on a low mip, and r.ScreenPercentage never reaches
        # a SceneCapture (the engine uses FLegacyScreenPercentageDriver(1.0)).
        # This test is the guard against re-adding either one.
        preset = vram.low_vram_ini_args()
        for name in (
            "r.Streaming.PoolSize",
            "r.Streaming.LimitPoolSizeToVRAM",
            "r.ScreenPercentage",
        ):
            self.assertNotIn(f"ini:Engine:[SystemSettings]:{name}", preset)

    def test_preset_keys_are_single_command_line_arguments(self):
        for key in vram.low_vram_ini_args():
            self.assertTrue(key.startswith("ini:Engine:[SystemSettings]:"))
            self.assertNotIn(" ", key)


class LogPathTests(unittest.TestCase):
    def test_editor_log_is_derived_from_the_project_file(self):
        path = vram.resolve_ue_log_path(project_file="/repo/ue/rm_sim_spear.uproject")
        self.assertEqual(path, Path("/repo/ue/Saved/Logs/rm_sim_spear.log"))

    def test_explicit_log_wins(self):
        path = vram.resolve_ue_log_path(
            project_file="/repo/ue/proj.uproject", explicit="/tmp/forced.log"
        )
        self.assertEqual(path, Path("/tmp/forced.log"))


class LogWatcherTests(unittest.TestCase):
    def test_watcher_reports_a_crash_appended_after_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "proj.log"
            log_path.write_text("LogVulkanRHI: Display: boot\n", encoding="utf-8")
            seen = []
            watcher = vram.UeLogWatcher(
                log_path=log_path,
                poll_seconds=0.02,
                on_failure=seen.append,
            )
            watcher.start()
            try:
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(CRASH_DUMP)
                deadline = time.monotonic() + 5.0
                while not seen and time.monotonic() < deadline:
                    time.sleep(0.05)
            finally:
                watcher.stop()
            self.assertTrue(seen, "watcher did not report the appended crash")
            self.assertEqual(seen[0].name, "vulkan_alloc_failed")
            self.assertIsNotNone(watcher.failure)
            self.assertIn("Out Of Memory", watcher.tail_text())

    def test_watcher_follows_a_rotated_log(self):
        """UE renames the old log on launch; the watcher must read the new one."""
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "proj.log"
            log_path.write_text("old run\n" * 500, encoding="utf-8")
            seen = []
            watcher = vram.UeLogWatcher(log_path=log_path, poll_seconds=0.02, on_failure=seen.append)
            watcher.start()
            try:
                log_path.write_text(CRASH_DUMP, encoding="utf-8")
                deadline = time.monotonic() + 5.0
                while not seen and time.monotonic() < deadline:
                    time.sleep(0.05)
            finally:
                watcher.stop()
            self.assertTrue(seen, "watcher skipped the rotated log file")

    def test_watcher_reports_a_dead_engine_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "proj.log"
            log_path.write_text("LogVulkanRHI: Display: boot\n", encoding="utf-8")
            seen = []
            watcher = vram.UeLogWatcher(
                log_path=log_path,
                poll_seconds=0.02,
                on_failure=seen.append,
                process_probe=lambda: False,
            )
            watcher.start()
            try:
                deadline = time.monotonic() + 5.0
                while not seen and time.monotonic() < deadline:
                    time.sleep(0.05)
            finally:
                watcher.stop()
            self.assertTrue(seen, "watcher did not notice the dead engine process")
            self.assertEqual(seen[0].name, "engine_process_exited")

    def test_watcher_ignores_history_and_disarm_silences_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "proj.log"
            log_path.write_text(CRASH_DUMP, encoding="utf-8")
            seen = []
            watcher = vram.UeLogWatcher(log_path=log_path, poll_seconds=0.02, on_failure=seen.append)
            watcher.disarm()
            watcher.start()
            try:
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write("LogCore: still alive\n")
                time.sleep(0.3)
            finally:
                watcher.stop()
            self.assertEqual(seen, [])


class StaleProcessTests(unittest.TestCase):
    class _Process:
        def __init__(self, info):
            self.info = info

    def _processes(self):
        return [
            self._Process({
                "pid": 101,
                "name": "UnrealEditor",
                "cmdline": ["/engine/UnrealEditor", "/repo/ue/rm_sim_spear.uproject", "-game"],
            }),
            self._Process({
                "pid": 102,
                "name": "UnrealEditor",
                "cmdline": ["/engine/UnrealEditor", "/other/other.uproject"],
            }),
            self._Process({"pid": 103, "name": "python", "cmdline": ["python", "x.py"]}),
        ]

    def test_only_this_projects_editor_is_reported(self):
        found = vram.find_ue_processes(
            "/repo/ue/rm_sim_spear.uproject",
            process_iter=lambda attrs: self._processes(),
        )
        self.assertEqual([process.pid for process in found], [101])
        self.assertIn("-game", found[0].cmdline)

    def test_without_a_project_every_unreal_process_is_reported(self):
        found = vram.find_ue_processes(process_iter=lambda attrs: self._processes())
        self.assertEqual(sorted(process.pid for process in found), [101, 102])

    def test_processes_that_vanish_while_iterating_are_skipped(self):
        def exploding_iter(attrs):
            yield self._Process({"pid": 7, "name": "UnrealEditor", "cmdline": None})
            raise RuntimeError("process 8 exited")

        found = vram.find_ue_processes(process_iter=exploding_iter)
        self.assertEqual([process.pid for process in found], [7])

    def test_kill_processes_ignores_missing_pids(self):
        self.assertEqual(vram.kill_processes([]), ())
        self.assertEqual(vram.kill_processes([999999999]), ())


class SamplerTests(unittest.TestCase):
    def test_sampler_keeps_the_peak_and_writes_csv(self):
        snapshots = iter([
            vram.VramSnapshot("GPU", 8188, 1000, 7188),
            vram.VramSnapshot("GPU", 8188, 6000, 2188),
            vram.VramSnapshot("GPU", 8188, 4000, 4188),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "vram.csv"
            sampler = vram.VramSampler(csv_path=csv_path, poll_seconds=0.01)
            with mock.patch.object(vram, "query_vram", lambda **_: next(snapshots, None)):
                sampler.start()
                deadline = time.monotonic() + 5.0
                while sampler.summary()["samples"] < 3 and time.monotonic() < deadline:
                    time.sleep(0.02)
                sampler.stop()
            self.assertIsNotNone(sampler.peak_used_mb)
            self.assertLessEqual(sampler.peak_used_mb, 6000)
            # The smallest free reading is what predicts a failed contiguous
            # allocation, so it has to be reported next to the peak.
            self.assertLessEqual(sampler.min_free_mb, 2188)
            self.assertTrue(csv_path.is_file())
            self.assertEqual(
                csv_path.read_text(encoding="utf-8").splitlines()[0],
                "time,used_mb,free_mb",
            )
            self.assertGreater(sampler.summary()["samples"], 0)

    def test_rows_survive_a_run_that_never_reaches_stop(self):
        """A crashed run is the interesting case, so rows are flushed eagerly."""
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "vram.csv"
            sampler = vram.VramSampler(csv_path=csv_path, poll_seconds=0.01)
            with mock.patch.object(
                vram, "query_vram", lambda **_: vram.VramSnapshot("GPU", 8188, 5000, 3188)
            ):
                sampler.start()
                deadline = time.monotonic() + 5.0
                rows = []
                while len(rows) < 2 and time.monotonic() < deadline:
                    time.sleep(0.02)
                    # Deliberately never call stop(): simulate os._exit(4).
                    rows = csv_path.read_text(encoding="utf-8").splitlines()
            self.assertGreater(len(rows), 1)
            sampler.stop()

    def test_disabled_sampling_stays_empty(self):
        sampler = vram.VramSampler(poll_seconds=0)
        sampler.start()
        sampler.stop()
        self.assertIsNone(sampler.peak_used_mb)
        self.assertEqual(sampler.summary()["samples"], 0)


if __name__ == "__main__":
    unittest.main()
