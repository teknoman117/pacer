#!/usr/bin/env python3
"""pacer — pipe stdin to stdout at a bandwidth capped by time of day.

Restrict throughput to a base rate, with optional per-time-of-day windows that
override it (e.g. fast overnight, slow during the day) to respect ISP fair-use
policies. Live transfer stats are printed to stderr; when the active window
changes, that window's summary is frozen on its own line.

Example:
    generate | pacer -r 1MB -w 01:00-03:00:20MB -w 03:00-07:00:unlimited | consume

    1 MB/s baseline, 20 MB/s between 01:00 and 03:00, uncapped 03:00-07:00.
"""

import argparse
import re
import signal
import sys
import time
from datetime import datetime

# Read at most this many bytes per syscall. Small enough to keep the stats line
# and window-boundary checks responsive even at high rates.
BUFSIZE = 64 * 1024

# How often to repaint the live stats line, in seconds.
STATUS_INTERVAL = 0.2

# Window over which the "current rate" is averaged, in seconds.
RATE_WINDOW = 1.0

# Default burst = this many seconds of traffic at the active rate. Small so a
# stalled output (e.g. a high-latency VPN/satellite link whose send buffer keeps
# filling) can't hoard tokens and then dump a big catch-up burst. Overridable
# with --burst.
DEFAULT_BURST_SECONDS = 0.1

# Never let the computed burst fall below this, so very low rates still move data
# in reasonably-sized (not byte-at-a-time) writes.
MIN_BURST = 4 * 1024

SECONDS_PER_DAY = 24 * 60 * 60

_UNITS = {
    "b": 1,
    "k": 1000, "kb": 1000, "kib": 1024,
    "m": 1000**2, "mb": 1000**2, "mib": 1024**2,
    "g": 1000**3, "gb": 1000**3, "gib": 1024**3,
    "t": 1000**4, "tb": 1000**4, "tib": 1024**4,
}

_RATE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*$")


