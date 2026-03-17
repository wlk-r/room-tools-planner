"""Measure GLB model bounding boxes and write measured_dimensions to vendor metadata.

Loads each .glb file, computes the axis-aligned bounding box, and writes
measured_dimensions into the companion vendor .json metadata file. The original
vendor-listed dimensions field is never modified.

Run before build_catalog.py to ensure products without vendor dimensions can
still get catalog profiles and footprints.

GLTF world space convention: X = width, Y = height (up), Z = depth (front).

Usage:
    python measure_glb.py                    # measure all products/
    python measure_glb.py --source path/to   # measure a specific directory
    python measure_glb.py --force            # re-measure even if measured_dimensions exists
    python measure_glb.py --compare          # compare, flag issues, copy flagged to FLAGGED/
"""

import argparse
import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import struct

import numpy as np

try:
    import trimesh
except ImportError:
    print("trimesh not installed. Run: pip install trimesh")
    sys.exit(1)

try:
    import DracoPy
    HAS_DRACO = True
except ImportError:
    HAS_DRACO = False

DEFAULT_SOURCE = "products"

# Thresholds for flagging
VENDOR_DIFF_PCT = 15       # flag if any axis differs >15% from vendor
FLAT_Y_RATIO = 0.15        # flag wall items if measured Y < 15% of max(X, Z)


def _measure_draco_glb(glb_path):
    """Decode Draco-compressed GLB and return bounding box extents.

    Parses the GLB binary directly, finds all Draco-compressed primitives,
    decodes them with DracoPy, and computes the combined bounding box.
    Returns (x, y, z) extents or None on failure.
    """
    with open(glb_path, "rb") as f:
        magic, version, total = struct.unpack("<III", f.read(12))
        if magic != 0x46546C67:  # 'glTF'
            return None

        # JSON chunk
        chunk_len, chunk_type = struct.unpack("<II", f.read(8))
        gltf_json = json.loads(f.read(chunk_len))

        # Binary chunk
        chunk_len2, chunk_type2 = struct.unpack("<II", f.read(8))
        bin_data = f.read(chunk_len2)

    all_points = []
    for mesh in gltf_json.get("meshes", []):
        for prim in mesh.get("primitives", []):
            draco_ext = prim.get("extensions", {}).get("KHR_draco_mesh_compression")
            if not draco_ext:
                continue
            bv_idx = draco_ext["bufferView"]
            bv = gltf_json["bufferViews"][bv_idx]
            offset = bv.get("byteOffset", 0)
            length = bv["byteLength"]
            draco_bytes = bin_data[offset:offset + length]

            decoded = DracoPy.decode(draco_bytes)
            points = np.array(decoded.points).reshape(-1, 3)
            all_points.append(points)

    if not all_points:
        return None

    combined = np.vstack(all_points)
    extents = combined.max(axis=0) - combined.min(axis=0)
    return float(extents[0]), float(extents[1]), float(extents[2])


def measure_glb(glb_path):
    """Load a GLB file and return (x, y, z) bounding box extents in meters.

    Raw GLTF world space axes: X (left-right), Y (up), Z (front-back).
    No remapping to width/depth/height — downstream code handles that.
    Falls back to DracoPy for Draco-compressed meshes if trimesh returns zeros.
    Returns (x, y, z, error).
    """
    try:
        scene = trimesh.load(str(glb_path), force="scene")
    except Exception as e:
        return None, None, None, str(e)

    try:
        bbox = scene.bounding_box.extents
    except Exception as e:
        return None, None, None, str(e)

    x = round(float(bbox[0]), 4)
    y = round(float(bbox[1]), 4)
    z = round(float(bbox[2]), 4)

    # Draco fallback: trimesh returns zeros for Draco-compressed meshes
    if x == 0 and y == 0 and z == 0:
        if not HAS_DRACO:
            return None, None, None, "DRACO: mesh is Draco-compressed but DracoPy not installed"
        try:
            result = _measure_draco_glb(glb_path)
        except Exception as e:
            return None, None, None, f"DRACO_ERROR: {e}"
        if result is None:
            return None, None, None, "DRACO: no decodable primitives found"
        x = round(result[0], 4)
        y = round(result[1], 4)
        z = round(result[2], 4)

    return x, y, z, None


