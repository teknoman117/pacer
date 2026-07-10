# pacer — design

## Purpose and goals

`pacer` copies bytes from stdin to stdout while holding throughput under a cap
that changes with the time of day. The motivating use case is bulk transfer over
a link with an ISP fair-use policy: run fast overnight, throttle during the day,
all from a single long-lived pipe. Secondary goals:

- **Drop-in pipe filter.** Data rides on stdout so `pacer` slots into any shell
  pipeline (`producer | pacer … | consumer`). Everything human-facing goes to
  stderr.
- **Live, legible progress.** A single self-rewriting status line shows the
  active cap, current rate, and totals; each time the schedule changes, the
  finished window's summary is frozen on its own line.
- **Smooth output**, even when the downstream link is high-latency.
- **Zero runtime dependencies.** Standard library only.

Non-goals: it does not rate-limit by shaping the kernel/TCP layer, does not
persist state across runs, and does not try to hit a rate more precisely than
the OS scheduler's sleep granularity allows.

## Module layout

```
src/pacer/
  core.py       all logic: parsing, metering, the pump loop, the CLI
  __init__.py   re-exports the public API + __version__
  __main__.py   enables `python -m pacer`
tests/
  test_pacer.py pytest suite
pyproject.toml  setuptools build + `pacer` console-script entry point
```

`core.py` is intentionally a single cohesive module — the pieces are small and
tightly related, and keeping them together makes the data flow easy to follow.

## Data flow

```
stdin.buffer ──▶ [ token bucket gate ] ──▶ stdout.buffer
                        │
                        ├─▶ RateMeter    (sliding-window "current rate")
                        ├─▶ WindowStats  (per-window byte total)
                        └─▶ StatusPrinter (stderr: live line + frozen summaries)
```

The core is a single loop in `run()`:

1. Read the wall clock and pick the active rate for this instant
   (`active_window`).
2. If the active window changed since last iteration, freeze the outgoing
   window's summary line and start fresh per-window accounting.
3. Refill the token bucket, wait just long enough to earn one evenly-sized
   chunk (bounded by the status interval), then read that many bytes.
4. Write them to stdout and flush; update the meter and counters.
5. Repaint the status line roughly every `STATUS_INTERVAL` (0.2s).

The loop exits on EOF (empty read), then prints a final summary.

## Rate limiting: the token bucket

Rate limiting uses a classic **token bucket** (`TokenBucket`). Tokens are bytes;
they accrue continuously at `rate` bytes/second and are spent as data is sent.
Two parameters matter:

- **`rate`** — the fill rate (`None` means unlimited: the gate is a no-op).
- **`capacity`** (the *burst*) — the maximum tokens that can accumulate. This is
  the single most important knob for output smoothness (see below).

`take(want)` grants `min(want, floor(tokens))`; `eta(want)` reports how long
until `want` tokens exist. The bucket starts **empty**, so there is no burst at
startup.

### The burst / smoothness problem

The original implementation set `capacity = max(rate, BUFSIZE)` — a full
**second** of traffic. On a low-latency link that is invisible. On a high-RTT
link (VPN, satellite) it is not: the downstream `write()` blocks whenever the
socket send buffer fills, and while it is blocked wall-clock time passes but no
tokens are spent. The bucket refills to its full one-second capacity, and the
instant the write unblocks, that entire second is dumped at once — then the
stream goes quiet while the bucket refills again. The result is visible
stop/start bursting.

Two changes fix this:

1. **Small default burst.** `capacity` defaults to `DEFAULT_BURST_SECONDS`
   (0.1s) of the active rate, floored at `MIN_BURST` (4 KiB) so very low rates
   still write reasonably sized chunks. This bounds any catch-up spike to ~0.1s
   of data regardless of how long output was stalled.
2. **Even pacing.** Each iteration targets one chunk of
   `min(BUFSIZE, capacity)` bytes and sleeps *exactly* long enough to earn it,
   rather than draining a full bucket in a back-to-back loop. Tokens reserved
   but not used (a short read) are refunded, so the long-run average stays
   exact.

`--burst SIZE` overrides the default: shrink it for maximum smoothness on a bad
link, grow it to better hold the average rate across bursty output. This tradeoff
is the reason it is exposed rather than hard-coded.

Note the honest limit: `pacer` only governs what it writes into the pipe. The
kernel socket buffer, TCP slow-start, and the VPN can still reshape traffic
downstream — but the token hoarding, which *was* under our control, is fixed.

### Chunking and responsiveness

