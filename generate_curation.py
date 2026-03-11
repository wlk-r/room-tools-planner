"""Stage 1: Curate products from catalog and assign to rooms.

Reads a quantized floor plan CSS and product catalog, calls the LLM to select
products for each room, and writes a curation JSON file.

Usage:
    python generate_curation.py gallery_hall
    python generate_curation.py quantize_room.output   # batch all plans
    python generate_curation.py gallery_hall --model opus --vibe "warm scandinavian"
    python generate_curation.py gallery_hall --verbose --report

Reads:   quantize_room.output/<stem>_plan.css, <stem>_catalog.json
Writes:  quantize_room.output/<stem>_curation.json
         quantize_room.output/<stem>_report.json  (with --report)
"""

import json
import argparse
from datetime import datetime
from pathlib import Path

from llm_utils import call_llm

DEFAULT_OUTPUT_DIR = "quantize_room.output"
PROMPTS_DIR = Path(__file__).parent / "prompts"

REQUIRED_ROLE_KEYS = {"room", "role", "qty", "candidates"}


def validate_curation(parsed):
    """Validate curation output structure. Returns (roles, errors)."""
    if not isinstance(parsed, list):
        return None, ["expected JSON array"]
    errors = []
    for i, role in enumerate(parsed):
        if not isinstance(role, dict):
            errors.append(f"role[{i}]: not an object")
            continue
        missing = REQUIRED_ROLE_KEYS - set(role.keys())
        if missing:
            errors.append(f"role[{i}]: missing keys {missing}")
        if not isinstance(role.get("candidates"), list) or not role.get("candidates"):
            errors.append(f"role[{i}]: candidates must be a non-empty array")
        if not isinstance(role.get("qty"), int) or role.get("qty", 0) < 1:
            errors.append(f"role[{i}]: qty must be a positive integer")
    if errors:
        return None, errors
    return parsed, []


def stage_curate(plan_css, catalog, model, verbose=False, vibe="", timeout=300):
    """LLM curates products from catalog and assigns to rooms."""
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

    print(f"  Curating products ({len(prompt)} chars)...", end="", flush=True)
    parsed, raw, duration, error = call_llm(prompt, model, verbose, timeout)

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

    roles, validation_errors = validate_curation(parsed)
    if validation_errors:
        print(f" validation failed ({duration}s)")
        for e in validation_errors:
            print(f"    {e}")
        report["error"] = "VALIDATION: " + "; ".join(validation_errors)
        report["parsed"] = parsed
        return None, report

    print(f" done ({len(roles)} roles, {duration}s)")
    report["parsed"] = roles
    return roles, report


def process_plan(plan_stem, output_dir, model, verbose=False, write_report=False, vibe="", timeout=300):
    """Generate curation for a single plan. Returns True on success."""
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

    print(f"  {len(catalog['products'])} products")

    report = {
        "plan": plan_stem,
        "model": model,
        "timestamp": datetime.now().isoformat(),
        "catalog_products": len(catalog["products"]),
        "stage": "curate",
    }

    curation, s1_report = stage_curate(plan_css, catalog, model, verbose, vibe, timeout)
    report.update(s1_report)

    if curation is None:
        print("  Curation failed.")
        if write_report:
            _write_report(output_dir, plan_stem, report)
        return False

    for role in curation:
        print(f"    {role['room']} {role.get('role','?')} x{role.get('qty',1)}: {role.get('candidates', [])}")

    # Write curation JSON with metadata
    curation_doc = {
        "plan": plan_stem,
        "model": model,
        "roles": curation,
    }
    if vibe:
        curation_doc["vibe"] = vibe

    out_path = output_dir / f"{plan_stem}_curation.json"
    with open(out_path, "w") as f:
        json.dump(curation_doc, f, indent=2)
    print(f"  -> {out_path} ({len(curation)} roles)")

    if write_report:
        _write_report(output_dir, plan_stem, report)

    return True


def _write_report(output_dir, plan_stem, report):
    """Write report JSON."""
    report_path = output_dir / f"{plan_stem}_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  -> {report_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Stage 1: Curate products from catalog and assign to rooms"
    )
    parser.add_argument("input", help="Plan stem (e.g. gallery_hall) or output directory for batch mode")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--model", default="sonnet", help="Model for LLM calls (default: sonnet)")
    parser.add_argument("--vibe", default="", help="Style brief (e.g. 'warm scandinavian, earth tones')")
    parser.add_argument("--timeout", type=int, default=300, help="LLM call timeout in seconds (default: 300)")
    parser.add_argument("--force", action="store_true", help="Regenerate existing curation")
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
        curation_path = output_dir / f"{stem}_curation.json"
        if curation_path.exists() and not args.force:
            print(f"[{stem}] skip (exists)")
            skipped += 1
            continue

        print(f"[{stem}]")
        if process_plan(stem, output_dir, args.model, args.verbose, args.report, args.vibe, args.timeout):
            done += 1
        else:
            failed += 1
        print()

    print(f"Done: {done} generated, {skipped} skipped, {failed} failed")


if __name__ == "__main__":
    main()
