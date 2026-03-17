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

import argparse
import json
import os
import sys
import time
from pathlib import Path

from llm_utils import call_llm_vision, extract_json


DEFAULT_SOURCE = "products"
REQUIRED_PROFILE_KEYS = {"tier", "placement", "categories", "tags"}

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
    if "dimensions" not in data and "measured_dimensions" not in data:
        return None
    return {
        "item_no": data.get("item_no") or data.get("tcin"),
        "name": data["name"],
        "color": data.get("color", ""),
    }


def validate_profile(profile):
    """Validate profile shape before writing template output."""
    if not isinstance(profile, dict):
        return False, "not an object"

    missing = REQUIRED_PROFILE_KEYS - set(profile.keys())
    if missing:
        return False, f"missing keys {missing}"

    if profile["tier"] not in {"anchor", "accent", "fill"}:
        return False, f"invalid tier: {profile['tier']}"
    if profile["placement"] not in {"floor", "wall", "surface"}:
        return False, f"invalid placement: {profile['placement']}"
    if not isinstance(profile["categories"], list) or not all(isinstance(x, str) for x in profile["categories"]):
        return False, "categories must be a string array"
    if not isinstance(profile["tags"], list) or not all(isinstance(x, str) for x in profile["tags"]):
        return False, "tags must be a string array"

    return True, None


def generate_profile(metadata_path, image_path, item_no, model="sonnet"):
    """Generate a product profile from image + metadata via LLM vision call."""
    with open(metadata_path, encoding="utf-8") as f:
        metadata_content = f.read()

    prompt_template = PROMPT_PATH.read_text(encoding="utf-8")
    prompt = prompt_template.format(
        image_path=image_path.resolve(),
        metadata_path=metadata_path.resolve(),
        metadata_content=metadata_content,
    )

    parsed, raw, duration, error = call_llm_vision(
        prompt, image_path, model=model, timeout=120, stage="profile",
    )

    if error:
        print(f"  {error} ({item_no}, {duration}s)")
        return None

    if parsed and isinstance(parsed, dict) and "tier" in parsed:
        return parsed

    # Fallback: brute-force search for a JSON object with "tier"
    if raw:
        for candidate_start in range(len(raw)):
            if raw[candidate_start] == "{":
                for candidate_end in range(len(raw), candidate_start, -1):
                    if raw[candidate_end - 1] == "}":
                        try:
                            obj = json.loads(raw[candidate_start:candidate_end])
                            if isinstance(obj, dict) and "tier" in obj:
                                return obj
                        except json.JSONDecodeError:
                            continue
                break

    print(f"  PARSE ERROR ({item_no})")
    if raw:
        print(f"    response: {raw[:300]}")
    return None


EMBEDDING_MODEL = "models/gemini-embedding-2-preview"
EMBEDDING_DIMENSIONS = 768


def generate_embedding(metadata_path, image_path):
    """Generate a multimodal embedding from product image + metadata via Gemini.

    Returns (embedding_list, duration_s, error).
    """
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        return None, 0, "Google genai SDK not installed. Run: pip install google-genai"

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, 0, "GEMINI_API_KEY not set"

    with open(metadata_path, encoding="utf-8") as f:
        metadata_text = f.read()

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    client = genai.Client(api_key=api_key)
    t0 = time.time()
    try:
        response = client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=[
                genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                metadata_text,
            ],
            config=genai_types.EmbedContentConfig(
                output_dimensionality=EMBEDDING_DIMENSIONS,
            ),
        )
    except Exception as e:
        return None, round(time.time() - t0, 1), f"EMBEDDING_ERROR: {e}"

    duration = round(time.time() - t0, 1)
    if response.embeddings and response.embeddings[0].values:
        return list(response.embeddings[0].values), duration, None
    return None, duration, "EMBEDDING_EMPTY"


def write_embedding(source_dir, item_no, stem, embedding):
    """Write per-product <stem>.embeddings.json alongside vendor files."""
    data = {
        "item_no": item_no,
        "model": EMBEDDING_MODEL,
        "dimensions": len(embedding),
        "embedding": [round(v, 6) for v in embedding],
    }
    path = source_dir / f"{stem}.embeddings.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return path


