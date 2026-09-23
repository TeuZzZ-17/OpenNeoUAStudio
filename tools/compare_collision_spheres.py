"""Read-only manual/automatic collision comparison on real Studio assets.

Example:
  python tools/compare_collision_spheres.py --manual-script Vehicles.cfg
      --set-bas Set.BAS --vehicle-ids 1,3,6,15,16,22,24,26,32,37 --output report.json

The caller must select an authenticated manual script. Current coll_* data is
not automatically assumed to be manual. No asset or game script is written.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6.QtWidgets import QApplication

from collision_editor.editor import (
    CollisionEditorWindow,
    CollisionSphere,
    OPENNEOUA,
    import_collision_block,
    read_script_file,
    script_model_references,
    find_script_blocks,
    plan_script_update,
)
from collision_editor.sphere_generator import (
    ACCURACY_PRESETS,
    generate_collision_spheres,
)


def comparison_metrics(manual, automatic, spacing=None):
    """Compare unions on a shared domain containing every complete sphere."""
    reference, candidate = np.asarray(manual, float), np.asarray(automatic, float)
    combined = np.concatenate([reference, candidate])
    lower = (combined[:, :3] - combined[:, 3, None]).min(0)
    upper = (combined[:, :3] + combined[:, 3, None]).max(0)
    if spacing is None:
        spacing = float(max(upper - lower) / 110)
    axes = [
        np.arange(lo - spacing, hi + spacing * 1.5, spacing)
        for lo, hi in zip(lower, upper)
    ]

    def occupancy(spheres):
        mask = np.zeros(tuple(map(len, axes)), bool)
        for x, y, z, radius in spheres:
            mask |= (axes[0][:, None, None] - x) ** 2 + (
                axes[1][None, :, None] - y
            ) ** 2 + (axes[2][None, None, :] - z) ** 2 <= radius**2
        return mask

    ref, auto = occupancy(reference), occupancy(candidate)
    overlap = int((ref & auto).sum())
    union = int((ref | auto).sum())
    silhouettes = []
    for axis in range(3):
        a, b = ref.any(axis), auto.any(axis)
        silhouettes.append(float((a & b).sum() / max(1, (a | b).sum())))
    return {
        "union_iou": overlap / max(1, union),
        "manual_volume_covered": overlap / max(1, int(ref.sum())),
        "auto_volume_outside_manual": int((auto & ~ref).sum())
        / max(1, int(auto.sum())),
        "auto_to_manual_volume": int(auto.sum()) / max(1, int(ref.sum())),
        "silhouette_iou_yz_xz_xy": silhouettes,
        "spacing": spacing,
    }


def run_comparison(script, set_bas, ids, output, baseline=None, verify=False):
    app = QApplication.instance() or QApplication([])
    text, _, _ = read_script_file(script)
    refs = {
        r.block.object_id: r
        for r in script_model_references(text)
        if r.block.kind == "new_vehicle"
    }
    missing = set(ids) - refs.keys()
    if missing:
        raise ValueError(f"Vehicle definitions missing: {sorted(missing)}")
    window = CollisionEditorWindow()
    if not window.open_base(set_bas):
        raise ValueError("Could not resolve the selected SET.BAS.")
    report = {
        "manual_script": str(script.resolve()),
        "manual_script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "set_bas": str(set_bas.resolve()),
        "set_bas_sha256": hashlib.sha256(set_bas.read_bytes()).hexdigest(),
        "provenance_note": "Caller-selected manual references; authenticity must be established separately.",
        "models": [],
    }
    old = json.loads(baseline.read_text()) if baseline else []
    try:
        for object_id in ids:
            ref = refs[object_id]
            if not window.open_vehicle_script(
                script, vehicle_id=object_id, set_bas_path=set_bas
            ):
                raise ValueError(f"Cannot resolve {ref.label}")
            owner = window._current_owner
            obj = next(o for o in window.family.all_objects() if o.owner_path == owner)
            animation = []
            triangles = window.viewport.local_owner_triangles(
                owner, animated_indices=animation
            )
            _, spheres, warnings = import_collision_block(text, ref.block, OPENNEOUA)
            if not spheres or warnings:
                raise ValueError(f"Invalid manual reference {ref.label}: {warnings}")
            manual = [[s.x, s.y, s.z, s.radius] for s in spheres]
            model = {
                "name": ref.block.name,
                "id": object_id,
                "vp": ref.vp_normal,
                "model": obj.base_object.skeleton_name,
                "owner": owner,
                "manual_count": len(manual),
                "manual_spheres": manual,
                "triangle_count": len(triangles),
                "presets": {},
            }
            for preset in ACCURACY_PRESETS:
                started = time.perf_counter()
                result = generate_collision_spheres(
                    triangles, preset.key, animated_triangles=animation
                )
                auto = [[s.x, s.y, s.z, s.radius] for s in result.spheres]
                entry = asdict(result)
                entry["seconds"] = time.perf_counter() - started
                entry["comparison"] = comparison_metrics(manual, auto)
                entry["radius_range"] = [
                    min(s[3] for s in auto),
                    max(s[3] for s in auto),
                ]
                if verify and preset.key in ("high", "ultra"):
                    repeated = generate_collision_spheres(
                        triangles, preset.key, animated_triangles=animation
                    )
                    if result != repeated:
                        raise AssertionError(
                            f"Non-deterministic generation: {ref.label}"
                        )
                    project = copy.deepcopy(window.project)
                    project.compound = [CollisionSphere(OPENNEOUA, *s) for s in auto]
                    updated, _, _ = plan_script_update(
                        text,
                        ref.block.kind,
                        object_id,
                        project,
                        replace_all_managed=True,
                    )
                    second, _, _ = plan_script_update(
                        updated,
                        ref.block.kind,
                        object_id,
                        project,
                        replace_all_managed=True,
                    )
                    if updated != second:
                        raise AssertionError(f"Non-idempotent overwrite: {ref.label}")
                    block = next(
                        b
                        for b in find_script_blocks(updated)
                        if b.kind == ref.block.kind and b.object_id == object_id
                    )
                    _, reloaded, warnings = import_collision_block(
                        updated, block, OPENNEOUA
                    )
                    if (
                        warnings
                        or [[s.x, s.y, s.z, s.radius] for s in reloaded] != auto
                    ):
                        raise AssertionError(
                            f"Collision round trip changed geometry: {ref.label}"
                        )
                    entry["verification"] = {
                        "deterministic": True,
                        "overwrite_idempotent": True,
                        "script_round_trip_exact": True,
                    }
                previous = next(
                    (
                        r
                        for r in old
                        if r["name"] == ref.block.name and r["preset"] == preset.key
                    ),
                    None,
                )
                if previous:
                    old_spheres = [
                        [s["x"], s["y"], s["z"], s["radius"]]
                        for s in previous["spheres"]
                    ]
                    entry["previous_count"] = len(old_spheres)
                    entry["previous_comparison"] = comparison_metrics(
                        manual, old_spheres
                    )
                model["presets"][preset.key] = entry
                print(
                    f"{ref.block.name} {preset.key}: {len(auto)} spheres; "
                    f"manual IoU {entry['comparison']['union_iou']:.3f}; "
                    f"outside manual {entry['comparison']['auto_volume_outside_manual']:.3f}",
                    flush=True,
                )
            report["models"].append(model)
            output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    finally:
        window._set_modified(False)
        window.close()
    if (
        hashlib.sha256(script.read_bytes()).hexdigest()
        != report["manual_script_sha256"]
    ):
        raise AssertionError("The source script changed during comparison.")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manual-script", type=Path, required=True)
    parser.add_argument("--set-bas", type=Path, required=True)
    parser.add_argument("--vehicle-ids", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    run_comparison(
        args.manual_script,
        args.set_bas,
        [int(i) for i in args.vehicle_ids.split(",")],
        args.output,
        args.baseline,
        args.verify,
    )
