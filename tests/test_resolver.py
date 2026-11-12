from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.resolver import (
    BlockIn,
    EventIn,
    OverrideIn,
    ZoneCfg,
    next_change,
    parse_hvac_tags,
    resolve,
)

CT = ZoneInfo("America/Chicago")
Z = ZoneCfg(occupied_heat=68, occupied_cool=72, unoccupied_heat=60, unoccupied_cool=82,
            min_setpoint=60, max_setpoint=85, lead_minutes=45, tail_minutes=15)
# Wednesday 2026-11-11 10:00 Central
NOW = datetime(2026, 11, 11, 10, 0, tzinfo=CT)


def ev(start_h, end_h, tag=None):
    return EventIn(NOW.replace(hour=start_h), NOW.replace(hour=end_h), "Mtg", tag, "e1")


def test_default_is_unoccupied():
    d = resolve(Z, NOW)
    assert (d.heat, d.cool, d.source) == (60, 82, "default")


def test_calendar_lead_time_counts():
    d = resolve(Z, NOW, events=[EventIn(NOW + timedelta(minutes=30), NOW + timedelta(hours=2))])
    assert (d.heat, d.cool, d.source) == (68, 72, "calendar")


def test_calendar_outside_lead_ignored():
    d = resolve(Z, NOW, events=[EventIn(NOW + timedelta(minutes=50), NOW + timedelta(hours=2))])
    assert d.source == "default"


def test_tail_keeps_conditioning():
    d = resolve(Z, NOW, events=[EventIn(NOW - timedelta(hours=1), NOW - timedelta(minutes=10))])
    assert d.source == "calendar"


def test_nohvac_tag_ignored():
    d = resolve(Z, NOW, events=[ev(9, 11, {"ignore": True})])
    assert d.source == "default"


def test_event_tag_setpoints_and_guardrails():
    d = resolve(Z, NOW, events=[ev(9, 11, {"cool": 55})])
    # cool clamped up to min 60, then deadband pushes it apart from heat 68
    assert d.source == "calendar" and d.cool - d.heat >= 4 and d.heat >= 60


def test_override_beats_calendar():
    o = OverrideIn(1, "custom", 70, 74, NOW - timedelta(minutes=5), NOW + timedelta(hours=1), 7)
    d = resolve(Z, NOW, overrides=[o], events=[ev(9, 11)])
    assert (d.heat, d.cool, d.source) == (70, 74, "override")


def test_thermostat_override_beats_group_override():
    g = OverrideIn(2, "custom", 66, 76, NOW - timedelta(hours=1), NOW + timedelta(hours=1), 1)
    t = OverrideIn(0, "custom", 69, 73, NOW - timedelta(hours=2), NOW + timedelta(hours=1), 2)
    assert resolve(Z, NOW, overrides=[g, t]).ref == "override:2"


def test_off_is_widest_band_not_hvac_off():
    o = OverrideIn(1, "off", None, None, NOW - timedelta(minutes=1), NOW + timedelta(hours=1))
    d = resolve(Z, NOW, overrides=[o])
    assert (d.heat, d.cool, d.intent) == (60, 85, "off")


def test_schedule_block_weekday():
    wed = 1 << 2
    b = BlockIn(wed, time(8), time(12), "occupied")
    assert resolve(Z, NOW, blocks=[b]).source == "schedule"
    assert resolve(Z, NOW + timedelta(days=1), blocks=[b]).source == "default"


def test_zone_block_beats_group_block():
    every = 127
    zb = BlockIn(every, time(8), time(12), "occupied", id=1)
    gb = BlockIn(every, time(8), time(12), "unoccupied", level=1, id=2)
    assert resolve(Z, NOW, blocks=[gb, zb]).ref == "block:1"


def test_overnight_block_tail_is_active_next_morning():
    tue = 1 << 1
    b = BlockIn(tue, time(22), time(2), "occupied")
    one_am_wed = datetime(2026, 11, 11, 1, 0, tzinfo=CT)
    assert resolve(Z, one_am_wed, blocks=[b]).source == "schedule"
    # Wednesday is also set and it's 1am: still the tail of Tuesday night.
    b2 = BlockIn(tue | (1 << 2), time(22), time(2), "occupied")
    assert resolve(Z, one_am_wed, blocks=[b2]).source == "schedule"
    assert resolve(Z, one_am_wed.replace(hour=3), blocks=[b2]).source == "default"


def test_next_change_is_event_lead_start():
    e = EventIn(NOW + timedelta(hours=2), NOW + timedelta(hours=3))
    t = next_change(Z, NOW, events=[e])
    assert t == NOW + timedelta(hours=1, minutes=15)  # 45 min lead


def test_next_change_capped_at_horizon():
    assert next_change(Z, NOW, horizon=timedelta(hours=4)) == NOW + timedelta(hours=4)


def test_parse_tags():
    assert parse_hvac_tags("Choir #NoHVAC") == {"ignore": True}
    assert parse_hvac_tags("x", "please #hvac:cool=70 and #hvac:heat=66") == {"cool": 70, "heat": 66}
    assert parse_hvac_tags("plain", None) is None


def test_next_change_lands_on_the_boundary_not_the_step_after():
    # 1:14 PM Wednesday; an 8–17 block ends at 17:00 sharp, not 17:04.
    now = datetime(2026, 11, 11, 13, 14, 37, tzinfo=CT)
    b = BlockIn(weekdays=127, start=time(8), end=time(17))
    assert next_change(Z, now, blocks=[b]) == datetime(2026, 11, 11, 17, 0, tzinfo=CT)


