"""Operating system helpers."""

import contextlib
import ctypes
import logging
import multiprocessing
import multiprocessing.pool
import multiprocessing.queues
import signal
import subprocess
import sys
import threading
import typing as t

from dashcam import terminal

# How long to wait for the log records of a failed worker pool, in seconds.
FAILED_POOL_LOG_TIMEOUT_S = 5

# pylint: disable=logging-fstring-interpolation
LOGGER = logging.getLogger(__name__)


def initialize_worker(log_queue: multiprocessing.queues.SimpleQueue) -> None:
    """
    Prepare a worker process: send its log records to the parent, and ignore Ctrl+C.

    Ctrl+C reaches every process of the terminal. The parent stops the workers itself, so they
    do not print tracebacks of their own.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    terminal.configure_worker_logging(log_queue)


@contextlib.contextmanager
def worker_pool(process_count: int) -> t.Iterator[multiprocessing.pool.Pool]:
    """
    Start a pool of worker processes whose log records are written by this process.

    With a single writer, the lines of parallel jobs never tear or interleave.
    """
    log_queue = multiprocessing.SimpleQueue()
    forwarder = threading.Thread(
        target=terminal.forward_worker_logs, args=(log_queue,), daemon=True
    )
    forwarder.start()
    try:
        with multiprocessing.Pool(
            process_count,
            initializer=initialize_worker,
            initargs=(log_queue,),
        ) as process_pool:
            yield process_pool
            # Wait for the workers to exit, so that none is killed while sending a record.
            process_pool.close()
            process_pool.join()
    except BaseException:
        # The workers were terminated, and one may have left a record half-sent, so the
        # forwarder may never see the sentinel. It is a daemon thread and ends with the process.
        log_queue.put(None)
        forwarder.join(timeout=FAILED_POOL_LOG_TIMEOUT_S)
        raise

    # Every worker has exited, so all their records are in the queue before this sentinel.
    log_queue.put(None)
    forwarder.join()
    log_queue.close()


@contextlib.contextmanager
def prevent_os_sleep() -> t.Iterator[None]:
    """Prevent the operating system from sleeping."""
    LOGGER.info(f"Preventing the OS from sleeping (platform: '{sys.platform}')")

    # Windows: call the Win32 API.
    if sys.platform.startswith("win"):
        es_continuous = 0x80000000
        es_system_required = 0x00000001
        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadExecutionState(es_continuous | es_system_required)
        try:
            yield
        finally:
            LOGGER.info("Allowing the OS to sleep again")
            kernel32.SetThreadExecutionState(es_continuous)
        return

    # macOS: launch `caffeinate`. Linux: launch `systemd-inhibit`.
    if sys.platform == "darwin":
        inhibit_cmd = ["caffeinate"]
    elif sys.platform == "linux":
        inhibit_cmd = [
            "systemd-inhibit",
            "--what=idle:sleep",
            "--why=Prevent OS sleep while dashcam is running",
            "sleep",
            "infinity",
        ]
    else:
        LOGGER.warning(f"Sleep prevention not implemented for '{sys.platform}'")
        yield
        return

    try:
        inhibit_proc = subprocess.Popen(inhibit_cmd)
    except OSError as exc:
        LOGGER.warning(f"Could not prevent the OS from sleeping: {exc}")
        yield
        return

    try:
        yield
    finally:
        LOGGER.info("Allowing the OS to sleep again")
        inhibit_proc.terminate()
        inhibit_proc.wait()