def parse_rate(text):
    """Parse a rate/size string to bytes-per-second. None means unlimited."""
    t = text.strip().lower()
    if t in ("unlimited", "inf", "none", "max", "0"):
        # 0 and the aliases both mean "no cap".
        return None
    # Strip an optional trailing "/s", "ps", or "bps"-style suffix.
    for suffix in ("/s", "/sec", "ps"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
            break
    m = _RATE_RE.match(t)
    if not m:
        raise argparse.ArgumentTypeError(f"invalid rate: {text!r}")
    value, unit = m.group(1), m.group(2)
    unit = unit or "b"
    if unit not in _UNITS:
        raise argparse.ArgumentTypeError(
            f"unknown unit {unit!r} in rate {text!r}"
        )
    return float(value) * _UNITS[unit]


def parse_size(text):
    """Parse a byte-size string (like parse_rate but without 'unlimited')."""
    m = _RATE_RE.match(text.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(f"invalid size: {text!r}")
    value, unit = m.group(1), m.group(2) or "b"
    if unit not in _UNITS:
        raise argparse.ArgumentTypeError(f"unknown unit {unit!r} in size {text!r}")
    size = float(value) * _UNITS[unit]
    if size < 1:
        raise argparse.ArgumentTypeError(f"size must be at least 1 byte: {text!r}")
    return size


def burst_for(rate, override):
    """Bucket capacity in bytes for a rate: explicit --burst, else the default."""
    if rate is None:
        return 0
    if override is not None:
        return max(1, int(override))
    return max(MIN_BURST, int(rate * DEFAULT_BURST_SECONDS))


def _parse_clock(text):
    """Parse a HH:MM (or HH:MM:SS) clock into a second-of-day offset."""
    parts = text.split(":")
    if not 1 <= len(parts) <= 3:
        raise argparse.ArgumentTypeError(f"invalid time: {text!r}")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid time: {text!r}")
    nums += [0] * (3 - len(nums))
    h, m, s = nums
    if not (0 <= h <= 24 and 0 <= m < 60 and 0 <= s < 60):
        raise argparse.ArgumentTypeError(f"time out of range: {text!r}")
    total = h * 3600 + m * 60 + s
    if total > SECONDS_PER_DAY:
        raise argparse.ArgumentTypeError(f"time out of range: {text!r}")
    return total


class Window:
    """A time-of-day span [start, end) mapping to a rate (bytes/s or None)."""

    def __init__(self, start, end, rate, label):
        self.start = start
        self.end = end
        self.rate = rate
        self.label = label
        # A window whose end is <= start wraps across midnight (e.g. 23:00-02:00).
        self.wraps = end <= start

    def contains(self, sod):
        """Is second-of-day `sod` inside this window?"""
        if self.wraps:
            return sod >= self.start or sod < self.end
        return self.start <= sod < self.end


# Window boundaries are HH:MM (minutes mandatory). Requiring the minutes makes
# the ':' rate separator unambiguous: a bare trailing hour like "03" can't be
# mistaken for HH:MM, so "01:00-03:00" (rate omitted) fails to match instead of
# being silently read as end "03" with rate "00".
_WIN_SPAN_RE = re.compile(r"^\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*$")


def parse_window(text):
    """Parse 'HH:MM-HH:MM:RATE' (or 'HH:MM-HH:MM=RATE') into a Window."""
    # Split the rate off the trailing '=' or ':'. Times keep their own colon.
    if "=" in text:
        span, _, rate_str = text.rpartition("=")
    else:
        span, _, rate_str = text.rpartition(":")
    m = _WIN_SPAN_RE.match(span)
    if not m or not rate_str.strip():
        raise argparse.ArgumentTypeError(
            f"window must be START-END:RATE with HH:MM times, got {text!r}"
        )
    start_str, end_str = m.group(1), m.group(2)
    start = _parse_clock(start_str)
    end = _parse_clock(end_str)
    rate = parse_rate(rate_str)
    label = f"{start_str}-{end_str}"
    return Window(start, end, rate, label)


def second_of_day(dt):
    return dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6


def human_bytes(n):
    """Format a byte count with an SI-ish suffix."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.2f} {unit}"
        n /= 1000


def human_rate(bps):
    if bps is None:
        return "unlimited"
    return human_bytes(bps) + "/s"


def format_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


class TokenBucket:
    """Continuously-refilling byte bucket. rate=None disables limiting.

    `capacity` (the burst size) bounds how many tokens can accumulate while
    output is stalled, and therefore how large a catch-up burst can be once it
    resumes. Keep it small to smooth traffic on high-latency links; make it
    larger to hold the average rate through jittery output.
    """

    def __init__(self, rate, burst):
        self.rate = rate
        self.capacity = max(1, int(burst)) if rate else 0
        # Start empty so there is no burst at startup.
        self.tokens = 0.0
        self.last = time.monotonic()

    def refill(self, now):
        if self.rate is None:
            return
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now

    def take(self, want):
        """Consume up to `want` bytes; return how many were granted now."""
        if self.rate is None:
            return want
        grant = min(want, int(self.tokens))
        self.tokens -= grant
        return grant

    def eta(self, want):
        """Seconds until `want` tokens are available (0 if unlimited/ready)."""
        if self.rate is None or self.tokens >= want:
            return 0.0
        return (want - self.tokens) / self.rate


class RateMeter:
    """Sliding-window throughput estimate over the last RATE_WINDOW seconds."""

    def __init__(self):
        self.samples = []  # (timestamp, bytes)
        self.total = 0

    def add(self, now, n):
        self.samples.append((now, n))
        self.total += n

    def rate(self, now):
        cutoff = now - RATE_WINDOW
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.pop(0)
        span = now - self.samples[0][0] if self.samples else 0.0
        # Need a couple of samples over a meaningful span; a single fresh sample
        # over a near-zero span yields a wild spike (e.g. just after a reset).
        if len(self.samples) < 2 or span < 0.1:
            return 0.0
        moved = sum(n for _, n in self.samples)
        return moved / span


class WindowStats:
    """Per-window running totals, reset at each window boundary."""

    def __init__(self, label, rate, start_time):
        self.label = label
        self.rate = rate
        self.start_time = start_time
        self.bytes = 0


def active_window(windows, base_rate, sod):
    """Return (rate, label) for the given second-of-day. First match wins."""
    for w in windows:
        if w.contains(sod):
            return w.rate, w.label
    return base_rate, "base"


class StatusPrinter:
    """Renders live stats to stderr, one repainting line + frozen summaries."""

    def __init__(self, stream, use_tty):
        self.stream = stream
        self.use_tty = use_tty
        self.line_dirty = False

    def live(self, label, rate_cap, current_rate, window_bytes, total_bytes):
        text = (
            f"[{label}] cap {human_rate(rate_cap)} | "
            f"now {human_rate(current_rate)} | "
            f"window {human_bytes(window_bytes)} | "
            f"total {human_bytes(total_bytes)}"
        )
        if self.use_tty:
            # \r to column 0, \x1b[K clears to end of line.
            self.stream.write("\r\x1b[K" + text)
        else:
            self.stream.write(text + "\n")
        self.stream.flush()
        self.line_dirty = self.use_tty

    def freeze_summary(self, stats, now, reason):
        """End the current live line with a permanent window summary."""
        elapsed = max(now - stats.start_time, 1e-9)
        avg = stats.bytes / elapsed
        text = (
            f"[{stats.label}] {reason}: moved {human_bytes(stats.bytes)} "
            f"in {format_duration(elapsed)} "
            f"(cap {human_rate(stats.rate)}, avg {human_rate(avg)})"
        )
        if self.use_tty and self.line_dirty:
            self.stream.write("\r\x1b[K")
        self.stream.write(text + "\n")
        self.stream.flush()
        self.line_dirty = False


def _chunk_size(bucket):
    """Bytes to write per iteration: capped at the buffer and the burst size."""
    if bucket.rate is None:
        return BUFSIZE
    return max(1, min(BUFSIZE, bucket.capacity))


def run(base_rate, windows, burst, stdin, stdout, status):
    now = time.monotonic()
    sod = second_of_day(datetime.now())
    rate, label = active_window(windows, base_rate, sod)

    bucket = TokenBucket(rate, burst_for(rate, burst))
    chunk_size = _chunk_size(bucket)
    meter = RateMeter()
    stats = WindowStats(label, rate, now)
    total_bytes = 0
    last_status = 0.0
    eof = False

    while not eof:
        now = time.monotonic()
        sod = second_of_day(datetime.now())

        # Detect a window boundary and roll over per-window accounting.
        cur_rate, cur_label = active_window(windows, base_rate, sod)
        if cur_label != stats.label:
            status.freeze_summary(stats, now, "window ended")
            rate, label = cur_rate, cur_label
            bucket = TokenBucket(rate, burst_for(rate, burst))
            chunk_size = _chunk_size(bucket)
            stats = WindowStats(label, rate, now)

        bucket.refill(now)
        # Aim to emit one evenly-sized chunk. If the tokens for a full chunk
        # aren't there yet, sleep exactly long enough to earn them (bounded by
        # the status interval so stats/window checks stay responsive), then send
        # whatever we have. Small chunks + short waits = smooth, non-bursty output.
        wait = min(bucket.eta(chunk_size), STATUS_INTERVAL)
        if wait > 0:
            time.sleep(wait)
            bucket.refill(time.monotonic())
        allowed = bucket.take(chunk_size)

        if allowed >= 1:
            chunk = stdin.read(allowed)
            if not chunk:
                eof = True
            else:
                stdout.write(chunk)
                stdout.flush()
                n = len(chunk)
                # Refund any tokens we reserved but didn't use (short read).
                if n < allowed:
                    bucket.tokens += allowed - n
                now = time.monotonic()
                meter.add(now, n)
                stats.bytes += n
                total_bytes += n

        if now - last_status >= STATUS_INTERVAL or eof:
            status.live(
                label, rate, meter.rate(time.monotonic()),
                stats.bytes, total_bytes,
            )
            last_status = now

    status.freeze_summary(stats, time.monotonic(), "EOF")
    return total_bytes


def build_parser():
    p = argparse.ArgumentParser(
        prog="pacer",
        description="Pipe stdin to stdout at a time-of-day-dependent bandwidth cap.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "RATE accepts sizes like 1MB, 500KB, 20MiB, 1.5GB, or 'unlimited'.\n"
            "SI units (KB/MB/GB) are powers of 1000; binary (KiB/MiB/GiB) are\n"
            "powers of 1024. A trailing '/s' is optional.\n\n"
            "WINDOW is START-END:RATE using 24h HH:MM clock times, e.g.\n"
            "  -w 01:00-03:00:20MB   -w 23:00-02:00:unlimited (wraps midnight)\n"
            "Windows are matched in the order given; the first match wins and\n"
            "overrides the base rate. Times are local.\n\n"
            "BURST is the token-bucket capacity: the most that can be sent in one\n"
            "catch-up spike after the output stalls. It defaults to ~0.1s of the\n"
            "active rate. Lower it (e.g. --burst 16KB) for smoother output on\n"
            "high-latency links (VPN/satellite); raise it to better hold the\n"
            "average rate through bursty output.\n"
        ),
    )
    p.add_argument(
        "-r", "--rate", type=parse_rate, default=None, metavar="RATE",
        help="base rate when no window matches (default: unlimited)",
    )
    p.add_argument(
        "-w", "--window", type=parse_window, action="append", default=[],
        dest="windows", metavar="START-END:RATE",
        help="time-of-day override; repeatable",
    )
    p.add_argument(
        "-b", "--burst", type=parse_size, default=None, metavar="SIZE",
        help="max burst size (default: ~0.1s of the active rate)",
    )
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    use_tty = sys.stderr.isatty()
    status = StatusPrinter(sys.stderr, use_tty)

    # Restore default SIGINT so Ctrl-C raises KeyboardInterrupt for clean summary.
    signal.signal(signal.SIGINT, signal.default_int_handler)

    try:
        run(args.rate, args.windows, args.burst, stdin, stdout, status)
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        sys.stderr.flush()
        return 130
    except BrokenPipeError:
        # Downstream consumer closed; exit quietly.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
