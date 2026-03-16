"""Stage 2: Arrange curated items in each room with exact coordinates.

Rooms are arranged in parallel (one LLM call per room, all fired concurrently).
Surface items are resolved deterministically without an LLM call.

Usage:
    python generate_arrangement.py gallery_hall
    python generate_arrangement.py quantize_room.output   # batch all plans
    python generate_arrangement.py gallery_hall --model haiku
    python generate_arrangement.py gallery_hall --room r1  # single room only
    python generate_arrangement.py gallery_hall --verbose --report

Reads:   quantize_room.output/<stem>_plan.css, <stem>_catalog.json, <stem>_curation.json
Writes:  quantize_room.output/<stem>_placement.json
         quantize_room.output/<stem>_report.arrange.json  (with --report)

Architecture note:
    This module contains unused tier-splitting and occupied-zone utilities
    (group_roles_by_tier, build_occupied_block, format_occupied_css). These
    support a two-pass arrangement strategy (anchor+accent first, then fill
    seeing occupied zones) that was implemented and tested but found to be
    slower than single-call due to ~50s fixed overhead per `claude --print`
    invocation. The code is retained for future use — if the LLM backend
    switches to direct API calls (sub-second overhead), re-enabling tier
    splitting would improve placement quality on rooms with 10+ items.
    See arrange_room() for the integration point.
"""

import json
import random
import re
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from llm_utils import call_llm

DEFAULT_OUTPUT_DIR = "quantize_room.output"
PROMPTS_DIR = Path(__file__).parent / "prompts"

MODEL_TAGS = {
    "opus": "opus",
    "sonnet": "sonnet",
    "haiku": "haiku",
    "claude-opus-4-6": "opus",
    "claude-sonnet-4-6": "sonnet",
    "claude-haiku-4-5-20251001": "haiku",
    "gemini-flash": "flash",
    "gemini-pro": "gpro",
    "gemini-2.0-flash": "flash",
    "gemini-2.5-pro-preview-06-05": "gpro",
    "nvidia-glm": "nglm",
    "z-ai/glm4.7": "nglm",
    "nvidia-deepseek": "nds",
    "deepseek-ai/deepseek-v3.2": "nds",
    "nvidia-devstral": "ndvs",
    "mistralai/devstral-2-123b-instruct-2512": "ndvs",
    "nvidia-kimi": "nkimi",
    "moonshotai/kimi-k2.5": "nkimi",
}

DEFAULT_TIMEOUT = 600  # seconds — generous since rooms run in parallel


def gen_id(model):
    """Generate placement ID: YYYYMMDD-<4hex>-<model tag>."""
    date = datetime.now().strftime("%Y%m%d")
    hex4 = f"{random.randint(0, 0xFFFF):04x}"
    tag = MODEL_TAGS.get(model, model.split("-")[-1][:6])
    return f"{date}-{hex4}-{tag}"


# ---------- CSS parsing ----------

def parse_plan_css(text):
    """Parse plan CSS into header metadata + list of rules."""
    header = re.match(
        r"/\*\s*(\d+)x(\d+)\s+grid\s*\|\s*1px\s*=\s*([\d.]+)m\s*\|\s*ceiling:\s*(\d+)px\s*\*/",
        text,
    )
    meta = {
        "width": int(header.group(1)) if header else 256,
        "height": int(header.group(2)) if header else 256,
        "m_per_px": float(header.group(3)) if header else 0,
        "ceiling": int(header.group(4)) if header else 0,
    }

    rules = []
    for m in re.finditer(r"#([\w]+)\.(room|obstacle|door|window)\s*\{([^}]+)\}", text):
        rule_id, cls, body = m.group(1), m.group(2), m.group(3)
        props = {}
        for pair in body.split(";"):
            kv = re.match(r"\s*([\w-]+)\s*:\s*(.+)", pair.strip())
            if kv:
                props[kv.group(1)] = kv.group(2).strip()
        comment_m = re.search(r"/\*\s*(.+?)\s*\*/", body)
        comment = comment_m.group(1) if comment_m else ""
        rules.append({"id": rule_id, "cls": cls, "props": props, "comment": comment})
    return meta, rules


def get_room_ids(rules):
    """Get unique base room IDs (e.g. r1, r2) from CSS rules."""
    rooms = set()
    for r in rules:
        if r["cls"] == "room":
            base = re.sub(r"_\d+$", "", r["id"])
            rooms.add(base)
    return sorted(rooms)


def get_room_name(rules, room_id):
    """Get room name from comment of first component."""
    for r in rules:
        if r["cls"] == "room" and (r["id"] == room_id or r["id"].startswith(room_id + "_")):
            name = re.sub(r"\s*\(\d+/\d+\)$", "", r["comment"])
            if name:
                return name
    return room_id


