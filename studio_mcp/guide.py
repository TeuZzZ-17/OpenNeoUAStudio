"""Built-in manual for assistants: studio_help(topic). Text lives here so every MCP client sees the same advice."""

from __future__ import annotations

import inspect

TOPICS = ("start", "design", "conventions", "selectors", "uv", "recipes", "wireframe", "ops")

START = """WORKFLOW (read-only steps are always safe)
1. Find a model: studio_list_vp (Set.BAS models) or studio_list_models (loose models under Data/Models).
2. Look: studio_materials(source) lists texture blocks; studio_render_model(source, views=['iso','top','front']).
   Add label_points / label_polygons / highlight_polygons (selectors) to SEE the numbers the edit tools need.
3. Make an editable copy: studio_export_model('set1:VPwGLID2', out_dir, base_name, skeleton_stem).
   The skeleton_stem must be as long as the original (VPWGLID2 -> VPKGLID2) and different.
   A NEW design instead starts empty: studio_new_model(out_dir, base_name, skeleton_stem) - see studio_help('design').
4. Edit. studio_edit_batch(model, operations=[...]) runs several steps on one in-memory copy and writes ONCE,
   after verification; if any step fails nothing is written. Every single-purpose tool (studio_extrude, ...) is the
   same engine with one step.
5. Check: studio_render_model again, studio_analyze_model, studio_validate_model. Wrong? studio_undo / studio_redo.
`model` is the .BASE path, or a folder holding exactly one .BASE; relative paths start at the game Data folder.
Writes are only allowed under Data/Models. Every write keeps an undo snapshot (studio_history lists them).
"""

DESIGN = """NEW DESIGNS IN THE URBAN ASSAULT LOOK (measured on the original Set1 units)
1. Start empty: studio_new_model(out_dir, 'MYUNIT', 'VPNUNIT1')  (stem: 6-8 characters, new and unique).
2. Shape with few big flat faces. Original units have 6-65 polygons (tanks 10-35, aircraft 25-55, host stations
   ~100); faceted, angular, no rounded detail, no greebles.
   - loft = one closed hull from cross-sections (fuselage, tank hull, bomb, missile, pod). half=true + mirror.
   - create_primitive = boxes/wedges/prisms for turrets, barrels, fins, skids.
   - import_geometry = reuse original parts (tracks, rotors, turrets, cockpits) from a faction donor:
     the surest way to the faction look. Animated parts (tracks, rotors, glows) travel with add_fx.
3. Texture with faction swatches: studio_swatches() lists them with a picture; pass swatch='ghorkov.hull' to
   loft / create_primitive / add_polygons, or studio_paint later. Every face shows one whole panel (like the
   originals). Use the faction's own atlas only: Resistance BAGGER/MTL, Ghorkov HUBI, Taerkasten FAERROBO + dark
   HUBI armour, Mykonian AIR1TXT veins, Sulgogar FBAGGER flesh, Black Sect ROBONEU.
4. Light: the engine has none, every face carries a baked shade. End with {"op":"auto_shade"} (top bright,
   sides mid, belly dark). Without it the model looks flat and wrong.
5. Check scale and look: studio_render_model(model, views=[...], compare_with='set1:<similar unit>') puts the
   reference beside it AT THE SAME SCALE; studio_analyze_model must report no problems.
Example (a bomb, 72 long):
  {"op":"loft","half":true,"swatch":"taerkasten.plate","sections":[
     {"at":40,"points":[[0,0]]},
     {"at":28,"points":[[0,-6],[4,-4],[6,0],[4,4],[0,6]]},
     {"at":-18,"points":[[0,-9],[6,-6],[9,0],[6,6],[0,9]]},
     {"at":-32,"points":[[0,-5],[3.5,-3.5],[5,0],[3.5,3.5],[0,5]]}]}
  {"op":"create_primitive","shape":"box","center":[0,-11,-30],"size":[1.5,10,12],"swatch":"taerkasten.plate2"}
  {"op":"auto_shade"}
"""

CONVENTIONS = """CONVENTIONS
- Axes: x right, y DOWN (negative y = up), z forward. A glider is ~200 units wide, so sizes are large numbers.
- Polygon facing: the visible (outward) side follows UA's winding, handled for you: create_primitive, extrude,
  mirror and flip keep it right. studio_analyze_model reports inside-out solids and mismatched neighbours.
- Polygons should be flat and convex (3 or 4 corners is the norm). Triangles are always flat: use them for slopes.
- Points are shared: moving a point moves every polygon that uses it (that is how you bend a hull).
- Every polygon is mapped to a material block (texture + shading). New polygons need `material`:
  {'like_polygon': ID} copies a polygon's block, shading values and (same corner count) UV pattern;
  {'block': N} picks a block by number (see studio_materials). 'takes_new_polygons' must be true.
- Animated 'area.class' polygons (tracks, rotors) are special: they can be moved but not extruded/flipped/mirrored.
  Particle emitters are tied to point NUMBERS: when an edit renumbers points the emitters follow their point
  (or the point is re-created at the same place if no polygon uses it any more).
- Limits: 65536 points and 32768 polygons per skeleton.
- Renumbering: delete_polygons, subdivide, weld and cleanup shift indices. Inside a batch use save_as/named
  selections; across separate calls re-run studio_select.
- Organize model folders under Data/Models/Base/Alpha_Models, Original_Refined or New; use descriptive unit and state folder names, while preserving the BASE filename.
"""

