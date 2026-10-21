"""Engine scenarios: the spec's acceptance checklist plus edge cases.

Every scenario runs in the simulator (tests/sim.py), which asserts after
every step that no two non-bypassed valves are open at once.
"""

from __future__ import annotations

import copy

import pytest

from custom_components.irrigation_sequencer import engine as E
from custom_components.irrigation_sequencer.estimate import cycle_end_estimate
from custom_components.irrigation_sequencer.model import fix_order, normalize, resolve_order, swap_order

from .sim import World, hms, opts, ts, world

DAY = "2026-10-12"  # a Monday (Sydney, daylight saving time)


def cyc(o: dict, c: int, time: str, minutes: list[int], order: list[int] | None = None,
        soak: list[bool] | None = None, days: list[str] | None = None) -> dict:
    o["cycles"][c]["time"] = time
    o["cycles"][c]["minutes"] = minutes
    if order:
        o["cycles"][c]["order"] = order
    if soak:
        o["cycles"][c]["soak"] = soak
    if days is not None:
        o["cycles"][c]["days"] = days
    return o


def assert_never_two(w: World) -> None:
    assert w.max_open <= 1, w.dump()


# --------------------------------------------------------------------------
# Sequencing
# --------------------------------------------------------------------------
def test_cycle_runs_in_run_order_with_gap():
    o = cyc(opts(3, 2), 0, "06:00", [2, 1, 3], order=[3, 1, 2])
    w = world(o, f"{DAY} 05:50")
    w.run_until(f"{DAY} 06:30")
    assert w.order() == ["T2", "T3", "T1"], w.dump()
    (a2, b2), = w.on_periods(1)
    (a3, b3), = w.on_periods(2)
    (a1, b1), = w.on_periods(0)
    assert a2 == ts(f"{DAY} 06:00")
    assert b2 - a2 == 60 and b3 - a3 == 180 and b1 - a1 == 120
    assert a3 - b2 == 5 and a1 - b3 == 5  # the gap
    assert any("Cycle 1 started at 06:00 (100%, 6 min of watering)" in t for t in w.texts("cycle"))
    assert any("Cycle 1 finished" in t for t in w.texts("cycle"))
    assert w.state.mode == E.IDLE and w.state.cycle == -1
    assert_never_two(w)


def test_two_cycles_follow_their_own_order():
    o = cyc(opts(3, 2), 0, "06:00", [1, 1, 1], order=[1, 2, 3])
    cyc(o, 1, "07:00", [1, 1, 1], order=[3, 2, 1])
    w = world(o, f"{DAY} 05:50")
    w.run_until(f"{DAY} 07:30")
    assert w.order() == ["T1", "T2", "T3", "T3", "T2", "T1"], w.dump()
    assert_never_two(w)


def test_old_shared_run_orders_are_renumbered_keeping_the_sequence():
    """Settings saved by 1.0.0 could give two taps the same number: they are renumbered on load."""
    o = cyc(opts(3, 1), 0, "06:00", [1, 1, 1], order=[2, 1, 1])
    assert normalize(o)["cycles"][0]["order"] == [3, 1, 2]
    w = world(o, f"{DAY} 05:50")
    w.run_until(f"{DAY} 06:20")
    assert w.order() == ["T2", "T3", "T1"]  # same sequence as before
    assert not any("more than one tap" in t for t in w.texts("config"))


def test_run_order_helpers():
    assert fix_order([1, 2, 3]) == [1, 2, 3]
    assert fix_order([3, 3, 3]) == [1, 2, 3]
    assert fix_order([2, 2, 1, 1, 3, 3]) == [3, 4, 1, 2, 5, 6]
    # the card: one tap picks a number another tap has -> the two swap
    assert swap_order([1, 2, 3, 4], 0, 3) == [3, 2, 1, 4]
    assert swap_order([1, 2, 3, 4], 3, 1) == [4, 2, 3, 1]
    assert swap_order([1, 2, 3, 4], 1, 2) == [1, 2, 3, 4]
    assert swap_order([1, 2, 3], 0, 9) == [3, 2, 1]  # clamped to the tap count
    # the settings form: every number edited at once
    assert resolve_order([1, 2, 3, 4], [4, 3, 2, 1]) == [4, 3, 2, 1]  # a full new order is kept
    assert resolve_order([1, 2, 3, 4], [1, 2, 1, 4]) == [3, 2, 1, 4]  # one change -> swap
    assert resolve_order([1, 2, 3, 4], [2, 2, 2, 4]) is None  # two changes, numbers shared -> ask again
    assert resolve_order([1, 2, 3], [1, 2, 3]) == [1, 2, 3]
    # normalize always ends with every number used once
    for order in ([1, 1], [5, 5, 5], [0, -3, 2], [2, 9, 9, 1]):
        got = normalize({"tap_count": len(order), "cycle_count": 1, "cycles": [{"order": order}]})
        assert sorted(got["cycles"][0]["order"]) == list(range(1, len(order) + 1)), (order, got)


def test_tap_over_safety_limit_runs_in_parts():
    o = cyc(opts(2, 1, general={"max_run_min": 20}), 0, "06:00", [45, 5])
    w = world(o, f"{DAY} 05:50")
    w.run_until(f"{DAY} 08:00")
    parts = w.on_periods(0)
    assert [b - a for a, b in parts] == [1200, 1200, 300], w.dump()
    assert w.watered(0) == 45 * 60
    assert any("over the 20 min safety limit" in t for t in w.texts("config"))
    assert_never_two(w)


def test_minimum_60_seconds_and_seasonal_120():
    o = cyc(opts(2, 1), 0, "06:00", [1, 10])
    w = world(o, f"{DAY} 05:50")
    w.state.seasonal = 120
    w.run_until(f"{DAY} 07:00")
    assert w.watered(0) == 72 and w.watered(1) == 720
    assert any("(120%, 13 min of watering)" in t for t in w.texts("cycle")), w.texts()


def test_seasonal_low_still_gives_60_seconds():
    o = cyc(opts(1, 1), 0, "06:00", [1])
    w = world(o, f"{DAY} 05:50")
    w.state.seasonal = 10
    w.run_until(f"{DAY} 06:30")
    assert w.watered(0) == 60


def test_seasonal_zero_skips_and_notifies():
    o = cyc(opts(2, 1), 0, "06:00", [2, 2])
    w = world(o, f"{DAY} 05:50")
    w.state.seasonal = 0
    w.run_until(f"{DAY} 06:30")
    assert w.log == []
    assert any("skipped: seasonal adjustment is 0%" in t for t in w.texts("skip"))


def test_seasonal_entity_unavailable_uses_100_and_warns():
    o = cyc(opts(1, 1, general={"seasonal_entity": "sensor.pct"}), 0, "06:00", [2])
    w = world(o, f"{DAY} 05:50")
    w.seasonal_ext = None
    w.run_until(f"{DAY} 06:30")
    assert w.watered(0) == 120
    assert any("Seasonal adjustment is unavailable" in t for t in w.texts("config"))


