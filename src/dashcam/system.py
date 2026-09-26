"""Operating system helpers."""

import contextlib
import ctypes
import logging
import subprocess
import sys
import typing as t

# Format of log messages.
LOG_FORMAT = "%(asctime)s | %(levelname)8s | %(message)s"

# pylint: disable=logging-fstring-interpolation
LOGGER = logging.getLogger(__name__)


def configure_logging() -> None:
    """
    Set up console logging.

    Also used as the initializer of worker processes, which start without the parent's logging
    configuration on macOS.
    """
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


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
