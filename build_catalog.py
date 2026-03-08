"""
Build product catalog templates from vendor files.

Stage 1 (deterministic): Extract product entries from metadata JSON.
Stage 2 (VLM via Claude Code CLI): Generate profiles from images + metadata.

Usage:
    python build_catalog.py                        # build per-product templates
    python build_catalog.py --force                # regenerate existing templates
    python build_catalog.py --model opus           # use a specific model

Source files:  products/<stem>.{json,jpg,glb}
Output:        catalog/<item_no>/<stem>_catalog.json

Templates are merged automatically by quantize_plan.py at plan quantization time.
"""

import json
import subprocess
import sys
import argparse
from pathlib import Path


DEFAULT_SOURCE = "products"
DEFAULT_OUTPUT = "catalog"

PROFILE_SCHEMA = json.dumps({
    "type": "object",
    "required": ["tier", "categories", "tags"],
    "additionalProperties": False,
    "properties": {
        "tier": {
            "type": "string",
            "enum": ["anchor", "accent", "fill"],
        },
        "categories": {
            "type": "array",
            "items": {"type": "string"},
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
})

PROMPT_PATH = Path(__file__).parent / "prompts" / "profile.md"


def find_products(source_dir):
    """Group vendor files by item number (last segment of filename stem)."""
    products = {}
    for json_file in sorted(source_dir.glob("*.json")):
        stem = json_file.stem
        item_no = stem.rsplit("_", 1)[-1]
        if not item_no.isdigit():
            continue
        img_file = json_file.with_suffix(".jpg")
        products[item_no] = {
            "stem": stem,
            "metadata": json_file,
            "image": img_file if img_file.exists() else None,
        }
    return products


def extract_product(metadata_path):
    """Deterministically extract product entry from vendor metadata."""
    with open(metadata_path) as f:
        data = json.load(f)
    if "dimensions" not in data:
        return None, None
    return {
        "item_no": data["item_no"],
        "name": data["name"],
        "color": data.get("color", ""),
    }, data


def generate_profile(metadata_path, image_path, item_no, model="sonnet"):
    """Call Claude Code CLI to generate a product profile from image + metadata."""
    prompt_template = PROMPT_PATH.read_text(encoding="utf-8")
    prompt = prompt_template.format(
        image_path=image_path.resolve(),
        metadata_path=metadata_path.resolve(),
    )

    cmd = [
        "claude", "--print",
        "--model", model,
        "--allowedTools", "Read",
        "--json-schema", PROFILE_SCHEMA,
        prompt,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        print(f"  TIMEOUT: {item_no}")
        return None

    if result.returncode != 0:
        print(f"  ERROR ({item_no}): {result.stderr.strip()[:200]}")
        return None

    text = result.stdout.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to extract JSON object from mixed output
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        print(f"  PARSE ERROR ({item_no}): {text[:200]}")
        return None


def write_template(output_dir, item_no, stem, product, profile):
    """Write per-product <stem>_catalog.json."""
    item_dir = output_dir / item_no
    item_dir.mkdir(parents=True, exist_ok=True)

    template = {
        "products": [product],
        "profiles": [{
            "item_no": item_no,
            **profile,
        }],
    }

    path = item_dir / f"{stem}_catalog.json"
    with open(path, "w") as f:
        json.dump(template, f, indent=2)
    return path



def main():
    parser = argparse.ArgumentParser(
        description="Build product catalog templates from vendor files"
    )
    parser.add_argument(
        "--source", default=DEFAULT_SOURCE,
        help=f"Vendor files directory (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT,
        help=f"Output catalog directory (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate templates even if they already exist",
    )
    parser.add_argument(
        "--model", default="sonnet",
        help="Model for VLM profile generation (default: sonnet)",
    )
    args = parser.parse_args()

    output_dir = Path(args.output)
    source_dir = Path(args.source)
    if not source_dir.is_dir():
        print(f"Source directory not found: {source_dir}")
        sys.exit(1)

    vendor_products = find_products(source_dir)
    if not vendor_products:
        print(f"No products found in {source_dir}")
        sys.exit(1)

    print(f"Found {len(vendor_products)} products in {source_dir}/\n")

    built = 0
    skipped = 0
    failed = 0

    for item_no, files in vendor_products.items():
        stem = files["stem"]
        template_path = output_dir / item_no / f"{stem}_catalog.json"

        if template_path.exists() and not args.force:
            print(f"  [{item_no}] skip (exists)")
            skipped += 1
            continue

        # Stage 1: deterministic product extraction
        product, raw_data = extract_product(files["metadata"])
        if product is None:
            print(f"  [{item_no}] skip (no dimensions)")
            skipped += 1
            continue

        print(f"  [{item_no}] {product['name']}")

        # Stage 2: VLM profile generation
        if files["image"] is None:
            print(f"    no image — skipping profile generation")
            failed += 1
            continue

        print(f"    generating profile ({args.model})...", end="", flush=True)
        profile = generate_profile(
            files["metadata"], files["image"], item_no, model=args.model,
        )

        if profile is None:
            print(" failed")
            failed += 1
            continue

        print(f" done")
        print(f"    tier={profile['tier']}  categories={profile['categories']}  tags={profile['tags']}")

        # Write template
        path = write_template(output_dir, item_no, stem, product, profile)
        print(f"    → {path}")
        built += 1

    print(f"\nDone: {built} built, {skipped} skipped, {failed} failed")

    if built > 0 or skipped > 0:
        print(f"Run quantize_plan.py to merge templates and generate footprints")


if __name__ == "__main__":
    main()