`BUFSIZE` (64 KiB) caps a single read/write so the loop stays responsive at high
rates — it repaints stats and re-checks the window boundary frequently. Waits
are always capped at `STATUS_INTERVAL`, so even at a crawl the status line and
schedule checks keep ticking. Idle waiting is a single `time.sleep`, so a paused
stream costs ~no CPU (measured: 0.04s CPU over 5s at 8 KB/s).

## Time-of-day windows

A `Window` is a half-open second-of-day span `[start, end)` mapped to a rate
(bytes/s, or `None` for unlimited). Design points:

- **Midnight wrap.** If `end <= start` the window wraps (`23:00-07:00` covers
  23:00→24:00 and 00:00→07:00, i.e. 8 hours). `contains()` handles both cases;
  no special syntax is needed.
- **First match wins.** `active_window` scans windows in CLI order and returns
  the first that contains the current time; if none match, the base rate applies
  under the label `base`. This gives predictable precedence for overlapping
  windows.
- **Local time.** Schedules are expressed against the machine's local clock,
  matching how ISP windows ("1am–3am") are described.

### Grammar and a parsing subtlety

Windows are `START-END:RATE` (or `START-END=RATE`), with `START`/`END` as 24h
`HH:MM`. The rate is split off the trailing `:`/`=`. The catch: `:` separates
both the digits of a time *and* the rate. Requiring **mandatory minutes**
(`HH:MM`, validated by regex) removes the ambiguity — a bare trailing hour like
`03` can't masquerade as `HH:MM`, so `01:00-03:00` (rate omitted) fails to parse
instead of being silently read as end `03` with rate `00`. Minute granularity is
also the right resolution for a daily bandwidth schedule; second precision is not
supported by design.

Rates and sizes accept SI units (`KB`/`MB`/`GB` = powers of 1000), binary units
(`KiB`/`MiB`/`GiB` = powers of 1024), an optional trailing `/s`, and the aliases
`unlimited`/`inf`/`none`/`max`/`0` (rates only) for "no cap".

## Current-rate meter

`RateMeter` keeps `(timestamp, bytes)` samples and reports throughput over the
last `RATE_WINDOW` (1s), evicting older samples. It reports `0` until it has at
least two samples spanning ≥0.1s — this guards against the spike a naive
`bytes/span` produces right after a window reset, when a single fresh sample over
a near-zero span would read as an absurd multi-GB/s rate.

## Status output and terminal control

`StatusPrinter` writes to stderr and adapts to whether stderr is a TTY:

- **TTY:** the live line is repainted in place with `\r` (carriage return) +
  `\x1b[K` (clear to end of line). Window summaries clear the live line, print
  the summary, and end with `\n`, leaving the counter to continue below.
- **Non-TTY** (redirected/piped stderr): each update is a plain newline-
  terminated line, so logs stay readable.

Summaries report bytes moved, elapsed time, the cap, and the achieved average —
emitted on a window boundary, at EOF, and on Ctrl-C.

## Process behavior

- stdin/stdout are used in **binary** mode (`sys.std*.buffer`); stdout is flushed
  after every write so downstream sees data promptly rather than in Python
  buffer-sized gulps.
- **TTY guard.** As a pipe filter, pacer guards its data streams: if exactly one
  of stdin/stdout is a terminal it exits 2 with a "cowardly refusing to
  read/write to a tty" message (reading a terminal would hang on typed input;
  writing one would dump raw bytes at the user); if *both* are terminals — bare
  `pacer` with no redirection — there's nothing to pump, so it just prints help
  and exits 0. `--force` bypasses the guard entirely. (stderr may always be a
  tty; that is where the live status goes.)
- **SIGINT** is restored to the default handler so Ctrl-C raises
  `KeyboardInterrupt`; `main()` prints the in-progress summary and exits 130.
- **BrokenPipeError** (downstream closed early, e.g. `| head`) exits 0 quietly.

## Testing

`tests/test_pacer.py` (pytest) covers the parsing grammar and its edge cases,
midnight-wrap membership, first-match-wins selection, burst sizing, the token
bucket (empty start, capacity cap, grant/deplete, eta, unlimited), the rate
meter (spike guard + eviction), formatting, and end-to-end pumps through `run()`
that assert byte-for-byte fidelity, a rate lower-bound, and the EOF summary.

Timing-dependent behavior is tested with generous lower bounds (never tight
upper bounds) to stay non-flaky; the deterministic bucket/meter logic is tested
directly rather than through wall-clock timing.

## Packaging

Standard `pyproject.toml` with the setuptools backend and a `src/` layout. The
`pacer` console script is declared via `[project.scripts]`
(`pacer = "pacer.core:main"`); `python -m pacer` works via `__main__.py`. Install
with `pip install .` (or `-e .` for development, `.[test]` to pull in pytest).
