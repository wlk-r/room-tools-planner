"""
Quantizes a floor plan and product catalog into a single output folder.

Plan  → CSS file  (spatial layout for LLM visual reasoning)
Catalog → JSON file (product info + CSS footprint snippets)

Usage:
    python quantize_plan.py <floor_plan.json> [--products <folder>]

Output:
    <plan_stem>/
        <stem>_plan.css      - quantized floor plan as CSS rules
        <stem>_catalog.json  - product info + footprint CSS snippets
"""

import json
import math
import sys
import argparse
from pathlib import Path


DOOR_CLEARANCE_M = 0.8
WINDOW_ZONE_DEPTH_M = 0.3
GRID_SIZE = 256
DEFAULT_PRODUCTS_DIR = "products"
DEFAULT_CATALOG_DIR = "catalog"


# ---------- Floor plan quantization ----------

def has_diagonal_edges(polygon):
    """True if any edge is not axis-aligned (i.e. neither horizontal nor vertical)."""
    for i in range(len(polygon)):
        p1 = polygon[i]
        p2 = polygon[(i + 1) % len(polygon)]
        if abs(p1[0] - p2[0]) > 0.001 and abs(p1[1] - p2[1]) > 0.001:
            return True
    return False


def decompose_diagonal_room(polygon, to_gx, to_gy, room_grid, room_idx,
                             canvas_w, canvas_h):
    """Decompose a room with diagonal edges into rect + triangle components.

    Returns list of component dicts with left/top/width/height and optional
    clip_path (only on triangle components).
    """
    gx = [to_gx(p[0]) for p in polygon]
    gy = [to_gy(p[1]) for p in polygon]
    n = len(gx)
    cx, cy = sum(gx) / n, sum(gy) / n

    # Classify edges and compute inside corners for diagonal edges
    is_diag = []
    inside_corner = []
    for i in range(n):
        ax, ay = gx[i], gy[i]
        bx, by = gx[(i + 1) % n], gy[(i + 1) % n]
        diag = abs(ax - bx) >= 0.5 and abs(ay - by) >= 0.5
        is_diag.append(diag)
        if diag:
            c1, c2 = (bx, ay), (ax, by)
            d1 = (c1[0] - cx) ** 2 + (c1[1] - cy) ** 2
            d2 = (c2[0] - cx) ** 2 + (c2[1] - cy) ** 2
            inside_corner.append(c1 if d1 < d2 else c2)
        else:
            inside_corner.append(None)

    # Group consecutive diagonal edge indices
    groups = []
    for i in range(n):
        if not is_diag[i]:
            continue
        if groups and i == groups[-1][-1] + 1:
            groups[-1].append(i)
        else:
            groups.append([i])
    if len(groups) >= 2 and groups[-1][-1] == n - 1 and groups[0][0] == 0:
        groups[0] = groups[-1] + groups[0]
        groups.pop()

    def _make_tri_component(pts):
        tl = int(math.floor(min(p[0] for p in pts)))
        tt = int(math.floor(min(p[1] for p in pts)))
        tr = int(math.ceil(max(p[0] for p in pts)))
        tb = int(math.ceil(max(p[1] for p in pts)))
        w, h = tr - tl, tb - tt
        if w < 1 or h < 1:
            return None
        pcts = []
        for px, py in pts:
            pcts.append(
                f"{round((px - tl) / w * 100)}% {round((py - tt) / h * 100)}%"
            )
        return {
            "left": tl, "top": tt, "width": w, "height": h,
            "clip_path": f"polygon({', '.join(pcts)})",
        }

    # Build triangle components (merge 2 consecutive diagonals into one tri)
    tri_components = []
    tri_polys = []          # grid-coord vertices for raster subtraction
    for group in groups:
        if len(group) == 1:
            i = group[0]
            pts = [(gx[i], gy[i]), inside_corner[i],
                   (gx[(i + 1) % n], gy[(i + 1) % n])]
        elif len(group) == 2:
            i, j = group
            pts = [(gx[i], gy[i]),
                   (gx[(i + 1) % n], gy[(i + 1) % n]),
                   (gx[(j + 1) % n], gy[(j + 1) % n])]
        else:
            for i in group:
                pts = [(gx[i], gy[i]), inside_corner[i],
                       (gx[(i + 1) % n], gy[(i + 1) % n])]
                comp = _make_tri_component(pts)
                if comp:
                    tri_components.append(comp)
                    tri_polys.append(pts)
            continue
        comp = _make_tri_component(pts)
        if comp:
            tri_components.append(comp)
            tri_polys.append(pts)

    # Subtract triangle areas from rasterised room mask, then extract rects
    mask = [[room_grid[y][x] == room_idx for x in range(canvas_w)]
            for y in range(canvas_h)]

    for tri_pts in tri_polys:
        tl = max(0, int(math.floor(min(p[0] for p in tri_pts))))
        tt = max(0, int(math.floor(min(p[1] for p in tri_pts))))
        tr = min(canvas_w, int(math.ceil(max(p[0] for p in tri_pts))))
        tb = min(canvas_h, int(math.ceil(max(p[1] for p in tri_pts))))
        for y in range(tt, tb):
            for x in range(tl, tr):
                if point_in_polygon(x + 0.5, y + 0.5, tri_pts):
                    mask[y][x] = False

    rects = extract_rectangles(mask, canvas_w, canvas_h)
    rect_components = [
        {"left": x, "top": y, "width": w, "height": h}
        for x, y, w, h in rects
    ]

    return rect_components + tri_components