def test_cycle_without_minutes_is_silent():
    o = cyc(opts(2, 1), 0, "06:00", [0, 0])
    w = world(o, f"{DAY} 05:50")
    w.run_until(f"{DAY} 06:30")
    assert w.log == [] and w.msgs == []


def test_days_filter():
    o = cyc(opts(1, 1), 0, "06:00", [1], days=["tue"])
    w = world(o, f"{DAY} 05:50")  # Monday
    w.run_until(f"{DAY} 06:30")
    assert w.log == []
    w.run_until("2026-10-13 06:30")
    assert w.order() == ["T1"]


# --------------------------------------------------------------------------
# Manual runs
# --------------------------------------------------------------------------
def test_manual_before_cycle_runs_its_timer_then_cycle_starts_late():
    o = cyc(opts(3, 2), 0, "06:00", [1, 1, 1])
    cyc(o, 1, "18:00", [0, 3, 0])  # tap 2's longest is 3 min
    w = world(o, f"{DAY} 05:50")
    w.at(f"{DAY} 05:59", w.user_on(1))
    w.run_until(f"{DAY} 06:30")
    (a, b) = w.on_periods(1)[0]
    assert b - a == 180, w.dump()  # manual timer = longest minutes
    assert w.order()[0] == "T2" and w.order()[1:] == ["T1", "T2", "T3"]
    assert any("Manual run: Tap 2 for 3 min, until 06:02" in t for t in w.texts("manual"))
    assert any("2 min late (due 06:00)" in t for t in w.texts("cycle")), w.texts()
    assert_never_two(w)


def test_manual_timer_capped_and_all_zero_uses_safety_limit():
    o = cyc(opts(2, 1, general={"max_run_min": 20}), 0, "06:00", [45, 0])
    w = world(o, f"{DAY} 05:00")
    w.at(f"{DAY} 05:01", w.user_on(0))
    w.run_until(f"{DAY} 05:30")
    assert w.watered(0) == 1200
    w.at(f"{DAY} 05:35", w.user_on(1))  # tap 2 has no minutes anywhere: safety limit
    w.run_until(f"{DAY} 05:59")
    assert w.watered(1) == 1200


def test_manual_mid_cycle_pauses_and_resumes_with_remaining():
    o = cyc(opts(3, 1), 0, "06:00", [4, 2, 0])
    w = world(o, f"{DAY} 05:50")
    w.at(f"{DAY} 06:01", w.user_on(2))  # tap 3: never in a cycle -> safety limit 20 min
    w.run_until(f"{DAY} 07:00")
    p1 = w.on_periods(0)
    assert len(p1) == 2, w.dump()
    assert abs(w.watered(0) - 240) <= 1  # resumed with its remaining time
    assert w.on_periods(2)[0][0] == ts(f"{DAY} 06:01")
    assert any("Cycle 1 paused" in t for t in w.texts("manual"))
    assert any("Cycle 1 resumes" in t for t in w.texts("manual"))
    assert any("Cycle 1 finished" in t for t in w.texts("cycle"))
    assert_never_two(w)


def test_second_manual_tap_closes_the_first():
    o = cyc(opts(3, 1), 0, "06:00", [5, 5, 5])
    w = world(o, f"{DAY} 04:00")
    w.at(f"{DAY} 04:01", w.user_on(0))
    w.at(f"{DAY} 04:02", w.user_on(1))
    w.run_until(f"{DAY} 04:30")
    assert w.on_periods(0) == [(ts(f"{DAY} 04:01"), ts(f"{DAY} 04:02"))], w.dump()
    assert w.on_periods(1)[0] == (ts(f"{DAY} 04:02"), ts(f"{DAY} 04:07"))
    assert any("Tap 1 was closed" in t for t in w.texts("manual"))
    assert_never_two(w)


def test_manual_turned_off_by_hand():
    o = cyc(opts(1, 1), 0, "06:00", [5])
    w = world(o, f"{DAY} 04:00")
    w.at(f"{DAY} 04:01", w.user_on(0))
    w.at(f"{DAY} 04:03", w.user_off(0))
    w.run_until(f"{DAY} 04:30")
    assert w.state.mode == E.IDLE
    assert any("finished at 04:03 (turned off)" in t for t in w.texts("manual"))


def test_scheduled_tap_turned_off_by_hand_is_skipped():
    o = cyc(opts(3, 1), 0, "06:00", [3, 3, 3])
    w = world(o, f"{DAY} 05:50")
    w.at(f"{DAY} 06:01", w.user_off(0))
    w.run_until(f"{DAY} 06:30")
    assert w.watered(0) == 60 and w.watered(1) == 180 and w.watered(2) == 180, w.dump()
    assert any("Tap 1 was turned off during cycle 1, so it is skipped" in t for t in w.texts("cycle"))


# --------------------------------------------------------------------------
# Restart / outage recovery
# --------------------------------------------------------------------------
def test_restart_mid_tap_end_still_ahead():
    o = cyc(opts(2, 1), 0, "06:00", [10, 2])
    w = world(o, f"{DAY} 05:50")
    stop, start = w.restart()
    w.at(f"{DAY} 06:03", stop)
    w.at(f"{DAY} 06:05", start)
    w.run_until(f"{DAY} 06:30")
    assert w.on_periods(0) == [(ts(f"{DAY} 06:00"), ts(f"{DAY} 06:10"))], w.dump()  # no off/on blip
    assert w.watered(1) == 120
    rec = w.texts("recovery")
    assert rec and "Recovered after restart at 06:05: cycle 1 continues with Tap 1 until 06:10" in rec[0]


def test_restart_past_the_end_closes_and_continues():
    o = cyc(opts(2, 1), 0, "06:00", [2, 2])
    w = world(o, f"{DAY} 05:50")
    stop, start = w.restart()
    w.at(f"{DAY} 06:01", stop)
    w.at(f"{DAY} 06:10", start)
    w.run_until(f"{DAY} 06:30")
    assert w.on_periods(0) == [(ts(f"{DAY} 06:00"), ts(f"{DAY} 06:10"))]
    assert w.on_periods(1)[0][0] == ts(f"{DAY} 06:10:05")
    assert "Tap 1 closed (was due off at 06:02)" in w.texts("recovery")[0]


def test_restart_record_says_on_but_valve_off():
    o = cyc(opts(2, 1), 0, "06:00", [3, 2])
    w = world(o, f"{DAY} 05:50")
    stop, start = w.restart()
    w.at(f"{DAY} 06:01", stop)
    w.at(f"{DAY} 06:01:30", w.user_off(0))  # valve's own timer closed it while HA was down
    w.at(f"{DAY} 06:05", start)
    w.run_until(f"{DAY} 06:30")
    assert w.order() == ["T1", "T2"]
    assert not any("skipped" in t for t in w.texts())


def test_unknown_valve_on_after_restart_is_manual():
    o = cyc(opts(2, 1), 0, "06:00", [3, 2])
    w = world(o, f"{DAY} 04:00")
    stop, start = w.restart()
    w.at(f"{DAY} 04:01", stop)
    w.at(f"{DAY} 04:02", w.user_on(1))
    w.at(f"{DAY} 04:03", start)
    w.run_until(f"{DAY} 04:30")
    assert w.on_periods(1) == [(ts(f"{DAY} 04:02"), ts(f"{DAY} 04:05"))], w.dump()


