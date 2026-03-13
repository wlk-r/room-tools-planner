"""Model tournament runner.

Runs the curation → arrangement → place pipeline for each model,
each in its own subfolder under the tournament directory.

Usage:
    python model-tournament/run_tournament.py model-tournament/amber_light
    python model-tournament/run_tournament.py model-tournament/amber_light --models gemini-flash nvidia-devstral
    python model-tournament/run_tournament.py model-tournament/amber_light --skip-place
    python model-tournament/run_tournament.py model-tournament/amber_light --force

The stem argument (e.g. model-tournament/amber_light) is split into:
- tournament dir: model-tournament/
- plan stem: amber_light

Expects these files in the tournament dir:
    <stem>_plan.css, <stem>_catalog.json, <stem>.json, <stem>_AO.ktx.glb

Creates per-model subfolders with curation/arrangement/report outputs.
Layout JSONs and product GLBs are collected into the tournament root.
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

PLACER_DIR = Path(r"C:\Users\Walker\Dev\room-tools-placer")
CATALOG_DIR = Path(r"C:\Users\Walker\Dev\room-tools-demo\catalog")
PROJECT_DIR = Path(__file__).resolve().parent.parent  # room-tools-planner root

# Default models to test. Claude models (sonnet/opus/haiku) use Anthropic SDK
# if ANTHROPIC_API_KEY is set, otherwise fall back to claude --print CLI.
DEFAULT_MODELS = [
    "sonnet",
    "opus",
    "haiku",
    "gemini-flash",
    "gemini-pro",
    "nvidia-glm",
    "nvidia-deepseek",
    "nvidia-devstral",
    # "nvidia-kimi",  # too slow — 29s on trivial prompts, times out on full curation (~125k chars)
]

SHARED_SUFFIXES = [
    "_plan.css",
    "_catalog.json",
    ".json",
    "_AO.ktx.glb",
]

# Files/dirs to keep during clean/archive (relative to tournament dir)
# Everything else is generated output.
KEEP_PATTERNS = {
    "run_tournament.py",
    "z.archive",
}

def _is_source_file(path, tournament_dir, stem):
    """Check if a path is a source file that should be kept."""
    name = path.name
    if name == "run_tournament.py":
        return True
    if name == "z.archive":
        return True
    for suffix in SHARED_SUFFIXES:
        if name == f"{stem}{suffix}":
            return True
    return False


def do_clean(tournament_dir, stem):
    """Delete all generated output, keeping only source files and z.archive."""
    removed = 0
    for item in sorted(tournament_dir.iterdir()):
        if _is_source_file(item, tournament_dir, stem):
            continue
        if item.is_dir():
            shutil.rmtree(item)
            print(f"  removed: {item.name}/")
        else:
            item.unlink()
            print(f"  removed: {item.name}")
        removed += 1
    print(f"Cleaned {removed} items")


def do_archive(tournament_dir, stem):
    """Move all generated output to z.archive/<timestamp>/."""
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    archive_dir = tournament_dir / "z.archive" / timestamp
    archive_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    for item in sorted(tournament_dir.iterdir()):
        if _is_source_file(item, tournament_dir, stem):
            continue
        dst = archive_dir / item.name
        shutil.move(str(item), str(dst))
        print(f"  archived: {item.name}")
        moved += 1
    print(f"Archived {moved} items to z.archive/{timestamp}/")


def copy_shared_files(tournament_dir, stem, model_dir):
    """Copy shared plan/catalog/room files into a model subfolder."""
    model_dir.mkdir(parents=True, exist_ok=True)
    for suffix in SHARED_SUFFIXES:
        src = tournament_dir / f"{stem}{suffix}"
        dst = model_dir / f"{stem}{suffix}"
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)


def run_curation(stem, model_dir, model, force=False, timeout=300):
    """Run generate_curation.py for a model."""
    curation_path = model_dir / f"{stem}_curation.json"
    if curation_path.exists() and not force:
        print(f"    curation: skip (exists)")
        return True, 0

    cmd = [
        sys.executable, str(PROJECT_DIR / "generate_curation.py"), stem,
        "--output-dir", str(model_dir.resolve()),
        "--model", model,
        "--timeout", str(timeout),
        "--report",
        "--force",
    ]
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30)
    duration = round(time.time() - t0, 1)

    if result.returncode != 0:
        print(f"    curation: FAILED ({duration}s)")
        print(f"      {result.stderr.strip()[:200]}")
        print(f"      {result.stdout.strip()[:200]}")
        return False, duration

    print(f"    curation: done ({duration}s)")
    return True, duration


def run_arrangement(stem, model_dir, model, force=False, timeout=600):
    """Run generate_arrangement.py for a model."""
    placement_path = model_dir / f"{stem}_placement.json"
    if placement_path.exists() and not force:
        print(f"    arrangement: skip (exists)")
        return True, 0

    cmd = [
        sys.executable, str(PROJECT_DIR / "generate_arrangement.py"), stem,
        "--output-dir", str(model_dir.resolve()),
        "--model", model,
        "--timeout", str(timeout),
        "--report",
        "--force",
    ]
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30)
    duration = round(time.time() - t0, 1)

    if result.returncode != 0:
        print(f"    arrangement: FAILED ({duration}s)")
        print(f"      {result.stderr.strip()[:200]}")
        print(f"      {result.stdout.strip()[:200]}")
        return False, duration

    print(f"    arrangement: done ({duration}s)")
    return True, duration


def run_place(stem, model_dir, model, tournament_dir):
    """Run place.py, then move layout JSON to root and copy GLBs to root products/."""
    placement_path = model_dir / f"{stem}_placement.json"
    if not placement_path.exists():
        print(f"    place: skip (no placement)")
        return False, 0

    # place.py writes GLBs to model_dir/products/ temporarily
    (model_dir / "products").mkdir(exist_ok=True)

    cmd = [
        sys.executable, str(PLACER_DIR / "place.py"),
        str(placement_path.resolve()),
        "--dir", str(model_dir.resolve()),
        "--catalog", str(CATALOG_DIR),
    ]
    t0 = time.time()
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120,
        cwd=str(PLACER_DIR),
    )
    duration = round(time.time() - t0, 1)

    if result.returncode != 0:
        print(f"    place: FAILED ({duration}s)")
        print(f"      {result.stderr.strip()[:300]}")
        print(f"      {result.stdout.strip()[:300]}")
        return False, duration

    # Rename layout to include model name and move to tournament root
    default_layout = model_dir / f"{stem}.layout.json"
    model_layout = tournament_dir / f"{stem}.{model}.layout.json"
    if default_layout.exists():
        shutil.move(str(default_layout), str(model_layout))
        print(f"    layout: {model_layout.name}")

    # Copy GLBs from subfolder products/ to root products/
    root_products = tournament_dir / "products"
    root_products.mkdir(exist_ok=True)
    sub_products = model_dir / "products"
    if sub_products.exists():
        for glb in sub_products.glob("*.glb"):
            dst = root_products / glb.name
            if not dst.exists():
                shutil.copy2(glb, dst)
        # Clean up subfolder products/
        shutil.rmtree(sub_products, ignore_errors=True)

    warn_count = sum(1 for l in result.stdout.splitlines() if l.strip().startswith("- "))
    print(f"    place: done ({duration}s, {warn_count} warnings)")
    return True, duration


def collect_report(stem, model_dir, model, timings):
    """Collect stats from output files for the report."""
    stats = {
        "model": model,
        "curate_time": timings.get("curate", 0),
        "arrange_time": timings.get("arrange", 0),
        "place_time": timings.get("place", 0),
        "total_time": round(sum(timings.values()), 1),
    }

    curation_path = model_dir / f"{stem}_curation.json"
    if curation_path.exists():
        curation = json.loads(curation_path.read_text())
        roles = curation.get("roles", curation if isinstance(curation, list) else [])
        stats["roles"] = len(roles)
        stats["rooms_curated"] = len(set(r.get("room", "") for r in roles))

    placement_path = model_dir / f"{stem}_placement.json"
    if placement_path.exists():
        placement = json.loads(placement_path.read_text())
        items = placement.get("items", [])
        stats["items_placed"] = len(items)
        stats["rooms_placed"] = len(set(i.get("room", "") for i in items))

    # Collect LLM-level stats from report files
    curate_report = model_dir / f"{stem}_report.curation.json"
    if curate_report.exists():
        r = json.loads(curate_report.read_text())
        stats["curate_llm_time"] = r.get("duration_s", 0)
        stats["curate_prompt_chars"] = r.get("prompt_chars", 0)
        if r.get("usage"):
            stats["curate_input_tokens"] = r["usage"].get("input_tokens", 0)
            stats["curate_output_tokens"] = r["usage"].get("output_tokens", 0)

    arrange_report = model_dir / f"{stem}_report.arrange.json"
    if arrange_report.exists():
        r = json.loads(arrange_report.read_text())
        stats["arrange_llm_time"] = r.get("total_duration_s", 0)
        # Sum token usage across all room calls
        arrange_in = 0
        arrange_out = 0
        for stage in r.get("stage2", []):
            if stage.get("usage"):
                arrange_in += stage["usage"].get("input_tokens", 0)
                arrange_out += stage["usage"].get("output_tokens", 0)
        if arrange_in or arrange_out:
            stats["arrange_input_tokens"] = arrange_in
            stats["arrange_output_tokens"] = arrange_out

    # Totals
    total_in = stats.get("curate_input_tokens", 0) + stats.get("arrange_input_tokens", 0)
    total_out = stats.get("curate_output_tokens", 0) + stats.get("arrange_output_tokens", 0)
    if total_in or total_out:
        stats["total_input_tokens"] = total_in
        stats["total_output_tokens"] = total_out

    return stats


def fmt_tokens(n):
    """Format token count as compact string (e.g. 125k, 1.2k)."""
    if not n:
        return "-"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def fmt_clock(seconds):
    """Format seconds as human-readable duration (e.g. 2m 15s, 1h 3m)."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins}m"