SELECTORS = """SELECTORS (which points / polygons an operation affects)
A selector is a list of indices, 'all', or a dict whose conditions must ALL hold. null/omitted = everything.
POINTS : {"indices":[..], "box":{"min":[x,y,z],"max":[x,y,z]} (null = open), "x":[min,max], "y":[..], "z":[..],
          "sphere":{"center":[..],"radius":r}, "polygons":[ids], "block":N, "connected_to":pointIndex,
          "nearest":{"to":[..],"count":n}, "unused":true, "named":"name", "not":{selector}, "or":[{..},{..}],
          "mirror_of":{"points":{selector},"axes":"x","include_source":false}, "invert":true}
POLYGONS: {"ids":[..], "block":N, "box":{..}/"x"/"y"/"z" (polygon center), "box_all":{..} (all corners inside),
          "facing":"up|down|left|right|front|back|+x|-x|..|[x,y,z]" or {"dir":..,"min_dot":0.7}, "connected_to":polyId,
          "points_any":{point selector}, "points_all":{point selector}, "vertices":3|4, "min_area":a, "max_area":a,
          "unmapped":true, "named":"name", "mirror_of":{"polygons":{selector},"axes":"x"}, "not":{..}, "or":[..], "invert":true}
mirror_of = the counterparts across the model's x/y/z planes through the origin (the editor's Mirror Select).
Examples: right half of the hull -> {"x":[0,null]};  the roof -> {"facing":"up"};  one wing ->
{"connected_to":12};  the nose points -> {"nearest":{"to":[0,0,150],"count":4}}.
Check a selector first with studio_select (or render with highlight_polygons / label_points).
In studio_edit_batch any step may add "save_as":"name"; later steps can then use {"named":"name"}.
"""

UV = """UV MODES (studio_set_uvs, and the `uv` argument of add_polygons / create_primitive / extrude)
UVs are byte pairs 0..255 on the block's texture (256x256 bitmap), one per polygon corner.
DEFAULT: without a uv spec, new polygons get 'world' UVs with the SAME texel size and texture region as the
model's original polygons of that material (measured when the model was opened), so additions look like part of it.
- planar   project along the polygon's main plane and stretch over `rect` (default [16,16,240,240]; with a
           reference polygon: that polygon's UV box = the same texture patch)
- rect     same, into an explicit "rect":[u0,v0,u1,v1]   (pick a texture area of the atlas)
- world    constant texel size: "texels_per_unit": 1.0 means 1 texture pixel per model unit; optional
           "region":[u0,v0,u1,v1] keeps polygons inside that part of the texture (tiles wrap inside it)
- copy     "from_polygon": ID  (same corner count: identical UVs; otherwise fitted into its UV box)
- explicit "uvs": [[u,v],...] one pair per corner
- adjust   change what is there: "offset":[du,dv], "scale":n|[su,sv], "flip_u", "flip_v", "rotate_90":turns
Open the texture with studio_render_model + studio_materials (uv_box per block) to see which areas exist.
"""

RECIPES = """RECIPES (studio_edit_batch operations)
Box on a hull:      {"op":"create_primitive","shape":"box","center":[0,-12,20],"size":[30,10,40],"material":{"like_polygon":3}}
Gun barrel:         {"op":"create_primitive","shape":"prism","segments":8,"size":[4,40,4],"rotate_deg":[90,0,0],
                     "center":[0,0,60],"caps":"top","material":{"like_polygon":3}}   (cylinder; top_scale=0 = cone)
Raise the roof:     {"op":"extrude","select":{"facing":"up","y":[null,-5]},"distance":-6,"scale":0.8}
Half then mirror:   model the +x half, then {"op":"mirror","select":{"x":[0,null]},"axis":"x"} and {"op":"weld","tolerance":0.05}
Make it symmetric:  {"op":"symmetrize","axis":"x","source":"positive"}
Resize everything:  {"op":"fit","size":[null,null,180],"uniform":true,"recenter":"xz"}
Bend a part:        {"op":"transform","select":{"y":[null,-4],"z":[40,null]},"rotate_deg":[10,0,0],"pivot":[0,0,40]}
More detail:        {"op":"subdivide","select":{"facing":"up"}}  then move single points with transform/set_points
Fix inside-out:     studio_analyze_model says which; {"op":"flip","select":[ids]}
Chain with names:   [{"op":"duplicate","select":{"connected_to":5},"translate":[30,0,0],"save_as":"copy"},
                     {"op":"transform","select":{"named":"copy"},"scale":0.5}]
Hull from sections: {"op":"loft","half":true,"swatch":"ghorkov.hull","sections":[{"at":60,"points":[[0,0]]},
                     {"at":30,"points":[[0,-10],[12,-6],[14,6],[0,10]]},{"at":-40,"points":[[0,-12],[16,-8],[18,8],[0,12]]}]}
Repaint a part:     {"op":"paint","select":{"facing":"up"},"swatch":"ghorkov.canopy"}
Bake the light:     {"op":"auto_shade"}   (last step; offset=+20 darkens a selection, -20 lightens it)
New model from scratch: studio_new_model (empty, plain blocks kept), then loft / primitives with `swatch`.
New model from an old one: export it, {"op":"clear"}, then build with create_primitive/add_polygons using
`material` blocks the exported copy kept (studio_materials shows them; they keep their textures).
"""


