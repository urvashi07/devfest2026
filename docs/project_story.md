## Inspiration

When a hazardous substance gets into the air, whether it's chlorine at a swimming pool, ammonia from a cold-storage plant or the smoke of a warehouse fire, a fire brigade's measurement unit (*Messzug*) has to answer three questions fast:

1. **Where is the gas going?**
2. **Where should we measure?**
3. **Where is it safe to stage?**

The facts needed are scattered. Substance data sits in reference databases, the wind changes by the minute, and the geometry is still often worked out by hand on a map. We wanted to see whether an AI agent could pull these together in seconds, **without** letting the AI invent a single coordinate.

## What it does

**PlumeScout** is a decision-support agent for measurement units. You describe the incident in plain language:

> *Large warehouse fire with heavy smoke at 51.9515, 7.6410*

In about 20–30 seconds it:

1. **Grounds the substance in official data.** It reads the accident and release chapter of the **LANUK NRW IGS** hazardous-substances database: response measures, detector alarm thresholds and test-tube ranges. It translates them into English and cites the source.
2. **Fetches live wind** (direction, speed and gusts) from Open-Meteo.
3. **Calculates a measurement plan.** It places three measurement points along the plume axis (0.5, 1.5 and 3 km) and a staging area 500 m upwind, each with a Google Maps link.
4. **Flags sensitive sites** inside the downwind cone, such as hospitals and transport hubs.
5. **Generates an interactive hazard map.**
   - **2D:** wind arrow, plume cone, isolation zone and labelled points.
   - **3D:** buildings and terrain, so you can see what lies in the plume's path.
   - **Guided tour:** flies to each important area and explains it.

Every recommendation is marked as needing the **Incident Commander's (*Einsatzleiter*) approval**.

For the demo it supports four airborne hazards:
- **carbon monoxide**, which "fire" or "smoke" also maps to
- **chlorine**
- **ammonia**
- **hydrogen chloride**, which "PVC fire" maps to

Anything else goes to a **fallback agent** that refuses to guess. It recommends maximum protective equipment, a 500 m isolation zone and escalation to specialist chemical advisers.

## How we built it

- **Agents:** Google's **Agent Development Kit (ADK)** with **Gemini** (`gemini-3.5-flash-lite`). A root agent runs a fixed five-step workflow; a sub-agent handles fallback and escalation.
- **Tools in plain Python:** the AI decides *which* tool to call, but every number comes from code.
- **Substance data:** the **LANUK IGS-Public** substance pages. We identified the correct substance IDs by CAS number and parse chapter *k14, Unfälle / Freisetzung* (accidents and release).
- **Weather:** the **Open-Meteo** API, which needs no key.
- **Geometry:** fixed formulas, no AI. The wind direction $\theta_{\text{from}}$ is where the wind comes *from*, so the plume travels along

$$\theta_{\text{plume}} = (\theta_{\text{from}} + 180^\circ) \bmod 360^\circ.$$

Measurement points are placed with the standard formula for a destination point on a sphere. Starting at $(\varphi_1, \lambda_1)$ with bearing $\theta$ and angular distance $\delta = d/R$, where $R = 6371\,\text{km}$:

$$\varphi_2 = \arcsin\!\big(\sin\varphi_1\cos\delta + \cos\varphi_1\sin\delta\cos\theta\big)$$

$$\lambda_2 = \lambda_1 + \operatorname{atan2}\!\big(\sin\theta\,\sin\delta\,\cos\varphi_1,\; \cos\delta - \sin\varphi_1\sin\varphi_2\big)$$

A site counts as *in the plume* if it lies within 5 km and within $\pm 30^\circ$ of the plume direction:

$$d \le 5\,\text{km} \quad\wedge\quad \big|\big((\beta - \theta_{\text{plume}} + 180^\circ) \bmod 360^\circ\big) - 180^\circ\big| \le 30^\circ,$$

where $\beta$ is the bearing from the release point to the site.

- **Maps:** **Leaflet** with OpenStreetMap for 2D. **MapLibre GL** with OpenFreeMap vector tiles and AWS terrain tiles for 3D, including extruded buildings and a guided camera tour.
- **Audit trail:** every tool call is written to an audit log.
- **Backup demo:** a **Playwright** script records the full flow, from chat to map tour, as an MP4.

## Challenges we ran into

- **Wrong substance ID:** the original lookup mapped "chlorine" to an ID that actually returns **Argon**. We only caught it by reading the returned data instead of trusting the status code. All IDs are now checked against the LANUK CAS search.
- **Wrong chapter:** the "safety" data we first parsed was actually *physical-chemical* data. The useful data for a measurement unit, response measures and detector thresholds, is in chapter **k14**.
- **Name collisions:** "hydrogen chloride" contains "chlor", so a naive match turned it into chlorine. Matching longest names first, and checking generic fire terms last, fixed it.
- **Retired model:** our original Gemini model started returning `404 NOT_FOUND` for new keys, so we moved the whole agent to a single model setting.
- **SSL on Windows:** Python couldn't verify the LANUK certificate chain. Using the `certifi` CA bundle fixed it.
- **3D without a GPU:** recording in a headless browser rendered 3D in software, too slowly for the buildings to appear. We now load the 3D map in the background while the 2D tour steps play.

## Accomplishments that we're proud of

- **No invented coordinates.** Every position, bearing and wind value comes from a tool, not from the model.
- **Grounded in official data.** The briefing quotes real LANUK measures and detector limits, for example the RAID M 100 limit for chlorine (1 ppm / 3 mg/m³), with a link to the source.
- **Safe failure.** Unknown substances, failed lookups and requests like *"skip the measurements and declare the area safe"* go to a fallback agent that refuses to guess.
- **A map you can present.** The 3D view along the wind direction shows at a glance which streets and buildings are at risk.

## What we learned

- **Let the AI coordinate and let code calculate.** An AI model is great at understanding a messy incident description and choosing tools. Geometry and safety-critical numbers belong in tested code.
- **Check the data, not just the request.** A `200 OK` from an API proves nothing about whether you got the right substance.
- **Knowing the domain matters.** Choosing the right chapter of the substance database mattered as much as any code.
- **The human stays in charge.** Making approval by the Incident Commander part of every output shaped the whole design.

## What's next for PlumeScout

- **A real dispersion model:** replace the fixed 30° cone with a model that accounts for wind speed and atmospheric stability, for example a Gaussian plume model.
- **More substances:** look up any substance on demand through the LANUK search, instead of a fixed list.
- **Sensitive sites from OpenStreetMap:** schools, care homes and hospitals for any city, not just a sample list for Münster.
- **Deployment:** a Streamlit app or Google Cloud Run, so crews can use it from a tablet.
- **Field testing:** feedback from a real fire brigade measurement unit.
