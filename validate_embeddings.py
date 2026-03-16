"""Validate embedding generation: structural integrity + semantic coherence.

Generates embeddings for a test catalog, then validates:
1. Structural: correct type, dimensionality, no NaN/null values, unit norm range
2. Semantic: cosine similarity triangulation (related items closer than unrelated)

Usage:
    python validate_embeddings.py "C:/Users/Walker/Desktop/New folder"
    python validate_embeddings.py products    # use main catalog
"""

import argparse
import json
import math
import sys
from pathlib import Path

from build_catalog import generate_embedding, EMBEDDING_DIMENSIONS
from rag_filter import cosine_similarity

# Load .env for GEMINI_API_KEY
from llm_utils import _load_dotenv
_load_dotenv()


def validate_structure(item_no, embedding):
    """Structural validation: type, dimensionality, numeric values."""
    errors = []

    if not isinstance(embedding, list):
        return [f"[{item_no}] embedding is {type(embedding).__name__}, expected list"]

    if len(embedding) != EMBEDDING_DIMENSIONS:
        errors.append(f"[{item_no}] expected {EMBEDDING_DIMENSIONS}d, got {len(embedding)}d")

    for i, v in enumerate(embedding):
        if not isinstance(v, (int, float)):
            errors.append(f"[{item_no}] element [{i}] is {type(v).__name__}, expected number")
            break
        if math.isnan(v) or math.isinf(v):
            errors.append(f"[{item_no}] element [{i}] is {v}")
            break

    # Check that the vector isn't all zeros (degenerate)
    norm = math.sqrt(sum(v * v for v in embedding))
    if norm < 0.01:
        errors.append(f"[{item_no}] near-zero norm ({norm:.6f}) — likely degenerate")

    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate embedding generation")
    parser.add_argument("source", help="Directory with product JSON + JPG files")
    args = parser.parse_args()

    source_dir = Path(args.source)
    if not source_dir.is_dir():
        print(f"Not a directory: {source_dir}")
        sys.exit(1)

    # Find products (JSON files with dimensions)
    products = {}
    for json_file in sorted(source_dir.glob("*.json")):
        if json_file.stem.endswith(".catalog") or json_file.stem.endswith(".embeddings"):
            continue
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if "dimensions" not in data:
            continue
        img = json_file.with_suffix(".jpg")
        if not img.exists():
            continue
        item_no = data.get("item_no", json_file.stem)
        products[item_no] = {
            "name": data.get("name", "?"),
            "categories": data.get("categories", []),
            "metadata": json_file,
            "image": img,
        }

    if not products:
        print(f"No products with images found in {source_dir}")
        sys.exit(1)

    print(f"Found {len(products)} products\n")

    # --- Generate embeddings ---
    embeddings = {}
    all_structural_errors = []

    for item_no, info in products.items():
        print(f"  [{item_no}] {info['name']}...", end="", flush=True)
        embedding, duration, error = generate_embedding(info["metadata"], info["image"])

        if error:
            print(f" ERROR: {error} ({duration}s)")
            continue

        # Structural validation
        errors = validate_structure(item_no, embedding)
        if errors:
            print(f" STRUCTURAL FAIL")
            for e in errors:
                print(f"    {e}")
            all_structural_errors.extend(errors)
            continue

        norm = math.sqrt(sum(v * v for v in embedding))
        print(f" OK ({len(embedding)}d, norm={norm:.4f}, {duration}s)")
        embeddings[item_no] = {
            "name": info["name"],
            "categories": info["categories"],
            "embedding": embedding,
        }

    print(f"\n{'='*60}")
    print(f"Structural: {len(embeddings)}/{len(products)} passed")
    if all_structural_errors:
        for e in all_structural_errors:
            print(f"  FAIL: {e}")

    if len(embeddings) < 2:
        print("\nNeed at least 2 embeddings for semantic validation")
        sys.exit(1 if all_structural_errors else 0)

    # --- Semantic validation: pairwise similarity matrix ---
    print(f"\n{'='*60}")
    print("Semantic: pairwise cosine similarity\n")

    items = list(embeddings.items())

    # Print similarity matrix
    max_name_len = max(len(v["name"]) for v in embeddings.values())
    header = " " * (max_name_len + 12)
    for item_no, _ in items:
        header += f"{item_no:>12s}"
    print(header)

    for i, (id_a, data_a) in enumerate(items):
        row = f"  {id_a:>8s} {data_a['name']:<{max_name_len}s}"
        for j, (id_b, data_b) in enumerate(items):
            sim = cosine_similarity(data_a["embedding"], data_b["embedding"])
            row += f"{sim:>12.4f}"
        print(row)

    # --- Semantic triangulation tests ---
    print(f"\n{'='*60}")
    print("Semantic: triangulation tests\n")

    # Group by broad category for automatic test generation
    category_groups = {}
    for item_no, data in embeddings.items():
        cats = data["categories"]
        broad = cats[0] if cats else "unknown"
        category_groups.setdefault(broad, []).append(item_no)

    tests_passed = 0
    tests_failed = 0

    # For each pair in the same category, verify they're closer to each other
    # than to items in different categories
    for cat, members in category_groups.items():
        if len(members) < 2:
            continue
        others = [item_no for item_no in embeddings if item_no not in members]
        if not others:
            continue

        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                sim_related = cosine_similarity(
                    embeddings[a]["embedding"], embeddings[b]["embedding"]
                )

                for other in others:
                    sim_unrelated = cosine_similarity(
                        embeddings[a]["embedding"], embeddings[other]["embedding"]
                    )

                    passed = sim_related > sim_unrelated
                    status = "PASS" if passed else "FAIL"
                    if passed:
                        tests_passed += 1
                    else:
                        tests_failed += 1

                    print(f"  {status}: sim({embeddings[a]['name']}, {embeddings[b]['name']}) = {sim_related:.4f}"
                          f"  >  sim({embeddings[a]['name']}, {embeddings[other]['name']}) = {sim_unrelated:.4f}")

    if tests_passed + tests_failed == 0:
        print("  (no triangulation tests possible — need items from different categories)")
    else:
        print(f"\nTriangulation: {tests_passed}/{tests_passed + tests_failed} passed")

    sys.exit(1 if all_structural_errors or tests_failed else 0)


if __name__ == "__main__":
    main()
