"""Stage 2: Arrange curated items in each room with exact coordinates.

Uses sequential tier-based placement: anchors first, then accents (seeing
placed anchors as occupied zones), then fill (seeing all prior placements).
Surface items are resolved deterministically without an LLM call.

Usage:
    python generate_arrangement.py gallery_hall
    python generate_arrangement.py quantize_room.output   # batch all plans
    python generate_arrangement.py gallery_hall --model haiku
    python generate_arrangement.py gallery_hall --room r1  # single room only
    python generate_arrangement.py gallery_hall --verbose --report

Reads:   quantize_room.output/<stem>_plan.css, <stem>_catalog.json, <stem>_curation.json
Writes:  quantize_room.output/<stem>_placement.json
         quantize_room.output/<stem>_report.json  (with --report)
"""

import json
import random
import re
import argparse
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
}

TIER_ORDER = ["anchor", "accent", "fill"]


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


def group_roles_by_tier(roles, profiles):
    """Split roles into tier buckets + separate surface items.

    Returns ({"anchor": [...], "accent": [...], "fill": [...]}, surface_roles).
    """
    tiers = {"anchor": [], "accent": [], "fill": []}
    surface_roles = []
    for role in roles:
        if get_role_placement_type(role, profiles) == "surface":
            surface_roles.append(role)
            continue
        tier = get_role_tier(role, profiles)
        tiers.get(tier, tiers["fill"]).append(role)
    return tiers, surface_roles


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

def resolve_surface_items(surface_roles, placed_items, profiles):
    """Deterministically place surface items on anchor-tier placed items.

    Round-robins across anchors. Falls back to any placed item if no anchors.
    Picks the first candidate for each surface role.
    """
    if not surface_roles or not placed_items:
        return []

    # Prefer anchor-tier items as surfaces
    anchors = [item for item in placed_items
               if profiles.get(item["item_no"], {}).get("tier") == "anchor"]
    if not anchors:
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
            result.append({
                "item_no": item_no,
                "x": anchor["x"],
                "y": anchor["y"],
                "r": 0,
            })
    return result


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


# ---------- Stage 2 (per tier) ----------

def stage_arrange(room_id, room_name, room_css, items_json, occupied_block, tier, model, verbose=False, timeout=300):
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
    parsed, raw, duration, error = call_llm(prompt, model, verbose, timeout)

    report = {
        "room": room_id,
        "room_name": room_name,
        "tier": tier,
        "prompt_chars": len(prompt),
        "duration_s": duration,
        "raw_response": raw,
        "error": error,
    }

    if error:
        print(f" {error} ({duration}s)")
        report["parsed"] = None
        return [], report

    if not isinstance(parsed, list):
        print(f" unexpected format ({duration}s)")
        report["error"] = "unexpected_format"
        report["parsed"] = parsed
        return [], report

    print(f" done ({len(parsed)} items, {duration}s)")
    report["parsed"] = parsed
    return parsed, report


# ---------- Main pipeline ----------

def process_plan(plan_stem, output_dir, model, verbose=False, write_report=False, room_filter=None, timeout=300):
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

    print(f"  {meta['width']}x{meta['height']}, {len(room_ids)} room(s), {len(curation)} roles")

    report = {
        "plan": plan_stem,
        "model": model,
        "timestamp": datetime.now().isoformat(),
        "rooms": len(room_ids),
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

    for room_id in room_ids:
        room_name = get_room_name(rules, room_id)
        room_css = extract_room_css(plan_css, meta, rules, room_id)

        # Get all roles for this room
        room_roles = [c for c in curation if c["room"] == room_id]
        if not room_roles:
            print(f"  [{room_id} {room_name}]: no items assigned, skipping")
            continue

        # Group by tier, separate surface items
        tier_groups, surface_roles = group_roles_by_tier(room_roles, profiles)

        placed_in_room = []

        # Sequential tier placement: anchor → accent → fill
        for tier in TIER_ORDER:
            tier_roles = tier_groups.get(tier, [])
            if not tier_roles:
                continue

            items_json = build_tier_items_json(tier_roles, footprints, products, profiles)
            if not items_json:
                continue

            occupied_block = build_occupied_block(placed_in_room, footprints, products)

            placed, s2_report = stage_arrange(
                room_id, room_name, room_css, items_json,
                occupied_block, tier, model, verbose, timeout,
            )
            report["stage2"].append(s2_report)

            for item in placed:
                if isinstance(item, dict):
                    item["room"] = room_id
                    placed_in_room.append(item)
                    all_items.append(item)

        # Resolve surface items deterministically
        surface_items = resolve_surface_items(surface_roles, placed_in_room, profiles)
        if surface_items:
            print(f"  [{room_id} {room_name}] surface: {len(surface_items)} items placed on anchors")
            for item in surface_items:
                item["room"] = room_id
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
    placement["items"] = all_items

    with open(placement_path, "w") as f:
        json.dump(placement, f, indent=2)

    report["gen"] = gen
    report["total_items"] = len(all_items)
    report["total_duration_s"] = round(
        sum(s["duration_s"] or 0 for s in report["stage2"]),
        1,
    )

    if write_report:
        _write_report(output_dir, plan_stem, report)

    print(f"  -> {placement_path} ({len(all_items)} items, {gen}, {report['total_duration_s']}s total)")
    return True


def _write_report(output_dir, plan_stem, report):
    """Write report JSON."""
    report_path = output_dir / f"{plan_stem}_report.json"
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
    parser.add_argument("--timeout", type=int, default=300, help="LLM call timeout in seconds (default: 300)")
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
