# pacer

Pipe stdin to stdout at a bandwidth cap that varies by time of day.

Set a base rate, then layer on zero or more time-of-day *windows* that override
it — e.g. crawl during the day but open the taps overnight to respect an ISP's
fair-use policy. Live transfer stats (total moved, current rate) are printed to
**stderr** on a single self-rewriting line; when the active window changes, that
window's summary is frozen on its own line and a fresh counter starts below it.

Data flows on stdout, so pacer drops transparently into any pipeline.

## Install

```
pip install .            # or: pip install -e .   for development
```

This puts a `pacer` command on your `PATH`. Requires Python 3.8+ and has no
runtime dependencies. You can also run it without installing via
`python -m pacer` (from `src/`) or `python src/pacer/core.py`.

## Usage

```
generate | pacer -r 1MB -w 01:00-03:00:20MB -w 03:00-07:00:unlimited | consume
```

- `-r, --rate RATE` — base rate when no window matches (default: unlimited).
- `-w, --window START-END:RATE` — repeatable time-of-day override.
- `-b, --burst SIZE` — max catch-up burst (default: ~0.1s of the active rate).

### Smoothness on high-latency links (`--burst`)

pacer meters with a token bucket. Its *capacity* (the burst size) is how many
tokens can pile up while the output is stalled — and therefore how big a
catch-up spike is emitted once it resumes. On a high-latency link (VPN,
satellite) the downstream `write()` blocks often as the send buffer fills, so a
large capacity produces visible stop/start bursts.

The burst defaults to ~0.1s of the active rate (small), and writes are paced in
even chunks. If you still see burstiness, lower it:

```
tar cf - /data | pacer -r 512KB --burst 16KB | ssh sat-host 'cat > backup.tar'
```

Smaller `--burst` = smoother output but less able to recover the average rate
across output jitter; larger = burstier but holds the average through stalls.

### Rates

`1MB`, `500KB`, `20MiB`, `1.5GB`, `100` (bytes), or `unlimited` (also `inf`,
`0`, `max`). SI units (`KB`/`MB`/`GB`) are powers of 1000; binary units
(`KiB`/`MiB`/`GiB`) are powers of 1024. A trailing `/s` is optional.

### Windows

`START-END:RATE` using 24-hour `HH:MM` clock times (local time). Minutes are
required. `=` may be used instead of the last `:` for clarity
(`01:00-03:00=20MB`). A window whose end is at or before its start wraps across
midnight (`23:00-02:00:unlimited`). Windows are matched in the order given —
**the first match wins** and overrides the base rate.

## Examples

```
# 1 MB/s baseline; 20 MB/s from 1–3am; uncapped 3–7am
pacer -r 1MB -w 01:00-03:00:20MB -w 03:00-07:00:unlimited < in > out

# Uncapped overnight, throttled during business hours
pacer -r unlimited -w 09:00-18:00:2MB < in > out

# Back up over the network without saturating the daytime link
tar cf - /data | pacer -r 512KB -w 00:00-06:00:10MB | ssh host 'cat > backup.tar'
```

Ctrl-C prints the in-progress window's summary and exits; a closed downstream
(e.g. `| head`) exits quietly.

## Development

```
pip install -e '.[test]'
pytest
```

See [DESIGN.md](DESIGN.md) for the architecture and the rationale behind the
token bucket, burst sizing, and the window grammar.

## License

BSD 3-Clause — see [LICENSE](LICENSE).
Nathan Lewis &lt;git@nrlewis.dev&gt;
