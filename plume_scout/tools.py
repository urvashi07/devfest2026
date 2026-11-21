import os
import json
import math
import ssl
import urllib.request
import urllib.parse
from datetime import datetime
from html.parser import HTMLParser

import certifi
from google.adk.tools.tool_context import ToolContext
from google.genai import types

# Use certifi's CA bundle: the Windows Python store can't verify the LANUK certificate chain.
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

AUDIT_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "audit_log.jsonl"
)

LANUK_BASE_URL = "https://igsvtu.lanuk.nrw.de/igs80s/StoffInfo"
LANUK_DEFAULT_SCOPE = "31e18b8ba18a0400"

# Default chapter: k14 "Unfälle / Freisetzung" (accident & release measures, incl. detector thresholds)
LANUK_DEFAULT_KAPITEL = "k14"

# Substance names / UN numbers -> LANUK IGS_NR, verified against the IGS-Public CAS search (11/2026).
# Operators can also pass a 6-digit IGS_NR directly (e.g., '101391').
# Keys are matched longest-first, so "chlorwasserstoff" wins over "chlor".
IGS_LOOKUP = {
    # Chlorine – CAS 7782-50-5, UN 1017
    "chlorine": "101391",
    "chlor": "101391",
    "1017": "101391",
    # Ammonia – CAS 7664-41-7, UN 1005
    "ammonia": "111171",
    "ammoniak": "111171",
    "1005": "111171",
    # Hydrochloric acid (solution) – CAS 7647-01-0, UN 1789
    "hydrochloric acid": "102191",
    "hydrochloric": "102191",
    "salzsäure": "102191",
    "1789": "102191",
    # Hydrogen chloride (gas) – CAS 7647-01-0, UN 1050
    "hydrogen chloride": "111170",
    "chlorwasserstoff": "111170",
    "1050": "111170",
}
IGS_NAMES = {
    "101391": "Chlorine (Chlor)",
    "111171": "Ammonia (Ammoniak)",
    "102191": "Hydrochloric acid (Salzsäure)",
    "111170": "Hydrogen chloride (Chlorwasserstoff)",
}

# Local Münster reference points for deterministic downwind cone checks
MUENSTER_SITES = [
    {"name": "UKM Universitätsklinikum Münster (Hospital)", "lat": 51.9607, "lon": 7.6003, "priority": "CRITICAL"},
    {"name": "St. Franziskus-Hospital", "lat": 51.9675, "lon": 7.6438, "priority": "CRITICAL"},
    {"name": "Münster Central Station (Hbf)", "lat": 51.9566, "lon": 7.6357, "priority": "HIGH"},
    {"name": "Münster Old Town / Prinzipalmarkt", "lat": 51.9625, "lon": 7.6280, "priority": "HIGH"},
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
    kapitel_nr: str = LANUK_DEFAULT_KAPITEL,
) -> dict:
    """
    Fetches official substance safety and emergency response data from the
    LANUK NRW IGS public database (igsvtu.lanuk.nrw.de). The source text is German.

    Args:
        material_or_igs_nr: Substance name, UN number, or LANUK IGS_NR (e.g., 'Chlorine', 'UN 1017', '101391').
        kapitel_nr: IGS chapter code. Default 'k14' = accident/release measures. Others: 'k12' protective
            measures, 'k10' first aid, 'k6' physical-chemical data.
    """
    q = material_or_igs_nr.strip().lower()
    igs_nr = None

    if q.isdigit() and len(q) == 6:
        igs_nr = q
    else:
        for key in sorted(IGS_LOOKUP, key=len, reverse=True):
            if key in q:
                igs_nr = IGS_LOOKUP[key]
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
        with urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT) as resp:
            raw_html = resp.read().decode("utf-8", errors="replace")

        parser = _HTMLTextExtractor()
        parser.feed(raw_html)
        clean_text = parser.get_text()

        # Skip the page header; the substance name follows "Stoffansicht"
        lines = clean_text.splitlines()
        start = lines.index("Stoffansicht") if "Stoffansicht" in lines else 0
        lanuk_name = lines[start + 1] if start + 1 < len(lines) else ""
        excerpt = "\n".join(lines[start:])[:3500]

        _log_audit("fetch_lanuk_stoffinfo", {"igs_nr": igs_nr, "kapitel": kapitel_nr}, "OK_LIVE_LANUK")
        return {
            "status": "live_lanuk",
            "igs_nr": igs_nr,
            "substance": IGS_NAMES.get(igs_nr, lanuk_name),
            "lanuk_substance_name": lanuk_name,
            "kapitel_nr": kapitel_nr,
            "source_url": url,
            "language_note": "LANUK text is German; translate it to English for the briefing.",
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
        with urllib.request.urlopen(url, timeout=4, context=SSL_CONTEXT) as resp:
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
            "label": "Staging area (500 m upwind)",
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

MAPS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "maps")
PLUME_CONE_HALF_ANGLE_DEG = 30.0
PLUME_MAX_DISTANCE_KM = 5.0
ISOLATION_RADIUS_M = 500

