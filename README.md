# PlumeScout

PlumeScout is a tactical decision-support agent for fire department measurement units (Feuerwehr Messzug / ABC-Erkunder) responding to hazardous-substance releases in Münster. It is built with the [Google Agent Development Kit (ADK)](https://google.github.io/adk-docs/) and Gemini.

Given a substance and an incident location, PlumeScout:

1. Looks up official hazard, PPE and emergency-measure data in the **LANUK NRW IGS** substance database.
2. Fetches **live wind** (speed, gusts, direction) from [Open-Meteo](https://open-meteo.com/).
3. Computes **measurement checkpoints** (Messpunkte) 0.5 km, 1.5 km and 3 km downwind, plus an **upwind staging area** (Bereitstellungsraum) 500 m upwind.
4. Flags **sensitive sites** (hospitals, transit hubs, residential districts) within a ±30° downwind cone.
5. Returns a briefing with Google Maps links for every point.

> **Decision support only.** All checkpoints and staging areas are recommendations and require approval from the Incident Commander (Einsatzleiter) before deployment.

## How it works

| Component | Model | Role |
|---|---|---|
| `plume_scout` (root agent) | `gemini-3.5-flash-lite` | Runs the four-step workflow and writes the briefing. |
| `emergency_fallback_agent` | `gemini-3.5-flash-lite` | Takes over for unknown substances, failed lookups, or requests to bypass safety rules. Recommends maximum PPE, a 500 m isolation zone, and escalation to TUIS / GESTIS / Fachberater Chemie. |

**Safety design**

- **No invented numbers:** all coordinates, bearings and wind values come from tool output, computed with deterministic spherical geometry in `tools.py`, never from the model.
- **Grounded sources:** every briefing cites the exact LANUK `source_url`.
- **Resilience:** HTTP retries on 429/5xx, automatic fallback to another Gemini model when the primary is overloaded, and cached wind data if Open-Meteo is unreachable.
- **Audit trail:** every tool call is appended to `audit_log.jsonl` in the repo root.

### Tools

| Tool | What it does |
|---|---|
| `fetch_lanuk_stoffinfo` | Resolves a name, UN number or IGS_NR (currently chlorine, ammonia, hydrochloric acid, or any IGS_NR you pass directly) and scrapes the LANUK StoffInfo page. |
| `get_live_wind_conditions` | 10 m wind from Open-Meteo; defaults to Münster Stadthafen. No API key needed. |
| `calculate_measurement_plan` | Downwind checkpoints and upwind staging area with Maps links. |
| `check_sensitive_sites_in_plume` | Checks a built-in list of Münster sites against the plume cone. |

## Project layout

```
plume_scout/
  __init__.py
  agent.py      # root agent, fallback sub-agent, retry + model-fallback config
  tools.py      # LANUK, wind, geometry and sensitive-site tools
  .env          # your API key (not committed)
```

## Getting started

Requires Python 3.10+ (developed on 3.13).

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install google-adk
```

Create `plume_scout/.env` with a [Gemini API key](https://aistudio.google.com/apikey):

```
GOOGLE_GENAI_USE_ENTERPRISE=FALSE
GOOGLE_API_KEY=your-key-here
```

Run it from the repo root:

```powershell
adk web     # browser UI, then pick "plume_scout"
adk run plume_scout   # terminal chat
```

### Example prompt

> Chlorine leak at the Münster Stadthafen, 51.9515, 7.6410. Where should the Messzug measure and stage?

## Limitations

- The substance lookup table and the sensitive-site list are small and Münster-specific; extend `IGS_LOOKUP` and `MUENSTER_SITES` in `tools.py` for other substances or areas.
- The plume model is a simple straight-line cone, not a dispersion model.
- The LANUK endpoint uses a session scope that can expire; the agent then falls back and points you to the source URL.
