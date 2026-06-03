# Map-matching: how provider segments are attached to OSM edges

After a provider's traffic segments are fetched, they must be **matched onto the OSM
`driving.edges`** so each edge gets a congestion level. This doc explains the matcher used
for **Mapbox and TomTom**. (Google is different — it matches *pixels* to edges and is not
covered here; see [traffic_status.md](traffic_status.md).)

There are two matchers, selectable with the `matcher` config key (or `--matcher`):

| `matcher` | What it is | Default |
|---|---|---|
| `route` | Route-based graph-DTW from the `network_matching` library | ✅ yes |
| `geometric` | Legacy corridor + bearing + overlap, most-severe-wins | no (fallback) |

Both produce the same output contract — `dict[edge_id → level]` (+ TomTom's raw
`traffic_level`) — so the database schema and `traffic_db` are unchanged.

---

## Route-based matcher (default)

Implemented in [`scripts/route_match.py`](../scripts/route_match.py), wrapping
`network_matching.DuckDBMapMatcher.match_routes`.

### Direction: provider → OSM
Provider segments are the **source (A)**; OSM edges are the **destination graph (B)**. Each
provider segment is matched to a **connected route of OSM edges**. OSM is the dense, directed,
well-connected network the matcher wants as B; the provider data (especially TomTom's sparse
segments) is not, so this direction is the robust choice. Because B is directed, a segment is
attached to the correctly-oriented carriageway, preserving directional congestion.

> MultiLineString provider segments are **exploded** to single LineStrings first — a
> multi-part geometry spanning disconnected roads can't map to one connected route.

### No filter — max-cover winner, store the quality numbers

The matcher returns `routes_long` (one row per **segment-edge → OSM-edge**). There is **no
filtering**: per OSM edge we simply take the **maximum-covering** candidate (largest
`edge_matched_len`). Its two numbers are computed and **stored** so matches can be filtered later
via SQL instead of at match time:

- **`covering_match`** = `edge_b_used_pct` — % of the OSM edge the winning segment covers.
- **`quality_match`** = `exp(−drift/τ) · max(0, cos(bearing))` — geometric **alignment** in `[0,1]`
  (drift = `edge_match_dist_avg`, bearing = `edge_bearing_diff`; `τ` = `match_tau`, default 15 m).
  High = well-aligned; opposite/perpendicular matches (bearing ≥ 90°) score 0.

Result: **exactly one row per OSM edge** (`congestion` + `covering_match` + `quality_match`, plus
TomTom's raw `traffic_level`), written to the `*_congestion_history` tables. No intermediate tables
are persisted; nothing is dropped at match time.

**Filter later, in queries.** Because the quality numbers are stored, you choose how strict to be
at read time, e.g.:
```sql
SELECT * FROM mapbox_congestion_history
WHERE quality_match >= 70 AND covering_match >= 40;
```

Engine knobs (config): `match_step_m` (sampling step), `match_tau` (quality drift scale),
`utm_srid` (auto-derived UTM if unset). Every fetch logs a short summary to the pipeline log:
segment parts → matched/unmatched → OSM edges assigned.

### Cost
Heavier than the geometric scan but parallel (`n_jobs=-1`). On the largest area (nacka,
~22k edges) a full source matches in well under a minute.

---

## Geometric matcher (legacy / fallback)

Selectable with `matcher: geometric`. For each provider segment: a 25 m corridor buffer,
keep OSM edges whose bearing agrees within 45° and that overlap the corridor by ≥40%; when
several segments hit one edge, **most severe wins**. Simple and fast, but it over-matches
(one edge often claimed by many segments, including parallel roads) and folds direction.
Kept as a safety fallback and for A/B comparison.

---

## Evaluating a match

Use [`scripts/matching_report.py`](../scripts/matching_report.py) and
[`notebook/6_matching_report.ipynb`](../notebook/6_matching_report.ipynb) to inspect coverage,
cardinality (M:1 / 1:N), per-highway coverage, and cross-source agreement for a given area and
source.

## Tuning Filter 1 — the explorer

[`scripts/build_match_explorer.py`](../scripts/build_match_explorer.py) builds a **self-contained
HTML** for tuning the Filter-1 thresholds and the candidate-search radius:

```bash
python scripts/build_match_explorer.py sodermalm tomtom    # → output/explorer_sodermalm_tomtom.html
```

Every provider segment carries its route metrics (`dtw_distance`, `max_dtw_distance`,
`bearing_diff`, `overlap_pct`), its length, and its distance to the nearest OSM edge. The panel
has live sliders (presets from `match_thresholds.yaml`): move them and segments flip
**matched ↔ rejected** so you can read off thresholds. The radius slider lights up `no-edge`
segments whose nearest road is within the radius — i.e. whether 25 m is large enough. OSM
background is coloured with the roadstyle `highsat` palette. Open the HTML in any browser.
