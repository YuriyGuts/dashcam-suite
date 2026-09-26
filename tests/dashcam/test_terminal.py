import io
import logging
import multiprocessing
import queue
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
    record = make_record("Renamed: [bold]A[/bold] (CX-5) [fixable: rename it]")

    # WHEN writing it
    output = emit(record)

    # THEN the brackets are printed as they are
    assert "Renamed: [bold]A[/bold] (CX-5) [fixable: rename it]" in output


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
    log_queue = queue.SimpleQueue()
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
    sent_record = log_queue.get_nowait()
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
