# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Local Streamlit tool that copies installed Project Zomboid Workshop mods into an independent Workshop project, giving each copy a prefixed `modId` so it can coexist with the originals. Target: Build **42.20.4** (not validated on 42.21). See also `AGENTS.md`; note that its "no automated tests" statement is out of date.

## Commands

Run from the repository root (PowerShell, Python 3.10+). Dependencies are installed into a local folder, not globally:

```powershell
python -m pip install --target .batman_modpack_deps -r requirements-batman-modpack.txt
python run_batman_modpack.py          # serves http://127.0.0.1:8501
python -m py_compile batman_modpack_builder.py mod_conflicts.py run_batman_modpack.py
python -m unittest test_mod_conflicts test_mod_names test_mod_updates test_settings -v
python -m unittest test_mod_names.ModNameTests.test_prefix_preserves_metadata_and_is_idempotent -v
```

- Tests use `unittest` (not pytest). Each test file prepends `.batman_modpack_deps` to `sys.path`, so run them from the repo root after installing dependencies.
- Tests build fake mods and packs in temporary directories. UI tests drive `app.main()` via `streamlit.testing.v1.AppTest.from_string(...)` and patch module constants such as `SETTINGS_FILE` and `WORKSHOP`.
- `test_mod_updates` needs `git` on PATH.
- There is no formatter or linter. Avoid unrelated reformatting.

## Architecture

Everything lives in `batman_modpack_builder.py`. Pure logic comes first and the Streamlit UI comes last. `streamlit` is imported only inside the `render_*` and `main` functions, so the logic can be imported and tested without the UI. `run_batman_modpack.py` only adds the local dependency folder to `sys.path` and calls Streamlit's CLI with loopback-only settings.

**Data model.** `PackConfig` (project folder, prefix, `pack_id`, `source_root`) determines every output path: `mods_root = project/Contents/mods` and the main pack entry point `variant = mods_root/<pack_id>/42.20`. `Mod` is a frozen record for one `mod.info` found in the Workshop. Generated values are deterministic:
- `Mod.new_id(prefix)`: `<prefix><workshopId>_<slug>_<sha256[:8]>`
- `Mod.destination(config)`: `mods_root/<prefix><workshopId>_<folder>`

Changing either formula breaks existing packs, because `load_pack_components` checks the manifest against them.

**Pipeline:**
1. `scan_catalog` reads `mod.info` files under the Workshop root. `choose_variant` selects the best version folder (root, `common`, or `42.x`) for `TARGET_VERSION`. The UI caches the result with `st.cache_data`, keyed by `CATALOG_SCHEMA`. Increment `CATALOG_SCHEMA` when the `Mod` shape or scanning logic changes.
2. `resolve_dependencies` follows `require=` recursively. `conflict_names` and `declared_conflicts` handle `incompatible=` declarations in either direction. `read_saved_lists` and `match_saved_ids` import lists from the game's `pz_modlist_settings.cfg` in read-only mode.
3. `inspect_selection` and `inspect_mod_files` run in a thread pool. They report errors (missing dependencies, ID collisions) and warnings (shared `media` paths, internal references). The user must acknowledge warnings (`allow_internal_refs`).
4. `build` is additive and non-destructive:
   - It loads the components already recorded in the manifest (`batman-modpack-selection.json`).
   - It checks that the current main `mod.info` exactly equals `update_pack_info(backup, existing)`. The backup is `mod.info.before-modpack-builder.bak`. If the file was edited by hand, the build stops.
   - It copies each new mod into a temporary staging folder and rewrites every `mod.info` there using `rewrite_info`. That rewrite changes `id`, `require`, `loadModAfter`, `loadModBefore` and `incompatible`, adds the original IDs to `incompatible`, and prefixes `name=` with `[<project folder>]`. Mods with no version folder also get a `42.20` copy.
   - It moves the staged copies to new destination folders only. It never overwrites a destination.
   - It writes the main `mod.info`. The generated `require` and `loadModAfter` entries sit between the `BEGIN` and `END` marker lines.
   - It writes the manifest, then re-reads the files to verify them.
5. `prefix_existing_mod_names` migrates older packs to the display-name prefix. It writes `mod.info.before-name-prefix.bak` backups and is idempotent.

**Conflicts tab.** `mod_conflicts.py` holds the static detector and has no dependency on the main module (no import cycle). `scan_footprint` reads only `media/lua/**/*.lua` and `media/scripts/**/*.txt` from the active roots (`common`, then the chosen variant, which overrides it). It records global Lua functions a mod defines, skipping names rooted in locals and parameters, and notes whether each one keeps a reference to the original function (`chained`). It also records item, `craftRecipe` and `vehicle` blocks as `Module.Name`. `find_conflicts` reports cross-mod overlaps. `analyse_conflicts` and `render_conflicts` in the main module connect this to `Mod`. `render_pack_builder` returns `(catalog, existing + pending mods)` so the tab can analyse the current selection.

**Git updates tab.** `run_mod_git` runs `git` without a shell. The other `mod_*` helpers use it to initialise a repository in the source folder, commit a baseline, and list changes against a commit, grouped by mod. This feature only compares files; it never changes pack components.

**Settings.** `load_source_settings` and `save_source_settings` persist the source folder in `.modpack-builder-settings.json`, which is excluded from Git. `discover_workshop_paths` reads Steam's `libraryfolders.vdf`, and `search_workshop_paths` stops after 20,000 folders. When the source folder changes, `render_settings` clears the selection and verification keys in `st.session_state`. The catalog reloads because its cache key includes the source path.

## Constraints

- All user-facing text (UI, errors, README, CHANGELOG) is in **French**. Keep it French.
- `WORKSHOP`, `DEFAULT_WORKSHOP_ROOT` and `SAVED_LISTS_FILE` are hard-coded to the author's machine. Tests patch them, and the UI replaces `WORKSHOP` with the saved setting.
- Source mods and saved lists must stay read-only. `validate_config` rejects any output folder inside the source tree. Keep the backup, manifest and staging-then-copy safeguards.
- Read and write `mod.info` as bytes. Preserve the BOM, CRLF or LF line endings, and the final newline, following the existing helpers.
- User-visible changes go in both `CHANGELOG.md` and the README changelog section, dated and written in French.
- Passing tests or a successful generation does not show that a pack works in the game. Report in-game validation separately.
