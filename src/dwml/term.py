"""Terminal presentation: ANSI styling, phase headers, progress (v2.6).

Everything degrades to plain text when the stream is not a TTY, when
NO_COLOR is set, or when TERM=dumb, so CI logs and redirected output stay
clean. FORCE_COLOR overrides the TTY check for tools like watch/tee.
"""
import os
import sys
import time

_CODES = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
}


def supports_color(stream):
    """Whether ANSI escapes should be emitted on this stream."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM", "") == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def paint(text, *styles, enabled=True):
    """Wrap text in ANSI style codes; plain passthrough when disabled."""
    if not enabled or not styles:
        return str(text)
    codes = ";".join(_CODES[s] for s in styles)
    return f"\x1b[{codes}m{text}\x1b[0m"


def fmt_elapsed(seconds):
    """Compact human elapsed time: 42s, 3m 07s, 1h 02m."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


class Console:
    """Styled writer with phase headers and a scan-elapsed clock."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.color = supports_color(self.stream)
        self.started = time.monotonic()

    def paint(self, text, *styles):
        return paint(text, *styles, enabled=self.color)

    def print(self, text=""):
        self.stream.write(str(text) + "\n")
        self.stream.flush()

    def banner(self, name, version, subtitle):
        self.print(f"{self.paint(name, 'bold')} {self.paint('v' + version, 'dim')}")
        self.print(self.paint(subtitle, "dim"))

    def phase(self, title, detail=""):
        line = "\n" + self.paint("::", "cyan", "bold") + " " + self.paint(title, "bold")
        if detail:
            line += "  " + self.paint(detail, "dim")
        self.print(line)

    def info(self, text):
        self.print(f"   {text}")

    def warn(self, text):
        self.print(f"   {self.paint(text, 'yellow')}")

    def error(self, text):
        self.print(f"   {self.paint(text, 'red')}")

    def elapsed(self):
        return fmt_elapsed(time.monotonic() - self.started)


class Progress:
    """Single-line progress bar for parallel work.

    On a TTY it renders in place with carriage returns; on anything else it
    prints one start line and stays silent, keeping CI logs readable.
    """

    def __init__(self, total, label, stream=None, width=28):
        self.stream = stream or sys.stderr
        self.total = max(int(total), 1)
        self.label = label
        self.width = width
        self.count = 0
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.color = supports_color(self.stream)
        if not self.tty:
            self.stream.write(f"{label}: {total} item(s)...\n")
            self.stream.flush()

    def update(self, detail=""):
        self.count += 1
        if not self.tty:
            return
        filled = int(self.width * self.count / self.total)
        bar = (paint("#" * filled, "cyan", enabled=self.color)
               + "-" * (self.width - filled))
        pct = int(100 * self.count / self.total)
        detail = str(detail)[:36]
        self.stream.write(
            f"\r  {self.label} [{bar}] "
            f"{self.count}/{self.total} {pct:>3}%  {detail:<36}"
        )
        self.stream.flush()

    def finish(self):
        if self.tty:
            self.stream.write("\r" + " " * (self.width + len(self.label) + 60) + "\r")
            self.stream.flush()
