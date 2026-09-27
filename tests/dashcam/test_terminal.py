import io
import logging
import multiprocessing
import sys
import time

import pytest
from rich.console import Console

from dashcam import system
from dashcam import terminal

# Start of any ANSI escape sequence.
ANSI_ESCAPE = "\x1b["

# Creation time of test records: 14:03:11 local time.
RECORD_TIME = time.mktime((2026, 9, 25, 14, 3, 11, 0, 0, -1))


def test_configure_output_encoding_writes_utf8_to_a_redirected_stdout(monkeypatch):
    # GIVEN stdout redirected to a file in a legacy Windows code page, with line endings kept as
    # they are, so that the output is the same on every platform
    redirected_bytes = io.BytesIO()
    redirected_stdout = io.TextIOWrapper(redirected_bytes, encoding="cp1252", newline="\n")
    monkeypatch.setattr(sys, "stdout", redirected_stdout)

    line = f"{terminal.ARROW} 2026-09-25 Вулиця.mp4"

    # WHEN configuring the output and printing a rename plan with a Cyrillic name
    terminal.configure_output_encoding()
    terminal.print_line(line)
    sys.stdout.flush()

    # THEN the line is written as UTF-8
    assert redirected_bytes.getvalue().decode("utf-8") == f"{line}\n"


