"""Tests for pacer.

These formalize the behavior that was checked by hand during development:
rate/size/window parsing, midnight-wrapping windows, first-match-wins window
selection, the token bucket's rate and burst limiting, the sliding-window rate
meter (including its post-reset spike guard), human-readable formatting, and an
end-to-end pump through ``run`` that both preserves bytes and honors the cap.
"""

import argparse
import io
import sys
import time

import pytest

from pacer import core
from pacer.core import (
    RateMeter,
    StatusPrinter,
    TokenBucket,
    Window,
    active_window,
    burst_for,
    format_duration,
    human_bytes,
    human_rate,
    parse_rate,
    parse_size,
    parse_window,
    run,
    second_of_day,
)

HOUR = 3600


# --------------------------------------------------------------------------- #
# Rate / size parsing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "text, expected",
    [
        ("1MB", 1_000_000),
        ("20MB/s", 20_000_000),
        ("500KB", 500_000),
        ("1.5GB", 1_500_000_000),
        ("20MiB", 20 * 1024**2),
        ("1024", 1024),
        ("100b", 100),
        ("2M", 2_000_000),
    ],
)
def test_parse_rate_values(text, expected):
    assert parse_rate(text) == expected


@pytest.mark.parametrize("text", ["unlimited", "inf", "none", "max", "0"])
def test_parse_rate_unlimited(text):
    assert parse_rate(text) is None


@pytest.mark.parametrize("text", ["5ZB", "abc", "", "MB"])
def test_parse_rate_invalid(text):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_rate(text)


def test_parse_size_rejects_unlimited_and_zero():
    assert parse_size("16KB") == 16_000
    with pytest.raises(argparse.ArgumentTypeError):
        parse_size("unlimited")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_size("0")


# --------------------------------------------------------------------------- #
# Burst sizing
# --------------------------------------------------------------------------- #

def test_burst_default_is_fraction_of_rate():
    assert burst_for(20_000_000, None) == int(20_000_000 * core.DEFAULT_BURST_SECONDS)


def test_burst_default_floored_for_low_rates():
    assert burst_for(1000, None) == core.MIN_BURST


def test_burst_override_wins():
    assert burst_for(1_000_000, 16_000) == 16_000


def test_burst_unlimited_is_zero():
    assert burst_for(None, None) == 0


# --------------------------------------------------------------------------- #
# Window parsing
# --------------------------------------------------------------------------- #

def test_parse_window_basic():
    w = parse_window("01:00-03:00:20MB")
    assert (w.start, w.end, w.rate, w.wraps) == (1 * HOUR, 3 * HOUR, 20_000_000, False)
    assert w.label == "01:00-03:00"


def test_parse_window_equals_separator():
    assert parse_window("01:00-03:00=20MB").rate == 20_000_000


def test_parse_window_single_digit_hour():
    assert parse_window("1:00-3:00:1MB").start == 1 * HOUR


def test_parse_window_unlimited():
    assert parse_window("03:00-07:00:unlimited").rate is None


def test_parse_window_wrap_detected():
    assert parse_window("23:00-02:00:1MB").wraps is True


@pytest.mark.parametrize(
    "text",
    [
        "01:00-03:00",        # rate omitted -> must not be mis-read as end "03"/rate "00"
        "01:00-03:00:",       # empty rate
        "0103",               # no span
        "25:00-26:00:1MB",    # hour out of range
        "1-3:1MB",            # minutes required
    ],
)
def test_parse_window_invalid(text):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_window(text)


# --------------------------------------------------------------------------- #
# Window membership and selection
# --------------------------------------------------------------------------- #

def test_window_contains_normal():
    w = parse_window("09:30-17:00:1MB")
    assert w.contains(12 * HOUR)
    assert not w.contains(9 * HOUR)      # before start
    assert not w.contains(17 * HOUR)     # end is exclusive


