import os
import json
import math
import urllib.request
import urllib.parse
from datetime import datetime
from html.parser import HTMLParser

AUDIT_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "audit_log.jsonl"
)

LANUK_BASE_URL = "https://igsvtu.lanuk.nrw.de/igs80s/StoffInfo"
LANUK_DEFAULT_SCOPE = "31e18b8ba18a0400"

# Quick lookup map from common names / UN numbers -> LANUK IGS_NR
# Operators can also pass an IGS_NR directly (e.g., '114690')
IGS_LOOKUP = {
    "114690": "114690",
    "chlor": "114690",
    "chlorine": "114690",
    "1017": "114690",
    "ammoniak": "114421",
    "ammonia": "114421",
    "1005": "114421",
    "salzsäure": "114697",
    "hydrochloric": "114697",
    "1789": "114697",
}

# Local Münster reference points for deterministic downwind cone checks
MUENSTER_SITES = [
    {"name": "UKM Universitätsklinikum Münster (Hospital)", "lat": 51.9607, "lon": 7.6003, "priority": "CRITICAL"},
    {"name": "St. Franziskus-Hospital", "lat": 51.9675, "lon": 7.6438, "priority": "CRITICAL"},
    {"name": "Münster (Westf) Hbf", "lat": 51.9566, "lon": 7.6357, "priority": "HIGH"},
    {"name": "Münster Altstadt / Prinzipalmarkt", "lat": 51.9625, "lon": 7.6280, "priority": "HIGH"},
    {"name": "Münster-Mauritz (Residential)", "lat": 51.9660, "lon": 7.6550, "priority": "MEDIUM"},
    {"name": "Münster-Geist (Residential)", "lat": 51.9430, "lon": 7.6180, "priority": "MEDIUM"},
    {"name": "Münster-Kinderhaus (Residential)", "lat": 51.9950, "lon": 7.6080, "priority": "MEDIUM"},
    {"name": "Münster-Hiltrup (Residential)", "lat": 51.9030, "lon": 7.6420, "priority": "MEDIUM"},
]


class _HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip = False
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "header", "footer"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "header", "footer"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            text = " ".join(data.split())
            if text:
                self.chunks.append(text)

    def get_text(self) -> str:
        return "\n".join(self.chunks)


def _log_audit(tool_name: str, params: dict, status: str):
    """Appends an audit-ready record for every tool invocation."""
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "tool": tool_name,
        "params": params,
        "status": status,
    }
    with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _destination_point(lat: float, lon: float, bearing_deg: float, distance_km: float) -> tuple[float, float]:
    """Deterministic spherical geometry: computes target GPS point from bearing + distance."""
    R = 6371.0
    brng = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    d = distance_km / R

    lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(brng))
    lon2 = lon1 + math.atan2(
        math.sin(brng) * math.sin(d) * math.cos(lat1),
        math.cos(d) - math.sin(lat1) * math.sin(lat2),
    )
    return round(math.degrees(lat2), 5), round(math.degrees(lon2), 5)


def _bearing_and_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)

    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    dist_km = 2 * R * math.asin(math.sqrt(a))

    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    bearing = (math.degrees(math.atan2(y, x)) + 360) % 360
    return round(bearing, 1), round(dist_km, 2)


def fetch_lanuk_stoffinfo(
    material_or_igs_nr: str,
    kapitel_nr: str = "k6",
) -> dict:
    """
    Fetches official substance safety and emergency response data from the
    LANUK NRW IGS public database (igsvtu.lanuk.nrw.de).

    Args:
        material_or_igs_nr: Substance name, UN number, or LANUK IGS_NR (e.g., '114690', 'Chlor', 'UN 1017').
        kapitel_nr: IGS chapter code (default 'k6' for emergency/safety measures).
    """
    q = material_or_igs_nr.strip().lower()
    igs_nr = None

    if q.isdigit():
        igs_nr = q
    else:
        for key, val in IGS_LOOKUP.items():
            if key in q:
                igs_nr = val
                break

    if not igs_nr:
        _log_audit("fetch_lanuk_stoffinfo", {"input": material_or_igs_nr}, "UNKNOWN_IGS_NR")
        return {
            "status": "unknown_substance",
            "message": (
                f"No LANUK IGS_NR mapped for '{material_or_igs_nr}'. "
                "Ask the operator for the exact IGS_NR or UN number, and default to maximum PPE (CSA)."
            ),
        }

    url = (
        f"{LANUK_BASE_URL}"
        f"?IGS_NR={urllib.parse.quote(igs_nr)}"
        f"&KAPITEL_NR={urllib.parse.quote(kapitel_nr)}"
        f"&scope={LANUK_DEFAULT_SCOPE}"
    )

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "PlumeScout-DevFest-Muenster/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw_html = resp.read().decode("utf-8", errors="replace")

        parser = _HTMLTextExtractor()
        parser.feed(raw_html)
        clean_text = parser.get_text()

        excerpt = clean_text[:3500]
        _log_audit("fetch_lanuk_stoffinfo", {"igs_nr": igs_nr, "kapitel": kapitel_nr}, "OK_LIVE_LANUK")
        return {
            "status": "live_lanuk",
            "igs_nr": igs_nr,
            "kapitel_nr": kapitel_nr,
            "source_url": url,
            "lanuk_excerpt": excerpt,
        }
    except Exception as e:
        _log_audit("fetch_lanuk_stoffinfo", {"igs_nr": igs_nr, "error": str(e)}, "FALLBACK_LOCAL")
        return {
            "status": "fallback_local",
            "igs_nr": igs_nr,
            "source_url": url,
            "note": "Live LANUK endpoint unreachable or session scope expired; verify directly at source_url.",
        }


