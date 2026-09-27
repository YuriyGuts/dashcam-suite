"""
Terminal output: colored log lines on stderr and styled command output on stdout.

Log lines look like `14:03:11 ✓ Encoding completed: 2026-09-25 Trip 11-17.mp4`. The symbol shows
the level, or the marker passed with `extra`:

    LOGGER.info(f"Encoding completed: {name}", extra=terminal.SUCCESS)

Colors are only used on a terminal. `NO_COLOR` and `FORCE_COLOR` are honored.
"""

import copy
import io
import logging
import multiprocessing.queues
import sys
import time
import typing as t

from rich.console import Console
from rich.highlighter import RegexHighlighter
from rich.text import Text
from rich.theme import Theme

# How often (in seconds) to log the progress of a loop over many items, such as loading tracks.
ITEM_PROGRESS_INTERVAL_S = 10

# Markers that change how a log line looks, passed as `extra` to a log call.
SUCCESS = {"marker": "success"}
COMMAND = {"marker": "command"}
PROGRESS = {"marker": "progress"}
HEADING = {"marker": "heading"}

# Arrow between an old and a new name.
ARROW = "→"

# Symbol of warnings, also used for problems shown outside the log.
WARNING_SYMBOL = "▲"

# Symbol and style of each log level and marker.
LEVEL_SYMBOLS = {
    logging.DEBUG: ("·", "dim"),
    logging.INFO: ("•", "dim"),
    logging.WARNING: (WARNING_SYMBOL, "yellow"),
    logging.ERROR: ("✗", "red"),
    logging.CRITICAL: ("✗", "bold red"),
}
MARKER_SYMBOLS = {
    "success": ("✓", "green"),
    "command": ("$", "dim"),
    "progress": ("•", "dim"),
    "heading": ("▸", "bold"),
}

# Style of the whole message, by log level and marker.
LEVEL_MESSAGE_STYLES = {
    logging.DEBUG: "dim",
    logging.WARNING: "yellow",
    logging.ERROR: "red",
    logging.CRITICAL: "bold red",
}
MARKER_MESSAGE_STYLES = {
    "command": "dim",
    "progress": "dim",
    "heading": "bold",
}

# Styles of the message parts found by `MessageHighlighter`.
THEME = Theme(
    {
        "log.quoted": "cyan",
        "log.code": "bold",
        "log.url": "underline cyan",
        "log.count": "not dim bold",
        "log.fixable": "blue",
    }
)

# Width of the `HH:MM:SS X ` prefix, used to indent the continuation lines of a message.
PREFIX_WIDTH = 11

# Console for command output (e.g. `status`) and for log lines. Markup and automatic
# highlighting are off, so that brackets in filenames are printed as they are.
STDOUT = Console(theme=THEME, markup=False, highlight=False, emoji=False)
STDERR = Console(stderr=True, theme=THEME, markup=False, highlight=False, emoji=False)


class MessageHighlighter(RegexHighlighter):
    """Highlights quoted names, `code`, URLs, percentages, counts, and doctor fix hints."""

    base_style = "log."
    highlights: t.ClassVar[list[str]] = [
        r"(?P<quoted>'[^'\n]+')",
        r"(?P<code>`[^`\n]+`)",
        r"(?P<url>https?://\S+)",
        r"(?P<count>\b\d+(\.\d+)?%|\b\d+/\d+\b)",
        r"(?P<fixable>\[fixable: [^\]]+\])",
    ]


