"""Off-loop image encoding for capture runs.

Why this exists: ``cv2.imwrite`` of a 1440x1080 PNG costs ~21 ms on this
machine (~1.6 ms for the raw bytes), and the capture loop was paying it inside
its own per-frame measurement.  A single worker thread with a bounded queue
keeps the loop from waiting for the encoder unless the disk falls behind, and
makes that cost separately observable.

The queue is bounded on purpose: an unbounded queue would trade the encoder
stall for unbounded memory growth if the disk cannot keep up, which on a
dataset run is worse than slowing the loop down.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

import cv2
import numpy as np

__all__ = ["AsyncImageWriter", "DEFAULT_QUEUE_SIZE"]

DEFAULT_QUEUE_SIZE = 4


class AsyncImageWriter:
    """Encode and write images on one daemon worker thread.

    ``submit`` returns the seconds the caller spent waiting for queue space, and
    ``close`` drains the queue before returning, so a caller that always closes
    still gets every file and the real encoder error if one happened on the
    worker.
    """

    def __init__(self, queue_size: int = DEFAULT_QUEUE_SIZE):
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._error: BaseException | None = None
        self._encode_seconds = 0.0
        self._count = 0
        self._thread = threading.Thread(
            target=self._run, name="rm_sim_image_writer", daemon=True)
        self._thread.start()

    def submit(self, path: Path, image: np.ndarray) -> float:
        """Queue one image; returns the seconds spent waiting for the worker.

        The array is handed over by reference (copying a 1440x1080 frame costs
        about as much as the wait this avoids), so the caller must not mutate it
        after submitting; every caller in this repository passes a fresh array.
        """
        start = time.perf_counter()
        self._queue.put((Path(path), image))
        return time.perf_counter() - start

    def close(self) -> None:
        """Drain the queue and re-raise whatever the worker hit.

        Must be called exactly once; the sentinel is what stops the worker, so a
        second call would enqueue a second sentinel and return immediately.
        """
        self._queue.put(None)
        self._thread.join()
        if self._error is not None:
            raise RuntimeError("image writer thread failed") from self._error

    def summary(self) -> dict:
        """Counters for the run's log line: what the loop did not pay for."""
        return {
            "images": self._count,
            "encode_ms_total": round(1000.0*self._encode_seconds, 3),
            "encode_ms_mean": round(
                1000.0*self._encode_seconds/self._count, 3) if self._count else None,
        }

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            path, image = item
            try:
                start = time.perf_counter()
                # cv2.imwrite reports failure by returning False instead of
                # raising, so an unwritable path would otherwise look like a
                # successful run with missing files.
                if not cv2.imwrite(str(path), image):
                    raise OSError(f"cv2.imwrite failed for {path}")
                self._encode_seconds += time.perf_counter() - start
                self._count += 1
            except BaseException as exc:
                # Keep draining so the producer cannot deadlock on a full queue;
                # the error surfaces from close().
                self._error = exc
            finally:
                self._queue.task_done()
