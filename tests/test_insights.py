"""Pure insight functions over 5-minute history."""

from datetime import UTC, date, datetime, timedelta

from app.insights import Point, daily_runtime, downsample, merge_room, occupancy, time_to_target

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)  # Sunday 6:00 AM Central
STEP = timedelta(minutes=5)


def pts(temps, heat, cool, **kw):
    """Points 5 minutes apart; heat/cool may be a constant or a list."""
    n = len(temps)
    heat = heat if isinstance(heat, list) else [heat] * n
    cool = cool if isinstance(cool, list) else [cool] * n
    return [Point(T0 + i * STEP, temps[i], heat[i], cool[i], kw.get("outdoor"),
                  kw.get("heat_s", [0] * n)[i], kw.get("cool_s", [0] * n)[i],
                  kw.get("occ", [None] * n)[i]) for i in range(n)]


def test_time_to_target_measures_minutes_to_reach_new_band():
    # Setback 62 until index 2, then 68; the room climbs 1°F per 5 min from 63.
    temps = [63, 63, 63, 64, 65, 66, 67, 67.6, 68, 68]
    heat = [62, 62, 68, 68, 68, 68, 68, 68, 68, 68]
    w = time_to_target(pts(temps, heat, 80))
    assert len(w) == 1
    assert (w[0].kind, w[0].target, w[0].start_temp) == ("heat", 68, 63)
    assert w[0].started == T0 + 2 * STEP
    assert w[0].minutes == 25  # index 7 (67.6 ≥ 68 − 0.5)


def test_cooldown_and_never_reached():
    temps = [79, 79, 79, 78.5, 78, 77.5, 77]
    cool = [80, 72, 72, 72, 72, 72, 72]
    w = time_to_target(pts(temps, 60, cool), give_up=timedelta(minutes=20))
    assert (w[0].kind, w[0].target) == ("cool", 72)
    assert w[0].minutes is None  # didn't get within 0.5°F of 72 in 20 min


def test_small_or_already_satisfied_steps_are_ignored():
    # 1°F bump, and a step up while the room is already warm enough.
    assert time_to_target(pts([70, 70, 70], [66, 67, 67], 80)) == []
    assert time_to_target(pts([70, 70, 70], [62, 68, 68], 80)) == []


def test_daily_runtime_in_local_days():
    # 12 intervals heating 300 s = 1 h; 6 cooling 150 s = 15 min.
    p = pts([70] * 18, 68, 72, heat_s=[300] * 12 + [0] * 6, cool_s=[0] * 12 + [150] * 6)
    days = daily_runtime(p, "America/Chicago")
    assert days == [(date(2026, 11, 15), 1.0, 0.25)]


def test_occupancy_conditioned_but_empty():
    # 6 intervals conditioned (68–72) and empty, 6 conditioned and occupied,
    # 6 at setback but occupied.
    heat = [68] * 12 + [62] * 6
    cool = [72] * 12 + [80] * 6
    occ = [False] * 6 + [True] * 12
    o = occupancy(pts([70] * 18, heat, cool, occ=occ), setback_heat=62, setback_cool=80)
    assert o == {"has_data": True, "conditioned_empty_h": 0.5, "occupied_setback_h": 0.5}
    assert occupancy(pts([70] * 3, 68, 72), 62, 80)["has_data"] is False


def test_merge_room_averages_thermostats():
    a = pts([70, 72], 68, 72, heat_s=[300, 0])
    b = pts([72, 74], 68, 72, heat_s=[0, 0], occ=[False, True])
    m = merge_room([a, b])
    assert [p.temp for p in m] == [71, 73]
    assert [p.heat_s for p in m] == [150, 0]
    assert [p.occupied for p in m] == [False, True]


def test_chart_renders_and_handles_empty():
    from app.charts import room_chart
    p = pts([63, 64, 65, 66], [62, 68, 68, 68], 80, outdoor=50, heat_s=[0, 300, 300, 150])
    svg = str(room_chart(p, T0, T0 + timedelta(hours=1), "America/Chicago"))
    assert svg.count('class="indoor"') == 1 and 'class="run-heat"' in svg
    assert "data-points" in svg
    assert "No history" in str(room_chart([], T0, T0 + timedelta(hours=1), "America/Chicago"))


def test_occupancy_ignores_thermostats_without_a_sensor():
    # No sensor: ecobee reports 0 (unoccupied) every interval, never 1.
    o = occupancy(pts([70] * 6, 68, 72, occ=[False] * 6), 62, 80)
    assert o["has_data"] is False


def test_downsample_averages_into_buckets():
    p = pts([60, 62, 64, 66, 68, 70], 68, 72, heat_s=[300, 300, 0, 0, 0, 0])
    d = downsample(p, timedelta(minutes=15))
    assert [x.temp for x in d] == [62, 68]
    assert [x.heat_s for x in d] == [200, 0]
    assert downsample(p, timedelta(minutes=5)) == p
