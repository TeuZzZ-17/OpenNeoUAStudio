"""Build the Sulgogar terrain textures from the player's OWN installed Set.BAS files.

Nothing from the original game is shipped: the textures are made here, on the player's machine, from
`Data/Sets/Set3` only (the Sulgogar flesh atlas FBAGGER and the ground textures of the night set),
then written to `Data/Sets/Set3/Loose/ILBM` where the game reads them. Run it again after changing the recipe.

Usage:  python tools/build_sulg_terrain_textures.py [--data "<game Data folder>"]

Writes (all in the palette of Set3):
  SGSKIN SGTORN SGASH1   station flesh: intact, torn, ash
  SGGRND                 infected ground atlas (flesh blended into Set3 BODEN5 at the edges)
  SGCORE                 animated green core (drawn here, no original pixels)
  SGT<a><b><H|V>         ground strips between infected ground (family 5) and the families 0-5 of the set
The station and strip models only reference these names; they hold no pixels.
It also writes the strip skeletons G<a><b><H|V>.sklt (the set's own S00H/S00V) to Sets/Set3/Loose/SKLT.
"""
from __future__ import annotations

import argparse
import math
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default=os.environ.get("ONUA_DATA", ""), help="game Data folder (default: ONUA_DATA)")
    parser.add_argument("--keep-in-models", action="store_true",
                        help="also keep a copy beside the models (Data/Models/Base/New/Sulgogar/Sulg_Power_Station/Textures)")
    return parser.parse_args()