def test_down_over_one_and_two_start_times_runs_missed_once():
    o = cyc(opts(2, 2), 0, "06:00", [1, 1])
    cyc(o, 1, "06:30", [2, 0])
    w = world(o, f"{DAY} 05:50")
    stop, start = w.restart()
    w.at(f"{DAY} 05:55", stop)
    w.at(f"{DAY} 07:00", start)
    w.run_until(f"{DAY} 08:00")
    assert w.order() == ["T1", "T2", "T1"], w.dump()  # cycle 1 then cycle 2, back to back
    late = [t for t in w.texts("cycle") if "late" in t]
    assert any(t.startswith("Running late: Cycle 1 started at 07:00, 60 min late") for t in late), late
    assert any("Cycle 2 started" in t and "late (due 06:30)" in t for t in late), late
    # the next day both run normally, once each
    w.run_until("2026-10-13 07:00")
    assert w.order() == ["T1", "T2", "T1", "T1", "T2", "T1"]


def test_missed_older_than_24h_is_ignored():
    o = cyc(opts(1, 1), 0, "06:00", [1], days=["mon"])
    w = world(o, f"{DAY} 05:50")
    stop, start = w.restart()
    w.at(f"{DAY} 05:55", stop)
    w.at("2026-10-13 07:00", start)  # Tuesday: Monday's 06:00 is 25 h ago
    w.run_until("2026-10-13 08:00")
    assert w.log == []


def test_first_ever_start_and_time_edit_cause_no_surprise_runs():
    o = cyc(opts(1, 1), 0, "06:00", [1])
    w = world(o, f"{DAY} 09:00")
    w.run_until(f"{DAY} 09:05")
    assert w.log == []
    o2 = copy.deepcopy(w.options)
    o2["cycles"][0]["time"] = "08:00"  # edited to a time already passed today
    w.set_options(o2)
    w.run_until(f"{DAY} 09:10")
    assert w.log == []
    w.run_until("2026-10-13 08:10")
    assert w.order() == ["T1"]


def test_corrupt_state_resets():
    st, ok = E.State.from_dict({"version": 1, "mode": "bogus", "master": True})
    assert not ok and st.mode == E.IDLE and st.master is True
    st, ok = E.State.from_dict({"version": 99})
    assert not ok
    st, ok = E.State.from_dict(None)
    assert not ok


def test_cycle_due_twice_while_running_is_not_doubled():
    o = cyc(opts(1, 1), 0, "06:00", [90], days=["mon", "tue"])
    o["general"]["max_run_min"] = 240
    w = world(o, f"{DAY} 05:50")
    w.run_until(f"{DAY} 08:00")
    assert w.watered(0) == 90 * 60


# --------------------------------------------------------------------------
# Switches and reminders
# --------------------------------------------------------------------------
def test_master_off_mid_cycle_cancels_and_reminds():
    o = cyc(opts(2, 2), 0, "06:00", [3, 3])
    cyc(o, 1, "07:00", [1, 1])
    w = world(o, f"{DAY} 05:50")

    def master_off(w):
        w.state.master = False
        return {"master_off"}

    def master_on(w):
        w.state.master = True
        return {"master_on"}

    w.at(f"{DAY} 06:01", master_off)
    w.run_until(f"{DAY} 06:55")
    assert w.on_periods(0) == [(ts(f"{DAY} 06:00"), ts(f"{DAY} 06:01"))]
    assert w.watered(1) == 0
    assert any("Cycle 1 stopped at 06:01: the master switch is off" in t for t in w.texts("cycle"))
    rem = [(hms(t), txt) for t, k, n, txt in w.msgs if k == "reminder"]
    assert rem == [("06:40:00", "Cycle 2 starts at 07:00 (in 20 min) but the master switch is off."),
                   ("06:50:00", "Cycle 2 starts at 07:00 (in 10 min) but the master switch is off.")], rem
    w.at(f"{DAY} 06:56", master_on)
    w.run_until(f"{DAY} 06:58")
    assert (ts(f"{DAY} 06:56"), "reminder_2") in w.clears
    w.run_until(f"{DAY} 07:10")
    assert w.order()[-2:] == ["T1", "T2"]


def test_reminder_cleared_when_cycle_time_passes():
    o = cyc(opts(1, 1), 0, "06:00", [1])
    w = world(o, f"{DAY} 05:00")
    w.state.master = False
    w.run_until(f"{DAY} 06:05")
    assert len(w.texts("reminder")) == 2
    assert (ts(f"{DAY} 06:00"), "reminder_1") in w.clears
    assert w.log == []


def test_cycle_switch_off_cancels_that_cycle_only():
    o = cyc(opts(2, 2), 0, "06:00", [3, 3])
    cyc(o, 1, "06:01", [1, 0])
    w = world(o, f"{DAY} 05:50")

    def c1_off(w):
        w.state.cycle_on[0] = False
        return {"cycle_off_0"}

    w.at(f"{DAY} 06:02", c1_off)
    w.run_until(f"{DAY} 06:30")
    assert w.on_periods(0)[0] == (ts(f"{DAY} 06:00"), ts(f"{DAY} 06:02"))
    assert w.order() == ["T1", "T1"]  # cycle 2 (pending) then runs tap 1
    assert any("Cycle 1 stopped at 06:02: its cycle switch is off" in t for t in w.texts("cycle"))


def test_manual_runs_work_with_master_off():
    o = cyc(opts(1, 1), 0, "06:00", [2])
    w = world(o, f"{DAY} 04:00")
    w.state.master = False
    w.at(f"{DAY} 04:01", w.user_on(0))
    w.run_until(f"{DAY} 04:30")
    assert w.watered(0) == 120


# --------------------------------------------------------------------------
# Cycle-and-soak
# --------------------------------------------------------------------------
def test_cycle_and_soak_fills_soak_time_with_other_taps():
    o = cyc(opts(3, 1, soak={"burst_min": 1, "soak_min": 2}), 0, "06:00", [3, 2, 2], soak=[True, False, False])
    w = world(o, f"{DAY} 05:50")
    w.run_until(f"{DAY} 07:00")
    # tap 1 (run order 1) goes again as soon as its soak is over; tap 3 fills the next soak
    assert w.order() == ["T1", "T2", "T1", "T3", "T1"], w.dump()
    assert w.watered(0) == 180 and w.watered(1) == 120 and w.watered(2) == 120
    p = w.on_periods(0)
    assert all(b - a == 60 for a, b in p)
    assert p[2][0] - p[1][1] >= 120  # rested at least the soak time
    assert_never_two(w)


def test_two_soak_taps_interleave():
    o = cyc(opts(2, 1, soak={"burst_min": 1, "soak_min": 1}), 0, "06:00", [2, 2], soak=[True, True])
    w = world(o, f"{DAY} 05:50")
    w.run_until(f"{DAY} 06:30")
    assert w.order() == ["T1", "T2", "T1", "T2"], w.dump()
    assert w.watered(0) == 120 and w.watered(1) == 120


