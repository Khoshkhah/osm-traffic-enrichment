"""
build_match_explorer — standalone HTML tool to investigate UNMATCHED provider segments,
for tuning Filter 1 (resolve_routes) thresholds and the candidate-search radius.

For one area + provider it runs the raw graph-DTW match (NO Filter 1) and records, per
provider segment: its route-quality metrics (dtw_distance, max_dtw_distance, bearing_diff,
overlap_pct) and its distance to the nearest OSM edge. Everything is baked into one
self-contained HTML with a live filter panel:

  * Filter-1 sliders (drift / max-drift / bearing / overlap) → segments flip matched ↔
    rejected live, so you can read off good thresholds.
  * Radius slider → "no-edge" segments whose nearest OSM road is within the radius light up,
    showing whether the 25 m candidate-search radius is large enough.

OSM road background is coloured with the roadstyle `highsat` palette.

Usage:
    python scripts/build_match_explorer.py sodermalm tomtom
    python scripts/build_match_explorer.py sodermalm mapbox --out output/explorer_sodermalm_mapbox.html
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb
import geopandas as gpd
import pandas as pd
from shapely.geometry import mapping

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from scripts.route_match import run_match_routes, utm_srid_for, load_match_thresholds
from scripts.matching_report import HIGHWAY_ORDER
from roadstyle.palettes import PALETTES

_HS = PALETTES["highsat"]
_METRICS = ["dtw_distance", "max_dtw_distance", "bearing_diff", "overlap_pct"]
PROVIDER_EXTRA = {"mapbox": (), "tomtom": ("traffic_level",)}


def _load_edges(db_path: Path) -> gpd.GeoDataFrame:
    con = duckdb.connect(str(db_path), read_only=True)
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    df = con.execute(
        "SELECT edge_id, highway, ST_AsText(geometry) w FROM driving.edges"
    ).df()
    con.close()
    g = gpd.GeoDataFrame(df.drop(columns="w"),
                         geometry=gpd.GeoSeries.from_wkt(df["w"]), crs="EPSG:4326")
    return g[g.geometry.geom_type.isin(["LineString", "MultiLineString"])]


def _edge_color(hw: str) -> str:
    st = _HS.get(hw) or _HS.get(str(hw).replace("_link", "")) or _HS.get("unclassified")
    return st.fill if st else "#888888"


def _edges_geojson(edges: gpd.GeoDataFrame) -> dict:
    feats = []
    for _, r in edges.iterrows():
        feats.append({
            "type": "Feature",
            "properties": {"hw": r["highway"], "c": _edge_color(r["highway"])},
            "geometry": mapping(r.geometry),
        })
    return {"type": "FeatureCollection", "features": feats}


def _segments_geojson(seg: gpd.GeoDataFrame) -> dict:
    num = set(_METRICS + ["nearest_edge_dist", "seg_length_m"])
    feats = []
    for _, r in seg.iterrows():
        props = {}
        for k in ["seg_id", "congestion", "status_raw", *num]:
            v = r[k]
            if k in num:
                props[k] = None if pd.isna(v) else round(float(v), 2)
            else:
                props[k] = None if pd.isna(v) else v
        feats.append({"type": "Feature", "properties": props,
                      "geometry": mapping(r.geometry)})
    return {"type": "FeatureCollection", "features": feats}


def build(area: str, source: str, db_dir="db", output_dir="output",
          out_html: str | None = None) -> Path:
    db_path = BASE / db_dir / f"{area}.duckdb"
    seg_path = BASE / output_dir / f"{area}_traffic_{source}.geojson"
    if not db_path.exists() or not seg_path.exists():
        raise FileNotFoundError(f"missing {db_path} or {seg_path}")

    edges = _load_edges(db_path)
    segments = gpd.read_file(seg_path)

    # Raw match (no Filter 1) → per-segment route metrics + match_type.
    rs, _rl, seg = run_match_routes(edges, segments, extra_cols=PROVIDER_EXTRA[source])
    if rs is None or seg.empty:
        raise RuntimeError(f"no usable segments for {area}/{source}")

    cols = ["source_id", "match_type", *_METRICS]
    seg = seg.merge(rs[cols].rename(columns={"source_id": "seg_id"}), on="seg_id", how="left")
    seg["status_raw"] = seg["match_type"].apply(
        lambda m: "routed" if (isinstance(m, str) and m != "NO_MATCH") else "no_edge")

    # Distance from each segment to the nearest OSM edge (for the radius question).
    srid = utm_srid_for(seg)
    seg_m = seg.to_crs(srid)
    edges_m = edges[["geometry"]].to_crs(srid)
    nd = gpd.sjoin_nearest(seg_m, edges_m, distance_col="_nd")
    nd = nd[~nd.index.duplicated(keep="first")]["_nd"]
    seg["nearest_edge_dist"] = nd.reindex(seg.index)
    seg["seg_length_m"] = seg_m.geometry.length

    seg_geo = _segments_geojson(seg)
    osm_geo = _edges_geojson(edges)

    th = load_match_thresholds(BASE / "config" / "match_thresholds.yaml")
    sm = (th.get(source, {}) or {}).get("segment_match", {}) or {}
    presets = {
        "dtw": sm.get("max_match_dist", 25),
        "bearing": sm.get("max_bearing_diff", 20),
        "overlap": sm.get("min_overlap_pct", 80),
        "radius": 25,
    }
    c = seg.to_crs("EPSG:4326").geometry.union_all().centroid
    counts = seg["status_raw"].value_counts().to_dict()

    # OSM highway categories present, sorted by importance, with palette colours.
    rank = {h: i for i, h in enumerate(HIGHWAY_ORDER)}
    cats = sorted([h for h in edges["highway"].dropna().unique()],
                  key=lambda h: rank.get(h, len(HIGHWAY_ORDER)))
    osm_colors = {h: _edge_color(h) for h in cats}

    html = _HTML.replace("__TITLE__", f"{area} · {source}")
    html = html.replace("__CENTER__", json.dumps([c.y, c.x]))
    html = html.replace("__PRESETS__", json.dumps(presets))
    html = html.replace("__SUBTITLE__", json.dumps(
        f"{len(seg):,} segments · routed {counts.get('routed', 0):,} · no-edge {counts.get('no_edge', 0):,}"))
    html = html.replace("__OSMCATS__", json.dumps(cats))
    html = html.replace("__OSMCOLORS__", json.dumps(osm_colors))
    html = html.replace("__OSM__", json.dumps(osm_geo))
    html = html.replace("__SEG__", json.dumps(seg_geo))

    out = Path(out_html) if out_html else BASE / output_dir / f"explorer_{area}_{source}.html"
    out.write_text(html)
    return out


# ── Self-contained HTML (Leaflet + a live filter panel) ────────────────────────
_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Match explorer — __TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  html,body{margin:0;height:100%;font-family:system-ui,sans-serif;background:#0b0f17}
  #map{position:absolute;inset:0}
  #panel{position:absolute;top:12px;left:12px;z-index:1000;width:280px;
    max-height:calc(100vh - 24px);overflow-y:auto;
    background:rgba(17,24,39,.92);color:#e5e7eb;border:1px solid #374151;border-radius:12px;
    padding:14px 16px;backdrop-filter:blur(8px);box-shadow:0 8px 24px rgba(0,0,0,.4);font-size:13px}
  #panel h1{font-size:14px;margin:0 0 2px}
  #panel .sub{color:#9ca3af;font-size:11px;margin-bottom:10px}
  .row{margin:9px 0}
  .row label{display:flex;justify-content:space-between;font-size:12px;color:#cbd5e1}
  .row label b{color:#fff;font-variant-numeric:tabular-nums}
  input[type=range]{width:100%;accent-color:#3b82f6;margin-top:3px}
  .legend{margin-top:12px;border-top:1px solid #374151;padding-top:10px}
  .legend div{display:flex;align-items:center;gap:8px;margin:5px 0;cursor:pointer;user-select:none}
  .sw{width:14px;height:6px;border-radius:2px;flex:none}
  .legend .ct{margin-left:auto;color:#fff;font-variant-numeric:tabular-nums}
  .off{opacity:.35}
  .hint{margin-top:10px;color:#9ca3af;font-size:10.5px;line-height:1.4}
  .leaflet-tooltip{font-size:11px}
</style></head>
<body><div id="map"></div>
<div id="panel">
  <h1>Filter 1 explorer · __TITLE__</h1>
  <div class="sub" id="subtitle"></div>
  <div class="row"><label>drift ≤ <b id="v-dtw"></b> m</label><input type="range" id="dtw" min="0" max="80" step="0.5"></div>
  <div class="row"><label>bearing ≤ <b id="v-bearing"></b>°</label><input type="range" id="bearing" min="0" max="90" step="0.5"></div>
  <div class="row"><label>overlap ≥ <b id="v-overlap"></b> %</label><input type="range" id="overlap" min="0" max="100" step="1"></div>
  <div class="row"><label>search radius ≤ <b id="v-radius"></b> m</label><input type="range" id="radius" min="5" max="100" step="1"></div>
  <div class="legend" id="legend">
    <div data-k="matched"><span class="sw" style="background:#9ca3af"></span>matched<span class="ct" id="c-matched"></span></div>
    <div data-k="rejected"><span class="sw" style="background:#f59e0b"></span>rejected by Filter 1<span class="ct" id="c-rejected"></span></div>
    <div data-k="reach"><span class="sw" style="background:#a855f7"></span>no-edge, within radius<span class="ct" id="c-reach"></span></div>
    <div data-k="noedge"><span class="sw" style="background:#ef4444"></span>no-edge, beyond radius<span class="ct" id="c-noedge"></span></div>
  </div>
  <div class="legend" id="osm-sec">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;color:#cbd5e1">
        <input type="checkbox" id="osm-toggle" checked> OSM roads
      </label>
      <span style="color:#9ca3af;font-size:11px">opacity <b id="v-osmop" style="color:#fff">45</b>%</span>
    </div>
    <input type="range" id="osm-opacity" min="0" max="100" value="45" style="margin-top:6px">
    <div style="display:flex;gap:10px;margin:6px 0 2px">
      <a id="osm-all" style="color:#60a5fa;font-size:10.5px;cursor:pointer">all</a>
      <a id="osm-none" style="color:#60a5fa;font-size:10.5px;cursor:pointer">none</a>
      <span style="color:#9ca3af;font-size:10.5px">road categories</span>
    </div>
    <div id="osm-cats" style="max-height:130px;overflow-y:auto"></div>
  </div>
  <div class="hint">Move the sliders to the threshold you're testing: <b>rejected</b> (orange) are
  segments Filter 1 would drop. <b>Purple</b> = no candidate within 25 m but a road sits within
  the radius slider → the search radius may be too small. <b>Red</b> = genuinely no nearby road.
  Click a legend row to show/hide it.</div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const CENTER=__CENTER__, PRESETS=__PRESETS__, OSM=__OSM__, SEG=__SEG__, SUBTITLE=__SUBTITLE__;
const OSMCATS=__OSMCATS__, OSMCOLORS=__OSMCOLORS__;
const COL={matched:"#9ca3af",rejected:"#f59e0b",reach:"#a855f7",noedge:"#ef4444"};
const hidden={matched:false,rejected:false,reach:false,noedge:false};
document.getElementById("subtitle").textContent=SUBTITLE;

const map=L.map("map",{preferCanvas:true}).setView(CENTER,14);

// Base-layer selection (Leaflet layers control, top-right)
const C="https://{s}.basemaps.cartocdn.com/", A="&copy; OpenStreetMap, &copy; CARTO";
const bases={
  "Dark":      L.tileLayer(C+"dark_all/{z}/{x}/{y}{r}.png",{attribution:A,subdomains:"abcd",maxZoom:20}),
  "Light":     L.tileLayer(C+"light_all/{z}/{x}/{y}{r}.png",{attribution:A,subdomains:"abcd",maxZoom:20}),
  "Voyager":   L.tileLayer(C+"rastertiles/voyager/{z}/{x}/{y}{r}.png",{attribution:A,subdomains:"abcd",maxZoom:20}),
  "Satellite": L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",{attribution:"&copy; Esri",maxZoom:20}),
  "OSM":       L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{attribution:"&copy; OpenStreetMap",maxZoom:19}),
};
bases["Dark"].addTo(map);
L.control.layers(bases,null,{position:"topright"}).addTo(map);

// OSM roads — controllable layer (roadstyle highsat colours), filterable by category
const osmCats={}; OSMCATS.forEach(h=>osmCats[h]=true);
const osmLayer=L.geoJSON(OSM,{style:f=>({color:f.properties.c,weight:1.2,opacity:.45})}).addTo(map);
function renderOSM(){
  const show=document.getElementById("osm-toggle").checked;
  const op=+document.getElementById("osm-opacity").value/100;
  document.getElementById("v-osmop").textContent=Math.round(op*100);
  osmLayer.eachLayer(l=>{
    const on=show && osmCats[l.feature.properties.hw]!==false;
    l.setStyle({opacity:on?op:0,weight:on?1.3:0});
  });
}
const _box=document.getElementById("osm-cats");
OSMCATS.forEach(h=>{
  const row=document.createElement("label");
  row.style.cssText="display:flex;align-items:center;gap:7px;margin:3px 0;font-size:11.5px;cursor:pointer;color:#cbd5e1";
  row.innerHTML=`<input type="checkbox" checked><span style="width:13px;height:4px;border-radius:2px;flex:none;background:${OSMCOLORS[h]||'#888'}"></span>${h}`;
  row.querySelector("input").addEventListener("change",e=>{osmCats[h]=e.target.checked;renderOSM();});
  _box.appendChild(row);
});
function _setAll(v){_box.querySelectorAll("input").forEach((cb,i)=>{cb.checked=v;osmCats[OSMCATS[i]]=v;});renderOSM();}
document.getElementById("osm-all").onclick=()=>_setAll(true);
document.getElementById("osm-none").onclick=()=>_setAll(false);
document.getElementById("osm-toggle").addEventListener("change",renderOSM);
document.getElementById("osm-opacity").addEventListener("input",renderOSM);
renderOSM();

function cls(p,t){
  if(p.status_raw==="no_edge")
    return (p.nearest_edge_dist!=null && p.nearest_edge_dist<=t.radius) ? "reach" : "noedge";
  const ok = (p.dtw_distance==null||p.dtw_distance<=t.dtw)
    && (p.bearing_diff==null||p.bearing_diff<=t.bearing)
    && (p.overlap_pct==null||p.overlap_pct>=t.overlap);
  return ok?"matched":"rejected";
}
function tip(p,k){
  return `seg ${p.seg_id} · <b>${k}</b><br>cong: ${p.congestion}<br>`
    +`drift ${p.dtw_distance??"–"} (max ${p.max_dtw_distance??"–"}) · bearing ${p.bearing_diff??"–"}°<br>`
    +`overlap ${p.overlap_pct??"–"}% · len ${p.seg_length_m??"–"} m · nearest edge ${p.nearest_edge_dist??"–"} m`;
}
const segLayer=L.geoJSON(SEG,{
  style:f=>({color:"#888",weight:3,opacity:.85}),
  onEachFeature:(f,l)=>{ l.bindTooltip(()=>tip(f.properties,cls(f.properties,thresholds())),{sticky:true}); }
}).addTo(map);

function thresholds(){return {
  dtw:+dtw.value,bearing:+bearing.value,overlap:+overlap.value,radius:+radius.value};}

function render(){
  const t=thresholds();
  document.getElementById("v-dtw").textContent=t.dtw;
  document.getElementById("v-bearing").textContent=t.bearing;
  document.getElementById("v-overlap").textContent=t.overlap;
  document.getElementById("v-radius").textContent=t.radius;
  const ct={matched:0,rejected:0,reach:0,noedge:0};
  segLayer.eachLayer(l=>{
    const k=cls(l.feature.properties,t); ct[k]++;
    if(hidden[k]){ l.setStyle({opacity:0,weight:0}); }
    else { l.setStyle({color:COL[k],weight:k==="matched"?2.5:3.5,opacity:.9}); }
  });
  for(const k in ct) document.getElementById("c-"+k).textContent=ct[k].toLocaleString();
}

for(const id of ["dtw","bearing","overlap","radius"]){
  const el=document.getElementById(id); el.value=PRESETS[id]; el.addEventListener("input",render);
}
document.querySelectorAll("#legend div").forEach(d=>d.addEventListener("click",()=>{
  const k=d.dataset.k; hidden[k]=!hidden[k]; d.classList.toggle("off",hidden[k]); render();
}));
render();
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("area")
    ap.add_argument("source", choices=["mapbox", "tomtom"])
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    out = build(args.area, args.source, out_html=args.out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