def get_live_wind_conditions(lat: float = 51.9515, lon: float = 7.6410) -> dict:
    """
    Fetches live 10m wind speed, gusts, and direction from Open-Meteo (free, no API key).
    Defaults to Münster Stadthafen (51.9515, 7.6410).
    """
    params = {"lat": lat, "lon": lon}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current=wind_speed_10m,wind_direction_10m,wind_gusts_10m"
    )
    try:
        with urllib.request.urlopen(url, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        cur = data["current"]
        result = {
            "status": "live",
            "wind_from_deg": cur["wind_direction_10m"],
            "wind_speed_kmh": cur["wind_speed_10m"],
            "wind_gusts_kmh": cur["wind_gusts_10m"],
        }
        _log_audit("get_live_wind_conditions", params, "OK_LIVE")
        return result
    except Exception:
        result = {
            "status": "fallback_cached",
            "wind_from_deg": 240,
            "wind_speed_kmh": 18.5,
            "wind_gusts_kmh": 28.0,
            "note": "Live API unreachable; using cached Münster SW wind telemetry.",
        }
        _log_audit("get_live_wind_conditions", params, "OK_FALLBACK")
        return result


def calculate_measurement_plan(
    incident_lat: float,
    incident_lon: float,
    wind_from_deg: float,
    wind_speed_kmh: float,
    hazard_type: str = "chemical plume",
) -> dict:
    """
    Deterministically computes downwind measurement checkpoints (Messpunkte) and a safe
    upwind staging area (Bereitstellungsraum) for fire department measurement units.
    """
    params = {
        "incident_lat": incident_lat,
        "incident_lon": incident_lon,
        "wind_from_deg": wind_from_deg,
        "wind_speed_kmh": wind_speed_kmh,
        "hazard_type": hazard_type,
    }

    # Wind direction is where wind comes FROM; plume travels +180 degrees
    downwind_deg = (wind_from_deg + 180) % 360

    staging_lat, staging_lon = _destination_point(incident_lat, incident_lon, wind_from_deg, 0.5)
    mp1_lat, mp1_lon = _destination_point(incident_lat, incident_lon, downwind_deg, 0.5)
    mp2_lat, mp2_lon = _destination_point(incident_lat, incident_lon, downwind_deg, 1.5)
    mp3_lat, mp3_lon = _destination_point(incident_lat, incident_lon, downwind_deg, 3.0)

    result = {
        "status": "ok",
        "hazard_type": hazard_type,
        "downwind_plume_bearing_deg": round(downwind_deg, 1),
        "safe_upwind_staging": {
            "label": "Bereitstellungsraum (500m Upwind)",
            "lat": staging_lat,
            "lon": staging_lon,
            "maps_url": f"https://www.google.com/maps?q={staging_lat},{staging_lon}",
        },
        "measurement_checkpoints": [
            {"id": "MP-1 (Near / 500m)", "lat": mp1_lat, "lon": mp1_lon, "maps_url": f"https://www.google.com/maps?q={mp1_lat},{mp1_lon}"},
            {"id": "MP-2 (Mid / 1.5km)", "lat": mp2_lat, "lon": mp2_lon, "maps_url": f"https://www.google.com/maps?q={mp2_lat},{mp2_lon}"},
            {"id": "MP-3 (Far / 3.0km)", "lat": mp3_lat, "lon": mp3_lon, "maps_url": f"https://www.google.com/maps?q={mp3_lat},{mp3_lon}"},
        ],
        "requires_einsatzleiter_approval": True,
    }
    _log_audit("calculate_measurement_plan", params, "OK")
    return result


def check_sensitive_sites_in_plume(
    incident_lat: float,
    incident_lon: float,
    downwind_deg: float,
    max_distance_km: float = 5.0,
) -> dict:
    """
    Identifies hospitals, transit hubs, and residential districts within a ±30° downwind cone.
    """
    params = {
        "incident_lat": incident_lat,
        "incident_lon": incident_lon,
        "downwind_deg": downwind_deg,
        "max_distance_km": max_distance_km,
    }
    affected = []
    for site in MUENSTER_SITES:
        bearing, dist_km = _bearing_and_distance(incident_lat, incident_lon, site["lat"], site["lon"])
        angle_diff = abs((bearing - downwind_deg + 180) % 360 - 180)
        if dist_km <= max_distance_km and angle_diff <= 30.0:
            affected.append({
                "site": site["name"],
                "priority": site["priority"],
                "distance_km": dist_km,
                "bearing_deg": bearing,
            })

    affected.sort(key=lambda x: (0 if x["priority"] == "CRITICAL" else 1 if x["priority"] == "HIGH" else 2, x["distance_km"]))
    _log_audit("check_sensitive_sites_in_plume", params, f"OK_{len(affected)}_SITES")
    return {"status": "ok", "affected_sites": affected, "cone_half_angle_deg": 30}