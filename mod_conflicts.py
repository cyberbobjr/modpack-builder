"""Static, read-only conflict detection between Project Zomboid mods.

The analysis is indicative: it finds places where two mods touch the same
global Lua function, ship the same Lua file or redefine the same script
object. Only in-game testing confirms an actual problem.
"""

from __future__ import annotations

import bisect
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

MAX_FILE_SIZE = 2_000_000
SEVERITY_HIGH = "Élevée"
SEVERITY_MEDIUM = "Moyenne"
SEVERITY_LOW = "Faible"
SEVERITY_ORDER = (SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW)
KIND_FUNCTION = "Fonction Lua redéfinie"
KIND_LUA_FILE = "Fichier Lua remplacé"
KIND_DEFINITION = "Définition de script en double"
SCRIPT_KINDS = {"item": "Objet", "craftRecipe": "Recette", "vehicle": "Véhicule"}

LUA_NAME = r"[A-Za-z_]\w*(?:[.:][A-Za-z_]\w*)*"
LUA_COMMENT_RE = re.compile(r"--\[(=*)\[.*?\]\1\]|--[^\n]*", re.S)
FUNCTION_DEF_RE = re.compile(rf"^[ \t]*function[ \t]+({LUA_NAME})[ \t]*\(", re.M)
FUNCTION_ASSIGN_RE = re.compile(rf"^[ \t]*({LUA_NAME})[ \t]*=[ \t]*function\b", re.M)
LOCAL_RE = re.compile(r"\blocal[ \t]+(?:function[ \t]+)?([A-Za-z_][\w \t,]*)")
PARAMETERS_RE = re.compile(r"\bfunction\b[^(\n]*\(([^)]*)\)")
FOR_RE = re.compile(r"\bfor[ \t]+([A-Za-z_][\w \t,]*?)[ \t]*(?:\bin\b|=)")
SCRIPT_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
MODULE_RE = re.compile(r"(?:^|\})[ \t]*module[ \t]+([A-Za-z_]\w*)", re.M)
# A block starts a line or follows "{"; recipe inputs such as "item 1 [Base.Nails]" never match.
DEFINITION_RE = re.compile(
    r"(?:^|\{)[ \t]*(" + "|".join(SCRIPT_KINDS) + r")[ \t]+([A-Za-z_][\w.]*)[ \t]*(?:\{|\r?$)", re.M
)


@dataclass(frozen=True)
class LuaOverride:
    target: str
    location: str
    chained: bool


