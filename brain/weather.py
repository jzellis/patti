#!/usr/bin/env python3
"""The weather where she lives, from Open-Meteo (free, no account), or MET
Norway when Open-Meteo's not answering, for her chest screen and to say.

    python3 weather.py --set "Glasgow"     # once: look the place up, keep it
    python3 weather.py                     # what the chest screen would get

The place is kept in ~/.config/robotpattinson/weather.json. now() returns
the CYD's weather command ("weather <now> <high> <low> <icon> <words>"), or
None without a place or a connection.
"""
import argparse
import calendar
import json
import os
import sys
import time
import urllib.parse
import urllib.request

CONF = os.path.expanduser("~/.config/robotpattinson/weather.json")
FORECAST = "https://api.open-meteo.com/v1/forecast"
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
# The backup, while Open-Meteo's not answering (then not tried again for
# DOWN_S): MET Norway, free too, but it wants to know who's asking.
MET_NO = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
MET_NO_AGENT = "Patti-home-robot/1.0 (+https://github.com/jzellis/robotpattinson)"
DAYS = 5
CACHE_S = 600           # one fetch does the chest screen, her answers and the forecast for this long
DOWN_S = 900

# WMO weather codes -> (icon by day, icon by night, words)
CODES = {
    0: ("sun", "moon", "Clear"),
    1: ("sun", "moon", "Mainly clear"),
    2: ("partly", "partlynight", "Partly cloudy"),
    3: ("cloud", "cloud", "Overcast"),
    45: ("fog", "fog", "Fog"), 48: ("fog", "fog", "Freezing fog"),
    51: ("drizzle", "drizzle", "Light drizzle"), 53: ("drizzle", "drizzle", "Drizzle"),
    55: ("drizzle", "drizzle", "Heavy drizzle"), 56: ("drizzle", "drizzle", "Icy drizzle"),
    57: ("drizzle", "drizzle", "Icy drizzle"),
    61: ("rain", "rain", "Light rain"), 63: ("rain", "rain", "Rain"), 65: ("rain", "rain", "Heavy rain"),
    66: ("rain", "rain", "Icy rain"), 67: ("rain", "rain", "Icy rain"),
    71: ("snow", "snow", "Light snow"), 73: ("snow", "snow", "Snow"), 75: ("snow", "snow", "Heavy snow"),
    77: ("snow", "snow", "Snow grains"),
    80: ("rain", "rain", "Showers"), 81: ("rain", "rain", "Showers"), 82: ("rain", "rain", "Heavy showers"),
    85: ("snow", "snow", "Snow showers"), 86: ("snow", "snow", "Snow showers"),
    95: ("thunder", "thunder", "Thunder"), 96: ("thunder", "thunder", "Thunder, hail"),
    99: ("thunder", "thunder", "Thunder, hail"),
}


