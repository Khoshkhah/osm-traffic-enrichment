# Mapbox / QGIS Visualizer Asset: Selected Edge UI/UX Specification (Conflict-Free Profile)

This document provides the updated visual specifications for a **selected edge (road segment)** within a web-based map visualizer, engineered to prevent color collisions with cyan motorway layers.

---

## 1. The Conflict-Free Selection Profile: Neon Violet Glow

The **Neon Violet Glow** profile is specifically chosen because it sits entirely outside the color spectrum of your highway network. True violet/purple tones do not match any road tier (Cyan, Pink, Orange, Yellow, Green) and provide a stark, unnatural contrast against the natural greens and grays of satellite and terrain imagery.

### Visual Styling Architecture

To implement this style, a selected feature must be split dynamically into three composited layers, stacked from bottom to top:

1. **Top Layer:** Core Selection Line (Solid Neon Violet)
2. **Middle Layer:** Active Casing Guard (High Contrast Stroke)
3. **Bottom Layer:** Pulsing Radial Glow (Semi-Transparent Halo)

### Layer Attribute Configurations

| Layer Element | Purpose | Color (HEX) | Width Formula | Alpha / Opacity | Dash / Style |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1. Radial Glow** *(Bottom)* | Creates an emissive "light pipeline" effect around the edge. | `#D000FF` | `Base Width * 3.0` | `0.35` (Dynamic) | Solid |
| **2. Casing Guard** *(Middle)* | Isolates the selection from underlying map data colors. | `#220033` | `Base Width * 1.8` | `1.00` | Solid |
| **3. Core Line** *(Top)* | The main highlighted feature vector. | `#EE00FF` | `Base Width * 1.5` | `1.00` | Solid |

> *Note: `Base Width` refers to the original pixel width assigned to that specific `highway=*` classification at the current zoom level as defined in the master roadmap style.*

---

## 2. Dynamic Theme Adaptation Matrix

While the core fill remains constant, the underlying casing adapts to the platform's active base map to ensure the selected boundary lines do not wash out:

| Active Base Layer Theme | Core Fill HEX | Casing HEX | Visual Interaction Behavior |
| :--- | :--- | :--- | :--- |
| **Light Canvas Mode** | `#EE00FF` | `#220033` (Deep Plum) | The dark plum casing frames the neon violet line, preventing it from blending into white or light-grey backgrounds. |
| **Dark / Night Mode** | `#EE00FF` | `#000000` (Pure Black) | Pure black acts as an inline buffer, isolating the glow cleanly from dark grays, blues, or shadows. |
| **Satellite Imagery** | `#EE00FF` | `#000000` (Pure Black) | The heavy black mask punches through high-frequency noise like building roofs, trees, asphalt, and sand patches. |

---

## 3. Mandatory Engineering & UX Pipeline Rules

### Rule 1: Absolute Vertical Elevation (`z-index`)
When an edge state shifts to `selected`, its geometries must immediately be hoisted out of the default highway layer stack and injected into a dedicated `selected-features-overlay` runtime layer. This layer must sit at the absolute top of the map container hierarchy, positioned **above administrative boundaries, water polygons, secondary roads, and street name labels**.

### Rule 2: The Dimming Mask (The Lightbox Effect)
To draw immediate visual focus to the user's data selection, your engine should apply a global dimming modifier to the rest of the highway network layer when an active selection exists:

```json
// Example Mapbox GL JS expression logic for non-selected elements
"line-opacity": [
    "case",
    ["boolean", ["feature-state", "selected"], false], 1.0, 
    0.35 // Drops unselected roads to 35% opacity
]