@pytest.mark.parametrize(
    ("taps", "minutes", "order", "soak", "times"),
    [
        (3, [2, 1, 3], [3, 1, 2], None, ["06:00:30", "06:01:02", "06:02:30", "06:04:03", "06:05:30"]),
        (3, [3, 2, 2], None, [True, False, False],
         ["06:00:30", "06:01:02", "06:02:00", "06:03:30", "06:05:00", "06:06:30", "06:07:10"]),
        (2, [2, 2], None, [True, True], ["06:00:10", "06:01:03", "06:02:30", "06:03:30"]),
    ],
)
def test_cycle_end_estimate_matches_the_run(taps, minutes, order, soak, times):
    """The card's "time left": an estimate made at any point of a cycle is when watering really ends."""
    o = cyc(opts(taps, 1, soak={"burst_min": 1, "soak_min": 2}), 0, "06:00", minutes, order=order, soak=soak)
    w = world(o, f"{DAY} 05:50")
    est: list[tuple[str, float | None]] = []
    for hms_ in times:
        w.at(f"{DAY} {hms_}", lambda w_, h=hms_: est.append((h, cycle_end_estimate(w_.state, w_.cfg, w_.now))))
    assert cycle_end_estimate(w.state, w.cfg, w.now) is None  # nothing running yet
    w.run_until(f"{DAY} 07:00")
    last_off = max(b for i in range(taps) for _, b in w.on_periods(i))
    assert len(est) == len(times)
    assert all(e is not None and abs(e - last_off) <= 1 for _, e in est), (est, hms(last_off), w.dump())
    assert cycle_end_estimate(w.state, w.cfg, w.now) is None  # finished


def test_cycle_end_estimate_with_a_manual_run_in_the_middle():
    """A manual run pauses the cycle: once it is under way, the estimate includes the rest after it."""
    o = cyc(opts(3, 1), 0, "06:00", [4, 2, 0])
    w = world(o, f"{DAY} 05:50")
    w.at(f"{DAY} 06:01", w.user_on(2))  # tap 3 by hand: 20 min (safety limit), cycle 1 paused
    est: list[float | None] = []
    w.at(f"{DAY} 06:05", lambda w_: est.append(cycle_end_estimate(w_.state, w_.cfg, w_.now)))
    w.at(f"{DAY} 06:25", lambda w_: est.append(cycle_end_estimate(w_.state, w_.cfg, w_.now)))
    w.run_until(f"{DAY} 07:00")
    last_off = max(b for i in (0, 1) for _, b in w.on_periods(i))
    assert all(e is not None and abs(e - last_off) <= 1 for e in est), ([hms(e or 0) for e in est], w.dump())