def print_report(all_stats, clock_total=None):
    """Print a summary comparison table."""
    print("\n" + "=" * 100)
    print("TOURNAMENT RESULTS")
    print("=" * 100)

    header = (
        f"{'Model':<20} {'Curate':>7} {'Arrange':>8} {'Place':>6} {'Clock':>8}"
        f" {'Roles':>6} {'Items':>6} {'Tok In':>8} {'Tok Out':>8}"
    )
    print(header)
    print("-" * 100)

    for s in sorted(all_stats, key=lambda x: x.get("clock_time", x["total_time"])):
        curate = f"{s['curate_time']}s" if s['curate_time'] else "skip"
        arrange = f"{s['arrange_time']}s" if s['arrange_time'] else "skip"
        place = f"{s['place_time']}s" if s['place_time'] else "fail"
        clock = fmt_clock(s.get('clock_time', s['total_time']))
        roles = str(s.get('roles', '-'))
        items = str(s.get('items_placed', '-'))
        tok_in = fmt_tokens(s.get('total_input_tokens'))
        tok_out = fmt_tokens(s.get('total_output_tokens'))
        print(
            f"{s['model']:<20} {curate:>7} {arrange:>8} {place:>6} {clock:>8}"
            f" {roles:>6} {items:>6} {tok_in:>8} {tok_out:>8}"
        )

    print("-" * 100)
    if clock_total is not None:
        print(f"Total clock time: {fmt_clock(clock_total)} ({clock_total}s)")
    print("=" * 100)