def extract_room_css(plan_text, meta, rules, room_id):
    """Extract CSS rules relevant to a specific room."""
    room_rules = [r for r in rules if r["cls"] == "room" and
                  (r["id"] == room_id or r["id"].startswith(room_id + "_"))]

    min_x, min_y = 9999, 9999
    max_x, max_y = 0, 0
    for r in room_rules:
        l = int(r["props"]["left"])
        t = int(r["props"]["top"])
        w = int(r["props"]["width"])
        h = int(r["props"]["height"])
        min_x = min(min_x, l)
        min_y = min(min_y, t)
        max_x = max(max_x, l + w)
        max_y = max(max_y, t + h)

    pad = 5
    bbox = (min_x - pad, min_y - pad, max_x + pad, max_y + pad)

    def intersects(r):
        l = int(r["props"]["left"])
        t = int(r["props"]["top"])
        w = int(r["props"]["width"])
        h = int(r["props"]["height"])
        return not (l + w < bbox[0] or l > bbox[2] or t + h < bbox[1] or t > bbox[3])

    context_rules = [r for r in rules if r["cls"] != "room" and intersects(r)]

    lines = [f"/* {meta['width']}x{meta['height']} grid | 1px = {meta['m_per_px']}m | ceiling: {meta['ceiling']}px */"]
    lines.append("")
    for r in room_rules:
        props_str = "; ".join(f"{k}: {v}" for k, v in r["props"].items())
        comment = f" /* {r['comment']} */" if r["comment"] else ""
        lines.append(f"#{r['id']}.{r['cls']} {{ {props_str};{comment} }}")
    lines.append("")
    for r in context_rules:
        props_str = "; ".join(f"{k}: {v}" for k, v in r["props"].items())
        comment = f" /* {r['comment']} */" if r["comment"] else ""
        lines.append(f"#{r['id']}.{r['cls']} {{ {props_str};{comment} }}")
    return "\n".join(lines)


# ---------- Catalog helpers ----------

def parse_footprints(catalog):
    """Parse footprint snippets from catalog into {item_no: {width, height}}."""
    footprints = {}
    for fp in catalog.get("footprints", []):
        m = re.match(r"#i(\d+)\s*\{([^}]+)\}", fp)
        if m:
            item_no = m.group(1)
            body = m.group(2)
            w = re.search(r"width:\s*(\d+)", body)
            h = re.search(r"height:\s*(\d+)", body)
            footprints[item_no] = {
                "width": int(w.group(1)) if w else 0,
                "height": int(h.group(1)) if h else 0,
            }
    return footprints


def get_role_tier(role, profiles):
    """Determine tier for a role based on first candidate's profile."""
    for item_no in role.get("candidates", []):
        if item_no in profiles:
            return profiles[item_no].get("tier", "fill")
    return "fill"


def get_role_placement_type(role, profiles):
    """Determine placement type (floor/wall/surface) for a role."""
    for item_no in role.get("candidates", []):
        if item_no in profiles:
            return profiles[item_no].get("placement", "floor")
    return "floor"


def _is_plant_role(role, profiles):
    """Check if a role's candidates are plants (by category)."""
    for item_no in role.get("candidates", []):
        cats = profiles.get(item_no, {}).get("categories", [])
        if "plant" in cats:
            return True
    return False


def group_roles_by_tier(roles, profiles):
    """Split roles into tier buckets + separate surface, plant, and wall items.

    Returns (tiers_dict, surface_roles, plant_roles, wall_roles).
    """
    tiers = {"anchor": [], "accent": [], "fill": []}
    surface_roles = []
    plant_roles = []
    wall_roles = []
    for role in roles:
        pt = get_role_placement_type(role, profiles)
        if pt == "surface":
            surface_roles.append(role)
            continue
        if pt == "wall":
            wall_roles.append(role)
            continue
        if _is_plant_role(role, profiles):
            plant_roles.append(role)
            continue
        tier = get_role_tier(role, profiles)
        tiers.get(tier, tiers["fill"]).append(role)
    return tiers, surface_roles, plant_roles, wall_roles


# ---------- Occupied zones ----------

def format_occupied_css(placed_items, footprints, products):
    """Format placed items as CSS occupied-zone rules."""
    lines = []
    for i, item in enumerate(placed_items):
        fp = footprints.get(item["item_no"], {"width": 10, "height": 10})
        w, h = fp["width"], fp["height"]
        rotated = item.get("r", 0) in (90, 270)
        draw_w, draw_h = (h, w) if rotated else (w, h)
        left = item["x"] - draw_w // 2
        top = item["y"] - draw_h // 2
        name = products.get(item["item_no"], {}).get("name", item["item_no"])
        lines.append(f"#placed_{i}.occupied {{ left: {left}; top: {top}; width: {draw_w}; height: {draw_h}; /* {name} */ }}")
    return "\n".join(lines)


