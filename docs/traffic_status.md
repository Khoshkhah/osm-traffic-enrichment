# Traffic Status Values

This document explains the four congestion levels used in the pipeline,
what they mean in practice, and exactly how each data source (Mapbox and Google Maps)
derives them — the methods are fundamentally different.

---

## Congestion levels

Both Mapbox and Google Maps use the same four-level scale, stored in
`congestion_mapbox` and `congestion_google` columns of `driving.edges`.

| Value | Color | Typical speed | Meaning |
|---|---|---|---|
| `low` | 🟢 Green | Near free-flow speed | Traffic moving normally. No significant delay. |
| `moderate` | 🟠 Orange | 60–80% of free-flow speed | Noticeable slowdown. Travel time slightly increased. |
| `heavy` | 🔴 Red | 40–60% of free-flow speed | Significant congestion. Expect delays. |
| `severe` | ⬛ Dark red | < 40% of free-flow speed | Near standstill. Major incident or rush hour peak. |
| `no data` | ⬜ Gray | Unknown | No sensor coverage, or road is not monitored. |

**Free-flow speed** is the expected speed under ideal conditions with no congestion
(usually close to the speed limit). Congestion is measured as the ratio of the
current observed speed to this baseline.

---

## How Mapbox computes congestion

### Data source
Mapbox Traffic v1 tileset (`mapbox.mapbox-traffic-v1`) — served as
Mapbox Vector Tiles (`.mvt` / `.pbf` binary format).

### How Mapbox collects data
Mapbox aggregates **anonymous GPS probe data** from millions of mobile devices
and vehicles in real time. Each probe is a timestamped GPS location with speed.
Mapbox processes these probes to estimate the current speed on every road segment
in its network.

### How congestion is computed (Mapbox internal algorithm)
1. **Baseline speed** — Mapbox maintains a historical baseline speed for each
   road segment at each time of day and day of week. This represents normal free-flow speed.
2. **Current speed** — Computed from recent GPS probes on the segment.
3. **Speed ratio** — `current_speed / baseline_speed`
4. **Classification:**

| Speed ratio | Congestion level |
|---|---|
| > 0.85 | `low` |
| 0.60 – 0.85 | `moderate` |
| 0.40 – 0.60 | `heavy` |
| < 0.40 | `severe` |

The exact thresholds are Mapbox's proprietary algorithm and may vary by road type,
region, and time of day. The ratios above are approximate.

### How the pipeline reads it
The congestion level is already a **text property** (`congestion: "low"`) embedded
directly in the MVT tile features — no color analysis or computation needed on our side.

```
Pipeline steps for Mapbox:
  1. Download mapbox.mapbox-traffic-v1 tiles (binary .mvt)    [notebook 3]
  2. Decode with mapbox_vector_tile library
  3. Each decoded feature already has: {class, congestion}
     e.g. congestion = "low"
  4. Spatial join to Mapbox streets-v8 tile (road geometry)
  5. Geometric map matching to OSM edges                       [notebook 4]
     - 25 m corridor buffer
     - 45° direction filter (rejects parallel/opposite roads)
     - 40% overlap threshold
  6. Write to driving.edges.congestion_mapbox
```

**Coverage:** Mapbox assigns a level to every road it has sensor data for.
A free-flowing road gets `low`, NOT `no data`.
`no data` only appears for roads Mapbox has no GPS probe coverage for at all.

---

## How Google Maps computes congestion

