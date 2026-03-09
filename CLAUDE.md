# roomie-tools-planner

Quantized floor plan + product catalog pipeline for LLM-generated furniture layouts.

## Coordinate Systems

### Source JSON (floor plans)
- Standard 2D: X increases rightward, Y increases **upward**
- Units: meters
- Origin: varies per plan (bounding box computed at quantization time)

### CSS Output (quantized plans)
- CSS convention: X increases rightward, Y (`top`) increases **downward**
- Units: grid pixels (256x256 canvas)
- `quantize_plan.py` flips Y during conversion: `to_gy(m) = (outer_max_y - m) * scale + offset_y`
- Plans are centered within the grid (equal padding on shorter axis)
- The inverse transform in rasterization must match: `my = outer_max_y - (y + 0.5 - offset_y) / scale`
- Door/window inward direction is negated for horizontal walls to account for the Y flip

### Placement JSON (LLM output)
- Uses CSS coordinate space (Y-down, grid pixels)
- `(x, y)` = center point of placed item
- Rotation: 0, 90, 180, 270 degrees clockwise; 90/270 swap width and height

## Pipeline

The pipeline transforms raw floor plan geometry + vendor product data into furnished room layouts via three scripts and a viewer. Each stage produces intermediate files consumed by the next.

```
floor_plan.sample/*.json ──┐
                           ├─► quantize_plan.py ──► quantize_room.output/*_plan.css
products/**/*.catalog.json ─┘                   └──► quantize_room.output/*_catalog.json
                                                          │
                                                          ▼
                                                   generate_placement.py
                                                     Stage 1: curate (whole plan)
                                                     Stage 2: arrange (per room)
                                                          │
                                                          ▼
                                               *_placement.json  (*_report.json)
                                                          │
                                                          ▼
                                                    viewer.html
```

### 1. build_catalog.py (product profiling)

Processes vendor product directories under `products/`. For each product, calls `claude --print` with product images to generate a `.catalog.json` profile containing tier (anchor/accent/fill), placement (floor/wall/surface), tags, and category. Run independently before the rest of the pipeline.

### 2. quantize_plan.py (floor plan quantization)

Converts floor plan JSON (meters, Y-up) to a 256x256 pixel grid in CSS format (Y-down).

**Inputs:** `floor_plan.sample/*.json` (rooms, walls, doors, windows as polygon/line geometry)
**Outputs per plan:**
- `<stem>_plan.css` — Room shapes, obstacles (walls), doors (clearance zones), windows as CSS rules with `left`, `top`, `width`, `height`. Diagonal walls use `clip-path: polygon(...)`.
- `<stem>_catalog.json` — Filtered product catalog with `footprints` array: `#i<item_no> { width: Wpx; height: Hpx; }` quantized to the plan's grid scale.

**Key behaviors:**
- Auto-scales geometry to maximize grid usage; centers the plan with equal padding.
- Y-flip: `to_gy(m) = (outer_max_y - m) * scale + offset_y`
- Flat-object detection: if `depth < 0.0254m`, swaps depth/height for floor footprint (rugs, mats).
- Rasterizes room polygons to determine pixel-level room/obstacle membership.
- Batch mode: pass a directory of JSON files to process all at once.

### 3. generate_placement.py (LLM furniture placement)

Two-stage LLM pipeline using `claude --print` to generate placement coordinates.

**Inputs:** `*_plan.css` + `*_catalog.json` from `quantize_room.output/`
**Output:** `<stem>_placement.json` conforming to `placement.schema.json`

#### Stage 1 — Curate (`prompts/curate.md`)
- **Scope:** Whole plan (all rooms + full catalog)
- **Role:** Interior designer / shopping curator
- **Sees:** Plan CSS (room sizes, names, layout) + catalog products/profiles (names, tiers, tags, colors). Does NOT see footprint dimensions.
- **Produces:** JSON array of roles — each has a functional label (e.g. `dining-table`), target room, quantity, and 2-3 ranked candidate `item_no`s.
- **Goal:** Cast a wide net for candidates; the arrange stage narrows down based on spatial fit. Ensures stylistic coherence across rooms. Caps at 4-8 roles per small room, 8-12 for large.

#### Stage 2 — Arrange (`prompts/arrange.md`)
- **Scope:** One room at a time (isolated LLM call per room)
- **Role:** Spatial reasoning engine
- **Sees:** Room-specific CSS geometry (room components + adjacent obstacles/doors/windows extracted via bounding-box intersection) + candidates with footprints (width/height in px).
- **Produces:** JSON array of `{ item_no, x, y, r }` placements — one entry per physical instance.
- **Goal:** Pick one candidate per role based on spatial fit, place at exact grid coordinates. Respects obstacle/door clearances, maintains walkable pathways, groups furniture functionally.

**Diagnostics:**
- `--verbose` / `-v`: Prints raw LLM responses (first 1000 chars) to console.
- `--report` / `-r`: Writes `<stem>_report.json` with full prompt sizes, timings, raw responses, parsed results, and errors for each LLM call.
- `--force`: Regenerates placement even if `*_placement.json` already exists.

### 4. viewer.html (visual verification)

Browser-based viewer that loads CSS plan files as rendered floor plans and overlays placement data.

- Auto-loads all `*_placement.json` from `quantize_room.output/` on page load.
- Color-coded placement overlays by tier: purple = anchor, green = accent, yellow = fill.
- Item labels show `item_no` and rotation.
- "Choose folder" button with dropdown file list for manual folder selection.

## Key Directories

- `floor_plan.sample/` — Source floor plan JSON files (meters, Y-up coordinate system)
- `products/` — Vendor product directories: metadata JSON, images, GLB models, `.catalog.json` profiles
- `quantize_room.output/` — All pipeline output: `*_plan.css`, `*_catalog.json`, `*_placement.json`, `*_report.json`
- `prompts/` — LLM prompt templates: `profile.md` (catalog profiling), `curate.md` (stage 1), `arrange.md` (stage 2)

## Product Footprints

- Floor footprint = `width x depth` from vendor dimensions
- Exception: flat objects (depth < 0.0254m / 1 inch) like rugs — swap depth and height so footprint = `width x height`
- Elevation = the remaining dimension (object height above floor)
