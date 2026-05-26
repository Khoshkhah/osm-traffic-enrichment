"""
Google Maps traffic fetching via Playwright screenshot and bearing-based map matching.

Usage:
    from scripts.google_traffic import GoogleTraffic

    traffic = GoogleTraffic(api_key=GOOGLE_MAPS_API_KEY, zoom=16)
    edge_cong, total_pixels = traffic.map_match(db_path, boundary_path)
"""

from __future__ import annotations

import asyncio
import math
import threading
from collections import defaultdict
from pathlib import Path
from typing import Optional

import geopandas as gpd
import numpy as np
import pyproj
import requests
from shapely.ops import transform as shapely_transform

_SEVERITY = {"severe": 4, "heavy": 3, "moderate": 2, "low": 1}


# ── Color classification ──────────────────────────────────────────────────────

def _rgb_to_lab(r: int, g: int, b: int) -> tuple[float, float, float]:
    """Convert sRGB (0-255) to CIE LAB (D65 illuminant)."""
    def linearize(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = linearize(r), linearize(g), linearize(b)
    X = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.9505
    Y =  r * 0.2126 + g * 0.7152 + b * 0.0722
    Z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.0890
    def f(t): return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    return 116 * f(Y) - 16, 500 * (f(X) - f(Y)), 200 * (f(Y) - f(Z))


# Google Maps JS API TrafficLayer reference colors (googletraffic R package)
_TRAFFIC_REFS = [
    ("low",      _rgb_to_lab(17,  214, 143)),   # #11D68F teal-green
    ("moderate", _rgb_to_lab(255, 207,  67)),   # #FFCF43 yellow
    ("heavy",    _rgb_to_lab(242,  78,  66)),   # #F24E42 red
    ("severe",   _rgb_to_lab(169,  39,  39)),   # #A92727 dark red
]
_TRAFFIC_THRESHOLD = 25.0   # CIE76 units


def _build_traffic_raster(screenshot_img) -> np.ndarray:
    """
    Classify every pixel by CIE76 distance to traffic reference colors.
    Returns uint8 array: 0=none, 1=low, 2=moderate, 3=heavy, 4=severe.
    """
    arr    = np.array(screenshot_img).astype(np.float64)
    rgb, a = arr[:, :, :3], arr[:, :, 3]
    mask   = (a >= 200) & ~((rgb[:,:,0] > 230) & (rgb[:,:,1] > 230) & (rgb[:,:,2] > 230))

    c      = rgb / 255.0
    linear = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    Xr = (linear[:,:,0]*0.4124 + linear[:,:,1]*0.3576 + linear[:,:,2]*0.1805) / 0.9505
    Yr =  linear[:,:,0]*0.2126 + linear[:,:,1]*0.7152 + linear[:,:,2]*0.0722
    Zr = (linear[:,:,0]*0.0193 + linear[:,:,1]*0.1192 + linear[:,:,2]*0.9505) / 1.0890
    def f(t): return np.where(t > 0.008856, t ** (1/3), 7.787*t + 16/116)
    lab   = np.stack([116*f(Yr) - 16, 500*(f(Xr) - f(Yr)), 200*(f(Yr) - f(Zr))], axis=-1)
    refs  = np.array([ref for _, ref in _TRAFFIC_REFS], dtype=np.float64)
    diff  = lab[:, :, np.newaxis, :] - refs[np.newaxis, np.newaxis, :, :]
    dists = np.sqrt((diff ** 2).sum(axis=-1))

    raster  = np.zeros(rgb.shape[:2], dtype=np.uint8)
    traffic = mask & (dists.min(axis=-1) <= _TRAFFIC_THRESHOLD)
    raster[traffic] = dists.argmin(axis=-1)[traffic] + 1
    return raster


# ── Playwright rendering ──────────────────────────────────────────────────────

def _render_playwright_screenshot(boundary_path: Path, zoom: int, api_key: str,
                                   proj_fwd, proj_inv):
    """Render Google Maps traffic via Playwright. Returns (img, x_min_m, y_max_m, pixel_scale)."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "playwright not installed — run: pip install playwright && playwright install chromium"
        )
    from io import BytesIO
    from PIL import Image

    gdf      = gpd.read_file(boundary_path).to_crs("EPSG:4326")
    boundary = gdf.geometry.union_all()
    bw, bs, be, bn = boundary.bounds
    pad_x = (be - bw) * 0.03;  pad_y = (bn - bs) * 0.03
    bw -= pad_x;  be += pad_x;  bs -= pad_y;  bn += pad_y

    x_min_m, _ = proj_fwd.transform(bw, bs)
    x_max_m, y_max_m = proj_fwd.transform(be, bn)
    _, y_min_m = proj_fwd.transform(bw, bs)

    pixel_scale = 40075016.686 / (256 * (2 ** zoom))
    img_w = math.ceil((x_max_m - x_min_m) / pixel_scale)
    img_h = math.ceil((y_max_m - y_min_m) / pixel_scale)

    MAX_PIXELS = 160_000_000
    while img_w * img_h > MAX_PIXELS and zoom > 12:
        zoom        -= 1
        pixel_scale  = 40075016.686 / (256 * (2 ** zoom))
        img_w        = math.ceil((x_max_m - x_min_m) / pixel_scale)
        img_h        = math.ceil((y_max_m - y_min_m) / pixel_scale)

    cx_lng, cx_lat = proj_inv.transform(
        (x_min_m + x_max_m) / 2, (y_min_m + y_max_m) / 2
    )

    html = f"""<!DOCTYPE html>
<html><head>
  <style>html,body{{margin:0;padding:0;overflow:hidden;}}#map{{width:{img_w}px;height:{img_h}px;}}</style>
  <script src="https://maps.googleapis.com/maps/api/js?key={api_key}"></script>
</head><body><div id="map"></div>
<script>
  const map = new google.maps.Map(document.getElementById('map'), {{
    center: {{lat:{cx_lat}, lng:{cx_lng}}}, zoom: {zoom}, disableDefaultUI: true,
    styles: [
      {{elementType:'labels',   stylers:[{{visibility:'off'}}]}},
      {{elementType:'geometry', stylers:[{{visibility:'off'}}]}},
      {{featureType:'road', elementType:'geometry', stylers:[{{visibility:'on'}},{{color:'#ffffff'}}]}},
      {{featureType:'landscape', stylers:[{{color:'#ffffff'}},{{visibility:'on'}}]}}
    ]
  }});
  new google.maps.TrafficLayer().setMap(map);
  map.addListener('tilesloaded', () => {{ window._ready = true; }});
</script></body></html>"""

    async def _render(html, w, h):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page    = await browser.new_page(viewport={"width": w, "height": h})
            await page.set_content(html, wait_until="domcontentloaded")
            await page.wait_for_function("window._ready === true", timeout=30000)
            await page.wait_for_timeout(2000)
            data = await page.screenshot()
            await browser.close()
            return data

    _out = {}
    def _run(): _out["png"] = asyncio.run(_render(html, img_w, img_h))
    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=60)
    if "png" not in _out:
        raise RuntimeError("Playwright rendering timed out after 60 s")

    img = Image.open(BytesIO(_out["png"])).convert("RGBA")
    return img, x_min_m, y_max_m, pixel_scale


# ── Public class ──────────────────────────────────────────────────────────────

class GoogleTraffic:
    """
    Fetch Google Maps traffic via Playwright screenshot and match to OSM edges.

    Parameters
    ----------
    api_key : Google Maps JS API key
    zoom    : Google Maps zoom level (default 16, ~2.4 m/px)
    """

    MATCH_BUFFER_M    = 10
    NEIGHBOR_RADIUS_M = 25
    BEARING_THRESHOLD = 40
    MIN_NEIGHBORS     = 5

    def __init__(self, api_key: str, zoom: int = 16) -> None:
        self.api_key = api_key
        self.zoom    = zoom

    def render_screenshot(self, boundary_path: str | Path):
        """
        Render a Google Maps traffic screenshot for the boundary area.

        Returns
        -------
        (img, x_min_m, y_max_m, pixel_scale)
            img         : PIL Image (RGBA)
            x_min_m     : west edge in EPSG:3857 metres
            y_max_m     : north edge in EPSG:3857 metres
            pixel_scale : metres per pixel
        """
        proj_fwd = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
        proj_inv = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
        return _render_playwright_screenshot(
            Path(boundary_path), self.zoom, self.api_key, proj_fwd, proj_inv
        )

    def build_raster(self, screenshot_img) -> np.ndarray:
        """Classify pixels by traffic color. Returns uint8 array (0=none, 1-4=level)."""
        return _build_traffic_raster(screenshot_img)

    def map_match(self, db_path: str | Path,
                  boundary_path: str | Path) -> tuple[dict[int, str], int]:
        """
        Full pipeline: render screenshot → classify pixels → bearing-based KD-tree match.

        Parameters
        ----------
        db_path       : path to DuckDB file (reads driving.edges)
        boundary_path : path to boundary GeoJSON

        Returns
        -------
        (edge_congestion, total_traffic_pixels)
            edge_congestion    : dict mapping edge_id → congestion level
            total_traffic_pixels : int count of classified traffic pixels
        """
        from scipy.spatial import cKDTree
        import duckdb

        proj_fwd = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
        proj_inv = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

        # Load OSM edges
        con = duckdb.connect(str(db_path), read_only=True)
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
        df = con.execute(
            "SELECT edge_id, osm_id, highway, name, length_m, ST_AsText(geometry) wkt_geom "
            "FROM driving.edges"
        ).df()
        con.close()

        edges = gpd.GeoDataFrame(
            df, geometry=gpd.GeoSeries.from_wkt(df["wkt_geom"]), crs="EPSG:4326"
        ).reset_index(drop=True)
        edges = edges[edges.geometry.geom_type.isin(["LineString", "MultiLineString"])]

        # Render and classify screenshot
        screenshot_img, x_min_m, y_max_m, pixel_scale = _render_playwright_screenshot(
            Path(boundary_path), self.zoom, self.api_key, proj_fwd, proj_inv
        )
        boundary_raster = _build_traffic_raster(screenshot_img)
        total = int((boundary_raster > 0).sum())
        if total == 0:
            return {}, 0

        # Traffic pixels → EPSG:3857
        traffic_rows, traffic_cols = np.where(boundary_raster > 0)
        traffic_vals = boundary_raster[traffic_rows, traffic_cols]
        pixel_xs  = x_min_m + traffic_cols * pixel_scale
        pixel_ys  = y_max_m - traffic_rows * pixel_scale
        pixel_pts = np.column_stack([pixel_xs, pixel_ys])

        # Edge sample points with local bearing
        edge_sample_pts      = []
        edge_sample_eids     = []
        edge_sample_oids     = []
        edge_sample_bearings = []

        for _, row in edges.iterrows():
            geom_m = shapely_transform(proj_fwd.transform, row.geometry)
            n_pts  = max(4, min(int(geom_m.length / 2), 500))
            oid    = int(row["osm_id"]) if int(row["osm_id"]) > 0 else -int(row["edge_id"])
            pts_m  = [geom_m.interpolate(d, normalized=True) for d in np.linspace(0.05, 0.95, n_pts)]
            for j, pt in enumerate(pts_m):
                prev = pts_m[max(0, j - 1)]
                nxt  = pts_m[min(len(pts_m) - 1, j + 1)]
                dx, dy  = nxt.x - prev.x, nxt.y - prev.y
                bearing = np.degrees(np.arctan2(dx, dy)) % 180
                edge_sample_pts.append((pt.x, pt.y))
                edge_sample_eids.append(int(row["edge_id"]))
                edge_sample_oids.append(oid)
                edge_sample_bearings.append(bearing)

        edge_pts_arr      = np.array(edge_sample_pts)
        edge_eids_arr     = np.array(edge_sample_eids,     dtype=np.int64)
        edge_oids_arr     = np.array(edge_sample_oids,     dtype=np.int64)
        edge_bearings_arr = np.array(edge_sample_bearings, dtype=np.float64)
        edge_tree         = cKDTree(edge_pts_arr)
        traffic_tree      = cKDTree(pixel_pts)

        nearby_edges   = edge_tree.query_ball_point(pixel_pts,   r=self.MATCH_BUFFER_M)
        nearby_traffic = traffic_tree.query_ball_point(pixel_pts, r=self.NEIGHBOR_RADIUS_M)

        INT_TO_LEVEL    = {1: "low", 2: "moderate", 3: "heavy", 4: "severe"}
        edge_pixel_vals: dict = defaultdict(list)

        for i in range(len(pixel_pts)):
            nb_e = nearby_edges[i]
            if not nb_e:
                continue
            nb_tr = nearby_traffic[i]
            if len(nb_tr) < self.MIN_NEIGHBORS:
                continue
            neighbor_pts   = pixel_pts[nb_tr]
            cov            = np.cov(neighbor_pts.T)
            _, eigvecs     = np.linalg.eigh(cov)
            direction      = eigvecs[:, 1]
            stripe_bearing = np.degrees(np.arctan2(direction[0], direction[1])) % 180

            nb_arr = np.array(nb_e)
            eb     = edge_bearings_arr[nb_arr]
            diffs  = np.abs(stripe_bearing - eb) % 180
            diffs  = np.minimum(diffs, 180 - diffs)
            ok     = diffs <= self.BEARING_THRESHOLD
            if not ok.any():
                continue
            nb_f = nb_arr[ok]
            if len(np.unique(edge_oids_arr[nb_f])) > 1:
                continue
            dists_local = np.linalg.norm(edge_pts_arr[nb_f] - pixel_pts[i], axis=1)
            eid = int(edge_eids_arr[nb_f[np.argmin(dists_local)]])
            edge_pixel_vals[eid].append(int(traffic_vals[i]))

        edge_cong = {eid: INT_TO_LEVEL[max(vals)] for eid, vals in edge_pixel_vals.items()}
        return edge_cong, total
