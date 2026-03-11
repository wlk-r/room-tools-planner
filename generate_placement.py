"""
Generate furniture placement for a quantized floor plan.

Stage 1 (curate):  LLM selects products from catalog, assigns to rooms with candidates.
Stage 2 (arrange): LLM places items per room with exact coordinates.

Usage:
    python generate_placement.py 01_single_room    # uses quantize_room.output/
    python generate_placement.py quantize_room.output  # batch all plans
    python generate_placement.py 01_single_room --model opus
    python generate_placement.py 01_single_room --verbose --report

Reads:   quantize_room.output/<stem>_plan.css, <stem>_catalog.json
Writes:  quantize_room.output/<stem>_placement.json
         quantize_room.output/<stem>_report.json  (with --report)
"""

import json
import os
import random
import re
import subprocess
import sys
import argparse
import time
from datetime import datetime
from pathlib import Path


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


def gen_id(model):
    """Generate placement ID: YYYYMMDD-<4hex>-<model tag>."""
    date = datetime.now().strftime("%Y%m%d")
    hex4 = f"{random.randint(0, 0xFFFF):04x}"
    tag = MODEL_TAGS.get(model, model.split("-")[-1][:6])
    return f"{date}-{hex4}-{tag}"


def extract_json(text):
    """Try to parse JSON from LLM response text."""
    text = text.strip()
    # Direct parse
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    # Find first [ or { and match
    for start_char, end_char in [("[", "]"), ("{", "}")]:
        start = text.find(start_char)
        if start < 0:
            continue
        end = text.rfind(end_char)
        if end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    # Unwrap markdown code fences
    fenced = re.search(r"```(?:json)?\s*\n([\s\S]*?)\n```", text)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    return None


def call_llm(prompt, model="sonnet", verbose=False):
    """Call claude --print with prompt via stdin.

    Returns (parsed, raw_response, duration_s, error).
    """
    cmd = ["claude", "--print", "--model", model]

    t0 = time.time()
    try:
        result = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        duration = round(time.time() - t0, 1)
        return None, None, duration, "TIMEOUT"

    duration = round(time.time() - t0, 1)

    if result.returncode != 0:
        err = f"exit={result.returncode}: {result.stderr.strip()[:300]}"
        return None, result.stdout.strip(), duration, err

    text = result.stdout.strip()
    if not text:
        return None, "", duration, f"EMPTY: {result.stderr.strip()[:300]}"

    if verbose:
        print(f"\n    --- raw response ({len(text)} chars, {duration}s) ---")
        print(f"    {text[:1000]}")
        if len(text) > 1000:
            print(f"    ... ({len(text) - 1000} more chars)")
        print(f"    --- end ---")

    # Unwrap --output-format json envelope if present
    parsed = extract_json(text)
    if isinstance(parsed, dict) and "result" in parsed and "role" not in parsed and "item_no" not in parsed:
        inner = parsed["result"]
        parsed = extract_json(inner) if isinstance(inner, str) else inner

    if parsed is not None:
        return parsed, text, duration, None

    return None, text, duration, "PARSE_ERROR"


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
    """Extract CSS rules relevant to a specific room: room components + adjacent obstacles + doors + windows."""
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


# ---------- Stages ----------

def stage_curate(plan_css, catalog, model, verbose=False, vibe=""):
    """Stage 1: LLM curates products from catalog and assigns to rooms."""
    catalog_view = {
        "products": catalog["products"],
        "profiles": catalog.get("profiles", []),
    }

    vibe_block = f"\n<style_brief>\n{vibe}\n</style_brief>\n" if vibe else ""

    prompt_template = (PROMPTS_DIR / "curate.md").read_text(encoding="utf-8")
    prompt = prompt_template.format(
        plan_css=plan_css,
        catalog_json=json.dumps(catalog_view, indent=2),
        vibe=vibe_block,
    )

    print(f"  Stage 1: curating products ({len(prompt)} chars)...", end="", flush=True)
    parsed, raw, duration, error = call_llm(prompt, model, verbose)

    report = {
        "prompt_chars": len(prompt),
        "duration_s": duration,
        "raw_response": raw,
        "error": error,
    }

    if error:
        print(f" {error} ({duration}s)")
        report["parsed"] = None
        return None, report

    if not isinstance(parsed, list):
        print(f" unexpected format ({duration}s)")
        report["error"] = "unexpected_format"
        report["parsed"] = parsed
        return None, report

    print(f" done ({len(parsed)} roles, {duration}s)")
    report["parsed"] = parsed
    return parsed, report


