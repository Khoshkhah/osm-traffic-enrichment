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
Google Maps traffic tile CDN (`mt0.google.com/vt?lyrs=m@321,traffic|en`) —
served as 256×256 RGBA PNG raster images.

### How Google collects data
Google aggregates GPS data from Android devices (with user consent), Waze community
reports, and road sensors to estimate real-time speeds across its road network.
The methodology is similar to Mapbox: compare current speed against a historical baseline
for the same road at the same time of day.

### How congestion is encoded in the tile
Unlike Mapbox (which uses text properties in a vector format), Google encodes
congestion as **pixel color** drawn directly onto the road lines in the PNG image.
Each congested road segment is painted in a color corresponding to its level.
Non-congested roads keep their default gray/white road color.

### How the pipeline reads it — HSV pixel color analysis

Because there is no text property to read, the pipeline must classify
each pixel's color. We use the **HSV color space** (Hue, Saturation, Value)
which separates color from brightness, making thresholds more robust
than raw RGB comparisons across lighting conditions.

| HSV component | Meaning | Range |
|---|---|---|
| **H** (Hue) | Which color — red=0°, yellow=60°, green=120° | 0–360° |
| **S** (Saturation) | How vivid — 0=gray, 1=pure color | 0–1 |
| **V** (Value) | Brightness — 0=black, 1=fully bright | 0–1 |

**Color → congestion mapping used by the pipeline:**

| Congestion | Pixel color | Hue | Saturation | Value |
|---|---|---|---|---|
| `low` | 🟢 Green | 90–150° | > 35% | > 35% |
| `moderate` | 🟠 Orange / Yellow | 20–60° | > 45% | > 55% |
| `heavy` | 🔴 Red | < 20° or > 340° | > 50% | > 40% |
| `severe` | ⬛ Dark red | < 20° or > 340° | > 35% | ≤ 40% |
| `no data` | ⬜ Gray / transparent | any | < 30% | any |

**Why gray pixels map to `no data`:**
The default road color on a Google Maps roadmap tile is light gray.
Gray has very low saturation (S < 30%), so it is rejected by all congestion checks.
This means roads with no active congestion remain `no data` in our output.

**Sampling strategy** — why dense sampling is needed:

Traffic-colored pixels are very sparse (a road is only 1–3 pixels wide in a 256×256 tile,
and colored segments represent only ~0.1–0.5% of all pixels). To reliably detect them:

1. Interpolate sample points along each OSM edge at **1 point per ~2 metres**
   (up to 200 points per edge, computed in Web Mercator metres for accuracy)
2. At each point, check a **3×3 pixel neighbourhood** around the exact position
3. Return the most severe congestion found across all samples

```
Pipeline steps for Google Maps:
  1. Download roadmap+traffic PNG tiles (mt0.google.com CDN)  [notebook 3b]
  2. Load tiles into memory as RGBA PIL Images (O(1) pixel lookup)
  3. For each OSM edge from DuckDB:
     a. Project geometry to metres (EPSG:3857) to compute length
     b. Interpolate 1 point per ~2 m along the edge (max 200)
     c. For each point: convert lon/lat → tile (x,y,z) → pixel (col, row)
     d. Check 3×3 pixel neighbourhood → classify each with RGB→HSV→level
     e. Return the most severe level found
  4. Write directly to driving.edges.congestion_google
     NO separate map-matching step — OSM edge geometry is used directly
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
