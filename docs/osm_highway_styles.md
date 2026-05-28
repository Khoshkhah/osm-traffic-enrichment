# OpenStreetMap Standard Highway Styles (OSM Carto)

This document outlines the standard color and style specifications for highway features as used in the default OpenStreetMap stylesheet (OSM Carto).

## Core Classification Table

| highway=* Tag | Standard HEX | Line Style & Casing Treatment |
| :--- | :--- | :--- |
| **motorway** | #e892a2 | Thickest line. Dark red casing (#dc2a48). Only core network. |
| **motorway_link** | #e892a2 | Narrower version of motorway. Motorway ramps/interchanges. |
| **trunk** | #f9b29c | Thick line. Dark orange/red casing (#c84e2f). High-importance roads. |
| **trunk_link** | #f9b29c | Narrower version of trunk. Trunk ramps and loops. |
| **primary** | #fcd6a4 | Medium-thick line. Dark yellow-orange casing (#a06b00). |
| **primary_link** | #fcd6a4 | Narrower version of primary. Primary road link ramps. |
| **secondary** | #f7fabf | Medium line. Pale yellow fill, light brown-yellow casing (#707d00). |
| **secondary_link** | #f7fabf | Narrower version of secondary. Secondary link ramps. |
| **tertiary** | #ffffff | Thin white line. Light grey casing (#bcbcbc). Renders at Z10/Z11. |
| **tertiary_link** | #ffffff | Narrower version of tertiary. Tertiary link ramps. |
| **unclassified** | #ffffff | White fill, grey casing (#bcbcbc). Renders early (Z12) for rural connectivity. |
| **residential** | #ffffff | White fill, grey casing (#bcbcbc). Renders later (Z13+) for neighborhood streets. |
| **living_street** | #ededed | Light grey fill. Darker grey casing (#ccccccc). Pedestrian-first streets. |
| **service** | #ffffff | Very thin white line, minimal casing. For alleys and parking lots. |
| **track** | #9e7b54 | Brown dashed line. For agricultural, forest, or logging roads. |
| **pedestrian** | #ededed | Light grey fill with a subtle dashed edge. For pedestrian plazas/zones. |
| **footway** | #9e5b5b | Thin, distinct salmon/red dashed line (dasharray="4,4"). |
| **cycleway** | #5c7cb6 | Thin blue dashed line. Exclusively for bicycles. |
| **path** | #9e5b5b | Thin salmon/red dashed line (dasharray="2,5"). Multi-use generic paths. |

## Structural Modification Rules

When rendering these layers, apply these global modifiers over the standard HEX values above:

* **tunnel=yes**: Drops opacity to roughly 50%, reduces line width slightly, and changes the solid casing into a dashed casing.
* **bridge=yes**: Forces a solid, thick dark charcoal outline (#000000 or #444444) underneath the road fill layer to create visual bridge "walls".