def discover_stems(tournament_dir):
    """Find all plan stems in a tournament directory by looking for *_plan.css files."""
    stems = []
    for css_file in sorted(tournament_dir.glob("*_plan.css")):
        stem = css_file.stem.replace("_plan", "")
        # Verify the other required files exist
        if (tournament_dir / f"{stem}_catalog.json").exists() and (tournament_dir / f"{stem}.json").exists():
            stems.append(stem)
    return stems


def run_tournament(stem, tournament_dir, models, force=False, skip_place=False, timeout=300):
    """Run the full tournament for a single plan stem. Returns (all_stats, clock_total)."""
    # Ensure root products/ exists
    (tournament_dir / "products").mkdir(exist_ok=True)

    print(f"Tournament: {stem} in {tournament_dir}/")
    print(f"Models: {', '.join(models)}")
    print()

    all_stats = []
    clock_start = time.time()

    for model in models:
        model_dir = tournament_dir / model
        print(f"[{model}]")
        model_clock_start = time.time()

        copy_shared_files(tournament_dir, stem, model_dir)

        timings = {}

        # Curation
        ok, dur = run_curation(stem, model_dir, model, force=force, timeout=timeout)
        timings["curate"] = dur
        if not ok:
            stats = collect_report(stem, model_dir, model, timings)
            stats["clock_time"] = round(time.time() - model_clock_start, 1)
            all_stats.append(stats)
            print()
            continue

        # Arrangement
        ok, dur = run_arrangement(stem, model_dir, model, force=force, timeout=timeout)
        timings["arrange"] = dur
        if not ok:
            stats = collect_report(stem, model_dir, model, timings)
            stats["clock_time"] = round(time.time() - model_clock_start, 1)
            all_stats.append(stats)
            print()
            continue

        # Place
        if not skip_place:
            ok, dur = run_place(stem, model_dir, model, tournament_dir)
            timings["place"] = dur

        stats = collect_report(stem, model_dir, model, timings)
        stats["clock_time"] = round(time.time() - model_clock_start, 1)
        all_stats.append(stats)
        print()

    clock_total = round(time.time() - clock_start, 1)
    return all_stats, clock_total


