"""Terminal renderer.

One write() per frame. Only changed rows are rewritten. The alt screen is
cleared once at start. Resizes rewrite in place — a full 2J clear between
frames is what flashed blank and shook the layout. Nothing here touches the bot.
"""
from __future__ import annotations

import os
import shutil
import signal
import sys
import time

from .safety import exception_summary, terminal_text
from .theme import RESET, enable_utf8_output, enable_windows_vt, glyphs, sgr
from .widgets import Row

ALT_ON = "\x1b[?1049h"
ALT_OFF = "\x1b[?1049l"
CURSOR_OFF = "\x1b[?25l"
CURSOR_ON = "\x1b[?25h"
WRAP_OFF = "\x1b[?7l"
WRAP_ON = "\x1b[?7h"
CLEAR = "\x1b[2J\x1b[H"
HOME = "\x1b[H"
ERASE_LINE = "\x1b[K"


def render_row(row: Row) -> str:
    out = []
    last = None
    for text, style in row:
        if not text:
            continue
        if style != last:
            out.append(sgr(style))
            last = style
        out.append(terminal_text(text, max_chars=0))
    out.append(RESET)
    return "".join(out)


class Renderer:
    def __init__(self, stream=None, min_cols: int = 40, min_rows: int = 10) -> None:
        self.stream = stream or sys.__stdout__
        self.min_cols, self.min_rows = min_cols, min_rows
        # Before the first glyph is chosen, not at first write: the frame is
        # box-drawing and block characters all the way down.
        self.utf8 = enable_utf8_output(self.stream)
        self.g = glyphs()
        self.last_error: str | None = None
        self._size_pending: tuple[int, int] | None = None
        self.cols, self.rows = 0, 0
        self.cols, self.rows = self.size()
        self._prev: list[str] = []
        self._force = True
        self._active = False
        self._resized = False
        self._old_winch = None
        self._winch_installed = False
        self.interactive = bool(getattr(self.stream, "isatty", lambda: False)())
        self.last_ms = 0.0

    # ------------------------------------------------------------- lifecycle
    def size(self) -> tuple[int, int]:
        try:
            c, r = shutil.get_terminal_size(fallback=(120, 40))
        except Exception as exc:
            self.last_error = f"terminal size failed: {exception_summary(exc)}"
            c, r = 120, 40
        c, r = max(self.min_cols, c), max(self.min_rows, r)
        # Windows reports a ±1 window size while a frame is painting. Adopting
        # that every frame rebuilds the layout and used to full-clear the alt
        # screen — the shake and the blank flash.
        current = (self.cols, self.rows)
        if self.cols <= 0 or self.rows <= 0 or (c, r) == current:
            self._size_pending = None
            return c, r
        if self._size_pending == (c, r):
            self._size_pending = None
            return c, r
        self._size_pending = (c, r)
        return current

    def _on_winch(self, *_a) -> None:
        self._resized = True

    def start(self) -> None:
        if not self.interactive:
            return
        if not enable_windows_vt():
            self.last_error = "terminal VT mode unavailable"
        if hasattr(signal, "SIGWINCH"):
            try:
                self._old_winch = signal.getsignal(signal.SIGWINCH)
                signal.signal(signal.SIGWINCH, self._on_winch)
                self._winch_installed = True
            except (ValueError, OSError) as exc:
                self.last_error = f"resize handler unavailable: {exception_summary(exc)}"
        try:
            self.stream.write(ALT_ON + CURSOR_OFF + WRAP_OFF + CLEAR)
            self.stream.flush()
        except Exception as exc:
            self.last_error = f"terminal start failed: {exception_summary(exc)}"
            self._restore_signal_handler()
            # Do not let an output-device failure kill the dashboard task (and
            # leave stdout captured).  The renderer is disabled for this run.
            self.interactive = False
            return
        self._active = True
        self._force = True

    def _restore_signal_handler(self) -> None:
        if not self._winch_installed or not hasattr(signal, "SIGWINCH"):
            return
        try:
            signal.signal(signal.SIGWINCH, self._old_winch)
        except (ValueError, OSError) as exc:
            self.last_error = f"resize handler restore failed: {exception_summary(exc)}"
        finally:
            self._winch_installed = False
            self._old_winch = None

    def stop(self) -> None:
        if not self._active:
            self._restore_signal_handler()
            return
        try:
            self.stream.write(RESET + WRAP_ON + CURSOR_ON + ALT_OFF)
            self.stream.flush()
        except Exception as exc:
            self.last_error = f"terminal restore failed: {exception_summary(exc)}"
        self._active = False
        self._restore_signal_handler()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def repaint(self) -> None:
        self._force = True

    # ---------------------------------------------------------------- draw
    def draw(self, frame: list[Row]) -> float:
        """Write only what changed. Returns milliseconds spent."""
        t0 = time.perf_counter()
        if not self.interactive:
            self.last_ms = (time.perf_counter() - t0) * 1000.0
            return self.last_ms

        # The frame was built at caller-set cols/rows. A second size() here
        # can disagree on Windows and used to 2J-flash every frame.
        if self._resized or (self._prev and len(frame) != len(self._prev)):
            self._resized = False
            self._force = True

        lines = [render_row(row) for row in frame]
        buf = []
        if self._force:
            # Never 2J here. A full clear blanks the alt screen for a frame
            # (the flash). start() already cleared once. On resize, rewrite
            # every row in place and erase leftovers from a taller previous
            # frame.
            for i, line in enumerate(lines):
                buf.append(f"\x1b[{i + 1};1H" + ERASE_LINE)
                buf.append(line)
            for i in range(len(lines), len(self._prev)):
                buf.append(f"\x1b[{i + 1};1H" + ERASE_LINE)
            self._force = False
        else:
            for i, line in enumerate(lines):
                if i >= len(self._prev) or self._prev[i] != line:
                    buf.append(f"\x1b[{i + 1};1H" + ERASE_LINE)
                    buf.append(line)
        wrote = False
        if buf:
            buf.append(f"\x1b[{len(lines)};{1}H")
            try:
                self._emit(buf)
                wrote = True
            except Exception as exc:
                self.last_error = f"terminal write failed: {exception_summary(exc)}"
                # A transient write failure must retry the complete frame.  If
                # `_prev` were advanced here, an identical next frame would be
                # treated as already displayed and the screen would freeze.
                self._force = True
        if not buf or wrote:
            self._prev = lines
        self.last_ms = (time.perf_counter() - t0) * 1000.0
        return self.last_ms

    def _emit(self, parts: list[str]) -> None:
        """Write the frame in bounded flushes.

        A single giant write can be truncated on Windows consoles (~64KiB),
        which left the bottom panels unpainted until a later diff. Chunked
        writes stay under that limit; one flush at the end avoids painting a
        half-frame that looks like shake.

        Chunks break between `parts`, never inside one. Slicing the joined
        payload at a fixed offset cut two of every three chunks in the middle
        of an SGR sequence ("ESC[0;38;5;25;48" + ";5;230m"); the console
        dropped the truncated code and painted the rest of that row in the
        default background - a blank-looking strip that moved with the content
        and vanished on the next repaint. Every part is either a cursor move
        or a full row ending in RESET, so a boundary between parts is always
        colour-neutral.
        """
        limit = 4096
        buf: list[str] = []
        size = 0
        for part in parts:
            if buf and size + len(part) > limit:
                self.stream.write("".join(buf))
                buf, size = [], 0
            buf.append(part)
            size += len(part)
        if buf:
            self.stream.write("".join(buf))
        self.stream.flush()


