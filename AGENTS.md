# Repository Guidelines

## Project Structure & Module Organization

- `batman_modpack_builder.py` contains the data models, Workshop scanning, dependency and conflict checks, metadata rewriting, pack generation, and Streamlit interface.
- `run_batman_modpack.py` launches the interface using dependencies in `.batman_modpack_deps/`.
- `requirements-batman-modpack.txt` declares runtime dependencies; `README.md` documents usage in French.
- There are no dedicated source, test, or asset directories. Preview images are generated programmatically. Generated packs live in a separate Workshop destination under `Contents/mods/`.

## Build, Test, and Development Commands

Use Python 3.10 or newer. Run these commands from the repository root in PowerShell:

```powershell
python -m pip install --target .batman_modpack_deps -r requirements-batman-modpack.txt
python run_batman_modpack.py
```

The first command installs local dependencies; the second serves the app at `http://127.0.0.1:8501`. No separate build step is required.

Check Python syntax after code changes:

```powershell
python -m py_compile batman_modpack_builder.py run_batman_modpack.py
```

## Coding Style & Naming Conventions

Follow the existing four-space indentation and Python naming conventions: `snake_case` functions and variables, `PascalCase` dataclasses, and `UPPER_CASE` constants. Use type annotations, `pathlib.Path` for filesystem paths, and small helpers for logic outside the interface. Preserve French user-facing text and explicit text encodings. No formatter or linter is configured; avoid unrelated formatting changes.

## Testing Guidelines

No automated testing framework or coverage threshold is configured. For relevant changes, manually verify catalog search, recursive dependency selection, declared conflicts, saved-list imports, initial pack creation, and additions to an existing pack. Use a disposable output project and inspect rewritten `mod.info` files and `batman-modpack-selection.json`.

The current target is Project Zomboid 42.20.4. Report in-game validation separately; syntax checks and successful generation do not establish game compatibility.

## Commit & Pull Request Guidelines

This checkout has no Git metadata, so historical commit conventions cannot be verified. Use concise imperative subjects, such as `Fix dependency resolution for saved lists`. Keep changes focused. Pull requests should describe the behavior change, reference relevant issues, list validation performed, and include screenshots for interface changes.

## Configuration & Data Safety

Review the Workshop and saved-list path constants before running locally. Keep source mods and saved lists read-only, and keep output outside the source Workshop tree. Preserve manifest and backup behavior. Exclude local dependencies, generated packs, and personal game data from contributions.