class ConsoleLogHandler(logging.Handler):
    """Writes log records as single colored lines with a level symbol."""

    def __init__(self, console: Console | None = None) -> None:
        super().__init__()
        self.console = console
        self.highlighter = MessageHighlighter()

    def format_line(self, record: logging.LogRecord) -> Text:
        """Build the styled line of a record: time, symbol, and message."""
        marker = getattr(record, "marker", None)
        symbol, symbol_style = MARKER_SYMBOLS.get(marker) or LEVEL_SYMBOLS.get(
            record.levelno, LEVEL_SYMBOLS[logging.INFO]
        )
        # Warnings and errors keep their level style even when marked.
        message_style = LEVEL_MESSAGE_STYLES.get(record.levelno) or MARKER_MESSAGE_STYLES.get(
            marker, ""
        )
        timestamp = logging.Formatter().formatTime(record, "%H:%M:%S")
        message = self.format(record).replace("\n", "\n" + " " * PREFIX_WIDTH)

        return Text.assemble(
            (timestamp, "dim"),
            " ",
            (symbol, symbol_style),
            " ",
            self.highlighter(Text(message, style=message_style)),
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            console = self.console or STDERR
            console.print(self.format_line(record), soft_wrap=True)
        except Exception:
            self.handleError(record)


class WorkerLogHandler(logging.Handler):
    """Sends log records of a worker process to the parent process through a queue."""

    def __init__(self, log_queue: multiprocessing.queues.SimpleQueue) -> None:
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Send the formatted message (with any traceback): the arguments and the exception
            # may not be picklable.
            sendable_record = copy.copy(record)
            sendable_record.msg = self.format(record)
            sendable_record.args = None
            sendable_record.exc_info = None
            sendable_record.exc_text = None
            sendable_record.stack_info = None
            # A `SimpleQueue` writes the record before `put` returns, so no line is lost.
            self.log_queue.put(sendable_record)
        except Exception:
            self.handleError(record)


def configure_output_encoding() -> None:
    """
    Write UTF-8 when stdout is redirected to a file or a pipe.

    Otherwise Windows uses its legacy code page there, which cannot encode arrows or Cyrillic
    trip names. A terminal keeps its own encoding.
    """
    stdout = sys.stdout
    is_utf8 = stdout.encoding.lower().replace("-", "") == "utf8"
    if isinstance(stdout, io.TextIOWrapper) and not stdout.isatty() and not is_utf8:
        stdout.reconfigure(encoding="utf-8")


def configure_logging() -> None:
    """Write log records of level INFO and above to the terminal."""
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    for handler in list(root_logger.handlers):
        if isinstance(handler, ConsoleLogHandler):
            root_logger.removeHandler(handler)
    root_logger.addHandler(ConsoleLogHandler())


def configure_worker_logging(log_queue: multiprocessing.queues.SimpleQueue) -> None:
    """
    Send the log records of a worker process to the parent process.

    Used as the initializer of worker processes, so that one process writes all lines.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers = [WorkerLogHandler(log_queue)]


def forward_worker_logs(log_queue: multiprocessing.queues.SimpleQueue) -> None:
    """Pass the records sent by worker processes to the loggers of this process until `None`."""
    while (record := log_queue.get()) is not None:
        logging.getLogger(record.name).handle(record)


def ask(prompt: str) -> str:
    """Ask the user for a line of input, with the prompt in bold."""
    return STDOUT.input(Text(prompt, style="bold"))


def print_line(*parts: str | tuple[str, str] | Text) -> None:
    """Print a line to stdout, made of plain strings and (text, style) pairs."""
    STDOUT.print(Text.assemble(*parts), soft_wrap=True)


def format_video_time(time_s: float) -> str:
    """Format a video time as `M:SS`, or `H:MM:SS` from one hour."""
    total_s = int(time_s)
    hours, minutes, seconds = total_s // 3600, total_s % 3600 // 60, total_s % 60
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_progress(
    name: str,
    position_s: float,
    total_duration_s: float | None,
    speed: float | None,
    done_verb: str,
) -> str:
    """
    Describe the progress of a job through a video.

    E.g. `Trip.mp4: 23% (1:23 of 6:00, 1.2x, ~4 min left)`, or `Trip.mp4: 1:23 encoded, 1.2x`
    when the total duration is unknown.
    """
    position_text = format_video_time(position_s)
    speed_text = "" if speed is None else f", {speed:.1f}x"
    if not total_duration_s:
        return f"{name}: {position_text} {done_verb}{speed_text}"

    percent = min(100, round(position_s / total_duration_s * 100))
    time_left_text = ""
    if speed is not None:
        remaining_s = max(0.0, total_duration_s - position_s) / speed
        if remaining_s < 60:
            time_left_text = ", <1 min left"
        else:
            time_left_text = f", ~{round(remaining_s / 60)} min left"
    return (
        f"{name}: {percent}% ({position_text} of {format_video_time(total_duration_s)}"
        f"{speed_text}{time_left_text})"
    )


class ProgressLogger:
    """Log the progress of a job through a video at a fixed interval of wall-clock time."""

    def __init__(
        self,
        logger: logging.Logger,
        name: str,
        total_duration_s: float | None,
        done_verb: str,
        interval_s: float,
    ) -> None:
        self.logger = logger
        self.name = name
        self.total_duration_s = total_duration_s
        self.done_verb = done_verb
        self.interval_s = interval_s
        self.started_at = time.monotonic()
        self.next_report_at = self.started_at + interval_s

    def update(self, position_s: float, speed: float | None = None) -> None:
        """
        Log the position if the interval has passed since the last report.

        Without `speed`, the average speed since the start is shown.
        """
        now = time.monotonic()
        if now < self.next_report_at:
            return
        if speed is None and position_s > 0:
            speed = position_s / (now - self.started_at)
        self.logger.info(
            format_progress(self.name, position_s, self.total_duration_s, speed, self.done_verb),
            extra=PROGRESS,
        )
        self.next_report_at = now + self.interval_s


class ItemProgressLogger:
    """Log how many of several items are done, at a fixed interval of wall-clock time."""

    def __init__(
        self,
        logger: logging.Logger,
        description: str,
        total_count: int,
        interval_s: float = ITEM_PROGRESS_INTERVAL_S,
    ) -> None:
        self.logger = logger
        self.description = description
        self.total_count = total_count
        self.interval_s = interval_s
        self.next_report_at = time.monotonic() + interval_s

    def update(self, done_count: int) -> None:
        """Log the count if the interval has passed since the last report."""
        now = time.monotonic()
        if now < self.next_report_at:
            return
        self.logger.info(f"{self.description}: {done_count}/{self.total_count}", extra=PROGRESS)
        self.next_report_at = now + self.interval_s