def test_window_wrap_is_eight_hours():
    w = parse_window("23:00-7:00:unlimited")
    inside = [23 * HOUR, 23 * HOUR + 59 * 60, 0, 3 * HOUR, 7 * HOUR - 1]
    outside = [22 * HOUR + 59 * 60, 22 * HOUR, 7 * HOUR, 12 * HOUR]
    for sod in inside:
        assert w.contains(sod), sod
    for sod in outside:
        assert not w.contains(sod), sod


def test_active_window_first_match_wins():
    w_all = parse_window("00:00-24:00:1MB")
    w_fast = parse_window("01:00-03:00:9MB")
    rate, label = active_window([w_all, w_fast], base_rate=None, sod=2 * HOUR)
    assert rate == 1_000_000 and label == "00:00-24:00"


def test_active_window_falls_back_to_base():
    w = parse_window("01:00-03:00:9MB")
    rate, label = active_window([w], base_rate=500_000, sod=12 * HOUR)
    assert rate == 500_000 and label == "base"


def test_second_of_day():
    import datetime as dt
    assert second_of_day(dt.datetime(2026, 7, 10, 1, 2, 3)) == HOUR + 2 * 60 + 3


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #

def test_human_bytes():
    assert human_bytes(500) == "500 B"
    assert human_bytes(1_500_000) == "1.50 MB"


def test_human_rate():
    assert human_rate(None) == "unlimited"
    assert human_rate(1_000_000) == "1.00 MB/s"


def test_format_duration():
    assert format_duration(5) == "5s"
    assert format_duration(65) == "1m05s"
    assert format_duration(3665) == "1h01m05s"


# --------------------------------------------------------------------------- #
# Token bucket
# --------------------------------------------------------------------------- #

def test_bucket_starts_empty():
    b = TokenBucket(1_000_000, burst=100_000)
    assert b.tokens == 0.0


def test_bucket_refill_caps_at_capacity():
    b = TokenBucket(1_000_000, burst=100_000)
    b.last = time.monotonic() - 10  # pretend 10s elapsed -> would be 10MB uncapped
    b.refill(time.monotonic())
    assert b.tokens == 100_000


def test_bucket_take_grants_and_depletes():
    b = TokenBucket(1_000_000, burst=100_000)
    b.tokens = 50_000
    assert b.take(30_000) == 30_000
    assert b.tokens == 20_000
    # Can't grant more than is available.
    assert b.take(30_000) == 20_000


def test_bucket_unlimited_grants_everything():
    b = TokenBucket(None, burst=0)
    assert b.take(1 << 20) == (1 << 20)
    assert b.eta(1 << 30) == 0.0


def test_bucket_eta():
    b = TokenBucket(1_000_000, burst=100_000)
    b.tokens = 0
    assert b.eta(100_000) == pytest.approx(0.1, rel=1e-6)
    b.tokens = 100_000
    assert b.eta(100_000) == 0.0


# --------------------------------------------------------------------------- #
# Rate meter
# --------------------------------------------------------------------------- #

def test_meter_needs_two_samples():
    m = RateMeter()
    m.add(100.0, 1000)
    assert m.rate(100.0) == 0.0  # single fresh sample -> no spike


def test_meter_computes_rate_over_window():
    m = RateMeter()
    m.add(100.0, 500_000)
    m.add(100.5, 500_000)
    # 1,000,000 bytes across a 0.5s span -> ~2 MB/s.
    assert m.rate(100.5) == pytest.approx(2_000_000, rel=0.01)


def test_meter_evicts_old_samples():
    m = RateMeter()
    m.add(100.0, 1_000_000)     # older than RATE_WINDOW at query time
    m.add(101.5, 100_000)
    m.add(102.0, 100_000)
    r = m.rate(102.0)
    # The 100.0 sample is >1s before 102.0 and must be dropped.
    assert m.samples[0][0] == 101.5
    assert r == pytest.approx(200_000 / 0.5, rel=0.01)


