import signal
import subprocess
import sys
import threading

import pytest

from dashcam import system


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

    # THEN it exits without a traceback
    assert handler is system.exit_on_interrupt
    with pytest.raises(SystemExit) as exc_info:
        system.exit_on_interrupt(signal.SIGINT, None)
    assert exc_info.value.code == system.INTERRUPTED_EXIT_CODE


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