def _check_vendor_flags(item_no, name, vendor_dims, measured):
    """Check for >VENDOR_DIFF_PCT discrepancy between vendor and measured.

    Compares by magnitude: sorts both sets of 3 values and compares
    largest-to-largest, since axis labeling conventions may differ.
    Returns list of flag dicts or empty list.
    """
    v_vals = sorted([
        vendor_dims.get("width", 0),
        vendor_dims.get("depth", 0),
        vendor_dims.get("height", 0),
    ], reverse=True)
    m_vals = sorted([
        measured.get("x", 0),
        measured.get("y", 0),
        measured.get("z", 0),
    ], reverse=True)

    labels = ["largest", "middle", "smallest"]
    flags = []
    for i, label in enumerate(labels):
        v = v_vals[i]
        m = m_vals[i]
        if v <= 0:
            continue
        pct = (m - v) / v * 100
        if abs(pct) > VENDOR_DIFF_PCT:
            flags.append({
                "item_no": item_no,
                "name": name,
                "flag": "vendor_discrepancy",
                "axis": label,
                "vendor": round(v, 4),
                "measured": round(m, 4),
                "diff_pct": round(pct, 1),
            })
    return flags


def _check_profile_flags(item_no, name, measured, profile):
    """Check for orientation issues using catalog profile data.

    Flags wall-placement items that appear to be lying flat in GLB space
    (measured Y is suspiciously small relative to X/Z extent).

    Returns list of flag dicts or empty list.
    """
    if not profile:
        return []

    placement = profile.get("placement", "")
    if placement != "wall":
        return []

    m_x = measured.get("x", 0)
    m_y = measured.get("y", 0)
    m_z = measured.get("z", 0)

    max_xz = max(m_x, m_z)
    if max_xz <= 0:
        return []

    # Wall item with very small Y (height) relative to its X/Z spread
    # → likely lying flat in GLB space
    if m_y / max_xz < FLAT_Y_RATIO:
        return [{
            "item_no": item_no,
            "name": name,
            "flag": "wall_item_flat",
            "placement": placement,
            "measured_y": round(m_y, 4),
            "measured_max_xz": round(max_xz, 4),
            "y_ratio": round(m_y / max_xz, 3),
            "detail": f"Wall item Y={m_y:.4f}m is only {m_y/max_xz:.0%} of max(X,Z)={max_xz:.4f}m — likely lying flat",
        }]

    return []