WIREFRAME = """WIREFRAMES (bare .SKL skeletons in Data/Wireless; new ones go in Data/Wireless/New/)
How the game draws them: screen x = x/1000, screen y = -z/1000, so +z is UP and +-1000 is the HUD frame edge.
ONLY THE FIRST SEGMENT of each line entry is drawn: every visible line is its own 2-point entry. The tools below
build lines only; studio_wire_get reports long_entries (fix with split_outlines). y is ignored on screen.
Conventions of the originals: unit outlines (OTL*) = isometric view from the front-left, larger half-extent 1000;
weapon outlines (OTLW*) = side view, nose at +x, x -1000..1000; sights/HUD (NH*, *VIS*) = symmetric, centred.
Tools: studio_wire_new -> studio_wire_edit(..., operations=[...]) -> studio_wire_render(frame=true for HUD).
studio_wire_from_model(source, skeleton, view='unit'|'weapon', detail=3) runs Studio's generator on a 3D model
(flames, smoke, rotors and particles left out unless include_effects=true).
Operations: add_lines(points=[[x,z],...], closed), add_shape(shape=circle|hexagon|octagon|triangle|square|
pentagon|arc|rect|cross|x|brackets, center, radius, size=[w,h], segments, start_deg, end_deg, rotate_deg, dashes),
trace_image(image=path, size=1000, detail 0..4, threshold, invert), add_points, add_polygons([[a,b],[a,b,c]] ->
lines; close_loops), delete_points, delete_polygons, transform(select, translate=[dx,dz], rotate_deg, scale, pivot,
mirror=true), set_points, align(axis,value), mirror_copy(axis 'x' = left/right copy, 'z' = up/down, plane),
weld(tolerance), normalize(size=1000), split_outlines, outline_move (OTL2 files: only moving, the count is fixed).
Example sight: [{"op":"add_shape","shape":"circle","radius":220,"dashes":true},
          {"op":"add_shape","shape":"brackets","size":[700,500],"radius":90},
          {"op":"add_lines","points":[[-160,0],[-60,0]]}, {"op":"add_lines","points":[[60,0],[160,0]]}]
Undo works the same way: studio_undo / studio_redo / studio_history with the .SKL path.
"""


def _ops_text() -> str:
    from studio_mcp import edit
    lines = ["OPERATIONS (name: arguments).  In studio_edit_batch pass {\"op\": name, ...arguments}."]
    for name, function in sorted(edit.OPS.items()):
        params = []
        for pname, parameter in inspect.signature(function).parameters.items():
            if pname in ("ctx", "save_as"):
                continue
            default = "" if parameter.default is inspect.Parameter.empty else f"={parameter.default!r}"
            params.append(pname + default)
        doc = " ".join((function.__doc__ or "").split())
        lines.append(f"- {name} [tool {edit.tool_name(name)}]({', '.join(params)})\n    {doc}")
    return "\n".join(lines)


def help_text(topic: str = "start") -> str:
    topic = (topic or "start").strip().lower()
    if topic == "all":
        return "\n\n".join(help_text(t) for t in TOPICS)
    if topic == "start":
        return START + ("\nMore: studio_help('design' | 'conventions' | 'selectors' | 'uv' | 'recipes' | 'wireframe' | "
                        "'ops' | 'all')")
    texts = {"design": DESIGN, "conventions": CONVENTIONS, "selectors": SELECTORS, "uv": UV, "recipes": RECIPES,
             "wireframe": WIREFRAME}
    if topic == "ops":
        return _ops_text()
    if topic in texts:
        return texts[topic]
    return f"Unknown topic {topic!r}. Topics: {', '.join(TOPICS)}, all"