def compute_diagonal_obstacles(polygon, to_gx, to_gy, canvas_h):
    """For each diagonal edge of a room polygon, compute the obstacle triangle
    between the edge and the bounding-box corner on the outside of the room."""
    gx = [to_gx(p[0]) for p in polygon]
    gy = [to_gy(p[1]) for p in polygon]
    n = len(gx)

    # Room centroid for determining "outside" direction
    cx = sum(gx) / n
    cy = sum(gy) / n

    obstacles = []
    for i in range(n):
        ax, ay = gx[i], gy[i]
        bx, by = gx[(i + 1) % n], gy[(i + 1) % n]

        # Skip axis-aligned edges
        if abs(ax - bx) < 0.5 or abs(ay - by) < 0.5:
            continue

        # Two candidate corners: (bx, ay) or (ax, by)
        corner1 = (bx, ay)
        corner2 = (ax, by)
        d1 = (corner1[0] - cx) ** 2 + (corner1[1] - cy) ** 2
        d2 = (corner2[0] - cx) ** 2 + (corner2[1] - cy) ** 2
        corner = corner1 if d1 > d2 else corner2

        tri = [(ax, ay), corner, (bx, by)]

        tri_left = int(math.floor(min(p[0] for p in tri)))
        tri_top = int(math.floor(min(p[1] for p in tri)))
        tri_right = int(math.ceil(max(p[0] for p in tri)))
        tri_bottom = int(math.ceil(max(p[1] for p in tri)))
        w = tri_right - tri_left
        h = tri_bottom - tri_top
        if w < 1 or h < 1:
            continue

        points = []
        for px, py in tri:
            pct_x = round((px - tri_left) / w * 100)
            pct_y = round((py - tri_top) / h * 100)
            points.append(f"{pct_x}% {pct_y}%")

        obstacles.append({
            "left": tri_left, "top": tri_top,
            "width": w, "height": h,
            "clip_path": f"polygon({', '.join(points)})",
        })

    # Fill rectangular gap between triangles and canvas boundary.
    # The triangles only cover the diagonal edge area; a rectangular region
    # may extend beyond them toward the canvas edge.
    if obstacles:
        min_l = min(o["left"] for o in obstacles)
        max_r = max(o["left"] + o["width"] for o in obstacles)
        max_b = max(o["top"] + o["height"] for o in obstacles)
        if max_b < canvas_h:
            obstacles.append({
                "left": min_l, "top": max_b,
                "width": max_r - min_l, "height": canvas_h - max_b,
            })

    return obstacles


