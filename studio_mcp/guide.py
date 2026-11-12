"""Built-in manual for assistants: studio_help(topic). Text lives here so every MCP client sees the same advice."""

from __future__ import annotations

import inspect

TOPICS = ("start", "design", "conventions", "selectors", "uv", "recipes", "wireframe", "ops", "sets", "set_ops",
          "textures")

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
Model writes go under Data/Models (set tools write Data/Sets). Every write keeps an undo snapshot
(studio_history / studio_set_history list them).
SETS AND SECTORS (whole sets, sector variants, new buildings and textures): studio_help('sets').
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

ANM (animated parts). Studio calls every animated segment an ANM (the .ANM file of the engine); older text said FX.
List them with studio_effects. Still polygon -> blinking/animated face: {"op":"animate_polygons","select":[...],
"shifts":[[0,-36]],"frames":[[1,1500],[0,700]],"name":"GRNOS"}  (frames = [state, ms]; the face keeps its UVs as
state 0). The polygon becomes an animated area.class block (drawn unshaded) and its .ANM is written beside the
BASE, exactly like Studio's Add UV Phase / Overwrite. Clone / remove an animated part: add_fx / delete_fx
(the `fx` argument is the ANM index from studio_effects). Whole-model spawn-in look: generate_genesis.
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


SETS = """SETS AND SECTORS (the Sector Editor's tables; the Sector Editor is in BETA: its rules may still change)
A set is Data/Sets/SetN: Objects/SET.BAS (every model of the set - vehicles too -, textures, skeletons),
Scripts/SET.SDF (the tables below), Scripts/override.lst, Palette/STANDARD.PAL (256 colours), HI (sky), Remap.
SET.SDF tables (studio_set_models / studio_set_buildings / studio_set_sectors):
- MODELS 0..4095: BASE (a label), collision SKLT, legacy symbol, shield 0-100, collision 0 own SKLT / 1 the set's
  sub-sector collision / 2 its compact collision, effects = destruction explosions [[VP, x, z, height], ...].
  The picture is the SET.BAS model at the same position, or the file named in override.lst.
- BUILDINGS 0..4095: four models = intact, damaged, heavy damage, destroyed, plus destructible 0/1.
- SECTORS 0..255 (248-255 = map border): '3x3' = 9 cells, one building each, or 'single' = one large cell.
  ground = connection family 0-5: picks the transition strip to the neighbour (studio_set_info shows what each
  family is used for). symbol = map symbol 0-255. Free slots: 1-247.
Geometry: sector 1200 x 1200, cells 300 x 300 centred at x,z = -300/0/+300; y DOWN (negative = up), ground y=0.
  cells   0 1 2   far row (+z)        A cell model is 300 x 300 and INCLUDES its own ground tile
          3 4 5                       (ST_EMPTY: one 300 x 300 polygon). Originals: houses 80 high, city blocks
          6 7 8   near row (-z)       200, towers 420, TV tower 1010; 13-40 polygons; a 'single' model is 900 x 900.
Damage states: placing a model as intact brings its chain: an existing building row that starts with it, else
the vanilla names XX_NAME -> XXxNAME (damaged) -> XXyNAME (heavy) -> XXzNAME (destroyed, rubble, may sink to +40).

WORKFLOW
1. Look: studio_set_list, studio_set_info(set), studio_set_sectors(set, text_filter='...'),
   studio_set_render(set, sectors=[...], label_cells=true), studio_set_models / studio_set_buildings,
   studio_set_list_textures + studio_texture_preview(source='set3:CITY1.ILBM', uv_grid=true).
2. Edit: studio_set_edit(set, operations=[...]) - every step on one copy, ONE verified write, undo snapshot - or a
   single-step tool (studio_set_new_sector, studio_set_place_model, ...). studio_help('set_ops') lists them all.
3. Check: studio_set_render again (state=1..3 for the damage states, grid=[[a,b],[c,d]] for the joins),
   studio_set_info -> problems must be [].
4. Wrong? studio_set_undo / studio_set_redo / studio_set_history.

RECIPES (studio_set_edit operations)
Variant of a sector:   {"op":"new_sector","copy_of":5,"comment":"Nobel city, tower","save_as":"v"},
                       {"op":"place_model","sector":{"named":"v"},"cells":[0,2],"model":"ST_HO.base"}
Sector from models:    {"op":"new_sector","models":["ST_HAUS1",null,"ST_HAUS1",null,"ST_CITY1",null,"ST_HAUS2",
                        null,"ST_HAUS2"],"comment":"suburb"}   (null = empty building 0; ground/symbol 'auto')
One big model:         {"op":"new_sector","layout":"single","models":"ST_MACHT.base","whole":true}
Start in ruins:        {"op":"shift_state","sector":40,"cells":[4],"state":3}
Explicit four states:  {"op":"place_states","sector":40,"cells":[1],"states":["ST_HO","STxHO","STyHO","STzHO"]}
Take sectors from another set: {"op":"import_sectors","from_set":"set1","sectors":[5,6]}  (models must exist here)

NEW BUILDING (model + states + sector), e.g. for Set3:
a. studio_new_model('Models/Base/New/Signal_Tower_Intact', 'NX_SIGT1', 'NX_SIGT1', donor='set3:ST_CITY1')
   (keeps ST_CITY1's material blocks = Set3 facade, roof and ground textures; stem as long as the donor's).
b. Build: ground tile first ({"op":"import_geometry","source":"set3:ST_EMPTY"}), then create_primitive /
   extrude with material {"block":N} (studio_materials), set_uvs rect on the facade atlas, auto_shade last.
   Footprint up to ~260 x 260 inside the 300 cell. studio_render_model(..., compare_with='set3:ST_CITY1').
c. Damaged / heavy / destroyed: new models NXxSIGT1, NXySIGT1, NXzSIGT1 (import_geometry from the intact one,
   then break it: delete the top, transform with jitter, damaged F-atlases such as FCITY1 via set_texture).
d. studio_set_edit('set3', [
     {"op":"install_model","model":"Models/Base/New/Signal_Tower_Intact/NX_SIGT1.BASE","like":"ST_CITY1"},
     ... the same for NXxSIGT1, NXySIGT1, NXzSIGT1 ...,
     {"op":"new_sector","copy_of":5,"save_as":"s"},
     {"op":"place_model","sector":{"named":"s"},"cells":[4],"model":"NX_SIGT1"}])   (the chain is automatic)
   install_model copies the BASE to Objects/, its skeleton to Skeleton/ and the textures the set lacks (in the
   set palette); like= copies shield, collision mode and explosions from a similar original.
e. Change it later: edit the model in Data/Models, then install_model again with replace=<model id>.
Changing an ORIGINAL set model: studio_export_model('set3:ST_CITY1', 'Models/Base/Original_Refined/
   City1_Intact', 'SX_CITY1', 'SX_CITY1'), edit it, then install_model with replace=<its row id> and
   collision='keep' (every sector using it changes).

NEW TEXTURES: studio_help('textures'). NEW SET: studio_set_create(8, from_set='set3') = full copy without sectors;
then textures, models and sectors as above; a map must use set 8 to show it.

ENGINE FACTS (OpenNeoUA source, Nov 2026) - the tools follow them:
- override.lst 'lego <id> <file>' loads Objects/<file>; the BASE name in SET.SDF alone changes nothing visible.
- A new model's skeleton lives in SetN/Skeleton/, new textures in SetN/Loose/ILBM (Set1-Set7) or the set root.
- SET.BAS names win: new skeleton, texture and model names must be new in that set (the tools refuse clashes).
- Repainting an existing texture (add_texture replace=true) works only in Set1-Set7 (Loose overrides).
- Not checked by any tool: the look in game, collisions, explosions, AI behaviour. Tell the user.
"""