def get(url, params, headers=None):
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers=headers or {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def place():
    try:
        with open(CONF) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def set_place(name):
    found = get(GEOCODE, {"name": name, "count": 1}).get("results")
    if not found:
        sys.exit("Couldn't find %r" % name)
    p = found[0]
    keep = {"name": p["name"], "country": p.get("country", ""), "lat": p["latitude"], "lon": p["longitude"]}
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    with open(CONF, "w") as f:
        json.dump(keep, f)
    return keep


def _open_meteo(p):
    w = get(FORECAST, {"latitude": p["lat"], "longitude": p["lon"], "timezone": "auto", "forecast_days": DAYS,
                       "current": "temperature_2m,weather_code,is_day",
                       "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"})
    cur, d = w["current"], w["daily"]
    return {"temp": cur["temperature_2m"], "code": cur["weather_code"], "is_day": cur.get("is_day", 1),
            "days": [{"date": date, "code": d["weather_code"][i], "hi": d["temperature_2m_max"][i],
                      "lo": d["temperature_2m_min"][i], "rain": d["precipitation_probability_max"][i]}
                     for i, date in enumerate(d["time"])]}


def wmo(symbol):
    """MET Norway's symbol ("lightrainshowers_day") as the nearest WMO code (CODES)."""
    s = symbol.split("_")[0]
    if "thunder" in s:
        return 95
    base = {"clearsky": 0, "fair": 1, "partlycloudy": 2, "cloudy": 3, "fog": 45,
            "lightrain": 61, "rain": 63, "heavyrain": 65, "lightrainshowers": 80, "rainshowers": 81,
            "heavyrainshowers": 82, "lightsleet": 66, "sleet": 67, "heavysleet": 67, "lightsleetshowers": 66,
            "sleetshowers": 67, "heavysleetshowers": 67, "lightsnow": 71, "snow": 73, "heavysnow": 75,
            "lightsnowshowers": 85, "snowshowers": 85, "heavysnowshowers": 86}
    return base.get(s, 3)


def _met_no(p):
    """The same from MET Norway (no rain chances for the UK: None)."""
    w = get(MET_NO, {"lat": round(p["lat"], 4), "lon": round(p["lon"], 4)}, {"User-Agent": MET_NO_AGENT})
    ts = w["properties"]["timeseries"]
    first = ts[0]["data"]
    symbol = (first.get("next_1_hours") or first.get("next_6_hours") or {}).get("summary", {}).get("symbol_code", "")
    days = {}
    for t in ts:
        at = calendar.timegm(time.strptime(t["time"], "%Y-%m-%dT%H:%M:%SZ"))
        date = time.strftime("%Y-%m-%d", time.localtime(at))
        d = days.setdefault(date, {"date": date, "code": 0, "hi": None, "lo": None, "rain": None})
        temp = t["data"]["instant"]["details"].get("air_temperature")
        if temp is not None:
            d["hi"] = temp if d["hi"] is None else max(d["hi"], temp)
            d["lo"] = temp if d["lo"] is None else min(d["lo"], temp)
        nxt = t["data"].get("next_1_hours") or t["data"].get("next_6_hours")
        if nxt:
            d["code"] = max(d["code"], wmo(nxt["summary"]["symbol_code"]))     # (the worst of the day, as Open-Meteo)
    return {"temp": first["instant"]["details"]["air_temperature"], "code": wmo(symbol) if symbol else 3,
            "is_day": 0 if symbol.endswith("_night") else 1, "days": list(days.values())[:DAYS]}


_cache = {"at": 0.0, "data": None, "down_at": -1e9}


def data():
    """The weather now and for DAYS days, from Open-Meteo or (while that's
    not answering) MET Norway; kept CACHE_S. None if neither answers."""
    p = place()
    if not p:
        return None
    if _cache["data"] and time.time() - _cache["at"] < CACHE_S:
        return _cache["data"]
    for name, fetch in (("open-meteo", _open_meteo), ("met.no", _met_no)):
        if name == "open-meteo" and time.time() - _cache["down_at"] < DOWN_S:
            continue
        try:
            d = fetch(p)
        except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
            print("weather: %s not answering (%s)" % (name, e), file=sys.stderr)
            if name == "open-meteo":
                _cache["down_at"] = time.time()
            continue
        d["source"] = name
        _cache.update(at=time.time(), data=d)
        return d
    return None


def now():
    w = data()
    if not w:
        return None
    day = w["days"][0]
    day_icon, night_icon, words = CODES.get(w["code"], ("none", "none", "Code %d" % w["code"]))
    deg = lambda v: "-" if v is None else str(round(v))
    return "weather %s %s %s %s %s" % (deg(w["temp"]), deg(day["hi"]), deg(day["lo"]),
                                       day_icon if w["is_day"] else night_icon, words)


def spoken():
    """The weather now and today, in a sentence or two she can say, or None."""
    p, w = place(), data()
    if not w:
        return None
    code, day = w["code"], w["days"][0]
    words = CODES.get(code, ("", "", ""))[2].lower()
    hi, lo, rain = day["hi"], day["lo"], day["rain"]
    deg = lambda v: "%s%d degree%s" % ("minus " if round(v) < 0 else "", abs(round(v)), "" if abs(round(v)) == 1 else "s")
    # "clear", "overcast" and so on describe the sky; "rain", "fog" and the rest are things there are
    sky = ("and " if code <= 3 else "with ") + words if words else ""
    out = "In %s it's %s %s. Today, a high of %d and a low of %d." % (p["name"], deg(w["temp"]), sky,
                                                                       round(hi), round(lo))
    if rain is not None and rain >= 80 and code < 51:
        out += " Rain's very likely today."
    elif rain is not None and rain >= 30 and code < 51:
        out += " There's a %d percent chance of rain." % round(rain)
    elif rain is None and code < 51 <= day["code"]:
        out += " Some rain's forecast today."
    return out.replace("  ", " ").replace(" .", ".")


def forecast(days=5):
    """[(day, words, high, low, rain %)] for today and the next few days, or None."""
    w = data()
    if not w:
        return None
    rnd = lambda v: "-" if v is None else str(round(v))
    return [(time.strftime("%a %d", time.strptime(d["date"], "%Y-%m-%d")),
             CODES.get(d["code"], ("", "", "Code %d" % d["code"]))[2], rnd(d["hi"]), rnd(d["lo"]), rnd(d["rain"]))
            for d in w["days"][:days]]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--set", metavar="PLACE", help="look up a town or city and keep it")
    args = ap.parse_args()
    if args.set:
        p = set_place(args.set)
        print("Weather for %s, %s (%.2f, %.2f)" % (p["name"], p["country"], p["lat"], p["lon"]))
    if not place():
        sys.exit("No place yet: python3 %s --set \"Your town\"" % sys.argv[0])
    print(now() or "No weather (no connection?)")
    print(spoken() or "")
    for f in forecast() or []:
        print("%s  %-14s %s/%s  rain %s%%" % f)