class PlainRenderer:
    """Non-TTY fallback: one status line every N seconds, zero escape codes.

    Piping the dashboard into a log file or systemd journal should not fill
    it with cursor moves.
    """

    def __init__(self, stream=None, every: float = 10.0) -> None:
        self.stream = stream or sys.__stdout__
        try:
            every = float(every)
        except (TypeError, ValueError, OverflowError):
            every = 10.0
        self.every = max(0.0, every) if every == every else 10.0
        self.utf8 = enable_utf8_output(self.stream)
        self.g = glyphs()
        self.interactive = False
        self._last = 0.0
        self.cols, self.rows = 120, 40
        self.last_ms = 0.0
        self.last_error: str | None = None

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def repaint(self) -> None:
        pass

    def size(self) -> tuple[int, int]:
        return self.cols, self.rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def status(self, snap: dict) -> None:
        now = time.monotonic()
        if now - self._last < self.every:
            return
        self._last = now
        spot = f"${snap['spot']:,.2f}" if snap["spot"] else "--"
        price_to_beat = (f"${snap['start_chainlink']:,.2f}"
                         if snap["start_chainlink"] is not None else "--")
        running_price = (f"${snap['chainlink']:,.2f}"
                         if snap["chainlink"] is not None else "--")
        secs = snap["seconds_left"]
        try:
            line = (
                f"[DASH] {snap['round_label']} T-{secs if secs is not None else '???'} "
                f"ptb={price_to_beat} running={running_price} spot={spot} "
                f"side={snap['decision'] or '--'} "
                f"ok={snap['orders_ok']} fail={snap['orders_fail']} "
                f"feed={snap['spot_status']} book={snap['book_status']}"
            )
            self.stream.write(terminal_text(line, max_chars=2000) + "\n")
            self.stream.flush()
        except Exception as exc:
            self.last_error = f"plain status write failed: {exception_summary(exc)}"


def make_renderer(stream=None) -> Renderer | PlainRenderer:
    stream = stream or sys.__stdout__
    if os.environ.get("TERM_FORCE_PLAIN", "").strip() in ("1", "true", "yes"):
        return PlainRenderer(stream)
    if getattr(stream, "isatty", lambda: False)():
        return Renderer(stream)
    return PlainRenderer(stream)