def build_occupied_block(placed_items, footprints, products):
    """Build the <occupied> XML block for the prompt, or empty string if none."""
    if not placed_items:
        return ""
    css = format_occupied_css(placed_items, footprints, products)
    return f"\n<occupied>\n{css}\n</occupied>\n"


# ---------- Surface resolution ----------

def _has_tabletop(item_no, profiles):
    """Check if an item has a flat surface suitable for placing things on."""
    cats = profiles.get(item_no, {}).get("categories", [])
    return "tabletop" in cats


def resolve_surface_items(surface_roles, placed_items, profiles):
    """Deterministically place surface items on tabletop-category placed items.

    Round-robins across valid targets. Falls back to any placed item only
    if no tabletop items exist (unlikely but safe).
    Picks the first candidate for each surface role.
    """
    if not surface_roles or not placed_items:
        return []

    # Only place surface items on things with a flat top (desks, tables, TV units)
    anchors = [item for item in placed_items if _has_tabletop(item["item_no"], profiles)]
    if not anchors:
        # Last resort: any placed item (avoids dropping surface items entirely)
        anchors = list(placed_items)

    result = []
    idx = 0
    for role in surface_roles:
        candidates = role.get("candidates", [])
        if not candidates:
            continue
        item_no = candidates[0]
        for _ in range(role.get("qty", 1)):
            anchor = anchors[idx % len(anchors)]
            idx += 1
            entry = {
                "item_no": item_no,
                "x": anchor["x"],
                "y": anchor["y"],
                "r": 0,
                "placement_type": "surface",
                "group_role": "surface",
                "anchor_item_no": anchor["item_no"],
            }
            # Inherit group_id from anchor if it has one
            if "group_id" in anchor:
                entry["group_id"] = anchor["group_id"]
            result.append(entry)
    return result


# ---------- Plant resolution ----------

def _rect_from_rule(rule):
    """Extract (left, top, right, bottom) from a parsed CSS rule."""
    l = int(rule["props"]["left"])
    t = int(rule["props"]["top"])
    w = int(rule["props"]["width"])
    h = int(rule["props"]["height"])
    return (l, t, l + w, t + h)