def main():
    parser = argparse.ArgumentParser(description="Run model tournament")
    parser.add_argument(
        "input",
        help="Tournament dir/stem (e.g. model-tournament/amber_light) or just a directory for batch mode",
    )
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS, help="Models to test")
    parser.add_argument("--force", action="store_true", help="Regenerate all outputs")
    parser.add_argument("--skip-place", action="store_true", help="Skip place.py step")
    parser.add_argument("--timeout", type=int, default=300, help="Timeout per LLM call (default: 300)")
    parser.add_argument("--clean", action="store_true", help="Delete all generated output and exit")
    parser.add_argument("--archive", action="store_true", help="Move all generated output to z.archive/<timestamp>/ and exit")
    args = parser.parse_args()

    input_path = Path(args.input)

    # Determine if input is a directory (batch mode) or dir/stem (single mode)
    if input_path.is_dir():
        tournament_dir = input_path
        stems = discover_stems(tournament_dir)
        if not stems:
            print(f"No plan stems found in {tournament_dir}/ (need *_plan.css + *_catalog.json + *.json)")
            sys.exit(1)
    else:
        tournament_dir = input_path.parent
        stems = [input_path.name]

    # Handle clean/archive before anything else
    if args.clean:
        print(f"Cleaning {tournament_dir}/")
        for stem in stems:
            do_clean(tournament_dir, stem)
        return
    if args.archive:
        print(f"Archiving {tournament_dir}/")
        for stem in stems:
            do_archive(tournament_dir, stem)
        return

    # Validate stems
    for stem in stems:
        missing = []
        for suffix in ["_plan.css", "_catalog.json", ".json"]:
            f = tournament_dir / f"{stem}{suffix}"
            if not f.exists():
                missing.append(str(f))
        if missing:
            print(f"Missing required files for {stem}: {missing}")
            sys.exit(1)

    if len(stems) > 1:
        print(f"Batch mode: {len(stems)} plans in {tournament_dir}/")
        print(f"Plans: {', '.join(stems)}\n")

    grand_stats = {}
    grand_clock_start = time.time()

    for stem in stems:
        if len(stems) > 1:
            print(f"{'=' * 100}")
            print(f"PLAN: {stem}")
            print(f"{'=' * 100}\n")

        all_stats, clock_total = run_tournament(
            stem, tournament_dir, args.models,
            force=args.force, skip_place=args.skip_place, timeout=args.timeout,
        )

        print_report(all_stats, clock_total)

        report_data = {
            "plan": stem,
            "clock_total_s": clock_total,
            "models": all_stats,
        }
        report_path = tournament_dir / f"{stem}_tournament.json"
        report_path.write_text(json.dumps(report_data, indent=2) + "\n")
        print(f"\nDetailed report: {report_path}")
        grand_stats[stem] = all_stats

        if len(stems) > 1:
            print("\n")

    if len(stems) > 1:
        grand_total = round(time.time() - grand_clock_start, 1)
        print(f"{'=' * 100}")
        print(f"ALL PLANS COMPLETE — {len(stems)} plans, {len(args.models)} models, {fmt_clock(grand_total)}")
        print(f"{'=' * 100}")


if __name__ == "__main__":
    main()