### Data source
Google Maps JavaScript API (`TrafficLayer`) rendered by a headless Chromium browser
via **Playwright** — the same approach used by the
[googletraffic R package](https://github.com/dime-worldbank/googletraffic).

### How Google collects data
Google aggregates GPS data from Android devices (with user consent), Waze community
reports, and road sensors to estimate real-time speeds across its road network.
The methodology is similar to Mapbox: compare current speed against a historical baseline
for the same road at the same time of day.

### How congestion is encoded
Unlike Mapbox (which uses text properties in a vector format), Google's `TrafficLayer`
renders congestion as **pixel color** painted on road lines. Each congested segment
is drawn in one of four colors; non-congested roads stay white (in our custom rendering).

### How the pipeline reads it — Playwright rendering + bearing-based matching

**Step 1 — Render a clean screenshot**

Instead of fetching CDN tiles (which contain road labels, icons, and signs), the pipeline
renders a custom Google Maps HTML page in a headless browser with:
- All labels → `visibility: off`
- All geometry → `visibility: off` (buildings, water, etc.)
- Roads → `visibility: on`, color `#ffffff` (white)
- Background → `#ffffff` (white)
- `TrafficLayer` added on top

The result is a clean PNG where **only traffic color stripes are visible**.
No false positives from map text, signs, or icons.

**Step 2 — Classify pixels (LAB color distance)**

The pipeline classifies each non-white pixel by computing its
**CIE76 distance in LAB color space** to four reference colors taken from the
`googletraffic` R package — the exact colors the Google Maps JS API renders:

| Congestion | Hex | RGB |
|---|---|---|
| `low` | `#11D68F` | (17, 214, 143) — teal-green |
| `moderate` | `#FFCF43` | (255, 207, 67) — yellow |
| `heavy` | `#F24E42` | (242, 78, 66) — red |
| `severe` | `#A92727` | (169, 39, 39) — dark red |

A pixel is assigned to the nearest reference if the CIE76 distance is ≤ 25 units;
otherwise it remains `no data`. LAB is perceptually uniform — equal distance means
equal visual difference, making the threshold robust to JPEG/PNG compression artifacts.

**Step 3 — Bearing-based pixel-to-edge matching**

Traffic stripes always run *along* roads. The pipeline exploits this to resolve
intersection ambiguity:

1. Extract all classified traffic pixels as EPSG:3857 coordinates
2. For each pixel:
   - Estimate the local **stripe direction** (PCA on neighboring traffic pixels within 25 m)
   - Find candidate edge sample points within **10 m**
   - Keep only candidates whose local bearing is within **40°** of the stripe bearing
   - If only one OSM way (`osm_id`) remains after the bearing filter → assign to nearest edge
   - If multiple ways remain (true intersection) → **discard** the pixel
3. Aggregate: each edge takes the most severe level among its assigned pixels

This avoids the intersection problem: pixels at road crossings are naturally discarded
because two perpendicular roads cannot both match the stripe bearing.

```
Pipeline steps for Google Maps:
  1. Render custom-styled Google Maps HTML in Playwright  [notebook 3b / pipeline]
     — white bg, white roads, TrafficLayer only, no labels/icons
  2. Build traffic raster: classify every pixel with CIE76 LAB distance
     — produces a uint8 array (0=none, 1=low, 2=moderate, 3=heavy, 4=severe)
  3. For each traffic pixel:
     a. Estimate local stripe direction from neighboring traffic pixels (PCA)
     b. Find candidate edges within 10 m
     c. Filter by bearing alignment (max 40° difference)
     d. Require single osm_id among filtered candidates (discard intersections)
     e. Assign to nearest edge
  4. Aggregate by edge: most severe level wins
  5. Write to driving.edges.congestion_google + edge_congestion_history
```

---

## Key differences between Mapbox and Google

| Aspect | Mapbox | Google Maps |
|---|---|---|
| Tile format | Vector (MVT binary) | Raster (PNG image) |
| Congestion encoding | Text property on each road segment | Pixel color drawn on road lines |
| How the pipeline reads it | Decode string property directly | Classify pixel color via HSV analysis |
| Free-flowing roads | Always assigned `low` | Keep default gray → `no data` |
| `no data` meaning | Road has **no GPS probe coverage** | Road is **flowing normally** OR has no coverage |
| Map matching needed? | **Yes** — Mapbox segment IDs differ from OSM | **No** — sampled directly on OSM edge geometry |
| Coverage | High — most sensor-covered roads get a value | Partial — only congested roads are colored |
| Rush hour | More edges get non-`low` values | Many more colored pixels during peak hours |

> **Critical difference in `no data` interpretation:**
> - Mapbox `no data` → this road has **no sensor coverage at all**
> - Google `no data` → this road is most likely **flowing normally** (not colored)
>
> When comparing the two sources, expect Google to show far more `no data`
> edges even when traffic is light, while Mapbox will mark those same edges as `low`.

---

## Example SQL queries

```sql
-- Current congestion from both sources for a specific road
SELECT name, highway,
       congestion_mapbox,    congestion_mapbox_at,
       congestion_google,    congestion_google_at
FROM driving.edges
WHERE name = 'Ringvägen';

-- Roads where Mapbox shows congestion but Google shows no data
-- (likely flowing normally — Google simply does not color non-congested roads)
SELECT edge_id, name, highway, congestion_mapbox, congestion_google
FROM driving.edges
WHERE congestion_mapbox IN ('moderate', 'heavy', 'severe')
  AND congestion_google = 'no data';

-- Roads where both sources agree on heavy or severe congestion
SELECT edge_id, name, highway, congestion_mapbox, congestion_google
FROM driving.edges
WHERE congestion_mapbox IN ('heavy', 'severe')
  AND congestion_google  IN ('heavy', 'severe');

-- History: how congestion changed on a road across all runs and sources
SELECT r.fetched_at, r.source, h.congestion
FROM edge_congestion_history h
JOIN runs r          ON h.run_id  = r.run_id
JOIN driving.edges e ON h.edge_id = e.edge_id
WHERE e.name = 'Ringvägen'
ORDER BY r.fetched_at, r.source;

-- Match rate comparison: how many edges each source covers
SELECT
    sum(CASE WHEN congestion_mapbox != 'no data' THEN 1 ELSE 0 END) AS mapbox_covered,
    sum(CASE WHEN congestion_google != 'no data' THEN 1 ELSE 0 END) AS google_covered,
    count(*) AS total_edges,
    round(sum(CASE WHEN congestion_mapbox != 'no data' THEN 1 ELSE 0 END) * 100.0 / count(*), 1)
        AS mapbox_pct,
    round(sum(CASE WHEN congestion_google != 'no data' THEN 1 ELSE 0 END) * 100.0 / count(*), 1)
        AS google_pct
FROM driving.edges;
```