def test_next_change_is_a_whole_minute_stable_across_calls():
    # The override ends between two whole minutes, on the coarse step itself. The hold end
    # must be the same whole minute whether asked now or a minute later, or the reconciler
    # sees a "later" end every minute and keeps re-sending.
    now = NOW + timedelta(seconds=9.9)
    o = OverrideIn(1, "custom", 70, 74, now - timedelta(minutes=1),
                   now + timedelta(minutes=10) - timedelta(seconds=0.1), 7)
    first = next_change(Z, now, overrides=[o])
    assert first == NOW + timedelta(minutes=11)
    assert next_change(Z, now + timedelta(minutes=1), overrides=[o]) == first


def test_block_priority_zone_then_group_then_building():
    every = 127
    zb = BlockIn(every, time(8), time(12), "occupied", level=0, id=1)
    gb = BlockIn(every, time(8), time(12), "unoccupied", level=1, id=2)
    bb = BlockIn(every, time(8), time(12), "off", level=2, id=3)
    assert resolve(Z, NOW, blocks=[bb, gb, zb]).ref == "block:1"
    assert resolve(Z, NOW, blocks=[bb, gb]).ref == "block:2"
    assert resolve(Z, NOW, blocks=[bb]).intent == "off"


def test_building_override_is_lowest_override_level():
    b = OverrideIn(3, "off", None, None, NOW - timedelta(hours=1), NOW + timedelta(hours=1), 9)
    z = OverrideIn(1, "custom", 70, 74, NOW - timedelta(hours=2), NOW + timedelta(hours=1), 8)
    assert resolve(Z, NOW, [b, z]).ref == "override:8"
    assert resolve(Z, NOW, [b]).ref == "override:9"


def test_same_booking_in_two_rooms_counts_once():
    from app.resolver import EventIn
    a = EventIn(NOW, NOW + timedelta(hours=1), "Youth night", None, "evt1", room="Kids 101")
    b = EventIn(NOW, NOW + timedelta(hours=1), "Youth night", None, "evt1", room="Kids 102")
    d = resolve(Z, NOW, events=[a, b])
    assert d.source == "calendar" and d.ref == "event:evt1"


# ---- special days (closures / special hours), campus overrides, all-day bookings ----

def _day(**kw):
    from app.resolver import DayIn
    base = {"first": NOW.date(), "last": NOW.date(), "id": 1}
    return DayIn(**(base | kw))


def test_closure_replaces_the_weekly_schedule_but_not_bookings():
    block = BlockIn(127, time(8), time(17), "occupied", id=1)
    closed = _day(closed=True, closed_mode="off", label="Thanksgiving")
    d = resolve(Z, NOW, blocks=[block], days=[closed])
    assert (d.source, d.intent, d.ref) == ("special", "off", "day:1")
    # A booking still wins…
    assert resolve(Z, NOW, events=[ev(10, 11)], blocks=[block], days=[closed]).source == "calendar"
    # …unless the closure says to ignore bookings too.
    hard = _day(closed=True, ignore_bookings=True)
    assert resolve(Z, NOW, events=[ev(10, 11)], days=[hard]).source == "special"
    # Other days are untouched.
    other = _day(first=NOW.date() + timedelta(days=1), last=NOW.date() + timedelta(days=2))
    assert resolve(Z, NOW, blocks=[block], days=[other]).source == "schedule"


def test_special_hours_and_building_beats_campus():
    hours = _day(closed=False, start=time(9), end=time(12), mode="occupied", level=1, id=2)
    assert resolve(Z, NOW, days=[hours]).intent == "occupied"
    assert resolve(Z, NOW.replace(hour=13), days=[hours]).intent == "unoccupied"
    bld = _day(closed=True, closed_mode="off", level=0, id=3)
    assert resolve(Z, NOW, days=[hours, bld]).ref == "day:3"


def test_next_change_sees_a_closure_starting_at_midnight():
    tomorrow = _day(first=NOW.date() + timedelta(days=1), last=NOW.date() + timedelta(days=1),
                    closed=True, closed_mode="off")
    late = NOW.replace(hour=22)
    assert next_change(Z, late, days=[tomorrow]) == datetime(2026, 11, 12, 0, 0, tzinfo=CT)


def test_campus_override_is_below_building():
    c = OverrideIn(4, "off", None, None, NOW - timedelta(hours=1), NOW + timedelta(hours=1), 5)
    b = OverrideIn(3, "occupied", None, None, NOW - timedelta(hours=1), NOW + timedelta(hours=1), 6)
    assert resolve(Z, NOW, [c, b]).ref == "override:6"
    assert resolve(Z, NOW, [c], events=[ev(10, 11)]).intent == "off"  # beats bookings


def test_all_day_bookings_are_ignored_unless_tagged():
    allday = EventIn(NOW.replace(hour=0), NOW.replace(hour=0) + timedelta(days=1), "Painting",
                     None, "a1", all_day=True)
    assert resolve(Z, NOW, events=[allday]).source == "default"
    tagged = EventIn(allday.starts_at, allday.ends_at, "Retreat", {"on": True}, "a2",
                     all_day=True)
    assert resolve(Z, NOW, events=[tagged]).source == "calendar"
    assert parse_hvac_tags("Retreat #hvac:on") == {"on": True}