def main():
    args = _args()
    if not args.data or not (Path(args.data) / "Sets").is_dir():
        sys.exit("Give the game Data folder with --data (it must contain Sets/Set3).")
    data = Path(args.data)
    os.environ["ONUA_DATA"] = str(data)
    os.environ["NME_GAME_DATA"] = str(data)
    from studio_mcp import core, textures      # imported after the data folder is known

    work = Path(data) / "Models/Base/New/Sulgogar/Sulg_Power_Station/Textures"   # Studio may only write under Models
    loose = data / "Sets/Set3/Loose/ILBM"
    loose.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="sulg_tex_"))

    def rgb(spec, set_name="set3"):
        return textures.load_picture(spec, set_name).convert("RGB")

    def save(name, picture):
        work.mkdir(parents=True, exist_ok=True)
        png = scratch / (name + ".png")
        picture.save(png)
        target = work / (name + ".ILBM")
        textures.make(str(target), [], source=str(png), set_name="set3", overwrite=target.exists())
        shutil.copy2(target, loose / target.name)
        if not args.keep_in_models:
            target.unlink()
        print("wrote", loose / target.name)

    # ---- station flesh and ground atlas -------------------------------------------------------------------
    # Identical to the Sulgogar Queen / Slime Lord in the night set: the same flesh region of the Set3 FBAGGER,
    # pre-darkened by the Queen's mean polygon light (shade 122 -> 1 - 122/256 = 0.524), so ground and buildings
    # (shade ~0) look like one piece with her hull.
    flesh = rgb("set3:FBAGGER.ILBM").crop((100, 6, 250, 96)).resize((128, 128))
    pixels = np.asarray(flesh).astype(float) * 0.524
    tile = Image.fromarray(np.clip(pixels, 0, 255).astype("uint8"))
    for axis in range(2):                       # blend opposite edges so the tile repeats without a seam
        values = np.asarray(tile).astype(float).copy()
        for k in range(8):
            t = (8 - k) / 8
            if axis == 0:
                mean = (values[:, k] + values[:, -1 - k]) * .5
                values[:, k] = (1 - t) * values[:, k] + t * mean
                values[:, -1 - k] = (1 - t) * values[:, -1 - k] + t * mean
            else:
                mean = (values[k] + values[-1 - k]) * .5
                values[k] = (1 - t) * values[k] + t * mean
                values[-1 - k] = (1 - t) * values[-1 - k] + t * mean
        tile = Image.fromarray(np.clip(values, 0, 255).astype("uint8"))
    skin = tile.resize((256, 256), Image.Resampling.NEAREST)
    save("SGSKIN", skin)

    # torn skin: darker, reddish tissue under cracks and scorched patches (the pattern of the skin shows through)
    mask = Image.new("L", skin.size)
    draw = ImageDraw.Draw(mask)
    rng = random.Random(731)
    for k in range(9):
        x, y = rng.randrange(15, 235), rng.randrange(10, 220)
        line = [(x, y)]
        for _ in range(5):
            x += rng.randrange(-17, 18)
            y += rng.randrange(7, 18)
            line.append((x, y))
        draw.line(line, fill=140 if k % 2 else 180, width=2 if k % 2 else 4)
        if k % 3 == 0:
            draw.polygon([(x - 14, y - 24), (x + 9, y - 32), (x + 20, y + 6), (x - 4, y + 18)], fill=130)
    amount = np.asarray(mask).astype(float)[:, :, None] / 255
    base_skin = np.asarray(skin).astype(float)
    burnt = base_skin * np.array([.62, .52, .48])
    save("SGTORN", Image.fromarray(np.clip(base_skin * (1 - amount) + burnt * amount, 0, 255).astype("uint8")))
    save("SGASH1", Image.blend(skin, Image.new("RGB", (256, 256), (15, 14, 9)), .62))

    original = rgb("set3:BODEN5.ILBM")
    atlas = original.copy()
    atlas.paste(tile, (0, 0))
    for qx, qy in [(1, 0), (0, 1), (1, 1)]:      # three quadrants fade the flesh into the clean set ground
        for y in range(128):
            for x in range(128):
                t = x / 127 if (qx, qy) == (1, 0) else y / 127
                alpha = max(0, min(1, 1 - t))
                alpha = alpha * alpha * (3 - 2 * alpha)
                clean = original.getpixel((255 if qx else x, qy * 128 + 127 if qy else y))
                f = tile.getpixel((x, y))
                atlas.putpixel((qx * 128 + x, qy * 128 + y),
                               tuple(round(alpha * f[c] + (1 - alpha) * clean[c]) for c in range(3)))
    save("SGGRND", atlas)

    # ---- green core: four animated frames, drawn here ------------------------------------------------------
    arr = np.zeros((256, 256, 3), dtype=np.uint8)
    y, x = np.mgrid[:128, :128]
    cloud = Image.fromarray((np.random.default_rng(50).random((16, 16)) * 255).astype("uint8")).resize(
        (128, 128), Image.Resampling.BICUBIC)
    cloud = np.asarray(cloud).astype(float) / 255
    for frame in range(4):
        wave = np.clip(.75 * np.roll(cloud, (frame * 5, frame * 3), (0, 1))
                       + .25 * (np.sin(x * .08 + y * .045 + frame * .9) + 1) / 2, 0, 1)
        cx, cy = (frame % 2) * 128, (frame // 2) * 128
        arr[cy:cy + 128, cx:cx + 128, 0] = 35 + wave * 90
        arr[cy:cy + 128, cx:cx + 128, 1] = 115 + wave * 130
        arr[cy:cy + 128, cx:cx + 128, 2] = 5 + wave * 22
    save("SGCORE", Image.fromarray(arr))

    # ---- strips between the families (reads the set's own strip models only to measure them) --------------
    def affine(source, target):
        matrix = np.array([[p[0], p[1], 1.] for p in source[:3]])
        return np.linalg.solve(matrix, np.array(target[:3], dtype=float))

    class GroundSampler:
        def __init__(self, texture, points, uvs):
            self.image = np.asarray(texture.convert("RGB"))
            self.affine = affine([[p[0], p[2]] for p in points], uvs)

        def __call__(self, world, center):
            x, z = np.asarray(world) - center
            px = ((x + 450.) % 300.) - 150.
            pz = 150. - ((450. - z) % 300.)
            if abs(x - 450.) < 1e-5: px = 150.
            if abs(x + 450.) < 1e-5: px = -150.
            if abs(z - 450.) < 1e-5: pz = 150.
            if abs(z + 450.) < 1e-5: pz = -150.
            uv = np.array([px, pz, 1.]) @ self.affine
            u, v = np.clip(np.floor(uv + 1e-6).astype(int), 0, 255)
            return self.image[v, u].astype(float)

    def native_ground(source):
        src = core.resolve_source(source)
        geometry = core.geometry(src, with_uvs=True)
        poly = geometry["polygons"][0]
        block = core.load_family(src).root_object.base_object.ades[poly["block"]]
        return GroundSampler(rgb("set3:" + block.texture.name), [geometry["points"][i] for i in poly["points"]], poly["uv"])

    square = [[-150, 0, 150], [150, 0, 150], [150, 0, -150], [-150, 0, -150]]
    samplers = {0: native_ground("set3:GR_EMTPY"), 1: native_ground("set3:ST_EMPTY"),   # GR_EMTPY: the set's own spelling
                2: native_ground("set3:GS_EMPTY"), 3: native_ground("set3:BP_MMMMM"),
                4: native_ground("set3:ST_EMPTY"),
                5: GroundSampler(atlas, square, [[0, 0], [127, 0], [127, 127], [0, 127]])}
    for suffix in ("H", "V"):
        for a in range(6):
            for b in range(6):
                if 5 not in (a, b):
                    continue
                ref = core.geometry(core.resolve_source(f"set3:S{3 if a == 5 else a}{3 if b == 5 else b}{suffix}"),
                                    with_uvs=True)
                points = np.asarray(ref["points"])[:, [0, 2]]
                edge0, edge1 = points[:4].mean(axis=0), points[4:8].mean(axis=0)
                depth = edge1 - edge0
                length = np.linalg.norm(depth)
                direction = depth / length
                centers = [edge0 - direction * 450, edge1 + direction * 450]
                out = np.zeros((256, 256, 3), dtype=np.uint8)
                for ids, (ox, oy) in (([1, 2, 3], (0, 0)), ([0], (128, 0)), ([4], (0, 128))):
                    poly = ref["polygons"][ids[0]]
                    if len(ids) == 3:
                        old = np.array(poly["uv"], dtype=float)
                        low = old.min(axis=0)
                        patch = (old - low) * 127 / (old.max(axis=0) - low)
                    else:
                        patch = np.array([[0., 0.], [127., 0.], [0., 127.]])
                    transform = affine(patch, points[poly["points"]])
                    for v in range(128):
                        for u in range(128):
                            world = np.array([u, v, 1.]) @ transform
                            t = max(0., min(1., float(np.dot(world - edge0, direction) / length)))
                            across = float(np.dot(world, np.array([-direction[1], direction[0]])))
                            t = max(0., min(1., t + .055 * math.sin(across * .021) * math.sin(math.pi * t)))
                            mix = t * t * (3 - 2 * t)
                            c0, c1 = samplers[a](world, centers[0]), samplers[b](world, centers[1])
                            out[oy + v, ox + u] = np.clip(np.rint(c0 * (1 - mix) + c1 * mix), 0, 255)
                save(f"SGT{a}{b}{suffix}", Image.fromarray(out))
    # The engine loads the strip models from Sets/Set3/Objects as loose files and finds their skeleton in Loose/SKLT.
    # Write that skeleton from the set's own copy (identical for every strip of a direction).
    archive, _ = core._setbas("set3")
    skeleton_dir = data / "Sets/Set3/Loose/SKLT"
    skeleton_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("H", "V"):
        resource = next(r for r in archive.find(f"S00{suffix}.sklt", "sklt.class") if r.decodable)
        payload = archive.payload_bytes(resource)
        for a in range(6):
            for b in range(6):
                if 5 in (a, b):
                    (skeleton_dir / f"G{a}{b}{suffix}.sklt").write_bytes(payload)
    shutil.rmtree(scratch, ignore_errors=True)
    print("done: textures are in", loose, "and strip skeletons in", skeleton_dir)


if __name__ == "__main__":
    main()
