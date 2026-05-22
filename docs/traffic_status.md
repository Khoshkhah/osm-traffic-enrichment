# Traffic Status Values

This document explains the four congestion levels, what they mean in practice, and
how each data source derives them — the methods are fundamentally different.

---

## Congestion levels

All three sources use the same four-level scale, stored in per-source history tables
(`mapbox_congestion_history`, `google_congestion_history`, `tomtom_congestion_history`).

| Value | Color | Typical speed | Meaning |
|---|---|---|---|
| `low` | 🟢 Green | Near free-flow | Traffic moving normally. No significant delay. |
| `moderate` | 🟠 Yellow | 60–85% of free-flow | Noticeable slowdown. Travel time slightly increased. |
| `heavy` | 🔴 Red | 40–60% of free-flow | Significant congestion. Expect delays. |
| `severe` | ⬛ Dark red | < 40% of free-flow | Near standstill or road closure. |
| `no data` | ⬜ Gray | Unknown | Road not monitored, or flowing normally (Google only). |

**Free-flow speed** is the expected speed under ideal conditions (usually close to the speed limit).

---

## Mapbox

**Source:** Traffic v1 tileset (`mapbox.mapbox-traffic-v1`) — MVT vector tiles.

### How Mapbox collects data
Anonymous GPS probe data from millions of mobile devices. Each probe is a timestamped
location + speed. Mapbox estimates current speed on every road segment in real time.

### Classification
Mapbox compares current speed against a historical baseline for the same road/time:

| Speed ratio (current / free-flow) | Level |
|---|---|
| > 0.85 | `low` |
| 0.60 – 0.85 | `moderate` |
| 0.40 – 0.60 | `heavy` |
| < 0.40 | `severe` |

The congestion level is a **text property** already embedded in the tile feature —
no color analysis needed.

### Pipeline steps
```
1. Download traffic-v1 + streets-v8 MVT tiles        scripts/mapbox_traffic.py
2. Decode tile features — each has {congestion: "low"|...}
3. Spatial join streets + traffic (sjoin_nearest, max 20 m)
4. Geometric map match to OSM edges:
   - 25 m corridor buffer
   - ≤ 45° bearing filter
   - ≥ 40% overlap threshold
   - Most severe congestion wins
5. Write to mapbox_congestion_history
```

**Coverage:** Nearly 100% — free-flowing roads get `low`, not `no data`.
`no data` only means the road has no GPS probe coverage at all.

---

## Google Maps

**Source:** Google Maps JavaScript API `TrafficLayer` rendered by Playwright.

### How Google collects data
GPS from Android devices, Waze reports, and road sensors. Same baseline-vs-current
approach as Mapbox.

### Classification
Google renders congestion as **pixel colors** painted on road lines. The pipeline
classifies pixels by CIE76 distance to four reference colors:

| Level | Hex | RGB |
|---|---|---|
| `low` | `#11D68F` | (17, 214, 143) teal-green |
| `moderate` | `#FFCF43` | (255, 207, 67) yellow |
| `heavy` | `#F24E42` | (242, 78, 66) red |
| `severe` | `#A92727` | (169, 39, 39) dark red |

A pixel is assigned if CIE76 distance ≤ 25 units; otherwise it remains `no data`.

### Pipeline steps
```
1. Render a custom Google Maps HTML page in headless Chromium    scripts/google_traffic.py
   — white background, white roads, TrafficLayer only, no labels
2. Classify every non-white pixel by CIE76 distance in LAB space
   → uint8 raster (0=none, 1=low, 2=moderate, 3=heavy, 4=severe)
3. Bearing-based pixel-to-edge matching:
   a. Estimate local stripe direction (PCA on traffic pixels within 25 m)
   b. Find candidate edge sample points within 10 m
   c. Filter: stripe bearing vs edge bearing must differ by ≤ 40°
   d. Require single OSM way (discard intersection pixels)
   e. Assign to nearest edge
4. Most severe pixel wins per edge
5. Write to google_congestion_history
```

**Coverage:** Partial — only congested roads are colored. `no data` usually means
the road is flowing normally, not that it has no coverage.

---

## TomTom

**Source:** TomTom Traffic Flow Tile API — PBF format, same z/x/y grid as Mapbox.

