"""Read-only estimates for the card (kept apart from the engine, which they only read)."""

from __future__ import annotations

from .engine import MANUAL, STUCK, WATERING, State
from .model import Cfg


def cycle_end_estimate(st: State, cfg: Cfg, now: float) -> float | None:
    """When the running (or paused) cycle's last tap should close, for the card's "time left".

    Plays the rest of the cycle forward the way engine._pick_next runs it: run order, the
    gap between taps, the safety limit and cycle-and-soak bursts and breaks. People
    waits and bypass holds cannot be known ahead, so they can only make it later.
    None when no cycle is running or a valve is stuck.
    """
    if st.cycle < 0 or st.cycle >= len(cfg.cycles) or st.mode == STUCK:
        return None
    c = cfg.cycles[st.cycle]
    T = len(cfg.taps)
    rem = [st.remaining[t] if t < len(st.remaining) and cfg.taps[t].valve else 0 for t in range(T)]
    ready = [st.ready[t] if t < len(st.ready) else 0.0 for t in range(T)]
    gap = max(cfg.gap_s, 0)
    maxrt = max(cfg.max_run_min * 60, 60)
    soak = max(cfg.soak_min, 0) * 60
    if st.mode in (WATERING, MANUAL):  # the open tap closes at st.end, then the gap
        end = max(st.end, now)
        nxt = end + gap
        if st.mode == WATERING and 0 <= st.tap < T and c.soak[st.tap] and rem[st.tap] > 0:
            ready[st.tap] = end + soak  # this burst's soak break starts when it closes
    else:  # between taps, soaking, waiting
        end = now
        nxt = max(st.end, now)
    for _ in range(10_000):  # bounded: every pass runs or waits for a tap
        cands = [t for t in range(T) if rem[t] > 0]
        if not cands:
            break
        ready_now = [t for t in cands if ready[t] <= nxt]
        if not ready_now:
            nxt = min(ready[t] for t in cands)
            continue
        b = min(ready_now, key=lambda t: (c.order[t] if 1 <= c.order[t] <= 99 else 99, t))
        run = min(rem[b], cfg.burst_min * 60) if c.soak[b] else rem[b]
        run = max(min(run, maxrt), 1)
        rem[b] -= run
        end = nxt + run
        if c.soak[b] and rem[b] > 0:
            ready[b] = end + soak
        nxt = end + gap
    return end
