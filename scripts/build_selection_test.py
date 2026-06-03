"""
build_selection_test — standalone HTML to test candidate selection by a single QUALITY score.

No filters. For every OSM edge we gather the list of candidate provider segments matched to it
(raw graph-DTW) and reduce each candidate to ONE number where higher = better match:

    covering = edge_b_used_pct / 100                  # fraction of THIS OSM edge covered
    align    = exp(-drift/TAU) * max(0, cos(bearing)) # geometric goodness; drift=edge_match_dist_avg
    quality  = covering * align                        # in [0,1]; high = covers well AND aligns well

Selection: per edge, the candidate with the highest quality wins (if it clears the min-quality
floor), else the edge is unmatched. The HTML has a live min-quality slider; click an edge to pin
it and list its candidates; toggle the provider segments overlay.

Usage:
    python scripts/build_selection_test.py sodermalm tomtom
    python scripts/build_selection_test.py sodermalm mapbox --tau 15
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb
import geopandas as gpd
import numpy as np
from shapely.geometry import mapping

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from scripts.route_match import run_match_routes
from roadstyle.palettes import PALETTES

DEFAULT_TAU = 15.0  # metres; drift scale in the alignment term (larger = more forgiving)
_HS = PALETTES["highsat"]
_SEG_COLOR = "#d946ef"  # provider overlay — fuchsia, distinct from the congestion palette
PROVIDER_EXTRA = {"mapbox": (), "tomtom": ("traffic_level",)}


def _load_edges(db_path: Path) -> gpd.GeoDataFrame:
    con = duckdb.connect(str(db_path), read_only=True)
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    df = con.execute("SELECT edge_id, highway, ST_AsText(geometry) w FROM driving.edges").df()
    con.close()
    g = gpd.GeoDataFrame(df.drop(columns="w"),
                         geometry=gpd.GeoSeries.from_wkt(df["w"]), crs="EPSG:4326")
    return g[g.geometry.geom_type.isin(["LineString", "MultiLineString"])]


def _edge_color(hw):
    st = _HS.get(hw) or _HS.get(str(hw).replace("_link", "")) or _HS.get("unclassified")
    return st.fill if st else "#888888"


def build(area: str, source: str, db_dir="db", output_dir="output", out_html=None,
          tau: float = DEFAULT_TAU) -> Path:
    db_path = BASE / db_dir / f"{area}.duckdb"
    seg_path = BASE / output_dir / f"{area}_traffic_{source}.geojson"
    edges = _load_edges(db_path)
    segments = gpd.read_file(seg_path)

    rs, rl, seg = run_match_routes(edges, segments, extra_cols=PROVIDER_EXTRA[source])
    if rl is None or len(rl) == 0:
        raise RuntimeError("no candidates")

    cong = seg.set_index("seg_id")["congestion"]
    rl = rl.copy()
    rl["cong"] = rl["source_id"].map(cong)
    rl["cov"] = rl["edge_b_used_pct"].astype(float)
    rl["qual"] = (np.exp(-rl["edge_match_dist_avg"].astype(float) / tau)   # alignment only
                  * np.clip(np.cos(np.radians(rl["edge_bearing_diff"].astype(float))), 0, None))

    # candidate list per OSM edge: [covering%, quality, congestion, seg_id]
    cand: dict[int, list] = {}
    for eid, g in rl.groupby("dest_id"):
        cand[int(eid)] = [[round(float(r.cov), 1), round(float(r.qual), 3), r.cong, int(r.source_id)]
                          for r in g.itertuples()]
    # inverse: per provider segment, the OSM edges it's a candidate for [edge_id, cov%, quality]
    segcand: dict[int, list] = {}
    for sid, g in rl.groupby("source_id"):
        segcand[int(sid)] = [[int(r.dest_id), round(float(r.cov), 1), round(float(r.qual), 3)]
                             for r in g.itertuples()]

    feats = [{"type": "Feature",
              "properties": {"id": int(r["edge_id"]), "hw": r["highway"], "c": _edge_color(r["highway"])},
              "geometry": mapping(r.geometry)} for _, r in edges.iterrows()]
    edges_geo = {"type": "FeatureCollection", "features": feats}

    sfeats = [{"type": "Feature",
               "properties": {"seg_id": int(r["seg_id"]), "cong": r["congestion"]},
               "geometry": mapping(r.geometry)} for _, r in seg.iterrows()]
    seg_geo = {"type": "FeatureCollection", "features": sfeats}

    c = edges.geometry.union_all().centroid
    html = _HTML.replace("__TITLE__", f"{area} · {source}")
    html = html.replace("__CENTER__", json.dumps([c.y, c.x]))
    html = html.replace("__TAU__", f"{tau:g}")
    html = html.replace("__SEGCOLOR__", _SEG_COLOR)
    html = html.replace("__SUB__", json.dumps(
        f"{len(edges):,} OSM edges · {len(cand):,} have ≥1 candidate · {len(rl):,} candidates"))
    html = html.replace("__EDGES__", json.dumps(edges_geo))
    html = html.replace("__CAND__", json.dumps(cand))
    html = html.replace("__SEGCAND__", json.dumps(segcand))
    html = html.replace("__SEG__", json.dumps(seg_geo))

    out = Path(out_html) if out_html else BASE / output_dir / f"selection_{area}_{source}.html"
    out.write_text(html)
    return out


_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Selection test — __TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  html,body{margin:0;height:100%;font-family:system-ui,sans-serif;background:#0b0f17}
  #map{position:absolute;inset:0}
  #panel{position:absolute;top:12px;left:12px;z-index:1000;width:272px;
    background:rgba(17,24,39,.93);color:#e5e7eb;border:1px solid #374151;border-radius:12px;
    padding:14px 16px;backdrop-filter:blur(8px);box-shadow:0 8px 24px rgba(0,0,0,.4);font-size:13px}
  #panel h1{font-size:14px;margin:0 0 2px}
  #panel .sub{color:#9ca3af;font-size:11px;margin-bottom:10px}
  .row{margin:10px 0}
  .row label{display:flex;justify-content:space-between;font-size:12px;color:#cbd5e1}
  .row label b{color:#fff;font-variant-numeric:tabular-nums}
  input[type=range]{width:100%;accent-color:#3b82f6;margin-top:3px}
  .legend{margin-top:12px;border-top:1px solid #374151;padding-top:10px}
  .legend div{display:flex;align-items:center;gap:8px;margin:5px 0}
  .sw{width:14px;height:6px;border-radius:2px;flex:none}
  .legend .ct{margin-left:auto;color:#fff;font-variant-numeric:tabular-nums}
  .hint{margin-top:10px;color:#9ca3af;font-size:10.5px;line-height:1.4}
  .leaflet-tooltip{font-size:11px}
</style></head>
<body><div id="map"></div>
<div id="panel">
  <h1>Match quality map · __TITLE__</h1>
  <div class="sub" id="sub"></div>
  <div class="row"><label>quality filter ≥ <b id="v-q0"></b></label>
    <input type="range" id="q0" min="0" max="1" step="0.01" value="0"></div>
  <div class="row"><label>covering floor ≥ <b id="v-cmin"></b> %</label>
    <input type="range" id="cmin" min="0" max="100" step="1" value="30"></div>
  <div class="row"><label style="cursor:pointer"><input type="checkbox" id="seg-toggle" style="margin-right:7px">show provider segments (dashed)</label></div>
  <div class="legend" id="legend">
    <div style="color:#9ca3af;font-size:10.5px;margin-bottom:2px">edge colour = match QUALITY</div>
    <div><span class="sw" style="background:#11D68F"></span>high&nbsp; q ≥ 0.85<span class="ct" id="c-high"></span></div>
    <div><span class="sw" style="background:#FFCF43"></span>good&nbsp; 0.70–0.85<span class="ct" id="c-good"></span></div>
    <div><span class="sw" style="background:#F24E42"></span>fair&nbsp; 0.50–0.70<span class="ct" id="c-fair"></span></div>
    <div><span class="sw" style="background:#A92727"></span>poor&nbsp; q < 0.50<span class="ct" id="c-poor"></span></div>
    <div><span class="sw" style="background:#475569"></span>unmatched<span class="ct" id="c-none"></span></div>
  </div>
  <div id="detail" style="display:none;margin-top:12px;border-top:1px solid #374151;padding-top:10px;font-size:11.5px;max-height:170px;overflow-y:auto"></div>
  <div class="hint">Click any edge — or a fuchsia provider segment — to pin it (white halo) and list
  its candidates. Each OSM edge takes its <b>most-covering</b> candidate (passing the quality + covering
  floors); the edge is then coloured by that match's <b>quality</b> = exp(−drift/__TAU__m)·cos(bearing)
  (high quality = high/green … low quality = poor/dark red).</div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const CENTER=__CENTER__, EDGES=__EDGES__, CAND=__CAND__, SUB=__SUB__, SEG=__SEG__, SEGCAND=__SEGCAND__;
const QC={high:"#11D68F",good:"#FFCF43",fair:"#F24E42",poor:"#A92727"};   // quality level → colour
document.getElementById("sub").textContent=SUB;

const map=L.map("map",{preferCanvas:true}).setView(CENTER,14);
const C="https://{s}.basemaps.cartocdn.com/",A="&copy; OpenStreetMap, &copy; CARTO";
const bases={
  "Dark":L.tileLayer(C+"dark_all/{z}/{x}/{y}{r}.png",{attribution:A,subdomains:"abcd",maxZoom:20}),
  "Light":L.tileLayer(C+"light_all/{z}/{x}/{y}{r}.png",{attribution:A,subdomains:"abcd",maxZoom:20}),
  "Voyager":L.tileLayer(C+"rastertiles/voyager/{z}/{x}/{y}{r}.png",{attribution:A,subdomains:"abcd",maxZoom:20}),
  "Satellite":L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",{attribution:"&copy; Esri",maxZoom:20}),
};
bases["Dark"].addTo(map); L.control.layers(bases,null,{position:"topright"}).addTo(map);

function pick(cands,qmin,cm){             // MAX COVERING among candidates passing quality + cover floors
  let best=null;
  for(const c of cands){ if(c[0]>=cm && c[1]>=qmin && (!best||c[0]>best[0])) best=c; }
  return best;                           // [cov,qual,cong,seg_id] or null
}
function qlevel(q){ return q>=0.85?"high":q>=0.70?"good":q>=0.50?"fair":"poor"; }  // quality→level (high q = high)
function tip(p,win,cands){
  const lines=cands.slice().sort((a,b)=>b[0]-a[0]).slice(0,6)
    .map(c=>`&nbsp;cov ${c[0]}% · q ${c[1]} → ${qlevel(c[1])}${c===win?" ✓":""}`).join("<br>");
  return `edge ${p.id} · ${p.hw}<br><b>${win?"q "+win[1]+" → "+qlevel(win[1]):"unmatched"}</b><br>${lines}`;
}

let sel=null, hlLayer=null;               // sel = {kind:"edge"|"seg", id}
function clickSel(kind,id){ sel=(sel&&sel.kind===kind&&sel.id===id)?null:{kind,id}; showHighlight(); showDetail(); }

const layer=L.geoJSON(EDGES,{
  style:()=>({color:"#475569",weight:1.5,opacity:.5}),
  filter:f=>CAND[f.properties.id]!==undefined,     // only edges with candidates
  onEachFeature:(f,l)=>{
    l.bindTooltip(()=>{ const c=CAND[f.properties.id]||[]; return tip(f.properties,pick(c,+q0.value,+cmin.value),c); },{sticky:true});
    l.on("click",()=>clickSel("edge",f.properties.id));
  }
}).addTo(map);

function showHighlight(){                 // white halo on the selected edge / segment (SVG, on top)
  if(hlLayer){ map.removeLayer(hlLayer); hlLayer=null; }
  if(!sel) return;
  const coll=sel.kind==="edge"?EDGES:SEG, key=sel.kind==="edge"?"id":"seg_id";
  const f=coll.features.find(x=>x.properties[key]===sel.id);
  if(f) hlLayer=L.geoJSON(f,{style:{color:"#ffffff",weight:7,opacity:.9,
    dashArray:sel.kind==="seg"?"5,4":null},renderer:L.svg()}).addTo(map);
}
function showDetail(){
  const d=document.getElementById("detail");
  if(!sel){ d.style.display="none"; return; }
  const qmin=+q0.value, cm=+cmin.value; d.style.display="block";
  if(sel.kind==="edge"){
    const cands=(CAND[sel.id]||[]).slice().sort((a,b)=>b[0]-a[0]);    // by COVERING (the selector)
    const win=pick(cands,qmin,cm);
    const rows=cands.map(c=>{
      const pass=c[0]>=cm && c[1]>=qmin, isw=(c===win);
      const col=isw?"#fff;font-weight:600":(pass?"#cbd5e1":"#6b7280");
      return `<div style="display:flex;gap:6px;color:${col}"><span style="width:54px">cov ${c[0]}%</span>`
        +`<span style="width:46px">q ${c[1]}</span><span>${qlevel(c[1])} · seg ${c[3]}${isw?' ✓':pass?'':' ✗'}</span></div>`;
    }).join("");
    d.innerHTML=`<div style="font-weight:600;color:#fff">OSM edge ${sel.id} · ${cands.length} candidate seg(s)</div>`
      +`<div style="color:#9ca3af;margin:2px 0 6px">winner = most-covering with cov ≥ ${cm}% & q ≥ ${qmin.toFixed(2)}`
      +` · quality <b style="color:#e5e7eb">${win?win[1]+" → "+qlevel(win[1]):"unmatched"}</b></div>${rows}`;
  } else {
    const items=(SEGCAND[sel.id]||[]).slice().sort((a,b)=>b[2]-a[2]);  // by quality
    const sf=SEG.features.find(x=>x.properties.seg_id===sel.id);
    const cong=sf?sf.properties.cong:"?";
    const rows=items.map(it=>{
      const [eid,cov,qual]=it, w=pick(CAND[eid]||[],qmin,cm), isw=(w&&w[3]===sel.id);
      return `<div style="display:flex;gap:6px;color:${isw?'#fff;font-weight:600':'#cbd5e1'}">`
        +`<span style="width:48px">q ${qual}</span><span style="width:54px">cov ${cov}%</span>`
        +`<span>edge ${eid}${isw?' ✓ wins':''}</span></div>`;
    }).join("");
    d.innerHTML=`<div style="font-weight:600;color:#fff">provider seg ${sel.id} · ${cong}</div>`
      +`<div style="color:#9ca3af;margin:2px 0 6px">candidate for ${items.length} OSM edge(s) · ✓ = this seg wins it</div>${rows}`;
  }
}

function render(){
  const q=+q0.value, cm=+cmin.value;
  document.getElementById("v-q0").textContent=q.toFixed(2);
  document.getElementById("v-cmin").textContent=cm;
  const ct={high:0,good:0,fair:0,poor:0,none:0};
  layer.eachLayer(l=>{
    const w=pick(CAND[l.feature.properties.id]||[],q,cm);     // winner = most covering
    if(w){ const lv=qlevel(w[1]); ct[lv]++; l.setStyle({color:QC[lv],weight:3,opacity:.95}); }  // colour by QUALITY
    else { ct.none++; l.setStyle({color:"#475569",weight:1.5,opacity:.5}); }
  });
  for(const k in ct) document.getElementById("c-"+k).textContent=ct[k].toLocaleString();
  showDetail();
}
q0.addEventListener("input",render); cmin.addEventListener("input",render);

const segLayer=L.geoJSON(SEG,{
  style:()=>({color:"__SEGCOLOR__",weight:2.5,opacity:.9,dashArray:"5,4"}),
  onEachFeature:(f,l)=>{
    l.bindTooltip(()=>`provider seg ${f.properties.seg_id} · ${f.properties.cong}`,{sticky:true});
    l.on("click",(e)=>{ L.DomEvent.stopPropagation(e); clickSel("seg",f.properties.seg_id); });
  }
});
document.getElementById("seg-toggle").addEventListener("change",e=>{
  if(e.target.checked){ segLayer.addTo(map); segLayer.bringToFront(); } else map.removeLayer(segLayer);
});

render();
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("area")
    ap.add_argument("source", choices=["mapbox", "tomtom"])
    ap.add_argument("--tau", type=float, default=DEFAULT_TAU,
                    help="drift scale in the alignment term (m); larger = more forgiving")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    print("wrote", build(args.area, args.source, out_html=args.out, tau=args.tau))


if __name__ == "__main__":
    main()
