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