def _rects_overlap(ax, ay, aw, ah, bx1, by1, bx2, by2):
    """Check if a centered rect (ax,ay,aw,ah) overlaps an LTRB rect."""
    return (ax - aw // 2 < bx2 and ax + aw // 2 > bx1 and
            ay - ah // 2 < by2 and ay + ah // 2 > by1)


def resolve_plant_items(plant_roles, placed_items, room_css, footprints, profiles):
    """Place plants along walls and in corners, away from doors and placed items.

    Strategy: generate candidate positions along room edges, score them by
    corner proximity and distance from existing items, then greedily assign
    plants largest-first.
    """
    if not plant_roles:
        return []

    # Parse room geometry from the room-specific CSS
    _, rules = parse_plan_css(room_css)
    room_rects = [_rect_from_rule(r) for r in rules if r["cls"] == "room"]
    door_rects = [_rect_from_rule(r) for r in rules if r["cls"] == "door"]

    if not room_rects:
        return []

    # Room bounding box
    r_min_x = min(r[0] for r in room_rects)
    r_min_y = min(r[1] for r in room_rects)
    r_max_x = max(r[2] for r in room_rects)
    r_max_y = max(r[3] for r in room_rects)

    # Build occupied rectangles (LTRB) from placed items with padding
    PAD = 8
    occupied = []
    for item in placed_items:
        fp = footprints.get(item["item_no"], {"width": 10, "height": 10})
        w, h = fp["width"], fp["height"]
        if item.get("r", 0) in (90, 270):
            w, h = h, w
        occupied.append((item["x"] - w // 2 - PAD, item["y"] - h // 2 - PAD,
                         item["x"] + w // 2 + PAD, item["y"] + h // 2 + PAD))

    # Generate candidate positions along room edges
    STEP = 8
    candidates = set()

    for rl, rt, rr, rb in room_rects:
        # Top and bottom edges
        for x in range(rl, rr, STEP):
            candidates.add((x, rt))  # top wall
            candidates.add((x, rb))  # bottom wall
        # Left and right edges
        for y in range(rt, rb, STEP):
            candidates.add((rl, y))  # left wall
            candidates.add((rr, y))  # right wall

    # Identify corner positions (room bounding box corners)
    corners = {
        (r_min_x, r_min_y), (r_max_x, r_min_y),
        (r_min_x, r_max_y), (r_max_x, r_max_y),
    }

    def near_door(x, y, margin=25):
        for dl, dt, dr, db in door_rects:
            if dl - margin <= x <= dr + margin and dt - margin <= y <= db + margin:
                return True
        return False

    def hits_occupied(cx, cy, pw, ph):
        for ol, ot, or_, ob in occupied:
            if _rects_overlap(cx, cy, pw, ph, ol, ot, or_, ob):
                return True
        return False

    def dist_to_nearest(x, y, items):
        if not items:
            return 9999
        return min(((x - it["x"]) ** 2 + (y - it["y"]) ** 2) ** 0.5 for it in items)

    def corner_dist(x, y):
        return min(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 for cx, cy in corners)

    # Collect plants to place, sorted largest footprint first
    plants_to_place = []
    for role in plant_roles:
        cands = role.get("candidates", [])
        if not cands:
            continue
        item_no = cands[0]
        fp = footprints.get(item_no, {"width": 10, "height": 10})
        for _ in range(role.get("qty", 1)):
            plants_to_place.append((item_no, fp["width"], fp["height"]))
    plants_to_place.sort(key=lambda p: p[1] * p[2], reverse=True)

    result = []
    placed_plants = []  # track to keep plants spread apart

    for item_no, pw, ph in plants_to_place:
        inset_x = pw // 2 + 2
        inset_y = ph // 2 + 2

        best = None
        best_score = -1

        for wx, wy in candidates:
            # Inset from wall so the plant footprint doesn't poke outside
            # Determine which edge this point is on and inset inward
            cx, cy = wx, wy
            if wx == r_min_x or any(wx == rl for rl, _, _, _ in room_rects):
                cx = wx + inset_x
            elif wx == r_max_x or any(wx == rr for _, _, rr, _ in room_rects):
                cx = wx - inset_x
            if wy == r_min_y or any(wy == rt for _, rt, _, _ in room_rects):
                cy = wy + inset_y
            elif wy == r_max_y or any(wy == rb for _, _, _, rb in room_rects):
                cy = wy - inset_y

            if near_door(cx, cy):
                continue
            if hits_occupied(cx, cy, pw, ph):
                continue

            # Score: prefer corners, prefer distance from other plants,
            # moderate distance from furniture (near groups but not on top)
            cd = corner_dist(cx, cy)
            corner_score = max(0, 50 - cd)  # bonus for being near corners
            furniture_dist = dist_to_nearest(cx, cy, placed_items)
            plant_dist = dist_to_nearest(cx, cy, placed_plants) if placed_plants else 200
            # Don't place too close to other plants (want spread)
            if plant_dist < 30:
                continue
            # Want moderate furniture distance: not on top (>20) but not isolated (penalty past 80)
            proximity_score = min(furniture_dist, 80)
            spread_score = min(plant_dist, 100)

            score = corner_score + proximity_score + spread_score
            if score > best_score:
                best_score = score
                best = (cx, cy)

        if best:
            entry = {"item_no": item_no, "x": best[0], "y": best[1], "r": 0}
            result.append(entry)
            placed_plants.append(entry)
            # Add to occupied so next plant avoids this spot
            occupied.append((best[0] - pw // 2 - PAD, best[1] - ph // 2 - PAD,
                             best[0] + pw // 2 + PAD, best[1] + ph // 2 + PAD))

    return result


# ---------- Wall item resolution ----------

# Rotation by edge: front (z+ in GLB, +y in CSS at r=0) faces into the room
_EDGE_ROTATION = {"top": 0, "bottom": 180, "left": 90, "right": 270}

# Default mount height (center of item, meters above floor)
_DEFAULT_MOUNT_HEIGHT = 1.7


def resolve_wall_items(wall_roles, placed_items, room_css, footprints, profiles):
    """Place wall items flush against room edges, facing inward.

    Rotation is deterministic: determined by which edge the item is placed on.
    Avoids doors, windows, and other placed/wall items.
    """
    if not wall_roles:
        return []

    _, rules = parse_plan_css(room_css)
    room_rects = [_rect_from_rule(r) for r in rules if r["cls"] == "room"]
    door_rects = [_rect_from_rule(r) for r in rules if r["cls"] == "door"]
    window_rects = [_rect_from_rule(r) for r in rules if r["cls"] == "window"]
    avoid_rects = door_rects + window_rects

    if not room_rects:
        return []

    # Build occupied rectangles from placed items
    PAD = 5
    occupied = []
    for item in placed_items:
        fp = footprints.get(item["item_no"], {"width": 10, "height": 10})
        w, h = fp["width"], fp["height"]
        if item.get("r", 0) in (90, 270):
            w, h = h, w
        occupied.append((item["x"] - w // 2 - PAD, item["y"] - h // 2 - PAD,
                         item["x"] + w // 2 + PAD, item["y"] + h // 2 + PAD))

    # Generate candidate positions along room edges, tagged with edge direction
    STEP = 6
    candidates = []  # (x, y, edge_name)

    for rl, rt, rr, rb in room_rects:
        for x in range(rl, rr, STEP):
            candidates.append((x, rt, "top"))
            candidates.append((x, rb, "bottom"))
        for y in range(rt, rb, STEP):
            candidates.append((rl, y, "left"))
            candidates.append((rr, y, "right"))

    def near_avoid(x, y, margin=10):
        """Check if position is too close to a door or window."""
        for al, at, ar, ab in avoid_rects:
            if al - margin <= x <= ar + margin and at - margin <= y <= ab + margin:
                return True
        return False

    def hits_occupied(cx, cy, w, h):
        for ol, ot, or_, ob in occupied:
            if _rects_overlap(cx, cy, w, h, ol, ot, or_, ob):
                return True
        return False

    # Collect wall items to place
    items_to_place = []
    for role in wall_roles:
        cands = role.get("candidates", [])
        if not cands:
            continue
        item_no = cands[0]
        fp = footprints.get(item_no, {"width": 10, "height": 10})
        for _ in range(role.get("qty", 1)):
            items_to_place.append((item_no, fp["width"], fp["height"]))

    result = []

    for item_no, fw, fh in items_to_place:
        best = None
        best_score = -1

        for wx, wy, edge in candidates:
            r = _EDGE_ROTATION[edge]

            # At r=0/180 (top/bottom walls): footprint is fw wide, fh deep
            # At r=90/270 (left/right walls): footprint is fh wide, fw deep
            if r in (90, 270):
                draw_w, draw_h = fh, fw
            else:
                draw_w, draw_h = fw, fh

            # Inset center so footprint is flush against the wall
            half_w, half_h = draw_w // 2, draw_h // 2
            if edge == "top":
                cx, cy = wx, wy + half_h
            elif edge == "bottom":
                cx, cy = wx, wy - half_h
            elif edge == "left":
                cx, cy = wx + half_w, wy
            else:  # right
                cx, cy = wx - half_w, wy

            if near_avoid(cx, cy):
                continue
            if hits_occupied(cx, cy, draw_w, draw_h):
                continue

            # Score: prefer distance from existing wall items (spread them out)
            if result:
                min_d = min(((cx - p["x"]) ** 2 + (cy - p["y"]) ** 2) ** 0.5 for p in result)
                if min_d < 20:
                    continue
                score = min(min_d, 100)
            else:
                score = 100

            if score > best_score:
                best_score = score
                best = (cx, cy, r, draw_w, draw_h)

        if best:
            cx, cy, r, dw, dh = best
            entry = {
                "item_no": item_no, "x": cx, "y": cy, "r": r,
                "placement_type": "wall",
                "mount_height": _DEFAULT_MOUNT_HEIGHT,
            }
            result.append(entry)
            occupied.append((cx - dw // 2 - PAD, cy - dh // 2 - PAD,
                             cx + dw // 2 + PAD, cy + dh // 2 + PAD))

    return result


# ---------- Post-processing ----------

def postprocess_items(items, profiles):
    """Validate and fix grouping invariants on placed items (in-place).

    - Ensures each group_id has exactly one anchor
    - Fixes missing anchor_item_no on dependents/surface members
    - Sets placement_type from catalog profile when non-default
    - Strips malformed group fields rather than propagating bad data
    """
    # Index items by group
    groups = {}  # group_id -> list of items
    item_by_no = {}  # item_no -> item (last wins, fine for anchor lookup)
    for item in items:
        if not isinstance(item, dict):
            continue
        item_by_no[item["item_no"]] = item
        gid = item.get("group_id")
        if gid:
            groups.setdefault(gid, []).append(item)

    for gid, members in groups.items():
        anchors = [m for m in members if m.get("group_role") == "anchor"]

        # No anchor: promote first member
        if not anchors:
            members[0]["group_role"] = "anchor"
            members[0].pop("anchor_item_no", None)
            anchors = [members[0]]

        # Multiple anchors: keep first, demote rest
        if len(anchors) > 1:
            for extra in anchors[1:]:
                extra["group_role"] = "dependent"
                extra["anchor_item_no"] = anchors[0]["item_no"]

        anchor = anchors[0]
        anchor.pop("anchor_item_no", None)  # anchors never reference another

        # Ensure all non-anchors reference the anchor
        for m in members:
            if m is anchor:
                continue
            if m.get("group_role") not in ("dependent", "surface"):
                m["group_role"] = "dependent"
            if not m.get("anchor_item_no"):
                m["anchor_item_no"] = anchor["item_no"]

    # Enrich placement_type from catalog profiles (only when non-default)
    for item in items:
        if not isinstance(item, dict):
            continue
        if "placement_type" not in item:
            pt = profiles.get(item["item_no"], {}).get("placement", "floor")
            if pt != "floor":
                item["placement_type"] = pt

    # Strip orphaned group fields (group_role without group_id)
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("group_role") and not item.get("group_id"):
            item.pop("group_role", None)
            item.pop("anchor_item_no", None)


# ---------- Item JSON builder ----------

def build_tier_items_json(roles, footprints, products, profiles):
    """Build the items JSON for a set of roles (single tier)."""
    if not roles:
        return None

    items = []
    for role in roles:
        candidates = []
        for item_no in role.get("candidates", []):
            entry = {"item_no": item_no}
            if item_no in products:
                entry["name"] = products[item_no]["name"]
            if item_no in profiles:
                entry["tier"] = profiles[item_no]["tier"]
                entry["placement"] = profiles[item_no]["placement"]
            if item_no in footprints:
                entry["footprint_w"] = footprints[item_no]["width"]
                entry["footprint_h"] = footprints[item_no]["height"]
            candidates.append(entry)
        items.append({
            "role": role.get("role", "unknown"),
            "qty": role.get("qty", 1),
            "candidates": candidates,
        })

    return json.dumps(items, indent=2)


# ---------- LLM call ----------

def stage_arrange(room_id, room_name, room_css, items_json, occupied_block, tier, model, verbose=False, timeout=DEFAULT_TIMEOUT):
    """LLM arranges items for a single tier in a single room."""
    prompt_template = (PROMPTS_DIR / "arrange.md").read_text(encoding="utf-8")
    prompt = prompt_template.format(
        room_id=room_id,
        room_name=room_name,
        room_css=room_css,
        occupied_block=occupied_block,
        items_json=items_json,
        tier=tier,
    )

    print(f"  [{room_id} {room_name}] {tier}: arranging ({len(prompt)} chars)...", end="", flush=True)
    parsed, raw, duration, error, usage = call_llm(prompt, model, verbose, timeout, stage="arrange")

    report = {
        "room": room_id,
        "room_name": room_name,
        "tier": tier,
        "prompt_chars": len(prompt),
        "duration_s": duration,
        "raw_response": raw,
        "error": error,
    }
    if usage:
        report["usage"] = usage
        tok_in = usage.get("input_tokens") or usage.get("prompt_tokens", "?")
        tok_out = usage.get("output_tokens") or usage.get("completion_tokens", "?")
        print(f" tokens: {tok_in} in / {tok_out} out", end="", flush=True)

    if error:
        print(f" {error} ({duration}s)")
        report["parsed"] = None
        return [], report

    if not isinstance(parsed, list):
        print(f" unexpected format ({duration}s)")
        report["error"] = "unexpected_format"
        report["parsed"] = parsed
        return [], report

    print(f" done ({len(parsed)} items, {duration:.1f}s)")
    report["parsed"] = parsed
    return parsed, report


# ---------- Per-room arrangement ----------

def arrange_room(room_id, room_name, room_css, room_roles,
                 footprints, products, profiles,
                 model, verbose, timeout):
    """Arrange one room: LLM for floor furniture, deterministic for surface/plant/wall.

    Returns (placed_items, reports, det_counts) where det_counts is a dict
    of deterministically-placed item counts by type.
    """
    tier_groups, surface_roles, plant_roles, wall_roles = group_roles_by_tier(room_roles, profiles)

    # All non-extracted roles in a single LLM call
    non_surface = tier_groups["anchor"] + tier_groups["accent"] + tier_groups["fill"]

    n_anchor = len(tier_groups["anchor"])
    n_accent = len(tier_groups["accent"])
    n_fill = len(tier_groups["fill"])
    print(f"  [{room_id} {room_name}] roles: {n_anchor} anchor, {n_accent} accent, {n_fill} fill"
          f" + {len(surface_roles)} surface, {len(wall_roles)} wall, {len(plant_roles)} plant", flush=True)

    placed = []
    reports = []

    if non_surface:
        items_json = build_tier_items_json(non_surface, footprints, products, profiles)
        if items_json:
            result, report = stage_arrange(
                room_id, room_name, room_css, items_json,
                "", "all", model, verbose, timeout,
            )
            reports.append(report)
            placed.extend(item for item in result if isinstance(item, dict))
            # Log placed items with product names
            for item in placed:
                p = products.get(item.get("item_no", ""))
                name = p["name"] if p else item.get("item_no", "?")
                gr = item.get("group_role", "")
                gid = item.get("group_id", "")
                grp = f" [{gr}->{gid}]" if gr and gid else ""
                print(f"    | ({item.get('x',0):>3},{item.get('y',0):>3}) r={item.get('r',0):>3}deg  {name}{grp}", flush=True)

    # Deterministic passes — each sees everything placed before it
    if surface_roles:
        print(f"  [{room_id} {room_name}] placing surface items...", flush=True)
    surface_items = resolve_surface_items(surface_roles, placed, profiles)
    placed.extend(surface_items)
    for item in surface_items:
        p = products.get(item.get("item_no", ""))
        name = p["name"] if p else item.get("item_no", "?")
        anc = products.get(item.get("anchor_item_no", ""))
        anc_name = anc["name"] if anc else "?"
        print(f"    | {name} -> on {anc_name}", flush=True)

    if wall_roles:
        print(f"  [{room_id} {room_name}] placing wall items...", flush=True)
    wall_items = resolve_wall_items(wall_roles, placed, room_css, footprints, profiles)
    placed.extend(wall_items)
    for item in wall_items:
        p = products.get(item.get("item_no", ""))
        name = p["name"] if p else item.get("item_no", "?")
        edge = {0: "top", 180: "bottom", 90: "left", 270: "right"}.get(item.get("r", 0), "?")
        print(f"    | {name} -> {edge} wall ({item.get('x',0)},{item.get('y',0)})", flush=True)

    if plant_roles:
        print(f"  [{room_id} {room_name}] placing plants...", flush=True)
    plant_items = resolve_plant_items(plant_roles, placed, room_css, footprints, profiles)
    placed.extend(plant_items)
    for item in plant_items:
        p = products.get(item.get("item_no", ""))
        name = p["name"] if p else item.get("item_no", "?")
        print(f"    | {name} -> ({item.get('x',0)},{item.get('y',0)})", flush=True)

    postprocess_items(placed, profiles)

    det_counts = {}
    if surface_items:
        det_counts["surface"] = len(surface_items)
    if wall_items:
        det_counts["wall"] = len(wall_items)
    if plant_items:
        det_counts["plant"] = len(plant_items)

    return placed, reports, det_counts


# ---------- Main pipeline ----------

def process_plan(plan_stem, output_dir, model, verbose=False, write_report=False, room_filter=None, timeout=DEFAULT_TIMEOUT):
    """Generate arrangement for a single plan. Returns True on success."""
    plan_css_path = output_dir / f"{plan_stem}_plan.css"
    catalog_path = output_dir / f"{plan_stem}_catalog.json"
    curation_path = output_dir / f"{plan_stem}_curation.json"

    if not plan_css_path.exists():
        print(f"  Plan not found: {plan_css_path}")
        return False
    if not catalog_path.exists():
        print(f"  Catalog not found: {catalog_path}")
        return False
    if not curation_path.exists():
        print(f"  Curation not found: {curation_path}")
        print(f"  Run generate_curation.py first.")
        return False

    plan_css = plan_css_path.read_text(encoding="utf-8")
    with open(catalog_path) as f:
        catalog = json.load(f)
    with open(curation_path) as f:
        curation_doc = json.load(f)

    # Support both wrapped format {"plan", "model", "roles"} and bare array
    if isinstance(curation_doc, dict) and "roles" in curation_doc:
        curate_model = curation_doc.get("model")
        curate_vibe = curation_doc.get("vibe")
        curation = curation_doc["roles"]
    else:
        curate_model = None
        curate_vibe = None
        curation = curation_doc

    meta, rules = parse_plan_css(plan_css)
    room_ids = get_room_ids(rules)

    if room_filter:
        room_ids = [r for r in room_ids if r in room_filter]
        if not room_ids:
            print(f"  No matching rooms for filter: {room_filter}")
            return False

    # Build lookup maps once
    footprints = parse_footprints(catalog)
    products = {p["item_no"]: p for p in catalog["products"]}
    profiles = {p["item_no"]: p for p in catalog.get("profiles", [])}

    # Prepare per-room tasks
    room_tasks = {}
    for room_id in room_ids:
        room_roles = [c for c in curation if c["room"] == room_id]
        if not room_roles:
            continue
        room_name = get_room_name(rules, room_id)
        room_css = extract_room_css(plan_css, meta, rules, room_id)
        room_tasks[room_id] = (room_name, room_css, room_roles)

    parallel = len(room_tasks) > 1
    scale = meta.get('m_per_px', '?')
    print(f"  {meta['width']}x{meta['height']}px, scale: {scale} m/px, {len(room_tasks)} room(s), {len(curation)} roles"
          + (" (parallel)" if parallel else ""))
    for room_id, (room_name, room_css, room_roles) in room_tasks.items():
        _, r_rules = parse_plan_css(room_css)
        rooms = [r for r in r_rules if r["cls"] == "room"]
        doors = [r for r in r_rules if r["cls"] == "door"]
        windows = [r for r in r_rules if r["cls"] == "window"]
        print(f"  [{room_id} {room_name}] {len(rooms)} zone(s), {len(doors)} door(s), {len(windows)} window(s)", flush=True)

    report = {
        "plan": plan_stem,
        "model": model,
        "timestamp": datetime.now().isoformat(),
        "rooms": len(room_tasks),
        "stage": "arrange",
        "stage2": [],
    }

    # If re-running a single room, merge with existing placement
    existing_items = []
    placement_path = output_dir / f"{plan_stem}_placement.json"
    if room_filter and placement_path.exists():
        with open(placement_path) as f:
            existing = json.load(f)
        existing_items = [item for item in existing.get("items", [])
                         if item.get("room") not in room_filter]

    all_items = list(existing_items)

    # Dispatch rooms in parallel
    with ThreadPoolExecutor(max_workers=max(len(room_tasks), 1)) as pool:
        futures = {}
        for room_id, (room_name, room_css, room_roles) in room_tasks.items():
            fut = pool.submit(
                arrange_room,
                room_id, room_name, room_css, room_roles,
                footprints, products, profiles,
                model, verbose, timeout,
            )
            futures[fut] = room_id

        for fut in as_completed(futures):
            room_id = futures[fut]
            try:
                placed, room_reports, det_counts = fut.result()
            except Exception as e:
                print(f"  [{room_id}] ERROR: {e}")
                report["stage2"].append({
                    "room": room_id,
                    "error": str(e),
                })
                continue

            report["stage2"].extend(room_reports)

            if det_counts:
                room_name = room_tasks[room_id][0]
                parts = [f"{v} {k}" for k, v in det_counts.items()]
                print(f"  [{room_id} {room_name}] deterministic: {', '.join(parts)}")

            for item in placed:
                if isinstance(item, dict):
                    item.setdefault("room", room_id)
                    all_items.append(item)

    # Build final placement
    gen = gen_id(model)
    placement = {
        "plan": plan_stem,
        "gen": gen,
        "m_per_px": meta["m_per_px"],
        "curate_model": curate_model or "unknown",
        "arrange_model": model,
    }
    if curate_vibe:
        placement["curate_vibe"] = curate_vibe
    placement["curation_roles"] = curation
    placement["items"] = all_items

    # Preserve previous placement before overwriting
    if placement_path.exists() and not room_filter:
        n = 2
        while (output_dir / f"{plan_stem}_placement_{n}.json").exists():
            n += 1
        import shutil
        shutil.copy2(str(placement_path), str(output_dir / f"{plan_stem}_placement_{n}.json"))

    with open(placement_path, "w") as f:
        json.dump(placement, f, indent=2)

    report["gen"] = gen
    report["total_items"] = len(all_items)
    report["total_duration_s"] = round(
        sum(s.get("duration_s") or 0 for s in report["stage2"]),
        1,
    )

    if write_report:
        _write_report(output_dir, plan_stem, report)

    print(f"  -> {placement_path} ({len(all_items)} items, {gen}, {report['total_duration_s']}s total)")
    return True


def _write_report(output_dir, plan_stem, report):
    """Write report JSON."""
    report_path = output_dir / f"{plan_stem}_report.arrange.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  -> {report_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Stage 2: Arrange curated items in rooms with exact coordinates"
    )
    parser.add_argument("input", help="Plan stem (e.g. gallery_hall) or output directory for batch mode")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--model", default="sonnet", help="Model for LLM calls (default: sonnet)")
    parser.add_argument("--room", action="append", dest="rooms", help="Only arrange specific room(s), e.g. --room r1 --room r3")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"LLM call timeout in seconds (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("--force", action="store_true", help="Regenerate existing placement")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print raw LLM responses")
    parser.add_argument("--report", "-r", action="store_true", help="Write report JSON with diagnostics")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)

    input_path = Path(args.input)
    if input_path.is_dir() and list(input_path.glob("*_plan.css")):
        plan_dir = input_path
        plan_files = sorted(plan_dir.glob("*_plan.css"))
        stems = [f.stem.replace("_plan", "") for f in plan_files]
        print(f"Batch mode: {len(stems)} plans in {plan_dir}/\n")
        output_dir = plan_dir
    else:
        stems = [args.input]

    done = 0
    skipped = 0
    failed = 0

    for stem in stems:
        placement_path = output_dir / f"{stem}_placement.json"
        if placement_path.exists() and not args.force and not args.rooms:
            print(f"[{stem}] skip (exists)")
            skipped += 1
            continue

        print(f"[{stem}]")
        if process_plan(stem, output_dir, args.model, args.verbose, args.report, args.rooms, args.timeout):
            done += 1
        else:
            failed += 1
        print()

    print(f"Done: {done} generated, {skipped} skipped, {failed} failed")


if __name__ == "__main__":
    main()
