# OpenUAStudio

OpenUAStudio is an independent, community-developed editing workbench for OpenUA and Microsoft Urban Assault (1998).

The project brings together tools and workflows for inspecting, editing, converting, and creating compatible game data.

Its structure, interface, supported formats, features, and integrated editors may change as development continues.

## Project status

OpenUAStudio is under active development.

Features, file layouts, commands, dependencies, workflows, and user-interface elements may be added, removed, renamed, or reorganized without notice.

The current repository should be treated as a development version rather than a final product specification.

## Basic use

Run from source:

```bash
python main.py
```

On normal startup, OpenUAStudio first shows a tool selector for the Main
Suite, Map Editor, Collision Editor, or Wireframe Editor.

A precompiled Windows executable may also be included in the repository for convenience.

## Textured renderer

The model viewer and Snapshot Studio use one **Textured** renderer. It keeps
Urban Assault palette indices through the reconstructed `SHADERMP` /
`TRACYRMP` pipeline until the final RGBA display conversion. The same renderer
is used for the live viewport, camera motion, VANM playback, geometry editing,
single Snapshot exports and batch exports.

Viewport frames are rendered at the real target resolution; there is no
low-resolution interaction mode and no RGB textured fallback. Completed frames
are cached only while camera, animation, geometry and viewport size remain
unchanged. NumPy is a supported runtime dependency and is included in the
official Windows build. Unsupported or ambiguous source data fails closed
instead of silently switching renderers.

See `RETAIL_INDEXED_RENDERER.md` for behavior, limits, provenance and testing.

## License

Copyright (C) 2025-2026 TeuZzZ-17

The original OpenUAStudio source code and original project components are licensed under the GNU General Public License version 3 only (`GPL-3.0-only`).

See the `LICENSE` file for the complete license terms.

The GNU GPL applies only to material for which the OpenUAStudio copyright holders have the legal authority to grant that license.

It does not relicense third-party software, game data, trademarks, artwork, textures, models, sounds, documentation, or other materials owned by their respective rights holders.

## Third-party game data and asset notice

OpenUAStudio is an unofficial, fan-made project.

It is not affiliated with, endorsed by, sponsored by, or approved by Microsoft, Xbox Game Studios, TerraTools, or any other original publisher, developer, or rights holder connected with Urban Assault.

Microsoft Urban Assault, its name, trademarks, logos, artwork, game data, audiovisual material, and other proprietary content remain the property of their respective owners.

### Sector preview images

The Map Editor includes visual preview images representing Urban Assault terrain sectors.

These previews are not retail game data files distributed in their original form, nor are they original textures, models, SET.BAS archives, or other source assets extracted directly from the game.

They were rendered from the sector graphics using a visualization utility and were subsequently cropped, processed, upscaled, organized, and adapted for use as functional map-editing references.

The visualization utility used to generate the original previews is not included or distributed with OpenUAStudio.

The previews are included solely to identify terrain sectors and display the editable map grid.

They are not intended to replace the original game, reproduce its underlying data, or provide access to its source assets.

The underlying Urban Assault designs and visual content remain the property of their respective rights holders.

Only the original processing, organization, tool integration, source code, and other independently created OpenUAStudio components are claimed by the project author.

The presence of these preview images in this repository:

- does not transfer ownership;
- does not grant additional rights to copy, sell, sublicense, or redistribute them;
- does not imply endorsement by the original rights holders;
- does not convert proprietary game content into free or open-source material;
- does not place third-party visual content under the GNU GPL.

Users are responsible for obtaining and using game data lawfully and for complying with applicable copyright, trademark, and other laws in their jurisdiction.

This notice is intended to clarify ownership and project scope.

It is not legal authorization to redistribute third-party material and does not replace permission from the relevant rights holders.

A rights holder who believes that material has been included improperly may contact the repository owner through the GitHub repository so the material can be reviewed.

## Safety and data handling

Treat original game files as read-only whenever possible.

Save edited assets and levels to explicit output paths and keep backups of source data.

OpenUAStudio is intended to support safe editing workflows, but users remain responsible for protecting their own files and installations.

## Warranty

OpenUAStudio is provided without warranty.

Use it at your own risk.

The full warranty disclaimer and limitation of liability are contained in the GNU GPL v3 license text.