# --------------------------------------------------------------------------
# Weather, soil, hot-day boost
# --------------------------------------------------------------------------
WX = {"rain_sensor": "sensor.rain", "rain_mm": 5, "weather_entity": "weather.home", "fc_prob": 60,
      "fc_mm": 3, "wind_sensor": "sensor.wind", "wind_kmh": 30, "temp_sensor": "sensor.temp",
      "cold_on": True, "cold_c": 2}


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ({"rain_state": "on"}, "rain sensor reports rain"),
        ({"rain_value": 6.2, "rain_state": "6.2"}, "rain sensor at 6.2 mm"),
        ({"weather_state": "pouring"}, "weather is pouring"),
        ({"forecast": {"precipitation_probability": 70}}, "forecast 70% chance of rain"),
        ({"forecast": {"precipitation": 4.5}}, "forecast 4.5 mm of rain"),
        ({"wind": 35.0}, "wind 35 km/h"),
        ({"temp": 1.5}, "only 1.5 °C"),
    ],
)
def test_weather_skip_each_check(setup, reason):
    o = cyc(opts(2, 1, weather=WX), 0, "06:00", [2, 2])
    w = world(o, f"{DAY} 05:50")
    for k, v in setup.items():
        setattr(w, k, v)
    w.run_until(f"{DAY} 06:30")
    assert w.log == [], w.dump()
    skip = w.texts("skip")
    assert skip and reason in skip[0], skip
    assert w.state.handled[0] == int(ts(f"{DAY} 06:00") // 60)  # counts as handled


def test_weather_skip_lists_every_reason_and_switch_off_waters():
    o = cyc(opts(1, 1, weather=WX), 0, "06:00", [2])
    w = world(o, f"{DAY} 05:50")
    w.rain_state, w.wind = "on", 40.0
    w.run_until(f"{DAY} 06:30")
    assert "rain sensor reports rain, wind 40 km/h" in w.texts("skip")[0]
    o2 = copy.deepcopy(w.options)
    o2["weather"]["on"] = False
    w.set_options(o2)
    w.run_until("2026-10-13 06:30")
    assert w.watered(0) == 120


def test_soil_per_tap():
    o = cyc(opts(3, 1, weather={"on": True}), 0, "06:00", [2, 2, 2])
    o["taps"][0].update(soil="sensor.soil_1", soil_pct=50)
    o["taps"][2].update(soil="sensor.soil_3", soil_pct=40)
    w = world(o, f"{DAY} 05:50")
    w.soil = [54.0, None, None]  # tap 1 wet, tap 2 no sensor (lawn), tap 3 unavailable
    w.soil_bad = [False, False, True]
    w.run_until(f"{DAY} 06:30")
    assert w.watered(0) == 0 and w.watered(1) == 120 and w.watered(2) == 120, w.dump()
    assert any("Skipped Tap 1 (soil 54%, wet enough at 50%)" in t for t in w.texts("skip"))
    assert any("soil sensor unavailable for Tap 3; watering as normal" in t for t in w.texts("config"))


def test_soil_every_tap_wet_skips_cycle():
    o = cyc(opts(2, 1), 0, "06:00", [2, 2])
    o["taps"][0].update(soil="sensor.a", soil_pct=50)
    o["taps"][1].update(soil="sensor.b", soil_pct=50)
    w = world(o, f"{DAY} 05:50")
    w.soil = [60.0, 51.0]
    w.run_until(f"{DAY} 06:30")
    assert w.log == []
    assert "skipped: soil wet enough for every tap" in w.texts("skip")[0]


def test_hot_day_boost_even_with_weather_skip_off():
    o = cyc(opts(1, 1, weather={"on": False, "temp_sensor": "sensor.t", "boost_pct": 150},
                 notify={"hot_c": 30}), 0, "06:00", [10])
    w = world(o, f"{DAY} 05:50")
    w.state.seasonal = 200
    w.temp = 33.0
    w.run_until(f"{DAY} 06:40")
    assert w.watered(0) == 25 * 60  # 200% x 150% capped at 250%
    assert any("hot-day boost at 33 °C" in t for t in w.texts("cycle"))


def test_too_hot_alert_once_after_10_min_with_next_cycle():
    o = cyc(opts(1, 1, weather={"temp_sensor": "sensor.t"}, notify={"hot_c": 30}), 0, "18:00", [1])
    w = world(o, f"{DAY} 13:00")
    w.temp = 31.0  # seen first at the 13:01 watchdog
    w.run_until(f"{DAY} 13:30")
    hot = [(hms(t), x) for t, k, n, x in w.msgs if k == "hot"]
    assert hot == [("13:11:00", "It is 31 °C (too hot at 30 °C). Next: Cycle 1 at Mon 18:00.")], hot
    w.temp = 29.5  # not 1 °C below: not re-armed
    w.run_until(f"{DAY} 13:40")
    w.temp = 31.0
    w.run_until(f"{DAY} 14:00")
    assert len(w.texts("hot")) == 1


def test_too_hot_with_master_off_mentions_it():
    o = cyc(opts(1, 1, weather={"temp_sensor": "sensor.t"}, notify={"hot_c": 30}), 0, "18:00", [1])
    w = world(o, f"{DAY} 13:00")
    w.state.master = False
    w.temp = 35.0
    w.run_until(f"{DAY} 13:20")
    assert "The master switch is OFF" in w.texts("hot")[0]


def test_started_raining_alert_is_debounced():
    o = cyc(opts(1, 1, weather={"rain_sensor": "binary_sensor.rain"}, notify={"rain": True}), 0, "18:00", [1])
    w = world(o, f"{DAY} 13:00")
    for k in range(6):  # the piezo flag flickers during rain
        w.rain_state = "on" if k % 2 == 0 else "off"
        w.run_until(ts(f"{DAY} 13:01") + k * 60)
    assert len(w.texts("rain")) == 1
    w.rain_state = "off"
    w.run_until(f"{DAY} 14:00")  # dry for 30 min: re-armed
    w.rain_state = "on"
    w.run_until(f"{DAY} 14:02")
    assert len(w.texts("rain")) == 2


# --------------------------------------------------------------------------
# People guard
# --------------------------------------------------------------------------
def test_people_guard_holds_back_pauses_resumes_and_gives_up():
    o = cyc(opts(3, 1, people={"on": True, "clear_min": 2, "max_wait_min": 30}), 0, "06:00", [2, 2, 2])
    w = world(o, f"{DAY} 05:50")
    w.people = [1, 0, 0]  # someone near tap 1 at the start

    def person(i, v, clear_at=0.0):
        def fn(w):
            w.people[i] = v
            w.people_clear_at[i] = clear_at
            return {"people"}
        return fn

    w.at(f"{DAY} 06:03", person(0, 2, ts(f"{DAY} 06:05")))  # leaves at 06:03, clear at 06:05
    w.at(f"{DAY} 06:05", person(0, 0))
    w.run_until(f"{DAY} 06:20")
    assert w.order()[:2] == ["T2", "T3"], w.dump()  # tap 1 held back, others first
    assert w.on_periods(0)[0][0] >= ts(f"{DAY} 06:05")
    # paused mid-run and resumed
    o2 = cyc(opts(2, 1, people={"on": True, "clear_min": 1}), 0, "06:00", [4, 1])
    w2 = world(o2, f"{DAY} 05:50")
    w2.at(f"{DAY} 06:01", person(0, 1))
    w2.at(f"{DAY} 06:03", person(0, 2, ts(f"{DAY} 06:04")))
    w2.at(f"{DAY} 06:04", person(0, 0))
    w2.run_until(f"{DAY} 06:30")
    assert w2.order() == ["T1", "T2", "T1"], w2.dump()
    assert abs(w2.watered(0) - 240) <= 1
    assert any("Tap 1 paused at 06:01: person detected" in t for t in w2.texts("people"))
    # gave up after the max wait
    o3 = cyc(opts(2, 1, people={"on": True, "max_wait_min": 10}), 0, "06:00", [1, 1])
    w3 = world(o3, f"{DAY} 05:50")
    w3.people = [1, 0]
    w3.run_until(f"{DAY} 06:30")
    assert w3.order() == ["T2"], w3.dump()
    assert any("skipped Tap 1 (people there for 10 min)" in t for t in w3.texts("people"))
    assert w3.state.mode == E.IDLE


def test_people_guard_off_and_manual_option():
    o = cyc(opts(1, 1, people={"on": False}), 0, "06:00", [2])
    w = world(o, f"{DAY} 05:50")
    w.people = [1]
    w.run_until(f"{DAY} 06:30")
    assert w.watered(0) == 120
    o2 = opts(1, 1, people={"on": True, "manual": True})
    w2 = world(o2, f"{DAY} 05:50")
    w2.at(f"{DAY} 05:51", w2.user_on(0))

    def walk_in(w):
        w.people[0] = 1
        return {"people"}

    w2.at(f"{DAY} 05:52", walk_in)
    w2.run_until(f"{DAY} 06:00")
    assert w2.on_periods(0) == [(ts(f"{DAY} 05:51"), ts(f"{DAY} 05:52"))]


# --------------------------------------------------------------------------
# Faults
# --------------------------------------------------------------------------
def test_valve_that_wont_close_blocks_everything_and_resumes():
    o = cyc(opts(2, 1), 0, "06:00", [1, 1])
    w = world(o, f"{DAY} 05:50")
    w.valves[0].responds_off = False

    def fix(w):
        w.valves[0].responds_off = True
        return set()

    w.at(f"{DAY} 06:02", w.user_on(1))  # a manual run while stuck: closed again
    w.at(f"{DAY} 06:08", fix)
    w.run_until(f"{DAY} 06:30")
    assert w.state.mode == E.IDLE, w.dump()
    p1 = w.on_periods(0)
    assert p1[0][0] == ts(f"{DAY} 06:00") and p1[0][1] == ts(f"{DAY} 06:11")  # retried at 06:06, 06:11
    t2 = w.on_periods(1)
    assert t2[0] == (ts(f"{DAY} 06:02"), ts(f"{DAY} 06:02"))  # closed again at once
    assert t2[1][0] > ts(f"{DAY} 06:11")  # the cycle resumed after tap 1 reported off
    faults = w.texts("tap")
    assert any("Could not confirm Tap 1 is off. Cycle 1 is paused" in t for t in faults), faults
    assert sum("Could not confirm" in t for t in faults) == 1  # notified once
    assert any("Tap 2 was closed again because Tap 1 cannot be confirmed off" in t for t in faults)
    assert any("Tap 1 is now confirmed off. Cycle 1 resumes." in t for t in faults)


def test_valve_offline_while_open_goes_stuck():
    o = cyc(opts(2, 1), 0, "06:00", [2, 1])
    w = world(o, f"{DAY} 05:50")

    def offline(w):
        w.valves[0].state = "offline"
        return {"valve"}

    def back_off(w):
        w.valves[0].state = "off"
        return {"valve"}

    w.at(f"{DAY} 06:01", offline)
    w.at(f"{DAY} 06:09", back_off)
    w.run_until(f"{DAY} 06:30")
    assert any("Tap 1 is offline and was due to stop at 06:02" in t for t in w.texts("tap")), w.texts()
    assert w.on_periods(1)[0][0] > ts(f"{DAY} 06:09")
    assert_never_two(w)


def test_other_valve_offline_when_next_tap_should_open_stops_cycle():
    o = cyc(opts(3, 1), 0, "06:00", [1, 1, 0])
    w = world(o, f"{DAY} 05:50")
    w.valves[2].state = "offline"
    w.run_until(f"{DAY} 06:30")
    assert w.log == []
    assert any("Cycle 1 stopped: cannot confirm Tap 3 is off" in t for t in w.texts("tap"))


def test_valve_that_wont_report_on_is_skipped():
    o = cyc(opts(2, 1), 0, "06:00", [1, 1])
    w = world(o, f"{DAY} 05:50")
    w.valves[0].responds_on = False
    w.run_until(f"{DAY} 06:30")
    assert w.watered(0) == 0 and w.watered(1) == 60
    assert any("Tap 1 did not report on; it is skipped this cycle" in t for t in w.texts("tap"))


def test_unconfigured_tap_is_ignored():
    o = cyc(opts(3, 1), 0, "06:00", [1, 1, 1])
    o["taps"][1]["valve"] = ""
    w = world(o, f"{DAY} 05:50")
    w.run_until(f"{DAY} 06:30")
    assert w.order() == ["T1", "T3"]


# --------------------------------------------------------------------------
# Bypass (pressure washer on Tap 1)
# --------------------------------------------------------------------------
def test_bypass_holds_cycles_until_off_then_runs_late():
    o = cyc(opts(3, 1), 0, "06:00", [1, 1, 1])
    o["taps"][0]["bypass"] = "input_boolean.pressure_washer_bypass"
    w = world(o, f"{DAY} 05:00")

    def bypass(v):
        def fn(w):
            w.bypass[0] = v
            return {"bypass"}
        return fn

    w.at(f"{DAY} 05:50", bypass(True))
    w.at(f"{DAY} 05:51", w.user_on(0))  # washer running on tap 1: not a manual run, no timer
    w.at(f"{DAY} 07:00", w.user_off(0))
    w.at(f"{DAY} 07:00", bypass(False))
    w.run_until(f"{DAY} 07:30")
    assert w.on_periods(0)[0] == (ts(f"{DAY} 05:51"), ts(f"{DAY} 07:00")), w.dump()
    assert not w.texts("manual")
    assert w.order() == ["T1", "T1", "T2", "T3"]
    assert any("Cycle 1 is waiting: bypass is on for Tap 1" in t for t in w.texts("cycle"))
    assert any("Running late: Cycle 1 started at 07:00, 60 min late" in t for t in w.texts("cycle")), w.texts()


def test_bypass_mid_cycle_pauses_and_resumes():
    o = cyc(opts(3, 1), 0, "06:00", [2, 2, 2])
    o["taps"][0]["bypass"] = "input_boolean.pw"
    w = world(o, f"{DAY} 05:50")

    def bypass(v):
        def fn(w):
            w.bypass[0] = v
            return {"bypass"}
        return fn

    w.at(f"{DAY} 06:03", bypass(True))  # tap 2 is watering
    w.at(f"{DAY} 06:20", bypass(False))
    w.run_until(f"{DAY} 06:40")
    assert w.on_periods(1)[0][1] == ts(f"{DAY} 06:03"), w.dump()
    assert abs(w.watered(1) - 120) <= 1 and w.watered(2) == 120
    assert any("Cycle 1 paused at 06:03: bypass is on for Tap 1" in t for t in w.texts("cycle"))
    assert any("Bypass off at 06:20: cycle 1 carries on" in t for t in w.texts("cycle"))


def test_bypass_tap_still_on_after_bypass_off_becomes_manual():
    o = cyc(opts(2, 1, general={"max_run_min": 20}), 0, "18:00", [5, 1])
    o["taps"][0]["bypass"] = "input_boolean.pw"
    w = world(o, f"{DAY} 05:00")

    def bypass(v):
        def fn(w):
            w.bypass[0] = v
            return {"bypass"}
        return fn

    w.at(f"{DAY} 05:01", bypass(True))
    w.at(f"{DAY} 05:02", w.user_on(0))
    w.at(f"{DAY} 06:00", bypass(False))
    w.run_until(f"{DAY} 06:30")
    assert w.on_periods(0) == [(ts(f"{DAY} 05:02"), ts(f"{DAY} 06:05"))], w.dump()


# --------------------------------------------------------------------------
# Services
# --------------------------------------------------------------------------
def test_services_run_cycle_run_tap_skip_tap_stop():
    o = cyc(opts(3, 2), 0, "06:00", [2, 2, 2])
    w = world(o, f"{DAY} 04:00")
    w.state.master = False  # run_cycle works with the master off

    def req(r):
        def fn(w):
            w.reconcile({"service"}, [r])
            return set()
        return fn

    w.at(f"{DAY} 04:01", req({"type": "run_cycle", "cycle": 0}))
    w.at(f"{DAY} 04:02", req({"type": "skip_tap", "tap": 1}))
    w.run_until(f"{DAY} 04:20")
    assert w.order() == ["T1", "T3"], w.dump()
    assert w.watered(0) == 120 and w.watered(1) == 0
    w.at(f"{DAY} 04:30", req({"type": "run_tap", "tap": 1, "seconds": 90}))
    w.run_until(f"{DAY} 04:40")
    assert w.on_periods(1) == [(ts(f"{DAY} 04:30"), ts(f"{DAY} 04:31:30"))]
    w.state.master = True
    w.at(f"{DAY} 04:50", req({"type": "run_cycle", "cycle": 0}))
    w.at(f"{DAY} 04:51", req({"type": "stop"}))
    w.run_until(f"{DAY} 05:00")
    assert w.on_periods(0)[-1] == (ts(f"{DAY} 04:50"), ts(f"{DAY} 04:51"))
    assert w.state.cycle == -1 and w.state.mode == E.IDLE
    assert_never_two(w)


def test_run_tap_mid_cycle_pauses_cycle():
    o = cyc(opts(2, 1), 0, "06:00", [3, 1])
    w = world(o, f"{DAY} 05:50")

    def req(w):
        w.reconcile({"service"}, [{"type": "run_tap", "tap": 1, "seconds": 60}])
        return set()

    w.at(f"{DAY} 06:01", req)
    w.run_until(f"{DAY} 06:30")
    assert abs(w.watered(0) - 180) <= 1, w.dump()
    assert w.watered(1) == 120
    assert_never_two(w)


# --------------------------------------------------------------------------
# Daylight saving and status
# --------------------------------------------------------------------------
def test_dst_change_days():
    # Sydney: clocks go back 2027-04-04 03:00 -> 02:00, forward 2026-10-04 02:00 -> 03:00
    o = cyc(opts(1, 1), 0, "06:00", [1])
    w = world(o, "2027-04-03 20:00")
    w.run_until("2027-04-05 07:00")
    assert len(w.on_periods(0)) == 2
    assert w.on_periods(0)[0][0] == ts("2027-04-04 06:00")
    w2 = world(o, "2026-10-03 20:00")
    w2.run_until("2026-10-05 07:00")
    assert [a for a, _b in w2.on_periods(0)] == [ts("2026-10-04 06:00"), ts("2026-10-05 06:00")]


def test_status_values():
    o = cyc(opts(2, 1, soak={"burst_min": 1, "soak_min": 3}), 0, "06:00", [2, 1], soak=[True, False])
    w = world(o, f"{DAY} 05:50")
    snap = w.snapshot(set())
    assert E.status(w.state, w.cfg, snap) == "idle"
    w.run_until(f"{DAY} 06:00:30")
    assert E.status(w.state, w.cfg, w.snapshot(set())) == "watering"
    w.run_until(f"{DAY} 06:02:30")
    assert E.status(w.state, w.cfg, w.snapshot(set())) == "soaking"


def test_long_random_soak_with_invariant():
    """A busy day: 6 taps x 4 cycles, soak, manual runs, restarts: never two open."""
    o = opts(6, 4, soak={"burst_min": 2, "soak_min": 3})
    cyc(o, 0, "06:00", [5, 8, 0, 12, 3, 6], order=[6, 5, 4, 3, 2, 1], soak=[True, False, False, True, False, False])
    cyc(o, 1, "06:20", [4, 0, 4, 0, 4, 0])
    cyc(o, 2, "07:00", [25, 1, 1, 1, 1, 1], soak=[False] * 6)
    cyc(o, 3, "19:00", [2] * 6, order=[2, 2, 1, 1, 3, 3])
    w = world(o, f"{DAY} 05:50")
    w.at(f"{DAY} 06:07", w.user_on(2))
    w.at(f"{DAY} 06:09", w.user_on(4))
    stop, start = w.restart()
    w.at(f"{DAY} 06:30", stop)
    w.at(f"{DAY} 06:33", start)
    w.at(f"{DAY} 07:05", w.user_off(0))
    w.run_until("2026-10-13 05:00")
    assert_never_two(w)
    assert w.state.mode == E.IDLE and not w.state.pending, w.dump()


# --------------------------------------------------------------------------
# Main water valve: tap-won't-close alert, close / open, hold cycles
# --------------------------------------------------------------------------
MAIN = {"valve": "light.main_water", "wait_min": 10}


def kinds(w: World, prefix: str = "main") -> list[tuple[str, str, str]]:
    return [(hms(t), k, txt) for t, k, _n, txt in w.msgs if k.startswith(prefix)]


def test_stuck_tap_asks_to_close_main_then_open_is_your_call():
    o = cyc(opts(2, 1, main=MAIN), 0, "06:00", [1, 1])
    w = world(o, f"{DAY} 05:50")
    w.valves[0].responds_off = False

    def fix(w):
        w.valves[0].responds_off = True
        return set()

    w.at(f"{DAY} 06:15", w.request({"type": "close_main"}))  # "Close main water" pressed
    w.at(f"{DAY} 06:18", fix)
    w.at(f"{DAY} 06:30", w.request({"type": "open_main"}))  # "Open main water" pressed
    w.run_until(f"{DAY} 06:45")
    assert_never_two(w)
    m = kinds(w)
    alerts = [x for x in m if x[1] == "main_alert"]
    assert len(alerts) == 1 and alerts[0][0] == "06:11:00", m  # stuck at 06:01, asked 10 min later
    assert "Tap 1 still can't be confirmed off after 10 min" in alerts[0][2]
    assert not [x for x in m if x[1] == "main_auto"]  # never closed by itself
    assert ("06:15:00", "main", ) == tuple(x[:2] for x in m if "Main water closed" in x[2])[0]
    prompt = [x for x in m if x[1] == "main_open"]
    assert len(prompt) == 1 and prompt[0][0] == "06:21:00", m  # retried at 06:21: now off -> asked to open
    assert ("MAIN off" in [ev for _t, ev in w.log]) and w.main.state == "on"
    assert [hms(t) for t, ev in w.log if ev.startswith("MAIN")] == ["06:15:00", "06:30:00"], w.dump()
    t2 = w.on_periods(1)
    assert t2 and t2[0][0] >= ts(f"{DAY} 06:30"), w.dump()  # the cycle waited for the main water
    cyc_txt = w.texts("cycle")
    assert any("Cycle 1 paused at 06:15: the main water is off" in t for t in cyc_txt), cyc_txt
    assert any("Main water on at 06:30: cycle 1 carries on" in t for t in cyc_txt), cyc_txt
    assert any("Tap 1 is now confirmed off. Cycle 1 waits for the main water." in t for t in w.texts("tap"))
    assert w.state.mode == E.IDLE and not w.state.stuck_since and not w.state.main_prompt


def test_stuck_tap_closes_main_automatically_when_set():
    o = cyc(opts(2, 1, main={**MAIN, "auto_close": True, "wait_min": 5}), 0, "06:00", [1, 1])
    w = world(o, f"{DAY} 05:50")
    w.valves[0].responds_off = False
    w.run_until(f"{DAY} 06:20")
    m = kinds(w)
    assert [hms(t) for t, ev in w.log if ev == "MAIN off"] == ["06:06:00"], w.dump()
    assert not [x for x in m if x[1] == "main_alert"]
    auto = [x for x in m if x[1] == "main_auto"]
    assert len(auto) == 1 and "after 5 min, so the main water is being closed (automatic)" in auto[0][2]
    assert any("Main water closed at 06:06." in x[2] for x in m)
    assert w.main.state == "off" and w.state.mode == E.STUCK  # still retrying the tap; main stays closed
    assert w.on_periods(1) == []  # nothing else opened


def test_main_closed_by_hand_answers_the_alert():
    o = cyc(opts(2, 1, main=MAIN), 0, "06:00", [1, 1])
    w = world(o, f"{DAY} 05:50")
    w.valves[0].responds_off = False

    def fix(w):
        w.valves[0].responds_off = True
        return set()

    w.at(f"{DAY} 06:13", w.main_to("off"))
    w.at(f"{DAY} 06:14", fix)
    w.run_until(f"{DAY} 06:30")
    m = kinds(w)
    assert any(x[0] == "06:13:00" and x[1] == "main" and "The main water is off (06:13)" in x[2] for x in m), m
    assert not w.state.main_ask
    assert any(x[1] == "main_open" for x in m)  # tap fixed at the 06:16 retry: asked to open it again
    assert w.main.state == "off" and w.on_periods(1) == []  # never opened by the app


def test_main_water_off_holds_cycles_and_they_run_late():
    o = cyc(opts(2, 1, main=MAIN), 0, "06:00", [1, 1])
    w = world(o, f"{DAY} 05:00")
    w.at(f"{DAY} 05:55", w.main_to("off"))
    w.at(f"{DAY} 06:40", w.main_to("on"))
    w.run_until(f"{DAY} 06:20")
    assert E.status(w.state, w.cfg, w.snapshot(set())) == "held"
    w.run_until(f"{DAY} 07:00")
    assert w.order() == ["T1", "T2"] and w.on_periods(0)[0][0] == ts(f"{DAY} 06:40"), w.dump()
    txt = w.texts("cycle")
    assert any("Cycle 1 is waiting: the main water is off" in t for t in txt), txt
    assert any("Main water on at 06:40: cycle 1 carries on" in t for t in txt), txt
    assert any("Running late: Cycle 1 started at 06:40, 40 min late" in t for t in txt), txt


def test_main_water_off_mid_cycle_pauses_the_tap_and_keeps_its_time():
    o = cyc(opts(2, 1, main=MAIN), 0, "06:00", [2, 2])
    w = world(o, f"{DAY} 05:50")
    w.at(f"{DAY} 06:00:30", w.main_to("off"))
    w.at(f"{DAY} 06:10", w.main_to("on"))
    w.run_until(f"{DAY} 06:30")
    assert_never_two(w)
    assert w.on_periods(0)[0] == (ts(f"{DAY} 06:00"), ts(f"{DAY} 06:00:30")), w.dump()
    assert w.on_periods(0)[1][0] == ts(f"{DAY} 06:10")
    assert abs(w.watered(0) - 120) <= 1 and abs(w.watered(1) - 120) <= 1
    assert any("Cycle 1 paused at 06:00: the main water is off" in t for t in w.texts("cycle"))


def test_without_a_main_valve_nothing_changes():
    o = cyc(opts(2, 1), 0, "06:00", [1, 1])
    w = world(o, f"{DAY} 05:50")
    w.valves[0].responds_off = False
    w.run_until(f"{DAY} 06:40")
    assert not kinds(w)
    assert any(t.endswith("Nothing else will open; retrying every 5 min.") for t in w.texts("tap")), w.texts("tap")


def test_main_requests_when_already_there_and_stuck_message_mentions_the_plan():
    o = cyc(opts(2, 1, main=MAIN), 0, "06:00", [1, 1])
    w = world(o, f"{DAY} 05:50")
    w.valves[0].responds_off = False
    w.at(f"{DAY} 05:55", w.request({"type": "open_main"}))
    w.run_until(f"{DAY} 06:03")
    assert any("The main water is already on." in x[2] for x in kinds(w)), kinds(w)
    assert any("you'll be asked to close the main water" in t for t in w.texts("tap")), w.texts("tap")


def test_old_saved_state_without_main_fields_loads():
    d = E.State().to_dict()
    for k in ("stuck_since", "main_alerted", "main_ask", "main_prompt", "main_note"):
        d.pop(k)
    st, ok = E.State.from_dict(d)
    assert ok and st.stuck_since == 0.0 and st.main_ask is False


def test_main_off_reminds_over_and_over_snooze_pauses_then_it_starts_again():
    o = cyc(opts(2, 1, main={**MAIN, "remind_min": 30}), 0, "06:00", [0, 0])
    w = world(o, f"{DAY} 08:00")
    w.at(f"{DAY} 08:05", w.main_to("off"))
    w.at(f"{DAY} 09:10", w.request({"type": "snooze_main", "hours": 2}))
    w.at(f"{DAY} 11:50", w.main_to("on"))
    w.run_until(f"{DAY} 13:00")
    rem = [x for x in kinds(w) if x[1] == "main_remind"]
    assert [x[0] for x in rem] == ["08:35:00", "09:05:00", "11:10:00", "11:40:00"], kinds(w)
    assert "off since Mon 08:05 (30 min)" in rem[0][2] and "(3 h 35 min)" in rem[3][2], rem
    assert any("snoozed for 2 h, until Mon 11:10" in x[2] for x in kinds(w)), kinds(w)
    assert (ts(f"{DAY} 11:50"), "main") in w.clears  # back on: the reminder disappears from the phone
    assert w.state.main_off_since == 0 and w.state.main_snooze_until == 0


def test_main_off_reminds_before_a_cycle_even_while_snoozed():
    o = cyc(opts(2, 1, main={**MAIN, "remind_min": 30}), 0, "06:00", [1, 1])
    w = world(o, f"{DAY} 04:55")
    w.at(f"{DAY} 05:00", w.main_to("off"))
    w.at(f"{DAY} 05:01", w.request({"type": "snooze_main", "hours": 5}))
    w.run_until(f"{DAY} 06:05")
    m = kinds(w)
    pre = [x for x in m if x[1] == "main_cycle"]
    assert [x[0] for x in pre] == ["05:40:00", "05:50:00"], m
    assert "Cycle 1 starts at 06:00 (in 20 min) but the main water is off" in pre[0][2]
    assert not [x for x in m if x[1] == "main_remind"]  # snoozed until 10:01
    assert (ts(f"{DAY} 06:00"), "main_cycle_1") in w.clears
    assert w.on_periods(0) == [] and any("Cycle 1 is waiting: the main water is off" in t for t in w.texts("cycle"))


def test_main_reminders_switched_off_and_resume_now():
    o = cyc(opts(2, 1, main={**MAIN, "remind_min": 0, "remind_cycle": False}), 0, "06:00", [1, 1])
    w = world(o, f"{DAY} 04:00")
    w.at(f"{DAY} 04:05", w.main_to("off"))
    w.run_until(f"{DAY} 07:00")
    assert not [x for x in kinds(w) if x[1] in ("main_remind", "main_cycle")], kinds(w)
    w.set_options(cyc(opts(2, 1, main={**MAIN, "remind_min": 60}), 0, "06:00", [1, 1]))
    w.run_until(f"{DAY} 08:02")  # switched on at the 07:01 pass: first reminder one interval later
    assert [x[0] for x in kinds(w) if x[1] == "main_remind"] == ["08:01:00"], kinds(w)
    w.at(f"{DAY} 08:10", w.request({"type": "snooze_main", "hours": 3}))
    w.at(f"{DAY} 08:20", w.request({"type": "snooze_main", "hours": 0}))  # "remind me again now"
    w.run_until(f"{DAY} 08:30")
    assert [x[0] for x in kinds(w) if x[1] == "main_remind"] == ["08:01:00", "08:20:00"], kinds(w)
    assert any("reminders are back on" in x[2] for x in kinds(w))


def test_main_reminder_while_a_tap_is_stuck_offers_no_open_button_and_open_by_app_needs_no_clear():
    o = cyc(opts(2, 1, main={**MAIN, "auto_close": True, "wait_min": 5, "remind_min": 15}), 0, "06:00", [1, 1])
    w = world(o, f"{DAY} 05:50")
    w.valves[0].responds_off = False
    w.run_until(f"{DAY} 06:25")
    m = kinds(w)
    assert [x[:2] for x in m if x[1].startswith("main_remind")] == [("06:21:00", "main_remind_stuck")], m
    assert "Tap 1 still can't be confirmed off." in [x for x in m if x[1] == "main_remind_stuck"][0][2]
    w.valves[0].responds_off = True
    w.run_until(f"{DAY} 06:40")  # confirmed off at the 06:26 retry; plain reminders from then on
    w.at(f"{DAY} 06:45", w.request({"type": "open_main"}))
    w.run_until(f"{DAY} 07:00")
    assert w.main.state == "on" and (ts(f"{DAY} 06:45"), "main") not in w.clears
    assert any(x[1] == "main_remind" and x[0] == "06:36:00" for x in kinds(w)), kinds(w)