def test_configure_output_encoding_keeps_a_terminal_encoding(monkeypatch):
    # GIVEN stdout attached to a terminal in a legacy code page
    class TerminalStream(io.TextIOWrapper):
        def isatty(self):
            return True

    terminal_stream = TerminalStream(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", terminal_stream)

    # WHEN configuring the output
    terminal.configure_output_encoding()

    # THEN its encoding is left to the terminal
    assert terminal_stream.encoding == "cp1252"


def make_console(is_terminal):
    return Console(
        file=io.StringIO(),
        force_terminal=is_terminal,
        color_system="standard" if is_terminal else None,
        theme=terminal.THEME,
        markup=False,
        highlight=False,
        emoji=False,
    )


def make_record(message, level=logging.INFO, extra=None):
    record = logging.LogRecord("dashcam.test", level, __file__, 1, message, None, None)
    record.created = RECORD_TIME
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return record


def emit(record, is_terminal=False):
    console = make_console(is_terminal)
    terminal.ConsoleLogHandler(console).emit(record)
    return console.file.getvalue()


@pytest.mark.parametrize(
    ("level", "extra", "expected_symbol"),
    [
        (logging.INFO, None, "•"),
        (logging.WARNING, None, "▲"),
        (logging.ERROR, None, "✗"),
        (logging.INFO, terminal.SUCCESS, "✓"),
        (logging.INFO, terminal.COMMAND, "$"),
        (logging.INFO, terminal.PROGRESS, "•"),
        (logging.INFO, terminal.HEADING, "▸"),
    ],
)
def test_console_log_handler_symbols(level, extra, expected_symbol):
    # GIVEN a record of the level and marker
    record = make_record("Done", level, extra)

    # WHEN writing it to a console that is not a terminal
    output = emit(record)

    # THEN the line has the time, the symbol, and the message, without colors
    assert output == f"14:03:11 {expected_symbol} Done\n"


def test_console_log_handler_colors_on_terminal():
    # GIVEN a success record
    record = make_record("Encoding completed: trip.mp4", extra=terminal.SUCCESS)

    # WHEN writing it to a terminal
    output = emit(record, is_terminal=True)

    # THEN the symbol is green
    assert "\x1b[32m✓" in output


def test_console_log_handler_highlights_quoted_names_on_terminal():
    # GIVEN a record with a quoted name
    record = make_record("Collecting files from '/sd/DCIM'")

    # WHEN writing it to a terminal
    output = emit(record, is_terminal=True)

    # THEN the quoted name is cyan
    assert "\x1b[36m'/sd/DCIM'" in output


def test_console_log_handler_keeps_brackets():
    # GIVEN a message with square brackets, which rich would read as markup
    record = make_record("Renamed: [bold]A[/bold] (Car) [fixable: rename it]")

    # WHEN writing it
    output = emit(record)

    # THEN the brackets are printed as they are
    assert "Renamed: [bold]A[/bold] (Car) [fixable: rename it]" in output


def test_console_log_handler_indents_continuation_lines():
    # GIVEN a message of several lines, such as an error with ffmpeg output
    record = make_record("Encoding failed\nfirst error\nsecond error", logging.ERROR)

    # WHEN writing it
    output = emit(record)

    # THEN the continuation lines line up with the message
    assert output.splitlines() == [
        "14:03:11 ✗ Encoding failed",
        "           first error",
        "           second error",
    ]


def test_console_log_handler_does_not_wrap_long_lines():
    # GIVEN a message longer than the default console width
    message = "ffmpeg " + "-option value " * 20
    record = make_record(message, extra=terminal.COMMAND)

    # WHEN writing it to a console that is not a terminal
    output = emit(record)

    # THEN it stays on one line
    assert output.count("\n") == 1


def test_console_log_handler_writes_traceback():
    # GIVEN a record with an exception
    try:
        raise ValueError("bad value")
    except ValueError:
        record = logging.LogRecord(
            "dashcam.test", logging.ERROR, __file__, 1, "Failed", None, sys.exc_info()
        )

    # WHEN writing it
    output = emit(record)

    # THEN the traceback follows the message
    assert "Failed" in output
    assert "ValueError: bad value" in output
    assert ANSI_ESCAPE not in output


def test_configure_logging_does_not_add_handler_twice():
    # GIVEN logging configured once
    terminal.configure_logging()

    # WHEN configuring it again
    terminal.configure_logging()

    # THEN there is one console handler
    handlers = logging.getLogger().handlers
    assert sum(isinstance(handler, terminal.ConsoleLogHandler) for handler in handlers) == 1


def test_worker_log_handler_sends_formatted_record():
    # GIVEN a worker handler writing to a queue
    log_queue = multiprocessing.SimpleQueue()
    handler = terminal.WorkerLogHandler(log_queue)

    # WHEN a record with arguments, a marker, and an exception is emitted
    try:
        raise ValueError("bad value")
    except ValueError:
        record = logging.LogRecord(
            "dashcam.test",
            logging.ERROR,
            __file__,
            1,
            "Failed: %s",
            ("trip.mp4",),
            sys.exc_info(),
        )
    record.marker = "success"
    handler.emit(record)

    # THEN the queued record carries the formatted message and the marker only
    sent_record = log_queue.get()
    assert sent_record.msg.startswith("Failed: trip.mp4\nTraceback")
    assert "ValueError: bad value" in sent_record.msg
    assert sent_record.args is None
    assert sent_record.exc_info is None
    assert sent_record.marker == "success"


def log_from_worker(name):
    logging.getLogger("dashcam.worker").info(f"Hello from {name}", extra=terminal.SUCCESS)
    return name


def test_worker_pool_forwards_worker_logs_to_parent(caplog):
    # GIVEN a parent process that captures INFO logs
    caplog.set_level(logging.INFO)

    # WHEN worker processes log
    with system.worker_pool(2) as process_pool:
        results = sorted(process_pool.imap_unordered(log_from_worker, ["a", "b", "c"]))

    # THEN every record reaches the parent's handlers, with its marker
    assert results == ["a", "b", "c"]
    worker_records = [record for record in caplog.records if record.name == "dashcam.worker"]
    assert sorted(record.getMessage() for record in worker_records) == [
        "Hello from a",
        "Hello from b",
        "Hello from c",
    ]
    assert all(record.marker == "success" for record in worker_records)
    assert multiprocessing.active_children() == []


def test_format_progress_with_total_and_speed():
    # GIVEN 1:23 of a 6-minute video done at 1.2x

    # WHEN describing the progress
    text = terminal.format_progress("trip.mp4", 83.0, 360.0, 1.2, "encoded")

    # THEN it has the percentage, times, speed, and the time left in minutes
    assert text == "trip.mp4: 23% (1:23 of 6:00, 1.2x, ~4 min left)"


def test_format_progress_before_speed_is_known():
    # GIVEN the first report, without a speed

    # WHEN describing the progress
    text = terminal.format_progress("trip.mp4", 0.0, 360.0, None, "encoded")

    # THEN the speed and the time left are left out
    assert text == "trip.mp4: 0% (0:00 of 6:00)"


def test_format_progress_without_total_duration():
    # GIVEN a job whose total duration is unknown

    # WHEN describing the progress
    text = terminal.format_progress("trip.mp4", 83.0, None, 1.2, "encoded")

    # THEN the time done and the speed are shown
    assert text == "trip.mp4: 1:23 encoded, 1.2x"


def test_format_video_time_from_one_hour():
    assert terminal.format_video_time(3723.9) == "1:02:03"


def test_format_video_time_under_one_hour():
    assert terminal.format_video_time(83.0) == "1:23"


@pytest.fixture
def wall_clock(monkeypatch):
    clock = {"now_s": 1000.0}
    monkeypatch.setattr("dashcam.terminal.time.monotonic", lambda: clock["now_s"])
    return clock


def progress_messages(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if getattr(record, "marker", None) == "progress"
    ]


def test_progress_logger_logs_at_fixed_interval(wall_clock, caplog):
    # GIVEN a progress logger with a 30-second interval, updated every 10 seconds
    caplog.set_level(logging.INFO)
    progress_logger = terminal.ProgressLogger(
        logging.getLogger("test"), "trip.mp4", 3600.0, "read", interval_s=30
    )

    # WHEN a job advances 5 minutes of video every 10 seconds, for 70 seconds
    for step in range(1, 8):
        wall_clock["now_s"] += 10
        progress_logger.update(step * 300.0)

    # THEN the progress is logged every 30 seconds, with the average speed so far
    assert progress_messages(caplog) == [
        "trip.mp4: 25% (15:00 of 1:00:00, 30.0x, ~2 min left)",
        "trip.mp4: 50% (30:00 of 1:00:00, 30.0x, ~1 min left)",
    ]


def test_progress_logger_prefers_given_speed(wall_clock, caplog):
    # GIVEN a progress logger past its interval
    caplog.set_level(logging.INFO)
    progress_logger = terminal.ProgressLogger(
        logging.getLogger("test"), "trip.mp4", 600.0, "encoded", interval_s=10
    )
    wall_clock["now_s"] += 10

    # WHEN updating it with a speed reported by ffmpeg
    progress_logger.update(60.0, speed=2.0)

    # THEN that speed is shown
    assert progress_messages(caplog) == ["trip.mp4: 10% (1:00 of 10:00, 2.0x, ~4 min left)"]


def test_item_progress_logger_logs_at_fixed_interval(wall_clock, caplog):
    # GIVEN a progress logger over 100 items with a 10-second interval
    caplog.set_level(logging.INFO)
    progress_logger = terminal.ItemProgressLogger(
        logging.getLogger("test"), "Loading tracks", 100, interval_s=10
    )

    # WHEN one item is done every 3 seconds
    for done_count in range(1, 10):
        wall_clock["now_s"] += 3
        progress_logger.update(done_count)

    # THEN the count is logged every 10 seconds
    assert progress_messages(caplog) == ["Loading tracks: 4/100", "Loading tracks: 8/100"]