def stage_arrange(room_id, room_name, room_css, items_json, model, verbose=False):
    """Stage 2: LLM arranges items in a single room."""
    prompt_template = (PROMPTS_DIR / "arrange.md").read_text(encoding="utf-8")
    prompt = prompt_template.format(
        room_id=room_id,
        room_name=room_name,
        room_css=room_css,
        items_json=items_json,
    )

    print(f"  Stage 2 [{room_id} {room_name}]: arranging ({len(prompt)} chars)...", end="", flush=True)
    parsed, raw, duration, error = call_llm(prompt, model, verbose)

    report = {
        "room": room_id,
        "room_name": room_name,
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


def build_room_items_json(curation, room_id, catalog):
    """Build the items JSON for a room's arrange call from curation + catalog footprints."""
    room_roles = [c for c in curation if c["room"] == room_id]
    if not room_roles:
        return None, []

    footprints = {}
    if catalog.get("footprints"):
        for fp in catalog["footprints"]:
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

    products = {p["item_no"]: p for p in catalog["products"]}
    profiles = {p["item_no"]: p for p in catalog.get("profiles", [])}

    items = []
    for role in room_roles:
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

    return json.dumps(items, indent=2), room_roles


# ---------- Main ----------

def process_plan(plan_stem, output_dir, model, verbose=False, write_report=False, vibe=""):
    """Generate placement for a single plan. Returns True on success."""
    plan_css_path = output_dir / f"{plan_stem}_plan.css"
    catalog_path = output_dir / f"{plan_stem}_catalog.json"

    if not plan_css_path.exists():
        print(f"  Plan not found: {plan_css_path}")
        return False
    if not catalog_path.exists():
        print(f"  Catalog not found: {catalog_path}")
        return False

    plan_css = plan_css_path.read_text(encoding="utf-8")
    with open(catalog_path) as f:
        catalog = json.load(f)

    meta, rules = parse_plan_css(plan_css)
    room_ids = get_room_ids(rules)

    print(f"  {meta['width']}x{meta['height']}, {len(room_ids)} rooms, {len(catalog['products'])} products")

    # Report structure
    report = {
        "plan": plan_stem,
        "model": model,
        "timestamp": datetime.now().isoformat(),
        "rooms": len(room_ids),
        "catalog_products": len(catalog["products"]),
        "stage1": None,
        "stage2": [],
    }

    # Stage 1: Curate
    curation, s1_report = stage_curate(plan_css, catalog, model, verbose, vibe)
    report["stage1"] = s1_report

    if curation is None:
        print("  Curation failed, skipping.")
        if write_report:
            _write_report(output_dir, plan_stem, report)
        return False

    for role in curation:
        print(f"    {role['room']} {role.get('role','?')} x{role.get('qty',1)}: {role.get('candidates', [])}")
    print()

    # Stage 2: Arrange per room
    all_items = []
    for room_id in room_ids:
        room_name = get_room_name(rules, room_id)
        room_css = extract_room_css(plan_css, meta, rules, room_id)
        items_json, room_roles = build_room_items_json(curation, room_id, catalog)

        if items_json is None:
            print(f"  Stage 2 [{room_id} {room_name}]: no items assigned, skipping")
            continue

        placed, s2_report = stage_arrange(room_id, room_name, room_css, items_json, model, verbose)
        report["stage2"].append(s2_report)

        for item in placed:
            if isinstance(item, dict):
                item["room"] = room_id
                all_items.append(item)

    # Build final placement
    gen = gen_id(model)
    placement = {
        "plan": plan_stem,
        "gen": gen,
        "m_per_px": meta["m_per_px"],
        "items": all_items,
    }

    out_path = output_dir / f"{plan_stem}_placement.json"
    with open(out_path, "w") as f:
        json.dump(placement, f, indent=2)

    report["gen"] = gen
    report["total_items"] = len(all_items)
    report["total_duration_s"] = round(
        (report["stage1"]["duration_s"] or 0) +
        sum(s["duration_s"] or 0 for s in report["stage2"]),
        1,
    )

    if write_report:
        _write_report(output_dir, plan_stem, report)

    print(f"  -> {out_path} ({len(all_items)} items, {gen}, {report['total_duration_s']}s total)")
    return True


def _write_report(output_dir, plan_stem, report):
    """Write report JSON alongside placement file."""
    report_path = output_dir / f"{plan_stem}_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  -> {report_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate furniture placement for quantized floor plan(s)"
    )
    parser.add_argument("input", help="Plan stem (e.g. 01_single_room) or output directory for batch mode")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--model", default="sonnet", help="Model for LLM calls (default: sonnet)")
    parser.add_argument("--vibe", default="", help="Style brief for curation (e.g. 'warm scandinavian, earth tones')")
    parser.add_argument("--force", action="store_true", help="Regenerate existing placements")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print raw LLM responses to console")
    parser.add_argument("--report", "-r", action="store_true", help="Write <stem>_report.json with full diagnostics")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)

    input_path = Path(args.input)
    # Batch mode: only if directory contains *_plan.css files
    if input_path.is_dir() and list(input_path.glob("*_plan.css")):
        plan_dir = input_path
        plan_files = sorted(plan_dir.glob("*_plan.css"))
        stems = [f.stem.replace("_plan", "") for f in plan_files]
        print(f"Batch mode: {len(stems)} plans in {plan_dir}/\n")
        output_dir = plan_dir
    else:
        # Single plan stem — use output_dir for file lookups
        stems = [args.input]

    done = 0
    skipped = 0
    failed = 0

    for stem in stems:
        placement_path = output_dir / f"{stem}_placement.json"
        if placement_path.exists() and not args.force:
            print(f"[{stem}] skip (exists)")
            skipped += 1
            continue

        print(f"[{stem}]")
        if process_plan(stem, output_dir, args.model, args.verbose, args.report, args.vibe):
            done += 1
        else:
            failed += 1
        print()

    print(f"Done: {done} generated, {skipped} skipped, {failed} failed")


if __name__ == "__main__":
    main()
