"""
Build product catalog templates from vendor files.

Stage 1 (deterministic): Extract product entries from metadata JSON.
Stage 2 (VLM via Claude Code CLI): Generate profiles from images + metadata.

Usage:
    python build_catalog.py                        # build per-product templates
    python build_catalog.py --force                # regenerate existing templates
    python build_catalog.py --model opus           # use a specific model

Source files:  products/<stem>.{json,jpg,glb}
Output:        products/<stem>.catalog.json (alongside vendor files)

Templates are merged automatically by quantize_plan.py at plan quantization time.
"""

import json
import os
import subprocess
import sys
import argparse
from pathlib import Path


DEFAULT_SOURCE = "products"

PROFILE_SCHEMA = json.dumps({
    "type": "object",
    "required": ["tier", "placement", "categories", "tags"],
    "additionalProperties": False,
    "properties": {
        "tier": {
            "type": "string",
            "enum": ["anchor", "accent", "fill"],
        },
        "placement": {
            "type": "string",
            "enum": ["floor", "wall", "surface"],
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
        if stem.endswith(".catalog"):
            continue
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
    with open(metadata_path, encoding="utf-8") as f:
        data = json.load(f)
    if "dimensions" not in data:
        return None, None
    return {
        "item_no": data.get("item_no") or data.get("tcin"),
        "name": data["name"],
        "color": data.get("color", ""),
    }, data


def generate_profile(metadata_path, image_path, item_no, model="sonnet"):
    """Call Claude Code CLI to generate a product profile from image + metadata."""
    # Read metadata and inline it so the model only needs Read for the image
    with open(metadata_path) as f:
        metadata_content = f.read()

    prompt_template = PROMPT_PATH.read_text(encoding="utf-8")
    prompt = prompt_template.format(
        image_path=image_path.resolve(),
        metadata_path=metadata_path.resolve(),
        metadata_content=metadata_content,
    )

    cmd = [
        "claude", "--print",
        "--model", model,
        "--allowedTools", "Read",
    ]

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        print(f"  TIMEOUT: {item_no}")
        return None

    if result.returncode != 0:
        print(f"  ERROR ({item_no}) exit={result.returncode}")
        print(f"    stderr: {result.stderr.strip()[:300]}")
        print(f"    stdout: {result.stdout.strip()[:300]}")
        return None

    text = result.stdout.strip()
    if not text:
        print(f"  EMPTY RESPONSE ({item_no})")
        print(f"    stderr: {result.stderr.strip()[:300]}")
        return None

    def _extract_json(s):
        """Try to parse JSON, or find a JSON object within a string."""
        try:
            return json.loads(s)
        except (json.JSONDecodeError, TypeError):
            pass
        if isinstance(s, str):
            start = s.find("{")
            end = s.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(s[start:end])
                except json.JSONDecodeError:
                    pass
        return None

    parsed = _extract_json(text)

    # --output-format json wraps in {"type":"result", "result": ...}
    if isinstance(parsed, dict) and "result" in parsed and "tier" not in parsed:
        inner = parsed["result"]
        parsed = _extract_json(inner)

    if parsed and isinstance(parsed, dict) and "tier" in parsed:
        return parsed

    # Last resort: scan the full text for a JSON object with "tier"
    for candidate_start in range(len(text)):
        if text[candidate_start] == "{":
            for candidate_end in range(len(text), candidate_start, -1):
                if text[candidate_end - 1] == "}":
                    try:
                        obj = json.loads(text[candidate_start:candidate_end])
                        if isinstance(obj, dict) and "tier" in obj:
                            return obj
                    except json.JSONDecodeError:
                        continue
            break

    print(f"  PARSE ERROR ({item_no})")
    print(f"    stdout: {text[:300]}")
    print(f"    stderr: {result.stderr.strip()[:300]}")
    return None


def write_template(source_dir, item_no, stem, product, profile):
    """Write per-product <stem>_catalog.json alongside vendor files."""
    template = {
        "products": [product],
        "profiles": [{
            "item_no": item_no,
            **profile,
        }],
    }

    path = source_dir / f"{stem}.catalog.json"
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
        "--force", action="store_true",
        help="Regenerate templates even if they already exist",
    )
    parser.add_argument(
        "--model", default="sonnet",
        help="Model for VLM profile generation (default: sonnet)",
    )
    args = parser.parse_args()

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
        template_path = source_dir / f"{stem}.catalog.json"

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
        print(f"    raw: {json.dumps(profile)[:300]}")

        # Unwrap if nested (e.g. --output-format json wraps in {"result": ...})
        if "result" in profile and "tier" not in profile:
            profile = profile["result"]

        print(f"    tier={profile['tier']}  placement={profile['placement']}  categories={profile['categories']}  tags={profile['tags']}")

        # Write template
        path = write_template(source_dir, item_no, stem, product, profile)
        print(f"    → {path}")
        built += 1

    print(f"\nDone: {built} built, {skipped} skipped, {failed} failed")

    if built > 0 or skipped > 0:
        print(f"Run quantize_plan.py to merge templates and generate footprints")


if __name__ == "__main__":
    main()