def write_template(source_dir, item_no, stem, product, profile):
    """Write per-product <stem>.catalog.json alongside vendor files."""
    template = {
        "products": [product],
        "profiles": [{
            "item_no": item_no,
            **profile,
        }],
    }

    path = source_dir / f"{stem}.catalog.json"
    with open(path, "w", encoding="utf-8") as f:
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
    parser.add_argument(
        "--skip-embeddings", action="store_true",
        help="Skip embedding generation (profile only)",
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

        emb_path = source_dir / f"{stem}.embeddings.json"
        needs_embedding = (not args.skip_embeddings and not emb_path.exists()
                           and files["image"] is not None)

        if template_path.exists() and not args.force:
            if needs_embedding:
                # Profile exists but embedding missing — generate embedding only
                print(f"  [{item_no}] embedding only...", end="", flush=True)
                embedding, emb_duration, emb_error = generate_embedding(
                    files["metadata"], files["image"],
                )
                if emb_error:
                    print(f" {emb_error} ({emb_duration}s)")
                elif embedding:
                    write_embedding(source_dir, item_no, stem, embedding)
                    print(f" done ({len(embedding)}d, {emb_duration}s)")
            else:
                print(f"  [{item_no}] skip (exists)")
            skipped += 1
            continue

        product = extract_product(files["metadata"])
        if product is None:
            print(f"  [{item_no}] skip (no dimensions)")
            skipped += 1
            continue

        print(f"  [{item_no}] {product['name']}")

        if files["image"] is None:
            print("    no image - skipping profile generation")
            failed += 1
            continue

        print(f"    generating profile ({args.model})...", end="", flush=True)

        profile = None
        for attempt in range(2):
            result = generate_profile(
                files["metadata"], files["image"], item_no, model=args.model,
            )
            if result is None:
                if attempt == 0:
                    print(" retrying...", end="", flush=True)
                continue
            # Unwrap nested result wrapper if present
            if "result" in result and "tier" not in result:
                result = result["result"]
            valid, validation_error = validate_profile(result)
            if valid:
                profile = result
                break
            if attempt == 0:
                print(f" invalid ({validation_error}), retrying...", end="", flush=True)

        if profile is None:
            print(" failed")
            failed += 1
            continue

        print(" done")

        print(f"    tier={profile['tier']}  placement={profile['placement']}  categories={profile['categories']}  tags={profile['tags']}")

        path = write_template(source_dir, item_no, stem, product, profile)
        print(f"    -> {path}")

        # Generate multimodal embedding (requires GEMINI_API_KEY)
        if not args.skip_embeddings:
            emb_path = source_dir / f"{stem}.embeddings.json"
            if emb_path.exists() and not args.force:
                print(f"    embedding: skip (exists)")
            else:
                print(f"    embedding...", end="", flush=True)
                embedding, emb_duration, emb_error = generate_embedding(
                    files["metadata"], files["image"],
                )
                if emb_error:
                    print(f" {emb_error} ({emb_duration}s)")
                elif embedding:
                    emb_out = write_embedding(source_dir, item_no, stem, embedding)
                    print(f" done ({len(embedding)}d, {emb_duration}s)")
                    print(f"    -> {emb_out}")

        built += 1

    print(f"\nDone: {built} built, {skipped} skipped, {failed} failed")

    # Merge all per-product embeddings into a single catalog-level file
    if not args.skip_embeddings:
        emb_files = sorted(source_dir.glob("*.embeddings.json"))
        if emb_files:
            merged = {}
            for emb_path in emb_files:
                data = json.loads(emb_path.read_text(encoding="utf-8"))
                item_no = data.get("item_no")
                if item_no:
                    merged[item_no] = {
                        "model": data.get("model", ""),
                        "dimensions": data.get("dimensions", 0),
                        "embedding": data.get("embedding", []),
                    }
            merged_path = source_dir / "catalog.embeddings.json"
            with open(merged_path, "w", encoding="utf-8") as f:
                json.dump(merged, f)
            print(f"\n-> {merged_path} ({len(merged)} embeddings)")

    if built > 0 or skipped > 0:
        print("Run quantize_plan.py to merge templates and generate footprints")


if __name__ == "__main__":
    main()
