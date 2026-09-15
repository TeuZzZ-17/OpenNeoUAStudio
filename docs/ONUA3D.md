# OpenNeoUA ONUA3D V1

ONUA3D is the canonical interchange container. `scene.glb` is a derived preview
of the original Complete Asset Family in `ua_family/`. The embedded family is
the lossless native baseline; the position-only importer applies validated changes
to that baseline, never reconstructing native assets from the GLB alone.

## Container and native baseline

The deterministic ZIP contains:

- `onua3d_manifest.json`: format `OpenNeoUA ONUA3D`, container version `1`,
  coordinate convention, owner/primitive mappings, warnings, member sizes and
  SHA-256 hashes;
- `scene.glb`: glTF 2.0 geometry, preview materials/images and VANM preview;
- `textures/*.png`: derived preview images;
- `ua_family/asset_family_manifest.json` and the original staged native family.

`semantic_sha256` identifies the canonical family's semantic snapshot. The
package is staged and validated through the existing Complete Asset Family
path, `validate_family_package()` and `load_asset_family()`. Persistent identity
does not change these native files, the family manifest or their semantics.

glTF positions use `(x, -y, z)` for UA `(x, y, z)`, preserving numerical units.
The same reflection is the inverse. UVs use `(u/256, v/256)`. Polygon fans reverse
winding after reflection. Identity owner nodes carry the logical KIDS hierarchy;
placement and rotation carriers implement Studio's independent object transforms.
Accessor positions are local geometry coordinates, not world coordinates.

## Persistent vertex identity, revision 1

New exports add `_ONUA3D_VERTEX_ID` to each primitive's attributes. The accessor
is non-normalized `FLOAT` (`componentType: 5126`), `SCALAR`, with positive integer
values from **1 through 16777216 (2^24)**. These integers are exactly representable
in float32. Zero is invalid. Export fails rather than rounding or overflowing IDs.

IDs are assigned deterministically across the whole exported family. Each
original exported polygon corner gets a distinct ID, including corners sharing
one native SKLT point, material/UV seams, KIDS, and distinct VANM preview states.
The assignment is stable for repeated exports of the same input; it is not a
persistent identifier across independent topology or material changes.

Only the ID is carried as a custom geometry attribute. Its native origin is
stored once in the new canonical mapping, scoped by the existing owner row:

```json
{
  "version": 1,
  "vertex_identity": {
    "version": 1,
    "attribute": "_ONUA3D_VERTEX_ID",
    "component_type": 5126,
    "accessor_type": "SCALAR",
    "duplicates": "identical_position"
  },
  "nodes": [
    {
      "owner_path": "root/kid[0]",
      "vertex_instances": {
        "42": {
          "point_id": 7,
          "polygon_id": 3,
          "corner": 1,
          "block_index": 2,
          "vanm_frame": 0
        }
      }
    }
  ]
}
```

This is an excerpt; existing container and node fields are retained. ID keys
are canonical decimal strings in JSON and globally unique across all owners.
`point_id`, `polygon_id`, `corner` and `block_index` are zero-based native indices;
`block_index: -1` denotes geometry with no mapped material block. `vanm_frame`
is omitted for static geometry and identifies the representative frame for the
exported VANM state. Repeated bitmap/UV states still use the existing deduplicated
state nodes and frame mapping.

`vertex_identity_mapping()` returns an ID-keyed table including each row's
`owner_path`. The older `point_ids`, face records and numeric node mappings
remain for compatibility; they do not establish correspondence after Blender
reorders or reconstructs arrays. Owner names and vertex coordinates are not
substitutes for persistent identity.

## Blender duplicates and position-only validation

**Blender may materialize several glTF vertices for one logical ONUA3D ID.**
For example, exporting different normals at polygon corners splits a Blender
vertex and copies its custom ID to both glTF representations. Normals remain
enabled and are ignored by the position-only identity reducer.

The rule is:

1. Group exclusively by `_ONUA3D_VERTEX_ID`.
2. Require every copy in a group to have exactly equal, finite `POSITION` values.
3. Return that one position for the logical ID. Signed zeros are normalized.
4. Reject the complete operation if any copy disagrees. There is no epsilon,
   average, majority vote, nearest-point matching or choice of a preferred copy.

Distinct IDs remain distinct even when their positions or native SKLT point IDs
coincide. Repeated references to shared accessors are allowed by the same rule.
Node order, primitive order and accessor row order do not identify native points.

`canonical_vertex_positions(manifest, primitives)` implements this rule for
**already decoded** per-primitive attributes. Each input item contains parallel
arrays for `_ONUA3D_VERTEX_ID` (scalar numeric values) and `POSITION` (triples).
It rejects missing attributes, mismatched counts, unknown/missing/non-integral/
out-of-range IDs, non-finite positions, ambiguous manifest identities and
conflicting copies. It returns a dictionary sorted by integer ID.

This helper is not a GLB parser or a complete ONUA3D importer. Its success proves
the ID/position contract only. It does not validate container integrity, glTF
accessor encoding, topology, placement transforms, UVs, materials or animation,
and writes no native files. `onua3d_import.py` performs the container, mapping,
scene and native-write checks described below before publishing an output.

