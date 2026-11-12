"""Is each managed thermostat set up so the app can do its job, and so it's safe if the app
stops? Pure functions (thermostat + zone settings in, plain-English problems out)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Issue:
    kind: str  # "mode" (stops the app working) | "program" (unsafe fallback)
    message: str


def mode_issue(name: str, hvac_mode: str | None) -> Issue | None:
    """Holds set both heat and cool; only `auto` lets the thermostat act on both."""
    if not hvac_mode or hvac_mode == "auto":
        return None
    what = {"off": "is turned off at the thermostat, so it won't heat or cool at all",
            "heat": "is set to heat only, so it won't cool",
            "auxHeatOnly": "is set to emergency heat only, so it won't cool",
            "cool": "is set to cool only, so it won't heat"}.get(
        hvac_mode, f"is in {hvac_mode} mode")
    return Issue("mode", f"{name} {what}. Set it to Auto at the thermostat or in the ecobee app.")


def program_issue(name: str, program: dict | None, setback_heat: float, setback_cool: float,
                  margin: float = 1.0) -> Issue | None:
    """If the app stops, holds expire and the device's own program takes over. That program
    should be a flat setback (no warmer/cooler than the zone's "empty" settings)."""
    if not program:
        return None
    comfy = [c for c in program.get("climates", [])
             if (c.get("heat") is not None and c["heat"] > setback_heat + margin)
             or (c.get("cool") is not None and c["cool"] < setback_cool - margin)]
    if not comfy:
        return None
    bands = ", ".join(f"{c['name']} {c['heat']:g}–{c['cool']:g}°" for c in comfy)
    return Issue("program", f"{name}'s own ecobee schedule (used if this app stops) heats or "
                            f"cools more than this zone's empty setting: {bands}. Set its "
                            f"comfort settings to a setback like {setback_heat:g}–"
                            f"{setback_cool:g}°.")