_MAP_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PlumeScout Hazard Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css">
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<style>
  html, body { height: 100%; margin: 0; }
  body { font: 14px/1.4 system-ui, sans-serif; }
  #map, #map3d { position: absolute; inset: 0; }
  #map3d { visibility: hidden; }
  body.mode3d #map { visibility: hidden; }
  body.mode3d #map3d { visibility: visible; }
  .panel { position: absolute; z-index: 1000; top: 10px; right: 10px; background: rgba(255,255,255,.95);
           padding: 10px 12px; border-radius: 8px; box-shadow: 0 1px 6px rgba(0,0,0,.3); max-width: 280px; }
  .panel h3 { margin: 0 0 6px; font-size: 15px; }
  .row { display: flex; align-items: center; gap: 8px; margin: 3px 0; }
  .sw { width: 14px; height: 14px; border-radius: 3px; flex: none; }
  .compass { width: 90px; height: 90px; display: block; margin: 4px auto 0; }
  .warn { color: #b00020; font-weight: 600; margin-top: 6px; }
  .toggle { position: absolute; z-index: 1000; left: 54px; top: 10px; display: flex;
            border-radius: 8px; overflow: hidden; box-shadow: 0 1px 6px rgba(0,0,0,.3); }
  .toggle button { font: 600 14px system-ui, sans-serif; padding: 8px 14px; border: 0;
                   background: #fff; color: #333; cursor: pointer; }
  .toggle button.on { background: #1565c0; color: #fff; }
  .toggle .cam { display: none; border-left: 1px solid #ddd; font-weight: 500; }
  body.mode3d .toggle .cam { display: block; }
  .hint3d { display: none; position: absolute; z-index: 1000; left: 10px; bottom: 28px; background: rgba(255,255,255,.92);
            padding: 6px 10px; border-radius: 6px; font-size: 12px; color: #444; box-shadow: 0 1px 4px rgba(0,0,0,.25); }
  body.mode3d .hint3d { display: block; }
  .pin { width: 16px; height: 16px; border-radius: 50%; border: 2px solid #fff; box-shadow: 0 0 0 1px #222; cursor: pointer; }
  @media (max-width: 600px) { .panel { max-width: 200px; font-size: 12px; } .compass { width: 60px; height: 60px; } }
</style>
</head>
<body>
<div id="map"></div>
<div id="map3d"></div>
<div class="toggle"><button id="b2d" class="on">2D</button><button id="b3d">3D</button><button id="bOverview" class="cam">Overview</button><button id="bClose" class="cam">Close-up</button></div>
<div class="hint3d">3D: right-click or Ctrl + drag to rotate and tilt · Buildings and terrain from OpenStreetMap</div>
<div id="legend" class="panel"></div>
<script>
const D = __DATA__;
const prioColor = {CRITICAL: '#7b1fa2', HIGH: '#c2185b', MEDIUM: '#5c6bc0'};
const siteColor = s => s.affected ? prioColor[s.priority] : '#9e9e9e';
const siteText = s => '<b>' + s.name + '</b><br>' + (s.affected ? 'IN DOWNWIND CONE – ' + s.distance_km + ' km' : 'outside the cone');
const mpText = mp => '<b>' + mp.id + '</b><br><a target="_blank" href="' + mp.maps_url + '">Google Maps</a>';
const stagingText = '<b>' + D.staging.label + '</b><br><a target="_blank" href="' + D.staging.maps_url + '">Google Maps</a>';
const incidentText = '<b>Release point</b><br>' + D.substance + '<br>' + D.incident.join(', ');

// ---------- 2D (Leaflet) ----------
const map = L.map('map');
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 18, attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);
const plume = L.polygon(D.cone, {color: '#d32f2f', weight: 2, fillColor: '#f44336', fillOpacity: 0.25})
  .addTo(map).bindPopup('Area at risk (downwind cone ±' + D.half_angle + '°, ' + D.max_km + ' km)');
L.polygon(D.isolation_ring, {color: '#b71c1c', weight: 2, dashArray: '6 4', fillOpacity: 0.1})
  .addTo(map).bindPopup('Isolation zone ' + D.isolation_m + ' m');
L.polyline([D.wind_start, D.incident, D.wind_end], {color: '#1565c0', weight: 4, dashArray: '10 6'}).addTo(map);
L.polygon(D.arrow_head, {color: '#1565c0', fillColor: '#1565c0', fillOpacity: 1, weight: 1}).addTo(map);
function dot(latlng, color, r) {
  return L.circleMarker(latlng, {radius: r || 8, color: '#222', weight: 1, fillColor: color, fillOpacity: 1}).addTo(map);
}
dot(D.incident, '#b71c1c', 10).bindPopup(incidentText).openPopup();
dot([D.staging.lat, D.staging.lon], '#2e7d32', 9).bindPopup(stagingText);
D.checkpoints.forEach(mp => dot([mp.lat, mp.lon], '#ff9800', 8).bindPopup(mpText(mp)));
D.sites.forEach(s => dot([s.lat, s.lon], siteColor(s), s.affected ? 8 : 6).bindPopup(siteText(s)));
map.fitBounds(plume.getBounds().extend(L.latLng(D.staging.lat, D.staging.lon)).pad(0.15));

// ---------- 3D (MapLibre: OSM buildings + terrain) ----------
const ll = p => [p[1], p[0]];  // [lat, lon] -> [lon, lat]
let map3d = null;
function init3d() {
  map3d = new maplibregl.Map({
    container: 'map3d',
    style: 'https://tiles.openfreemap.org/styles/liberty',
    center: ll(D.view_center),
    zoom: 13.6,
    pitch: 62,
    bearing: D.downwind,      // look along the plume
    maxPitch: 80,
    antialias: true,
  });
  map3d.addControl(new maplibregl.NavigationControl({visualizePitch: true}), 'top-left');
  map3d.on('load', () => {
    map3d.addSource('dem', {
      type: 'raster-dem', encoding: 'terrarium', tileSize: 256, maxzoom: 15,
      tiles: ['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'],
      attribution: 'Terrain: Mapzen / AWS Terrain Tiles',
    });
    map3d.setTerrain({source: 'dem', exaggeration: 1.5});
    map3d.addLayer({id: 'hillshade', type: 'hillshade', source: 'dem',
                    paint: {'hillshade-exaggeration': 0.35, 'hillshade-shadow-color': '#5a5a5a'}});

    // Make sure buildings are extruded even if the style has no 3D layer
    const has3d = map3d.getStyle().layers.some(l => l.type === 'fill-extrusion');
    if (!has3d) {
      map3d.addLayer({
        id: 'buildings-3d', type: 'fill-extrusion', source: 'openmaptiles', 'source-layer': 'building', minzoom: 13,
        paint: {
          'fill-extrusion-color': '#d6d0c8',
          'fill-extrusion-height': ['coalesce', ['get', 'render_height'], 10],
          'fill-extrusion-base': ['coalesce', ['get', 'render_min_height'], 0],
          'fill-extrusion-opacity': 0.9,
        },
      });
    }
    const firstExtrusion = (map3d.getStyle().layers.find(l => l.type === 'fill-extrusion') || {}).id;

    const poly = ring => ({type: 'Feature', geometry: {type: 'Polygon', coordinates: [ring.map(ll)]}});
    map3d.addSource('plume', {type: 'geojson', data: poly(D.cone.concat([D.cone[0]]))});
    map3d.addLayer({id: 'plume-fill', type: 'fill', source: 'plume',
                    paint: {'fill-color': '#f44336', 'fill-opacity': 0.35}}, firstExtrusion);
    map3d.addLayer({id: 'plume-line', type: 'line', source: 'plume',
                    paint: {'line-color': '#c62828', 'line-width': 3}});
    map3d.addSource('iso', {type: 'geojson', data: poly(D.isolation_ring)});
    map3d.addLayer({id: 'iso-line', type: 'line', source: 'iso',
                    paint: {'line-color': '#b71c1c', 'line-width': 3, 'line-dasharray': [2, 1.5]}});
    map3d.addSource('wind', {type: 'geojson', data: {type: 'FeatureCollection', features: [
      {type: 'Feature', geometry: {type: 'LineString', coordinates: [D.wind_start, D.incident, D.wind_end].map(ll)}},
      {type: 'Feature', geometry: {type: 'Polygon', coordinates: [D.arrow_head.concat([D.arrow_head[0]]).map(ll)]}},
    ]}});
    map3d.addLayer({id: 'wind-line', type: 'line', source: 'wind', filter: ['==', '$type', 'LineString'],
                    paint: {'line-color': '#1565c0', 'line-width': 5, 'line-dasharray': [2, 1]}});
    map3d.addLayer({id: 'wind-head', type: 'fill', source: 'wind', filter: ['==', '$type', 'Polygon'],
                    paint: {'fill-color': '#1565c0'}});
  });

  function pin(latlng, color, html, size) {
    const el = document.createElement('div');
    el.className = 'pin';
    el.style.background = color;
    if (size) { el.style.width = el.style.height = size + 'px'; }
    new maplibregl.Marker({element: el}).setLngLat(ll(latlng))
      .setPopup(new maplibregl.Popup({offset: 12}).setHTML(html)).addTo(map3d);
  }
  pin(D.incident, '#b71c1c', incidentText, 20);
  pin([D.staging.lat, D.staging.lon], '#2e7d32', stagingText, 18);
  D.checkpoints.forEach(mp => pin([mp.lat, mp.lon], '#ff9800', mpText(mp)));
  D.sites.forEach(s => pin([s.lat, s.lon], siteColor(s), siteText(s), s.affected ? 16 : 12));
}

function setMode(is3d) {
  document.body.classList.toggle('mode3d', is3d);
  document.getElementById('b3d').classList.toggle('on', is3d);
  document.getElementById('b2d').classList.toggle('on', !is3d);
  if (is3d) { if (!map3d) init3d(); else map3d.resize(); }
  else { map.invalidateSize(); }
}
document.getElementById('b2d').onclick = () => setMode(false);
document.getElementById('b3d').onclick = () => setMode(true);
const views = {
  overview: () => ({center: ll(D.view_center), zoom: 13.6, pitch: 62, bearing: D.downwind}),
  close: () => ({center: ll(D.incident), zoom: 15.6, pitch: 68, bearing: D.downwind}),
};
document.getElementById('bOverview').onclick = () => map3d && map3d.flyTo({...views.overview(), duration: 1800});
document.getElementById('bClose').onclick = () => map3d && map3d.flyTo({...views.close(), duration: 1800});

// ---------- Legend ----------
const n = D.sites.filter(s => s.affected).length;
document.getElementById('legend').innerHTML =
  '<h3>PlumeScout Hazard Map</h3>' +
  '<div>' + D.substance + '</div>' +
  '<div>Wind from <b>' + D.wind_from + '°</b> (' + D.wind_dir_text + '), <b>' + D.wind_speed + ' km/h</b>' +
    (D.gusts ? ', gusts ' + D.gusts + ' km/h' : '') + '</div>' +
  '<div>Plume heading <b>' + D.downwind + '°</b></div>' +
  '<svg class="compass" viewBox="-50 -50 100 100">' +
    '<circle r="46" fill="#fff" stroke="#999"/>' +
    '<text y="-34" text-anchor="middle" font-size="12">N</text>' +
    '<g transform="rotate(' + D.downwind + ')"><line y1="30" y2="-24" stroke="#1565c0" stroke-width="6"/>' +
    '<polygon points="0,-38 -11,-20 11,-20" fill="#1565c0"/></g></svg>' +
  '<div class="row"><span class="sw" style="background:#f44336;opacity:.6"></span>Area at risk (downwind)</div>' +
  '<div class="row"><span class="sw" style="border:2px dashed #b71c1c"></span>Isolation zone ' + D.isolation_m + ' m</div>' +
  '<div class="row"><span class="sw" style="background:#b71c1c;border-radius:50%"></span>Release point</div>' +
  '<div class="row"><span class="sw" style="background:#ff9800;border-radius:50%"></span>Measurement points MP-1..3</div>' +
  '<div class="row"><span class="sw" style="background:#2e7d32;border-radius:50%"></span>Staging area (upwind)</div>' +
  '<div class="row"><span class="sw" style="background:#7b1fa2;border-radius:50%"></span>Sensitive sites affected: ' + n + '</div>' +
  '<div class="warn">Recommendation only – requires Incident Commander approval.</div>' +
  '<div style="color:#666;font-size:12px">Generated: ' + D.generated + (D.wind_note ? '<br>' + D.wind_note : '') + '</div>';

if (location.hash === '#3d') setMode(true);
</script>
</body>
</html>
"""


def _compass_text(deg: float) -> str:
    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return names[int(((deg % 360) + 22.5) // 45) % 8]


async def generate_plume_map(
    incident_lat: float,
    incident_lon: float,
    wind_from_deg: float,
    wind_speed_kmh: float,
    substance: str = "Unknown hazardous substance",
    wind_gusts_kmh: float = 0.0,
    tool_context: ToolContext | None = None,
) -> dict:
    """
    Generates an interactive 2D/3D map (HTML) of the wind direction and the area at risk
    from an airborne hazardous substance: the downwind plume cone, a 500 m isolation
    circle, measurement checkpoints, the upwind staging area, and sensitive sites.
    The 3D view shows OpenStreetMap buildings and terrain.

    Args:
        incident_lat: Latitude of the release point.
        incident_lon: Longitude of the release point.
        wind_from_deg: Direction the wind blows FROM, in degrees (from get_live_wind_conditions).
        wind_speed_kmh: Wind speed in km/h (from get_live_wind_conditions).
        substance: Substance name shown on the map.
        wind_gusts_kmh: Gust speed in km/h, if known.
    """
    params = {
        "incident_lat": incident_lat,
        "incident_lon": incident_lon,
        "wind_from_deg": wind_from_deg,
        "wind_speed_kmh": wind_speed_kmh,
        "substance": substance,
    }
    downwind_deg = (wind_from_deg + 180) % 360

    # Same deterministic geometry as the planning tools
    plan = calculate_measurement_plan(incident_lat, incident_lon, wind_from_deg, wind_speed_kmh, substance)
    affected = check_sensitive_sites_in_plume(incident_lat, incident_lon, downwind_deg, PLUME_MAX_DISTANCE_KM)
    affected_names = {a["site"]: a for a in affected["affected_sites"]}

    cone = [[incident_lat, incident_lon]]
    steps = 24
    for i in range(steps + 1):
        b = downwind_deg - PLUME_CONE_HALF_ANGLE_DEG + i * (2 * PLUME_CONE_HALF_ANGLE_DEG / steps)
        cone.append(list(_destination_point(incident_lat, incident_lon, b, PLUME_MAX_DISTANCE_KM)))

    wind_start = list(_destination_point(incident_lat, incident_lon, wind_from_deg, 1.2))
    wind_end = list(_destination_point(incident_lat, incident_lon, downwind_deg, 2.2))
    tip = list(_destination_point(incident_lat, incident_lon, downwind_deg, 2.6))
    left = list(_destination_point(*wind_end, (downwind_deg - 90) % 360, 0.18))
    right = list(_destination_point(*wind_end, (downwind_deg + 90) % 360, 0.18))

    isolation_ring = [
        list(_destination_point(incident_lat, incident_lon, b, ISOLATION_RADIUS_M / 1000))
        for b in range(0, 361, 6)
    ]
    # 3D camera: stand upwind of the incident and look along the plume
    view_center = list(_destination_point(incident_lat, incident_lon, downwind_deg, 1.6))

    sites = []
    for s in MUENSTER_SITES:
        hit = affected_names.get(s["name"])
        sites.append({
            "name": s["name"], "lat": s["lat"], "lon": s["lon"], "priority": s["priority"],
            "affected": hit is not None,
            "distance_km": hit["distance_km"] if hit else None,
        })

    data = {
        "substance": substance,
        "incident": [incident_lat, incident_lon],
        "wind_from": round(wind_from_deg),
        "wind_dir_text": _compass_text(wind_from_deg),
        "wind_speed": round(wind_speed_kmh, 1),
        "gusts": round(wind_gusts_kmh, 1) if wind_gusts_kmh else None,
        "downwind": round(downwind_deg),
        "half_angle": round(PLUME_CONE_HALF_ANGLE_DEG),
        "max_km": PLUME_MAX_DISTANCE_KM,
        "isolation_m": ISOLATION_RADIUS_M,
        "cone": cone,
        "isolation_ring": isolation_ring,
        "view_center": view_center,
        "wind_start": wind_start,
        "wind_end": wind_end,
        "arrow_head": [tip, left, right],
        "staging": plan["safe_upwind_staging"],
        "checkpoints": plan["measurement_checkpoints"],
        "sites": sites,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "wind_note": "",
    }
    # Escape "</" so substance text can't close the script tag
    html = _MAP_TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))

    os.makedirs(MAPS_DIR, exist_ok=True)
    filename = f"plume_map_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    path = os.path.join(MAPS_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)

    artifact_version = None
    if tool_context is not None:
        try:
            artifact_version = await tool_context.save_artifact(
                filename,
                types.Part(inline_data=types.Blob(mime_type="text/html", data=html.encode("utf-8"))),
            )
        except Exception:
            artifact_version = None

    _log_audit("generate_plume_map", params, "OK")
    return {
        "status": "ok",
        "map_file": path,
        "map_file_url": "file:///" + path.replace("\\", "/"),
        "artifact_filename": filename if artifact_version is not None else None,
        "downwind_plume_bearing_deg": round(downwind_deg, 1),
        "affected_site_count": len(affected["affected_sites"]),
        "note": "Open the map from the Artifacts tab or the map_file path. Recommendation only; requires Einsatzleiter approval.",
    }