# --------------------------------------------------------------------------- #
# End-to-end pump
# --------------------------------------------------------------------------- #

def _silent_status():
    return StatusPrinter(io.StringIO(), use_tty=False)


def test_run_passthrough_unlimited_preserves_bytes():
    data = bytes(range(256)) * 1000  # 256 KB
    out = io.BytesIO()
    moved = run(None, [], None, io.BytesIO(data), out, _silent_status())
    assert out.getvalue() == data
    assert moved == len(data)


def test_run_respects_rate_cap():
    # 200 KB at 1 MB/s should take at least ~0.16s (generous lower bound to
    # avoid flakiness) and never less than the metered time.
    data = b"x" * 200_000
    out = io.BytesIO()
    start = time.monotonic()
    run(1_000_000, [], None, io.BytesIO(data), out, _silent_status())
    elapsed = time.monotonic() - start
    assert out.getvalue() == data
    assert elapsed >= 0.16


def test_run_emits_eof_summary():
    status = _silent_status()
    run(None, [], None, io.BytesIO(b"hello"), io.BytesIO(), status)
    assert "EOF: moved" in status.stream.getvalue()


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #

def test_parser_accepts_full_invocation():
    args = core.build_parser().parse_args(
        ["-r", "1MB", "-w", "01:00-03:00:20MB", "-w", "03:00-07:00:unlimited",
         "--burst", "16KB"]
    )
    assert args.rate == 1_000_000
    assert len(args.windows) == 2
    assert args.burst == 16_000


def test_parser_rejects_bad_window():
    with pytest.raises(SystemExit):
        core.build_parser().parse_args(["-w", "01:00-03:00"])


# --------------------------------------------------------------------------- #
# main(): tty guard and happy path
# --------------------------------------------------------------------------- #

class _FakeStd:
    """Stand-in for sys.stdin/stdout with a controllable isatty().

    ``buffer`` carries the binary payload pacer pumps; text writes (e.g.
    argparse's help output) are captured separately in ``text``.
    """

    def __init__(self, data=b"", tty=False):
        self.buffer = io.BytesIO(data)
        self.text = io.StringIO()
        self._tty = tty

    def isatty(self):
        return self._tty

    def write(self, s):
        return self.text.write(s)

    def flush(self):
        pass


def _wire_std(monkeypatch, stdin, stdout):
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", io.StringIO())


def test_main_refuses_to_read_from_tty(monkeypatch):
    _wire_std(monkeypatch, _FakeStd(tty=True), _FakeStd(tty=False))
    rc = core.main([])
    assert rc == 2
    assert "cowardly refusing to read from a tty" in sys.stderr.getvalue()


def test_main_refuses_to_write_to_tty(monkeypatch):
    _wire_std(monkeypatch, _FakeStd(b"data", tty=False), _FakeStd(tty=True))
    rc = core.main([])
    assert rc == 2
    assert "cowardly refusing to write to a tty" in sys.stderr.getvalue()


def test_main_both_ttys_prints_help(monkeypatch):
    out = _FakeStd(tty=True)
    _wire_std(monkeypatch, _FakeStd(tty=True), out)
    rc = core.main([])
    assert rc == 0
    assert "usage: pacer" in out.text.getvalue()


def test_main_force_bypasses_tty_guard(monkeypatch):
    data = b"forced" * 50
    out = _FakeStd(tty=True)
    _wire_std(monkeypatch, _FakeStd(data, tty=True), out)
    rc = core.main(["--force"])
    assert rc == 0
    assert out.buffer.getvalue() == data


def test_main_pipes_when_neither_is_a_tty(monkeypatch):
    data = b"payload" * 100
    out = _FakeStd(tty=False)
    _wire_std(monkeypatch, _FakeStd(data, tty=False), out)
    rc = core.main([])  # unlimited, no windows
    assert rc == 0
    assert out.buffer.getvalue() == data
