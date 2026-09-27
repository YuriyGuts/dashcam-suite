import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from dashcam import system
from dashcam import video


def follow_progress(cmd):
    """Follow the progress of a program, as a worker follows ffmpeg."""
    for _ in video.iter_ffmpeg_progress(cmd):
        pass


def wait_for(condition, timeout_s=10):
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError("The condition was not met in time")
        time.sleep(0.05)


def is_process_running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class FakeProcess:
    def __init__(self, cmd):
        self.cmd = cmd
        self.is_terminated = False

    def terminate(self):
        self.is_terminated = True

    def wait(self):
        return 0


@pytest.fixture
def started_processes(monkeypatch):
    processes = []

    def fake_popen(cmd):
        process = FakeProcess(cmd)
        processes.append(process)
        return process

    monkeypatch.setattr("dashcam.system.subprocess.Popen", fake_popen)
    return processes


def test_prevent_os_sleep_on_macos(monkeypatch, started_processes):
    # GIVEN macOS
    monkeypatch.setattr("dashcam.system.sys.platform", "darwin")

    # WHEN running code while preventing sleep
    with system.prevent_os_sleep():
        processes_inside = list(started_processes)

    # THEN `caffeinate` runs during the block and is terminated afterwards
    assert [process.cmd for process in processes_inside] == [["caffeinate"]]
    assert started_processes[0].is_terminated


def test_prevent_os_sleep_on_linux(monkeypatch, started_processes):
    # GIVEN Linux
    monkeypatch.setattr("dashcam.system.sys.platform", "linux")

    # WHEN running code while preventing sleep
    with system.prevent_os_sleep():
        pass

    # THEN `systemd-inhibit` is used and terminated afterwards
    assert started_processes[0].cmd[0] == "systemd-inhibit"
    assert started_processes[0].is_terminated


def test_prevent_os_sleep_when_inhibitor_is_missing(monkeypatch):
    # GIVEN Linux without `systemd-inhibit`
    monkeypatch.setattr("dashcam.system.sys.platform", "linux")

    def failing_popen(cmd):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr("dashcam.system.subprocess.Popen", failing_popen)
    ran_block = False

    # WHEN running code while preventing sleep
    with system.prevent_os_sleep():
        ran_block = True

    # THEN the block still runs
    assert ran_block


def test_prevent_os_sleep_terminates_inhibitor_on_error(monkeypatch, started_processes):
    # GIVEN macOS
    monkeypatch.setattr("dashcam.system.sys.platform", "darwin")

    # WHEN the block raises
    with pytest.raises(KeyboardInterrupt):
        with system.prevent_os_sleep():
            raise KeyboardInterrupt

    # THEN the inhibitor is still terminated
    assert started_processes[0].is_terminated


def test_prevent_os_sleep_on_unsupported_platform(monkeypatch, started_processes):
    # GIVEN an unsupported platform
    monkeypatch.setattr("dashcam.system.sys.platform", "freebsd14")

    # WHEN running code while preventing sleep
    with system.prevent_os_sleep():
        pass

    # THEN no process is started
    assert started_processes == []


def test_worker_pool_workers_exit_quietly_on_ctrl_c():
    # GIVEN a worker pool
    with system.worker_pool(1) as process_pool:
        # WHEN asking a worker how it handles Ctrl+C
        handler = process_pool.apply(signal.getsignal, (signal.SIGINT,))

    # THEN it exits without a traceback, with the exit code shells report for Ctrl+C
    assert handler is system.exit_on_signal
    with pytest.raises(SystemExit) as exc_info:
        system.exit_on_signal(signal.SIGINT, None)
    assert exc_info.value.code == 130


def test_worker_pool_workers_exit_quietly_when_terminated():
    # GIVEN a worker pool
    with system.worker_pool(1) as process_pool:
        # WHEN asking a worker how it handles termination
        handler = process_pool.apply(signal.getsignal, (signal.SIGTERM,))

    # THEN it exits by unwinding, so that its cleanup runs
    assert handler is system.exit_on_signal


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot catch process termination")
def test_worker_pool_failure_stops_programs_started_by_workers(tmp_path, endless_progress_cmd):
    # GIVEN a worker that follows the progress of a program that never ends
    pid_path = tmp_path / "pid.txt"
    with pytest.raises(ValueError, match="failed"):
        with system.worker_pool(1) as process_pool:
            process_pool.apply_async(follow_progress, (endless_progress_cmd(pid_path),))
            wait_for(lambda: pid_path.exists() and pid_path.read_text())

            # WHEN the pool fails and terminates its workers
            raise ValueError("failed")

    # THEN the program is stopped too
    program_pid = int(pid_path.read_text())
    wait_for(lambda: not is_process_running(program_pid))


def test_worker_pool_programs_started_by_workers_stop_on_ctrl_c():
    # GIVEN a worker pool
    code = "import signal; print(signal.getsignal(signal.SIGINT) is signal.SIG_IGN)"
    with system.worker_pool(1) as process_pool:
        # WHEN a worker starts a program (as it starts ffmpeg)
        output = process_pool.apply(
            subprocess.check_output, ([sys.executable, "-c", code],), {"text": True}
        )

    # THEN the program does not inherit an ignored Ctrl+C
    assert output.strip() == "False"


def test_worker_pool_stops_log_forwarding_when_the_body_fails():
    # GIVEN a worker pool whose body fails
    threads_before = set(threading.enumerate())

    # WHEN the error leaves the pool
    with pytest.raises(ValueError, match="failed"):
        with system.worker_pool(1):
            raise ValueError("failed")

    # THEN the log forwarding thread has ended
    assert set(threading.enumerate()) - threads_before == set()