def point_in_polygon(px, py, polygon):
    """Ray casting point-in-polygon test."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def get_room_edges(room):
    poly = room["interior_polygon"]
    return [(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly))]


def find_opening_walls(opening, rooms, wall_thickness):
    """Find all rooms whose edges are near this opening.
    Uses wall_thickness as tolerance to catch openings centered in wall gaps.
    Returns list of (room, orientation, inward_direction) tuples.
    Single entry = exterior door/window, two entries = interior door."""
    cx, cy = opening["center"]
    tol = wall_thickness + 0.01
    hits = []

    for room in rooms:
        for p1, p2 in get_room_edges(room):
            if abs(p1[1] - p2[1]) < 0.01:
                wall_y = p1[1]
                x_lo, x_hi = sorted([p1[0], p2[0]])
                if abs(cy - wall_y) < tol and x_lo - tol <= cx <= x_hi + tol:
                    center_y = sum(p[1] for p in room["interior_polygon"]) / len(room["interior_polygon"])
                    hits.append((room, "horizontal", (1 if center_y > wall_y else -1)))
                    break

            if abs(p1[0] - p2[0]) < 0.01:
                wall_x = p1[0]
                y_lo, y_hi = sorted([p1[1], p2[1]])
                if abs(cx - wall_x) < tol and y_lo - tol <= cy <= y_hi + tol:
                    center_x = sum(p[0] for p in room["interior_polygon"]) / len(room["interior_polygon"])
                    hits.append((room, "vertical", (1 if center_x > wall_x else -1)))
                    break

    return hits


def extract_rectangles(grid, w, h):
    visited = [[False] * w for _ in range(h)]
    rects = []

    for y in range(h):
        for x in range(w):
            if grid[y][x] and not visited[y][x]:
                x_end = x
                while x_end < w and grid[y][x_end] and not visited[y][x_end]:
                    x_end += 1

                y_end = y + 1
                while y_end < h:
                    if all(grid[y_end][xi] and not visited[y_end][xi] for xi in range(x, x_end)):
                        y_end += 1
                    else:
                        break

                for yi in range(y, y_end):
                    for xi in range(x, x_end):
                        visited[yi][xi] = True

                rects.append((x, y, x_end - x, y_end - y))

    return rects


def clamp_box(left, top, width, height, canvas_w, canvas_h):
    left = max(0, left)
    top = max(0, top)
    width = min(width, canvas_w - left)
    height = min(height, canvas_h - top)
    return left, top, width, height


def quantize_floor_plan(plan_path, grid_size=GRID_SIZE):
    with open(plan_path) as f:
        plan = json.load(f)

    wall_t = plan["defaults"]["exterior_wall_thickness"]
    ceiling_h = plan["defaults"]["ceiling_height"]

    all_x = [p[0] for r in plan["rooms"] for p in r["interior_polygon"]]
    all_y = [p[1] for r in plan["rooms"] for p in r["interior_polygon"]]

    outer_min_x = min(all_x) - wall_t
    outer_min_y = min(all_y) - wall_t
    outer_max_x = max(all_x) + wall_t
    outer_max_y = max(all_y) + wall_t

    total_w = outer_max_x - outer_min_x
    total_h = outer_max_y - outer_min_y

    scale = grid_size / max(total_w, total_h)

    def to_gx(m):
        return (m - outer_min_x) * scale

    def to_gy(m):
        return (m - outer_min_y) * scale

    canvas_w = grid_size
    canvas_h = grid_size

    # Rasterize: track which room each cell belongs to (None = obstacle)
    # Uses point-in-polygon test at cell centers for accurate geometry
    room_grid = [[None] * canvas_w for _ in range(canvas_h)]

    for room_idx, room in enumerate(plan["rooms"]):
        poly = room["interior_polygon"]
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        gx_min = max(0, int(math.floor(to_gx(min(xs)))))
        gx_max = min(canvas_w, int(math.ceil(to_gx(max(xs)))))
        gy_min = max(0, int(math.floor(to_gy(min(ys)))))
        gy_max = min(canvas_h, int(math.ceil(to_gy(max(ys)))))

        for y in range(gy_min, gy_max):
            my = (y + 0.5) / scale + outer_min_y
            for x in range(gx_min, gx_max):
                mx = (x + 0.5) / scale + outer_min_x
                if point_in_polygon(mx, my, poly):
                    room_grid[y][x] = room_idx

    # Detect rooms with diagonal edges
    room_is_diagonal = [has_diagonal_edges(r["interior_polygon"]) for r in plan["rooms"]]
    any_diagonal = any(room_is_diagonal)

    # Extract obstacle rectangles (cells not belonging to any room)
    obstacle_grid = [[cell is None for cell in row] for row in room_grid]
    structure_rects = extract_rectangles(obstacle_grid, canvas_w, canvas_h)

    obstacles = []
    for left, top, w, h in structure_rects:
        obstacles.append({"left": left, "top": top, "width": w, "height": h})

    # Replace staircase strips with clip-path obstacle triangles computed
    # from the room geometry. Each diagonal edge produces one triangle.
    if any_diagonal:
        half_w, half_h = canvas_w // 2, canvas_h // 2
        kept = []
        for obs in obstacles:
            w, h = obs["width"], obs["height"]
            is_full_border = (
                (obs["top"] == 0 and w > half_w)
                or (obs["left"] == 0 and h > half_h)
                or (obs["left"] + w >= canvas_w and h > half_h)
                or (obs["top"] + h >= canvas_h and w > half_w)
            )
            if min(w, h) > 2 or is_full_border:
                kept.append(obs)

        # Add precise triangular obstacles for each diagonal edge
        for room_idx, r in enumerate(plan["rooms"]):
            if room_is_diagonal[room_idx]:
                kept.extend(
                    compute_diagonal_obstacles(r["interior_polygon"], to_gx, to_gy, canvas_h)
                )

        obstacles = kept

    for i, obs in enumerate(obstacles):
        obs["id"] = f"structure_{i}"

    wall_features = []

    for opening in plan["openings"]:
        cx, cy = opening["center"]
        ow = opening["width"]
        elev = opening.get("elevation", 0)

        hits = find_opening_walls(opening, plan["rooms"], wall_t)
        if not hits:
            continue

        gcx = to_gx(cx)
        gcy = to_gy(cy)
        half_w_g = math.ceil((ow / 2) * scale)

        if elev == 0:
            # Door — generate clearance for each room side
            clearance_g = math.ceil(DOOR_CLEARANCE_M * scale)

            for side_idx, (room, orientation, inward) in enumerate(hits):
                suffix = f"_{room['id']}" if len(hits) > 1 else ""

                if orientation == "horizontal":
                    left = int(gcx - half_w_g)
                    top = int(gcy) if inward > 0 else int(gcy - clearance_g)
                    w, h = half_w_g * 2, clearance_g
                else:
                    top = int(gcy - half_w_g)
                    left = int(gcx) if inward > 0 else int(gcx - clearance_g)
                    w, h = clearance_g, half_w_g * 2

                left, top, w, h = clamp_box(left, top, w, h, canvas_w, canvas_h)
                obstacles.append({
                    "id": f"door_{opening['id']}_clearance{suffix}",
                    "left": left, "top": top, "width": w, "height": h,
                })

        else:
            # Window — use first hit (windows are on one wall)
            room, orientation, inward = hits[0]
            zone_g = math.ceil(WINDOW_ZONE_DEPTH_M * scale)

            if orientation == "horizontal":
                left = int(gcx - half_w_g)
                top = int(gcy) if inward > 0 else int(gcy - zone_g)
                w, h = half_w_g * 2, zone_g
            else:
                top = int(gcy - half_w_g)
                left = int(gcx) if inward > 0 else int(gcx - zone_g)
                w, h = zone_g, half_w_g * 2

            left, top, w, h = clamp_box(left, top, w, h, canvas_w, canvas_h)
            wall_features.append({
                "id": f"window_{opening['id']}",
                "left": left, "top": top, "width": w, "height": h,
                "sill_elevation": math.ceil(elev * scale),
            })

    # Decompose each room into rectangular components or clip-path
    rooms_out = []
    for room_idx, r in enumerate(plan["rooms"]):
        if room_is_diagonal[room_idx]:
            # Diagonal edges: decompose into rectangles + triangles
            components = decompose_diagonal_room(
                r["interior_polygon"], to_gx, to_gy,
                room_grid, room_idx, canvas_w, canvas_h,
            )
            room_entry = {
                "id": r["id"],
                "name": r.get("name", r["id"]),
                "components": components,
            }
        else:
            # Axis-aligned: rectangular decomposition (all components are valid)
            room_mask = [[room_grid[y][x] == room_idx for x in range(canvas_w)] for y in range(canvas_h)]
            components = extract_rectangles(room_mask, canvas_w, canvas_h)

            room_entry = {
                "id": r["id"],
                "name": r.get("name", r["id"]),
                "components": [
                    {"left": left, "top": top, "width": w, "height": h}
                    for left, top, w, h in components
                ],
            }
        rooms_out.append(room_entry)

    return {
        "canvas": {"width": canvas_w, "height": canvas_h},
        "scale": {
            "px_per_m": round(scale, 4),
            "m_per_px": round(1.0 / scale, 6),
        },
        "ceiling_elevation": math.ceil(ceiling_h * scale),
        "rooms": rooms_out,
        "obstacles": obstacles,
        "wall_features": wall_features,
    }, scale


# ---------- Product quantization ----------

def merge_catalog_templates(catalog_dir):
    """Merge all per-product catalog templates into combined products + profiles."""
    products = []
    profiles = []
    for template_path in sorted(Path(catalog_dir).glob("*/*_catalog.json")):
        with open(template_path) as f:
            t = json.load(f)
        products.extend(t["products"])
        profiles.extend(t["profiles"])
    return products, profiles


def compute_footprint(item_no, name, dimensions, scale):
    """Compute a CSS footprint snippet from product dimensions at the given scale."""
    w = math.ceil(dimensions["width"] * scale)
    h = math.ceil(dimensions["depth"] * scale)
    elev = math.ceil(dimensions["height"] * scale)
    return (
        f"#i{item_no}"
        f" {{ width: {w}; height: {h}; --elevation: {elev};"
        f" /* {name} */ }}"
    )


def build_footprints(products, products_dir, scale):
    """Look up vendor metadata for each product and compute footprints."""
    # Index vendor metadata by item_no
    vendor = {}
    for path in sorted(Path(products_dir).glob("*.json")):
        with open(path) as f:
            data = json.load(f)
        if "dimensions" in data and "item_no" in data:
            vendor[data["item_no"]] = data

    footprints = []
    for p in products:
        item_no = p["item_no"]
        if item_no not in vendor:
            print(f"  WARNING: no vendor metadata for {item_no}, skipping footprint")
            continue
        v = vendor[item_no]
        footprints.append(compute_footprint(item_no, v["name"], v["dimensions"], scale))
    return footprints


# ---------- CSS formatting ----------

def format_plan_css(plan_result):
    """Format quantized plan as a CSS string."""
    canvas = plan_result["canvas"]
    scale = plan_result["scale"]
    ceiling = plan_result["ceiling_elevation"]

    lines = [
        f"/* {canvas['width']}x{canvas['height']} grid"
        f" | 1px = {scale['m_per_px']}m"
        f" | ceiling: {ceiling}px */",
        "",
    ]

    # Rooms
    for room in plan_result["rooms"]:
        comps = room["components"]
        for i, c in enumerate(comps):
            rid = room["id"] if len(comps) == 1 else f"{room['id']}_{i}"
            label = room["name"]
            if len(comps) > 1:
                label += f" ({i + 1}/{len(comps)})"
            sel = f"#{rid}.room"
            clip = f" clip-path: {c['clip_path']};" if "clip_path" in c else ""
            lines.append(
                f"{sel:<28s}"
                f"{{ left: {c['left']:>3}; top: {c['top']:>3};"
                f" width: {c['width']:>3}; height: {c['height']:>3};{clip}"
                f" /* {label} */ }}"
            )

    lines.append("")

    # Obstacles (structures + door clearances)
    for obs in plan_result["obstacles"]:
        cls = "door" if obs["id"].startswith("door_") else "obstacle"
        sel = f"#{obs['id']}.{cls}"
        clip = f" clip-path: {obs['clip_path']};" if "clip_path" in obs else ""
        lines.append(
            f"{sel:<28s}"
            f"{{ left: {obs['left']:>3}; top: {obs['top']:>3};"
            f" width: {obs['width']:>3}; height: {obs['height']:>3};{clip} }}"
        )

    lines.append("")

    # Wall features (windows)
    for wf in plan_result["wall_features"]:
        sel = f"#{wf['id']}.window"
        sill = wf.get("sill_elevation", "")
        sill_str = f" --sill: {sill};" if sill != "" else ""
        lines.append(
            f"{sel:<28s}"
            f"{{ left: {wf['left']:>3}; top: {wf['top']:>3};"
            f" width: {wf['width']:>3}; height: {wf['height']:>3};{sill_str} }}"
        )

    lines.append("")
    return "\n".join(lines)


# ---------- Main ----------

def main():
    parser = argparse.ArgumentParser(
        description="Quantize a floor plan and product catalog to a 256-grid for LLM layout generation"
    )
    parser.add_argument("floor_plan", help="Path to floor plan JSON")
    parser.add_argument("--products", default=DEFAULT_PRODUCTS_DIR, help=f"Vendor metadata folder (default: {DEFAULT_PRODUCTS_DIR})")
    parser.add_argument("--catalog", default=DEFAULT_CATALOG_DIR, help=f"Catalog templates folder (default: {DEFAULT_CATALOG_DIR})")
    args = parser.parse_args()

    plan_path = Path(args.floor_plan)
    stem = plan_path.stem
    output_dir = Path(stem)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Quantize floor plan
    plan_result, scale = quantize_floor_plan(plan_path)

    plan_out = output_dir / f"{stem}_plan.css"
    with open(plan_out, "w") as f:
        f.write(format_plan_css(plan_result))

    # Merge catalog templates (products + profiles) and compute footprints
    catalog_dir = Path(args.catalog)
    products, profiles = merge_catalog_templates(catalog_dir)

    if not products:
        print(f"  No catalog templates found in {catalog_dir}/")
        print(f"  Falling back to vendor metadata in {args.products}/ (no profile data)")
        # Fallback: build products from vendor metadata (no profiles)
        products = []
        profiles = []
        for path in sorted(Path(args.products).glob("*.json")):
            with open(path) as f:
                data = json.load(f)
            if "dimensions" not in data:
                continue
            products.append({
                "item_no": data["item_no"],
                "name": data["name"],
                "color": data.get("color", ""),
            })

    footprints = build_footprints(products, args.products, scale)

    catalog_out = output_dir / f"{stem}_catalog.json"
    with open(catalog_out, "w") as f:
        catalog = {
            "scale_px_per_m": round(scale, 4),
            "products": products,
        }
        if profiles:
            catalog["profiles"] = profiles
        catalog["footprints"] = footprints
        json.dump(catalog, f, indent=2)

    # Summary
    print(f"Output: {output_dir}/")
    print(f"  {stem}_plan.css      - {plan_result['canvas']['width']}x{plan_result['canvas']['height']} canvas, "
          f"{len(plan_result['rooms'])} rooms, "
          f"{len(plan_result['obstacles'])} obstacles, "
          f"{len(plan_result['wall_features'])} windows")
    print(f"  {stem}_catalog.json  - {len(products)} products, "
          f"{len(profiles)} profiles, "
          f"{len(footprints)} footprints at {round(scale, 4)} px/m")
    print()
    for fp in footprints:
        print(f"    {fp}")


if __name__ == "__main__":
    main()
