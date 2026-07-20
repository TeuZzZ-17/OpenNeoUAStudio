# OpenUAStudio

OpenUAStudio is the unified editing workbench for **OpenUA / Microsoft Urban Assault (1998)**. It combines the 3D asset tools, BASE and SET.BAS workflows, texture conversion, the Wireframe Editor, and the Map Editor in one project.

## Integrated editors

From the **Tools** menu:

- **Wireframe Editor** opens the SKL/SKLT wireframe editor.
- **Map Editor** opens the integrated Map Editor for creating and editing Urban Assault LDF maps.

The Map Editor is stored inside the `map_editor/` subfolder with its original map-editing logic and resources. It runs in a separate process because OpenUAStudio uses Qt while Map Editor uses Tk. This keeps both interfaces stable while presenting them through one application.

## Running from source

```bash
python main.py
```

The Map Editor can also be launched directly through the OpenUAStudio entry point:

```bash
python main.py --map-editor
python main.py --map-editor path/to/level.ldf
```

Required Python packages include PySide6 and Pillow. Tkinter must be available in the Python installation.

## Windows one-file build

The included `OpenUAStudio.spec` packages the Map Editor code and its resources:

```bash
pyinstaller --noconfirm --clean OpenUAStudio.spec
```

## Safety

Original files should be treated as read-only whenever possible. Save edited assets and levels to explicit output paths and keep backups of source data.
