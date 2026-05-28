# Mapbox / QGIS Custom Style Asset: Multi-Layer OSM Highway Network Integration

This repository contains the definitive visual specifications and cartographic assets for rendering OpenStreetMap (OSM) highway layers over high-contrast base maps. 

Standard map stylesheets (like standard OSM Carto) fail when overlaid on varied base layers because their muted, desaturated warm tones bleed directly into the background. This styling asset resolves that issue by employing a **Luminance-Agnostic, High-Saturation Palette** coupled with a **Dynamic Casing Swap Engine** to ensure perfect vector road legibility across **Light, Dark, and Satellite** map canvases.

---

## Technical Specifications

### Core Network Layer Layout
For a polished asset presentation, your styling framework (Mapbox GL JS JSON, MapLibre style sheets, or QGIS project rules) should group features into separate **Casing** and **Fill** layer groups. 

| Layer Rendering Order | `highway=*` Tag | Core Fill HEX | Light Map Casing | Dark/Satellite Casing | Core Width (Z14+) | Casing Width (Z14+) |
| :---: | :--- | :--- | :--- | :--- | :---: | :---: |
| **1 (Lowest)** | `service` | `#A6A6A6` | None | `#000000` | 1.0 px | 2.0 px |
| **2** | `residential` | `#FFFFFF` | `#999999` | `#000000` | 2.0 px | 4.0 px |
| **3** | `unclassified` | `#FFFFFF` | `#999999` | `#000000` | 2.0 px | 4.0 px |
| **4** | `tertiary` | `#00E676` | `#007A3E` | `#000000` | 2.5 px | 4.5 px |
| **5** | `secondary` | `#FFEA00` | `#A39600` | `#000000` | 3.5 px | 6.0 px |
| **6** | `primary` | `#FF9100` | `#A86000` | `#000000` | 4.5 px | 7.5 px |
| **7** | `trunk` | `#FF007F` | `#9E004F` | `#000000` | 5.5 px | 9.0 px |
| **8 (Highest)**| `motorway` | `#00E5FF` | `#007785` | `#000000` | 6.0 px | 10.0 px |

---

## Architectural Rules for Deployment

### 1. The Geometry Sandwich Rule (Mandatory Z-Ordering)
To prevent intersecting road borders from slicing through higher-importance highways, you **must split your rendering tree**. Do not draw casing and fill simultaneously on an individual feature level.
* **Step 1:** Render all road classifications (`service` to `motorway`) using their respective **Casing Width** and **Casing HEX**.
* **Step 2:** Overlay all road classifications directly on top using their respective **Core Width** and **Core Fill HEX**.

### 2. Microscopic Overrides
* **`tunnel=yes`**: Force entire layer opacity to `45%` and transform solid line fills to a dashed pattern (`dasharray="4, 4"`).
* **`bridge=yes`**: Lock casing color to `#000000` (Pure Black) regardless of the chosen base map theme, and widen the casing by an extra `1.5px` to simulate structural shadow depth.
* **`footway` / `path`**: Assign a dark coral `#C0392B` using a dense dash pattern (`dasharray="1, 3"`) at a narrow width (`1.5px`) to cleanly slice through satellite foliage and dark textures.
* **`cycleway`**: Assign a crisp blue `#2980B9` with a balanced dash pattern (`dasharray="3, 3"`) at a width of (`1.5px`).

### 3. Base Layer Optimization
When implementing the **Satellite base map**, apply the following raster filters to your map container if supported by your runtime engine (e.g., CSS filters or WebGL post-processing adjustments):
```css
.map-satellite-tile-layer {
    filter: saturate(0.80) brightness(0.90);
}
```
*Reducing saturation by 20% and brightness by 10% drops the background noise of concrete roofs and vegetation, allowing your vector network to command primary visual hierarchy.*