## Position-only native import V1

**File > Import > Import OpenNeoUA 3D...** calls the separate core
`import_onua3d(package_path, output_root)`. Choose a parent folder; Studio creates
`<package-stem>_imported/`, containing a Complete Asset Family and its manifest,
and opens that validated family. Existing output folders are refused. Cancel or
failure preserves the currently loaded document and original source package.

The importer:

1. Validates ZIP member paths, duplicate members, format/version, coordinate
   contract, member hashes/sizes, the embedded family and its semantic hash.
2. Requires `vertex_identity` revision 1. Regenerates the original scene and
   mapping through the existing exporter from the isolated embedded family.
   The manifest's owner/point/polygon/corner/block/VANM mapping must match this
   canonical baseline; no associations are inferred from edited geometry.
3. Decodes the embedded, uncompressed GLB. Requires valid float32 scalar IDs,
   float32 local positions, complete ID coverage and consistent duplicates.
   Sparse/compressed geometry, skins and morph targets are outside V1.
4. Identifies nodes through their `extras.onua3d` metadata, then checks their
   hierarchy, geometry placement and local matrices. Node indices and names
   are not identity. Matrix/TRS re-expression is supported as described below.
5. Compares triangle connectivity and winding expressed through persistent IDs,
   canonical owner/node and material/block metadata. Primitive order, accessor
   order and coherent normal-split duplicates may change; topology may not.
   Existing exported UVs must remain equal per ID.
6. Groups again by **`(owner_path, SKLT point_id)`**. Every distinct ID for one
   native point must have exactly the same final local position, including
   material/UV seams and VANM preview states. Conflicts fail the whole import;
   there is no epsilon, averaging or preferred copy at either grouping level.
7. Converts local positions back to UA with `(x, -y, z)`. Object transforms
   are never baked into POO2. Unrepresented native points remain unchanged.
   Owners sharing one physical SKLT must also agree on its resulting points.
8. Calls the existing `save_sklt_with_poo2_points(..., update_sensors=False)`
   only for changed skeletons. The writer's default remains `True`, preserving
   the existing affine sensor behavior of all other call paths. Import verifies
   the exact expected byte changes for the requested POO2 points and reloads
   the SKLT. Unchanged components retain their original signed-zero storage.
9. Verifies family semantics against the baseline with only the approved points
   replaced, refreshes the Complete Asset Family manifest, validates staging,
   and publishes through `commit_verified_files()` to the new output folder.

No-op imports preserve **every embedded native byte, including the family
manifest**. Edited imports preserve all other native resources and all bytes
outside the requested POO2 updates: SEN2, polygon topology, UV, ADES, textures,
KIDS, ANM/VANM, particles/FX, collision sensors and other metadata are retained.
The family manifest is updated only when native point coordinates change.

GLB normals, PBR preview appearance, image payloads and VANM preview visibility
curves are derived and never become native edits. Blender resamples VANM scale
curves even on a no-op; these curves are not converted back to ANM. Added object
translation/rotation/geometry animation channels are refused. Editing preview
materials, textures or animation does not edit their native counterparts.
There is no support for topology, UV, texture or animation authoring on import.

### Object Mode equivalence — explicit tolerance

For each canonical node, compare the original and imported **local 4x4 matrix**
in glTF column-major order. TRS is reconstructed as glTF `T*R*S`; exporter
placement and rotation carriers together retain Studio's `T*S*R` convention.
All components must be finite and every component must satisfy:

```text
abs(original_component - imported_component) <= 1e-6
```

`TRANSFORM_EPSILON = 1e-6` applies **only to this Object Mode validation**. It is
not a position tolerance and does not authorize baking, normalizing, repairing
or applying an imported transform. Beyond the threshold, import fails with the
node metadata and measured maximum difference. BASE transforms stay native.
Differences within this explicitly permitted numerical equivalence are ignored.

Before implementing the gate, real Blender 4.5.9 LTS no-op probes measured zero
matrix difference for VP_HUBI2, VP_HUBI4, VP_BRGR1 and VPdBRGR1. A synthetic BASE
with nontrivial rotation, translation and nonuniform scale measured
`7.169116822414168e-8` after Blender's matrix-to-quaternion conversion. No probe
exceeded `1e-6`. The available SET2/3/4/5/6/7/46 archives provided no nontrivial
native BASE transform candidate in the bounded retail scan, so rotated coverage
is synthetic, not claimed as a retail transform test.

## Blender profile and verification

Verified with Blender **4.5.9 LTS**, normal export enabled:

- import glTF/GLB with Merge Vertices disabled;
- export glTF/GLB with **Mesh Attributes** enabled (`export_attributes=True`);
- keep Normals enabled (`export_normals=True`);
- enable Custom Properties (`export_extras=True`) to retain owner/placement
  metadata required by the importer. Primitive extras alone do not survive Blender.