@dataclass(frozen=True)
class ModFootprint:
    label: str
    overrides: tuple[LuaOverride, ...] = ()
    lua_files: tuple[str, ...] = ()
    definitions: tuple[tuple[str, str, str], ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Conflict:
    severity: str
    kind: str
    target: str
    mods: tuple[str, ...]
    details: tuple[str, ...]


def _blank_preserving_lines(pattern: re.Pattern[str], text: str) -> str:
    return pattern.sub(lambda match: "\n" * match.group(0).count("\n"), text)


def _line_number(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def local_names(code: str) -> set[str]:
    """Names declared as locals, parameters or loop variables anywhere in a file."""
    names = {"self"}
    for pattern in (LOCAL_RE, PARAMETERS_RE, FOR_RE):
        for match in pattern.finditer(code):
            names.update(part.split()[0] for part in match.group(1).split(",") if part.strip())
    return names


def _is_chained(code: str, target: str) -> bool:
    """True when the file keeps a reference to the previous function value."""
    reference = r"[.:]".join(re.escape(part) for part in target.split("."))
    return re.search(rf"=\s*(?:_G\.)?{reference}(?![\w.:])(?!\s*\()", code) is not None


def scan_lua(text: str, location: str) -> list[LuaOverride]:
    """Find global functions a Lua file (re)defines, skipping locals and parameters."""
    code = _blank_preserving_lines(LUA_COMMENT_RE, text)
    locals_ = local_names(code)
    found: dict[str, LuaOverride] = {}
    for pattern in (FUNCTION_DEF_RE, FUNCTION_ASSIGN_RE):
        for match in pattern.finditer(code):
            target = match.group(1).replace(":", ".").removeprefix("_G.")
            if target.split(".")[0] in locals_ or target in found:
                continue
            found[target] = LuaOverride(
                target, f"{location}:{_line_number(code, match.start(1))}", _is_chained(code, target)
            )
    return list(found.values())


def scan_script(text: str, location: str) -> list[tuple[str, str, str]]:
    """Return (kind, Module.Name, location) for item, craftRecipe and vehicle blocks."""
    code = _blank_preserving_lines(SCRIPT_COMMENT_RE, text)
    modules = [(match.start(), match.group(1)) for match in MODULE_RE.finditer(code)]
    starts = [start for start, _ in modules]
    definitions = []
    for match in DEFINITION_RE.finditer(code):
        index = bisect.bisect_right(starts, match.start()) - 1
        module = modules[index][1] if index >= 0 else "Base"
        name = match.group(2) if "." in match.group(2) else f"{module}.{match.group(2)}"
        definitions.append(
            (SCRIPT_KINDS[match.group(1)], name, f"{location}:{_line_number(code, match.start(1))}")
        )
    return definitions


def active_media_files(roots: Iterable[Path]) -> dict[str, Path]:
    """Map media-relative paths to files; later roots override earlier ones like the game."""
    files: dict[str, Path] = {}
    for root in roots:
        media = root / "media"
        for directory, _, names in os.walk(media):
            for name in names:
                path = Path(directory) / name
                files["/".join(path.relative_to(media).parts).casefold()] = path
    return files


def scan_footprint(label: str, folder: Path, roots: Iterable[Path]) -> ModFootprint:
    """Collect Lua overrides, Lua file paths and script definitions of one mod."""
    overrides: list[LuaOverride] = []
    lua_files: list[str] = []
    definitions: list[tuple[str, str, str]] = []
    errors: list[str] = []
    for relative, path in sorted(active_media_files(roots).items()):
        is_lua = relative.startswith("lua/") and relative.endswith(".lua")
        is_script = relative.startswith("scripts/") and relative.endswith(".txt")
        if not (is_lua or is_script):
            continue
        location = "/".join(path.relative_to(folder).parts)
        try:
            if path.stat().st_size > MAX_FILE_SIZE:
                errors.append(f"Fichier trop volumineux ignoré : {label} · {location}")
                continue
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            errors.append(f"Lecture impossible : {label} · {location} ({exc})")
            continue
        if is_lua:
            lua_files.append(relative)
            overrides.extend(scan_lua(text, location))
        else:
            definitions.extend(scan_script(text, location))
    return ModFootprint(label, tuple(overrides), tuple(lua_files), tuple(definitions), tuple(errors))


def scan_footprints(
    items: list[tuple[str, Path, list[Path]]], *, workers: int = 4,
    progress: Callable[[int, int], None] | None = None,
) -> list[ModFootprint]:
    """Scan (label, mod folder, active roots) entries in parallel, keeping input order."""
    results: list[ModFootprint | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as executor:
        futures = {executor.submit(scan_footprint, *item): index for index, item in enumerate(items)}
        for completed, future in enumerate(as_completed(futures), start=1):
            results[futures[future]] = future.result()
            if progress is not None:
                progress(completed, len(items))
    return [result for result in results if result is not None]


def _media_path(location: str) -> str:
    return location.rsplit(":", 1)[0].split("media/", 1)[-1].casefold()


def _function_conflicts(footprints: list[ModFootprint], shared_files: set[str]) -> list[Conflict]:
    by_target: dict[str, list[tuple[str, LuaOverride]]] = {}
    for footprint in footprints:
        for override in footprint.overrides:
            by_target.setdefault(override.target, []).append((footprint.label, override))
    conflicts = []
    for target, entries in by_target.items():
        mods = tuple(dict.fromkeys(label for label, _ in entries))
        if len(mods) < 2:
            continue
        paths = {_media_path(override.location) for _, override in entries}
        if len(paths) == 1 and paths <= shared_files:
            continue  # Already reported as a replaced Lua file.
        unchained = any(not override.chained for _, override in entries)
        conflicts.append(Conflict(
            SEVERITY_HIGH if unchained else SEVERITY_LOW, KIND_FUNCTION, target, mods,
            tuple(
                f"{label} · {override.location} · "
                + ("conserve l'original" if override.chained else "n'appelle pas l'original")
                for label, override in entries
            ),
        ))
    return conflicts


def find_conflicts(footprints: list[ModFootprint]) -> list[Conflict]:
    """Group cross-mod overlaps, most severe first."""
    by_file: dict[str, list[str]] = {}
    by_definition: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for footprint in footprints:
        for relative in dict.fromkeys(footprint.lua_files):
            by_file.setdefault(relative, []).append(footprint.label)
        for kind, name, location in footprint.definitions:
            by_definition.setdefault((kind, name), []).append((footprint.label, location))
    shared_files = {relative for relative, labels in by_file.items() if len(set(labels)) > 1}
    conflicts = _function_conflicts(footprints, shared_files)
    conflicts.extend(
        Conflict(SEVERITY_HIGH, KIND_LUA_FILE, f"media/{relative}", tuple(dict.fromkeys(by_file[relative])),
                 ("Seul le fichier du dernier mod chargé est exécuté.",))
        for relative in shared_files
    )
    for (kind, name), entries in by_definition.items():
        mods = tuple(dict.fromkeys(label for label, _ in entries))
        if len(mods) > 1:
            conflicts.append(Conflict(
                SEVERITY_MEDIUM, KIND_DEFINITION, f"{kind} {name}", mods,
                tuple(f"{label} · {location}" for label, location in entries),
            ))
    return sorted(conflicts, key=lambda item: (SEVERITY_ORDER.index(item.severity), item.kind, item.target.casefold()))