### How TomTom collects data
GPS probes from TomTom devices and mobile apps, combined with road sensor data.

### Raw value: `traffic_level`
TomTom provides a continuous float **`traffic_level`** (0.0 = blocked, 1.0 = free flow)
representing the ratio of current speed to free-flow speed. This raw value is stored
in `tomtom_congestion_history` alongside the classified level so thresholds can be
adjusted without re-fetching.

### Classification thresholds

| Condition | Level |
|---|---|
| `road_closure = true` | `severe` |
| `traffic_level < 0.40` | `severe` |
| `0.40 ≤ traffic_level < 0.60` | `heavy` |
| `0.60 ≤ traffic_level < 0.85` | `moderate` |
| `traffic_level ≥ 0.85` | `low` |

### Pipeline steps
```
1. Download Traffic Flow PBF tiles                              scripts/tomtom_traffic.py
2. Decode "Traffic flow" layer — each feature has:
   {traffic_level: float, road_closure: bool, road_type: str}
3. Classify traffic_level → congestion level
4. Geometric map match to OSM edges (same as Mapbox):
   - 25 m corridor buffer, ≤ 45° bearing, ≥ 40% overlap
   - Most severe congestion wins; winning segment's traffic_level is stored
5. Write to tomtom_congestion_history (congestion + traffic_level columns)
```

**Re-classification without re-fetching:**
```sql
-- Apply custom thresholds to stored traffic_level
SELECT edge_id,
       CASE WHEN traffic_level < 0.30 THEN 'severe'
            WHEN traffic_level < 0.55 THEN 'heavy'
            WHEN traffic_level < 0.80 THEN 'moderate'
            ELSE 'low' END AS recls
FROM tomtom_congestion_history
WHERE run_id = (SELECT max(run_id) FROM runs WHERE source = 'tomtom');
```

---

## Source comparison

| Aspect | Mapbox | Google Maps | TomTom |
|---|---|---|---|
| Tile format | Vector MVT | PNG (screenshot) | Vector PBF |
| Congestion encoding | Text property | Pixel color | Float ratio |
| Free-flowing roads | Always `low` | `no data` (not colored) | `low` (≥ 0.85) |
| `no data` meaning | No GPS coverage | Flowing normally OR no coverage | No coverage |
| Raw value stored | — | — | `traffic_level` (0.0–1.0) |
| Map matching | Geometric (25 m + bearing) | Bearing-based pixel match | Geometric (25 m + bearing) |
| Typical coverage | ~98% of edges | ~5–15% of edges | ~60–80% of edges |

> **Critical difference:** Mapbox `low` and Google `no data` often describe the same
> road — both mean normal traffic flow, expressed differently.
> TomTom is the most direct: `traffic_level ≥ 0.85` → `low`, with the raw float available for custom analysis.

---

## Example queries via `traffic_db`

```python
from scripts.traffic_db import TrafficDB

with TrafficDB('db/tartu.duckdb') as db:
    # Summary per source
    db.get_congestion_summary('mapbox')
    db.get_congestion_summary('google')
    db.get_congestion_summary('tomtom')

    # Cross-source comparison
    both = db.get_congestion_comparison(sources=['mapbox', 'google', 'tomtom'])

    # History for one road
    db.get_congestion_history(road_name='Pikk', source='mapbox')
    db.get_congestion_history(road_name='Pikk', source='tomtom')  # includes traffic_level
```

**Raw SQL — re-classify TomTom with tighter thresholds:**
```sql
WITH latest AS (
    SELECT h.edge_id, h.traffic_level,
           CASE WHEN h.traffic_level < 0.30 THEN 'severe'
                WHEN h.traffic_level < 0.55 THEN 'heavy'
                WHEN h.traffic_level < 0.80 THEN 'moderate'
                ELSE 'low' END AS recls
    FROM tomtom_congestion_history h
    JOIN runs r ON h.run_id = r.run_id
    WHERE r.run_id = (SELECT max(run_id) FROM runs WHERE source = 'tomtom')
)
SELECT e.name, e.highway, l.traffic_level, l.recls
FROM driving.edges e
JOIN latest l ON e.edge_id = l.edge_id
WHERE l.recls IN ('heavy', 'severe')
ORDER BY l.traffic_level;
```