The add-on described below applies this profile automatically, exports only the
active ONUA3D scene, keeps modifiers unapplied, and retains derived VANM preview
animation. It validates the new GLB before producing a new package.

## End-to-end workflow

Build the installable Blender add-on from the same shared core sources:

```text
python tools/build_blender_onua3d.py OpenNeoUA3D_Blender_Addon.zip
```

In Blender 4.5 LTS, install this ZIP through Preferences > Add-ons > Install from
Disk, then enable **OpenNeoUA 3D**. No Studio Python installation, Qt, or native UA
parser/writer is bundled or required in Blender.

1. In Studio, use **File > Export > Export OpenNeoUA 3D...**.
2. In Blender, use **File > Import > Open OpenNeoUA 3D...** and choose that package.
   The add-on validates its manifest, member hashes, embedded family inventory,
   semantic snapshot links and persistent mapping before importing `scene.glb`
   into a new scene. Existing scenes are retained.
3. Edit vertex positions in Edit Mode. Keep Object Mode transforms, topology,
   UVs and identity attributes unchanged. All representations of a native point,
   including seam copies and VANM preview states, must agree exactly. Editing
   just one such representation is refused if it creates a conflict.
4. Use **File > Export > Export OpenNeoUA 3D...** to choose a **new** package file.
   Keep the source `.onua3d` available at its original path and unchanged. The
   absolute source path and SHA-256 are saved on the scene and survive saving and
   reopening the `.blend`. Missing, replaced or corrupted sources fail closed;
   there is no name-based search or implicit source substitution.
5. In Studio, use **File > Import > Import OpenNeoUA 3D...** and select the new
   package. Studio validates and stages the complete result **before** asking for
   its destination. Choose an empty folder, or create a new folder in the picker.
   The selected folder itself is the output; no extra suffix folder is added.
6. Studio validates the complete native family, publishes it without overwriting
   existing files, and opens it through the normal family/viewer path.

`onua3d_package.py` is the shared container and GLB contract. The native importer
reuses it and additionally reloads the embedded native files, regenerates the
canonical mapping and validates the edited native family. The generated add-on
ZIP uses these same sources with relative imports, not a second implementation.
The existing path contract is shared through `asset_package_paths.py`, and the
coordinate/format constants through `onua3d_contract.py`; existing public
exports remain available at their original module locations.

Blender repackaging changes only `scene.glb` and that member's `size` and `sha256`
in the outer manifest. Every other manifest value and every other member payload,
including **all of `ua_family/` byte for byte**, is retained. The outer ZIP
container bytes may differ. Source and output paths must be different; existing
output files are refused. Both staged and published packages are read back and
validated through verified I/O.

Studio's `prepare_onua3d()` performs the existing native validation and POO2-only
edit in temporary staging; `materialize()` publishes the complete result to the
chosen new or empty folder. Nonempty folders, files, and symlink/junction/reparse
destinations or ancestors are refused. `commit_verified_files()` has an opt-in
`replace_existing=False` mode using atomic no-clobber file publication. Existing
callers keep their prior default. Final family validation runs while transaction
cleanup is still available; a handled publication or validation error removes
the files created by this transaction. There is no merge with existing data.

This is coordinated transactional I/O with rollback on reported errors, not a
filesystem-wide atomic directory swap or a guarantee against process termination
or machine power loss during publication. Filesystems that cannot perform the
required no-clobber hard-link publication fail rather than overwrite files.

The automated tests exercise a non-planar synthetic quad with material/UV seams
and KIDS, plus VP_HUBI2 and VP_HUBI4 retail families. They check no-op, movement of
vertices known to split on export, agreement among all copies, triangle
connectivity expressed by ID, and explicit node/primitive/vertex-array reorder.
Core tests reject artificially conflicting copies. Retail files stay outside Git.

Set `ONUA3D_BLENDER` to the Blender executable, `ONUA3D_HUBI2_PACKAGE` to the
original VP_HUBI2 package and `ONUA3D_RETAIL_ARCHIVE` to the retail SET.BAS fixture.
Run with `QT_QPA_PLATFORM=offscreen`:

```text
python -m unittest tests.test_onua3d_identity tests.test_onua3d_identity_blender -v
python -m unittest tests.test_onua3d_import tests.test_onua3d_import_blender tests.test_sklt_poo2_only -v
python -m unittest tests.test_onua3d_package tests.test_onua3d_destination tests.test_onua3d_workflow -v
```

Blender and retail tests explicitly skip when their executable/input is absent.
These are headless checks, not manual GUI or live OpenNeoUA runtime verification.

## Older ONUA3D packages

The container version remains 1; persistent identity is an additive, separately
versioned capability. A package without `vertex_identity` and its canonical
tables is not eligible for this editing contract. Its embedded family remains
readable and can regenerate an updated GLB through `write_onua3d()`.

The identity helper fails explicitly on a missing or unsupported capability.
It never infers lost IDs from legacy indices, names, positions or geometry.
Native parser behavior and runtime data are unchanged. The SKLT writer adds only
the optional SEN2 opt-out described above; existing callers retain their default.