def _load_profile(source_dir, stem):
    """Load a .catalog.json profile if it exists. Returns dict or None."""
    catalog_path = source_dir / f"{stem}.catalog.json"
    if not catalog_path.exists():
        return None
    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
        profiles = data.get("profiles", [])
        return profiles[0] if profiles else None
    except (json.JSONDecodeError, IndexError):
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Measure GLB model bounding boxes and write measured_dimensions to vendor metadata"
    )
    parser.add_argument(
        "--source", default=DEFAULT_SOURCE,
        help=f"Vendor files directory (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-measure even if measured_dimensions already exists",
    )
    parser.add_argument(
        "--compare", action="store_true",
        help="Compare vendor vs measured, flag issues, copy flagged GLBs to FLAGGED/",
    )
    parser.add_argument(
        "--workers", type=int, default=8,
        help="Number of parallel threads for measurement (default: 8)",
    )
    args = parser.parse_args()

    source_dir = Path(args.source)
    if not source_dir.is_dir():
        print(f"Source directory not found: {source_dir}")
        sys.exit(1)

    glb_files = sorted(source_dir.glob("*.glb"))
    if not glb_files:
        print(f"No .glb files found in {source_dir}")
        sys.exit(1)

    print(f"Found {len(glb_files)} GLB files in {source_dir}/\n")

    measured_count = 0
    skipped = 0
    failed = 0

    # For --compare: collected per-item data
    all_items = []  # list of {item_no, name, stem, vendor, measured, profile}

    # Pre-filter: separate already-measured (skip) from needs-measurement
    to_measure = []  # list of (glb_path, json_path, metadata, item_no, name, stem)

    for glb_path in glb_files:
        json_path = glb_path.with_suffix(".json")
        if not json_path.exists():
            print(f"  [{glb_path.stem}] skip (no metadata JSON)")
            skipped += 1
            continue

        with open(json_path, encoding="utf-8") as f:
            metadata = json.load(f)

        item_no = metadata.get("item_no", glb_path.stem)
        name = metadata.get("name", "?")
        stem = glb_path.stem

        if "measured_dimensions" in metadata and not args.force:
            md = metadata["measured_dimensions"]
            if args.compare:
                profile = _load_profile(source_dir, stem)
                all_items.append({
                    "item_no": item_no,
                    "name": name,
                    "stem": stem,
                    "vendor": metadata.get("dimensions", {}),
                    "measured": {"x": md.get("x", 0), "y": md.get("y", 0), "z": md.get("z", 0)},
                    "profile": profile,
                })
            skipped += 1
            continue

        to_measure.append((glb_path, json_path, metadata, item_no, name, stem))

    if skipped:
        print(f"  {skipped} already measured, skipping")

    def _measure_one(task):
        """Worker: measure a single GLB and write to its vendor JSON."""
        glb_path, json_path, metadata, item_no, name, stem = task

        x, y, z, error = measure_glb(glb_path)
        if error:
            return {"status": "failed", "item_no": item_no, "error": error}

        metadata["measured_dimensions"] = {
            "x": x,
            "y": y,
            "z": z,
            "unit": "m",
            "source": "glb_bounding_box",
        }

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

        return {
            "status": "ok",
            "item_no": item_no,
            "name": name,
            "stem": stem,
            "vendor": metadata.get("dimensions", {}),
            "measured": {"x": x, "y": y, "z": z},
            "dims_str": f"x={x} y={y} z={z}m",
        }

    if to_measure:
        workers = min(args.workers, len(to_measure))
        print(f"  Measuring {len(to_measure)} GLBs ({workers} workers)...\n")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_measure_one, task): task for task in to_measure}
            for future in as_completed(futures):
                result = future.result()
                if result["status"] == "failed":
                    print(f"  [{result['item_no']}] ERROR: {result['error']}")
                    failed += 1
                else:
                    print(f"  [{result['item_no']}] {result['dims_str']}")
                    measured_count += 1
                    if args.compare:
                        profile = _load_profile(source_dir, result["stem"])
                        all_items.append({
                            "item_no": result["item_no"],
                            "name": result["name"],
                            "stem": result["stem"],
                            "vendor": result["vendor"],
                            "measured": result["measured"],
                            "profile": profile,
                        })

    print(f"\nDone: {measured_count} measured, {skipped} skipped, {failed} failed")

    if not args.compare or not all_items:
        return

    # --- Compare & flag ---
    vendor_flagged = []      # flagged by vendor discrepancy
    profile_only_flagged = []  # flagged by profile check but NOT by vendor check
    vendor_flagged_ids = set()

    for item in all_items:
        vflags = []
        if item["vendor"]:
            vflags = _check_vendor_flags(item["item_no"], item["name"], item["vendor"], item["measured"])
        pflags = _check_profile_flags(item["item_no"], item["name"], item["measured"], item["profile"])

        if vflags:
            vendor_flagged.extend(vflags)
            vendor_flagged_ids.add(item["item_no"])

        if pflags and item["item_no"] not in vendor_flagged_ids:
            profile_only_flagged.extend(pflags)

    # Print vendor discrepancy table (magnitude-sorted comparison)
    print(f"\n{'='*80}")
    print(f"Vendor vs Measured Comparison (sorted by magnitude, largest first)")
    print(f"{'='*80}")
    print(f"  {'item_no':<12s} {'rank':<8s} {'vendor':>8s} {'measured':>8s} {'diff':>8s} {'pct':>7s}")
    print(f"  {'-'*12} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*7}")

    for item in all_items:
        if not item["vendor"]:
            continue
        v_vals = sorted([
            item["vendor"].get("width", 0),
            item["vendor"].get("depth", 0),
            item["vendor"].get("height", 0),
        ], reverse=True)
        m_vals = sorted([
            item["measured"].get("x", 0),
            item["measured"].get("y", 0),
            item["measured"].get("z", 0),
        ], reverse=True)
        for i, label in enumerate(["largest", "middle", "smallest"]):
            v, m = v_vals[i], m_vals[i]
            if v <= 0:
                continue
            diff = m - v
            pct = diff / v * 100
            flag = " ***" if abs(pct) > VENDOR_DIFF_PCT else ""
            print(f"  {item['item_no']:<12s} {label:<8s} {v:>8.4f} {m:>8.4f} {diff:>+8.4f} {pct:>+6.1f}%{flag}")

    # Print vendor-flagged summary
    if vendor_flagged:
        print(f"\n{'='*80}")
        print(f"Flagged by vendor discrepancy (>{VENDOR_DIFF_PCT}%): {len(vendor_flagged)} issues across {len(vendor_flagged_ids)} products")
        print(f"{'='*80}")
        for f in vendor_flagged:
            print(f"  {f['item_no']} {f['name']}: {f['axis']} vendor={f['vendor']} measured={f['measured']} ({f['diff_pct']:+.1f}%)")
    else:
        print(f"\nNo vendor discrepancies >{VENDOR_DIFF_PCT}%")

    # Print profile-only flags (not caught by vendor check)
    profile_only_ids = {f["item_no"] for f in profile_only_flagged}
    if profile_only_flagged:
        print(f"\n{'='*80}")
        print(f"Flagged by profile check ONLY (not caught by vendor comparison): {len(profile_only_flagged)} issues across {len(profile_only_ids)} products")
        print(f"{'='*80}")
        for f in profile_only_flagged:
            print(f"  {f['item_no']} {f['name']}: {f['detail']}")
    else:
        print(f"\nNo profile-only flags")

    # Collect all flagged item stems and copy to FLAGGED/
    all_flagged_ids = vendor_flagged_ids | profile_only_ids
    if not all_flagged_ids:
        print("\nNo flagged items to copy.")
        return

    # Map item_no -> stem
    id_to_stem = {item["item_no"]: item["stem"] for item in all_items}
    flagged_stems = {id_to_stem[fid] for fid in all_flagged_ids if fid in id_to_stem}

    flagged_dir = source_dir / "FLAGGED"
    flagged_dir.mkdir(exist_ok=True)

    copied = 0
    for stem in sorted(flagged_stems):
        for ext in [".glb", ".json", ".jpg", ".catalog.json"]:
            if ext == ".catalog.json":
                src = source_dir / f"{stem}.catalog.json"
            else:
                src = source_dir / f"{stem}{ext}"
            if src.exists():
                shutil.copy2(str(src), str(flagged_dir / src.name))
        copied += 1

    print(f"\n-> {flagged_dir}/ ({copied} products copied with all companion files)")

    # Write report JSON
    report = {
        "total_compared": len(all_items),
        "vendor_flagged": vendor_flagged,
        "vendor_flagged_count": len(vendor_flagged_ids),
        "profile_only_flagged": profile_only_flagged,
        "profile_only_flagged_count": len(profile_only_ids),
        "all_flagged_item_nos": sorted(all_flagged_ids),
        "thresholds": {
            "vendor_diff_pct": VENDOR_DIFF_PCT,
            "flat_y_ratio": FLAT_Y_RATIO,
        },
    }
    report_path = source_dir / "measure_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"-> {report_path}")


if __name__ == "__main__":
    main()