TEXTURES = """TEXTURES (8-bit, 256 colours of the set palette; UVs are 0..255 on a 256 x 256 texture)
Look:   studio_set_list_textures(set); studio_texture_preview(source='set3:CITY1.ILBM', set_name='set3',
        uv_grid=true) shows it in game colours with UV lines (pick set_uvs rects from it).
Make:   layers on a base picture: source = a set texture ('set3:CITY1.ILBM'), an ILBM, or a PNG/JPG (for
        example a picture made with an image generator), then layers in order. Always preview first.
- for a model being built: studio_make_texture('Models/Base/New/<Folder>/NXWALL.ILBM', layers, set_name='set3')
  writes it beside the BASE; then {"op":"set_texture","select":...,"texture":"NXWALL.ILBM"} and set_uvs.
  install_model copies it into the set later.
- directly in a set: {"op":"add_texture","name":"NXWALL.ILBM","layers":[...]} in studio_set_edit; replace=true
  repaints an original (Set1-Set7 only; size kept). New names: up to 12 characters + .ILBM, unique in the set,
  and not longer than the texture name they replace on a model.
Style: originals are painted, slightly noisy, mid-dark, few saturated colours; facades are window grids on
  concrete/brick, damaged versions (F-prefixed atlases like FCITY1) are darker with soot. Reuse crops of the
  set's own atlases ('image' layer with crop) before inventing new patterns.
LAYERS:
{layers}
Example facade: [{"op":"fill","color":"#5a554c"}, {"op":"noise","amount":10,"grain":2},
  {"op":"windows","cols":6,"rows":10,"glass":"#263038","frame":"#3a3630","lit":0.08},
  {"op":"dirt","amount":0.3,"blotches":30}]
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
                        "'ops' | 'sets' | 'set_ops' | 'textures' | 'all')")
    texts = {"design": DESIGN, "conventions": CONVENTIONS, "selectors": SELECTORS, "uv": UV, "recipes": RECIPES,
             "wireframe": WIREFRAME, "sets": SETS}
    if topic == "ops":
        return _ops_text()
    if topic == "set_ops":
        from studio_mcp import sets
        return sets.ops_help()
    if topic == "textures":
        from studio_mcp import textures
        return TEXTURES.replace("{layers}", textures.layers_help())
    if topic in texts:
        return texts[topic]
    return f"Unknown topic {topic!r}. Topics: {', '.join(TOPICS)}, all"
