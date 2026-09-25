"""Streamlit builder for a Project Zomboid 42.20 Workshop mod collection.

Run: python -m streamlit run batman_modpack_builder.py
Each generated mod has its own sibling directory under Contents/mods.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import regex as regex_engine


WORKSHOP = Path(r"D:\SteamLibrary\steamapps\workshop\content\108600")
SETTINGS_FILE = Path(__file__).with_name(".modpack-builder-settings.json")
DEFAULT_WORKSHOP_ROOT = Path(r"C:\Users\cyber\Zomboid\Workshop")
DEFAULT_PROJECT_FOLDER = "modpack-42-20"
SAVED_LISTS_FILE = Path(r"C:\Users\cyber\Zomboid\Lua\pz_modlist_settings.cfg")
TARGET_VERSION = (42, 20, 4)
TEXT_SUFFIXES = {".lua", ".txt", ".json", ".xml", ".ini", ".properties"}
MANIFEST_NAME = "batman-modpack-selection.json"
CATALOG_SCHEMA = 4
BEGIN = "# BEGIN BATMAN MODPACK BUILDER"
END = "# END BATMAN MODPACK BUILDER"
INVALID_ID_RE = re.compile(r"[,\\\x00-\x1f]")
PREFIX_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
PACK_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
PROJECT_FOLDER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]*\Z")


@dataclass(frozen=True)
class PackConfig:
    project: Path
    prefix: str
    pack_id: str
    source_root: Path = WORKSHOP

    @property
    def mods_root(self) -> Path:
        return self.project / "Contents" / "mods"

    @property
    def variant(self) -> Path:
        return self.mods_root / self.pack_id / "42.20"


def validate_config(config: PackConfig) -> list[str]:
    errors = []
    if not PREFIX_RE.fullmatch(config.prefix):
        errors.append("Préfixe invalide : utiliser des lettres ASCII, chiffres, _ ou -, en commençant par une lettre.")
    if not PACK_ID_RE.fullmatch(config.pack_id):
        errors.append("modId principal invalide : utiliser des lettres ASCII, chiffres, _ ou -.")
    if not config.project.is_absolute() or config.project == Path(config.project.anchor):
        errors.append("Le dossier de destination doit être un chemin absolu vers un projet Workshop.")
    if config.project.resolve() == config.source_root.resolve() or config.source_root.resolve() in config.project.resolve().parents:
        errors.append("Le projet de sortie ne peut pas se trouver dans les sources Workshop à copier.")
    if config.project.exists() and not config.project.is_dir():
        errors.append("La destination existe mais n'est pas un dossier.")
    if config.project.is_dir() and any(config.project.iterdir()) and not config.mods_root.is_dir():
        errors.append("Le dossier existe et n'a pas de structure Contents/mods : choisir un dossier vide ou un projet Workshop existant.")
    return errors


def write_preview(path: Path) -> None:
    """Create a small valid 256x256 PNG for a new Workshop project."""
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    row = b"\x00" + b"\x20\x25\x35" * 256
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", 256, 256, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(row * 256))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


@dataclass(frozen=True)
class Mod:
    workshop_id: str
    folder: Path
    mod_id: str
    name: str
    variant: str
    version_min: str
    requires: tuple[str, ...]
    aliases: tuple[str, ...]
    incompatible: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.workshop_id}/{self.folder.name}/{self.mod_id}"

    def new_id(self, prefix: str) -> str:
        short_id = self.mod_id.removeprefix(self.workshop_id + "/")
        slug = re.sub(r"[^A-Za-z0-9]+", "_", short_id).strip("_")[:50] or "mod"
        digest = hashlib.sha256(self.mod_id.encode("utf-8")).hexdigest()[:8]
        return f"{prefix}{self.workshop_id}_{slug}_{digest}"

    def destination(self, config: PackConfig) -> Path:
        return config.mods_root / f"{config.prefix}{self.workshop_id}_{self.folder.name}"


def read_info(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def read_saved_lists(path: Path = SAVED_LISTS_FILE) -> dict[str, tuple[str, ...]]:
    """Read Mod Manager's !fav! row and its named mod lists without editing the file."""
    lists: dict[str, tuple[str, ...]] = {}
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        name, separator, value = line.partition(":")
        if not separator or not name.strip():
            continue
        ids = tuple(dict.fromkeys(
            entry for raw in value.split(";")
            if (entry := raw.strip().removeprefix("?").strip())
        ))
        lists["Favoris" if name == "!fav!" else name] = ids
    return lists


def match_saved_ids(
    saved_ids: tuple[str, ...], catalog: list[Mod]
) -> tuple[list[Mod], list[str], dict[str, list[Mod]]]:
    by_id: dict[str, list[Mod]] = {}
    for mod in catalog:
        for alias in mod.aliases:
            by_id.setdefault(alias, []).append(mod)
    matched: dict[str, Mod] = {}
    missing: list[str] = []
    ambiguous: dict[str, list[Mod]] = {}
    for mod_id in saved_ids:
        choices = by_id.get(mod_id, [])
        if len(choices) == 1:
            matched[choices[0].key] = choices[0]
        elif choices:
            ambiguous[mod_id] = choices
        else:
            missing.append(mod_id)
    return list(matched.values()), missing, ambiguous


def resolve_dependencies(
    roots: set[str], catalog: list[Mod], existing: list[Mod], config: PackConfig
) -> tuple[set[str], set[str], list[str]]:
    """Expand manually selected mods through their declared required dependencies."""
    by_key = {mod.key: mod for mod in catalog}
    by_id: dict[str, list[Mod]] = {}
    for mod in catalog:
        for alias in mod.aliases:
            by_id.setdefault(alias, []).append(mod)
    selected = roots.intersection(by_key)
    selected = {key for key in selected if not by_key[key].destination(config).exists()}
    automatic: set[str] = set()
    fulfilled = {alias for mod in existing for alias in mod.aliases}
    fulfilled.update(alias for key in selected for alias in by_key[key].aliases)
    queue = [by_key[key] for key in sorted(selected)]
    issues: list[str] = []
    while queue:
        mod = queue.pop(0)
        for dependency in mod.requires:
            if dependency in fulfilled:
                continue
            choices = by_id.get(dependency, [])
            if not choices:
                issues.append(f"{mod.name} exige {dependency}, absent du catalogue Workshop.")
                continue
            if len(choices) > 1:
                issues.append(
                    f"{mod.name} exige {dependency}, trouvé dans {len(choices)} mods : cocher la bonne copie manuellement."
                )
                continue
            required = choices[0]
            if required.destination(config).exists():
                issues.append(
                    f"{mod.name} exige {dependency}, mais une copie existe hors manifeste : {required.destination(config)}."
                )
                continue
            if required.key not in selected:
                selected.add(required.key)
                automatic.add(required.key)
                fulfilled.update(required.aliases)
                queue.append(required)
    return selected, automatic, list(dict.fromkeys(issues))


def local_id_collisions(config: PackConfig, generated_ids: set[str]) -> dict[str, Path]:
    """Find IDs already used by a different local Workshop project."""
    collisions: dict[str, Path] = {}
    parent = config.project.parent
    if not parent.is_dir():
        return collisions
    for project in parent.iterdir():
        if not project.is_dir() or project.resolve() == config.project.resolve():
            continue
        mods_root = project / "Contents" / "mods"
        if not mods_root.is_dir():
            continue
        for folder in mods_root.iterdir():
            if not folder.is_dir():
                continue
            variant = choose_variant(folder)
            if variant is None:
                continue
            try:
                mod_id = read_info(variant / "mod.info").get("id", "")
            except OSError:
                continue
            if mod_id in generated_ids:
                collisions[mod_id] = variant / "mod.info"
    return collisions


def version_tuple(name: str) -> tuple[int, ...] | None:
    if not re.fullmatch(r"\d+(?:\.\d+)*", name):
        return None
    parts = tuple(int(x) for x in name.split("."))
    return parts if parts and parts[0] == 42 else None


def choose_variant(folder: Path) -> Path | None:
    variants = []
    for child in folder.iterdir():
        if child.is_dir() and (child / "mod.info").is_file():
            number = version_tuple(child.name)
            if number and number <= TARGET_VERSION:
                variants.append((number, child))
    if variants:
        return max(variants, key=lambda pair: pair[0])[1]
    common = folder / "common"
    if (common / "mod.info").is_file():
        return common
    return folder if (folder / "mod.info").is_file() else None


def matches_search(mod: Mod, query: str) -> bool:
    term = query.casefold().strip()
    return not term or any(
        term in value.casefold()
        for value in (mod.name, mod.mod_id, mod.workshop_id, *mod.aliases)
    )


def workshop_mod_link(mod: Mod) -> str:
    # The fragment carries the visible modId; Steam identifies the page by Workshop ID.
    return f"https://steamcommunity.com/sharedfiles/filedetails/?id={mod.workshop_id}#modid={mod.mod_id}"


def conflict_names(candidates: list[Mod], selected: list[Mod]) -> dict[str, tuple[Mod, ...]]:
    """Find declared incompatibilities in either direction, like the game selector."""
    by_id: dict[str, list[Mod]] = {}
    by_incompatible: dict[str, list[Mod]] = {}
    for mod in selected:
        by_id.setdefault(mod.mod_id, []).append(mod)
        for mod_id in mod.incompatible:
            by_incompatible.setdefault(mod_id, []).append(mod)
    result = {}
    for candidate in candidates:
        opponents: dict[str, Mod] = {}
        for mod_id in candidate.incompatible:
            opponents.update(
                (mod.key, mod) for mod in by_id.get(mod_id, [])
                if mod.key != candidate.key
            )
        opponents.update(
            (mod.key, mod) for mod in by_incompatible.get(candidate.mod_id, [])
            if mod.key != candidate.key
        )
        if opponents:
            result[candidate.key] = tuple(sorted(opponents.values(), key=lambda mod: mod.name.casefold()))
    return result


def declared_conflicts(selected: list[Mod]) -> list[tuple[Mod, Mod]]:
    conflicts = conflict_names(selected, selected)
    return [
        (mod, other)
        for mod in selected for other in conflicts.get(mod.key, ())
        if mod.key < other.key
    ]


def parse_ids(value: str) -> tuple[str, ...]:
    return tuple(x.strip().lstrip("\\") for x in value.split(",") if x.strip())


def valid_source_id(mod_id: str) -> bool:
    return bool(mod_id) and mod_id == mod_id.strip() and not INVALID_ID_RE.search(mod_id)


def scan_catalog(root: Path = WORKSHOP) -> tuple[list[Mod], list[str]]:
    mods: list[Mod] = []
    warnings: list[str] = []
    if not root.is_dir():
        return mods, [f"Workshop absent : {root}"]
    for workshop in sorted(root.iterdir()):
        if not workshop.is_dir() or not workshop.name.isdigit():
            continue
        mod_root = workshop / "mods"
        if not mod_root.is_dir():
            continue
        for folder in sorted(mod_root.iterdir()):
            if not folder.is_dir():
                continue
            try:
                variant = choose_variant(folder)
                if variant is None:
                    continue
                info = read_info(variant / "mod.info")
                mod_id = info.get("id", "")
                if not valid_source_id(mod_id):
                    warnings.append(f"ID non pris en charge : {folder} ({mod_id!r})")
                    continue
                aliases = tuple(dict.fromkeys(
                    declared for path in folder.rglob("mod.info")
                    if valid_source_id(declared := read_info(path).get("id", ""))
                ))
                mods.append(
                    Mod(
                        workshop.name, folder, mod_id,
                        info.get("name", folder.name),
                        variant.name if variant != folder else "racine",
                        info.get("versionMin", ""),
                        parse_ids(info.get("require", "")),
                        aliases,
                        parse_ids(info.get("incompatible", "")),
                    )
                )
            except (OSError, UnicodeError, ValueError) as exc:
                warnings.append(f"Lecture impossible : {folder} ({exc})")
    return mods, warnings


def inspect_mod_files(
    mod: Mod, reference_pattern: regex_engine.Pattern[str] | None
) -> tuple[list[str], list[str], list[str]]:
    """Inspect one mod in one directory walk; safe to run in a worker thread."""
    errors: list[str] = []
    media_paths: list[str] = []
    references: list[str] = []
    active_roots = {mod.folder / "common"}
    variant = choose_variant(mod.folder)
    if variant is not None:
        active_roots.add(variant)
    media_prefixes = {
        (active / "media").relative_to(mod.folder).parts for active in active_roots
    }
    try:
        for directory, _, names in os.walk(mod.folder):
            relative_dir = Path(directory).relative_to(mod.folder)
            for name in names:
                relative = relative_dir / name
                path = mod.folder / relative
                if name == "mod.info":
                    declared = read_info(path).get("id", "")
                    if declared not in mod.aliases:
                        errors.append(f"ID non indexé {declared!r} dans {relative}.")
                    continue
                parts = relative.parts
                for prefix in media_prefixes:
                    if parts[:len(prefix)] == prefix and len(parts) > len(prefix):
                        media_paths.append("/".join(parts[len(prefix):]).casefold())
                        break
                if path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > 2_000_000:
                    continue
                if reference_pattern is not None:
                    content = path.read_text(encoding="utf-8-sig", errors="replace")
                    for referenced in set(reference_pattern.findall(content)):
                        references.append(
                            f"Référence interne à {referenced} dans {mod.mod_id} : {relative}"
                        )
    except OSError as exc:
        errors.append(f"Analyse impossible : {mod.folder} ({exc})")
    return errors, media_paths, references


def inspect_selection(
    selected: list[Mod], catalog: list[Mod], config: PackConfig,
    *, existing_keys: frozenset[str] = frozenset(),
    workers: int = 4, progress: Callable[[int, int], None] | None = None,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    ids = [alias for mod in selected for alias in mod.aliases]
    all_ids = {alias for mod in catalog for alias in mod.aliases}
    if len(ids) != len(set(ids)):
        errors.append("Deux entrées sélectionnées partagent un modId ou un ancien alias.")
    for left, right in declared_conflicts(selected):
        errors.append(f"Incompatibilité déclarée : {left.name} ({left.mod_id}) ↔ {right.name} ({right.mod_id}).")
    reference_pattern = (
        regex_engine.compile(r"(?<![\w.])(?:" + "|".join(re.escape(item) for item in sorted(set(ids), key=len, reverse=True)) + r")(?![\w.])")
        if ids else None
    )
    if any(mod.new_id(config.prefix) == config.pack_id for mod in selected):
        errors.append(f"Collision avec l'identifiant {config.pack_id}.")
    generated_ids = [mod.new_id(config.prefix) for mod in selected]
    generated_paths = [mod.destination(config) for mod in selected]
    if len(set(generated_ids)) != len(generated_ids) or len(set(generated_paths)) != len(generated_paths):
        errors.append("Le préfixe choisi produit des identifiants ou dossiers en double.")
    collisions = set(generated_ids).intersection(all_ids)
    if collisions:
        errors.append("IDs générés déjà déclarés dans le Workshop : " + ", ".join(sorted(collisions)))
    local_collisions = local_id_collisions(config, set(generated_ids) | {config.pack_id})
    if local_collisions:
        errors.append(
            "IDs déjà utilisés dans un autre projet local : "
            + ", ".join(f"{mod_id} ({path})" for mod_id, path in sorted(local_collisions.items()))
        )
    base_info = config.variant / "mod.info.before-modpack-builder.bak"
    if not base_info.is_file():
        base_info = config.variant / "mod.info"
    if base_info.is_file():
        base_requires = parse_ids(read_info(base_info).get("require", ""))
        for dependency in base_requires:
            if dependency not in ids and dependency not in generated_ids:
                errors.append(
                    f"Le mod principal exige {dependency} : l'ajouter pour obtenir un pack autonome."
                )
    for mod in selected:
        if mod.destination(config).exists() and mod.key not in existing_keys:
            errors.append(f"Destination déjà présente : {mod.destination(config)}")
        for dependency in mod.requires:
            if dependency not in ids:
                suffix = " (disponible dans le catalogue)" if dependency in all_ids else ""
                errors.append(f"{mod.mod_id} exige {dependency} : sélectionner sa dépendance{suffix}.")
    file_results = [None] * len(selected)
    if workers <= 1:
        for completed, mod in enumerate(selected, start=1):
            file_results[completed - 1] = inspect_mod_files(mod, reference_pattern)
            if progress is not None:
                progress(completed, len(selected))
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(selected) or 1)) as executor:
            futures = {
                executor.submit(inspect_mod_files, mod, reference_pattern): index
                for index, mod in enumerate(selected)
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                file_results[futures[future]] = future.result()
                if progress is not None:
                    progress(completed, len(selected))
    resource_owner: dict[str, str] = {}
    for mod, result in zip(selected, file_results):
        mod_errors, media_paths, references = result
        errors.extend(mod_errors)
        for relative in media_paths:
            owner = resource_owner.setdefault(relative, mod.mod_id)
            if owner != mod.mod_id:
                warnings.append(f"Même chemin media/{relative} : {owner} et {mod.mod_id}")
        warnings.extend(references)
    if any(mod.mod_id == "TrueMoozic" for mod in selected):
        warnings.append(
            "Le correctif TCLootControl.lua de batman-tweaks masque le fichier de TrueMoozic : "
            "vérifier l'ordre de résolution après lancement."
        )
    return errors, warnings


def rewrite_info(data: bytes, aliases: tuple[str, ...], mapping: dict[str, str]) -> bytes:
    text = data.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    had_final = text.endswith(("\r", "\n"))
    lines = text.splitlines()
    out: list[str] = []
    saw_id = False
    for line in lines:
        if "=" not in line:
            out.append(line)
            continue
        key, value = line.split("=", 1)
        if key.strip() == "id":
            original_id = value.strip()
            if original_id not in aliases:
                raise ValueError(f"mod.info contient un ID non indexé : {value!r}")
            out.append(f"id={mapping[original_id]}")
            saw_id = True
        elif key.strip() in {"require", "loadModAfter", "loadModBefore"}:
            entries = parse_ids(value)
            out.append(f"{key}=" + ",".join("\\" + mapping.get(x, x) for x in entries))
        elif key.strip() == "incompatible":
            entries = list(parse_ids(value))
            for original_id in aliases:
                if original_id not in entries:
                    entries.append(original_id)
            out.append(f"{key}=" + ",".join("\\" + (x if x in aliases else mapping.get(x, x)) for x in entries))
        else:
            out.append(line)
    if not saw_id:
        raise ValueError("mod.info sans ID")
    if not any(line.startswith("incompatible=") for line in out):
        out.append("incompatible=" + ",".join("\\" + x for x in aliases))
    result = newline.join(out) + (newline if had_final else "")
    return result.encode("utf-8")


def update_pack_info(data: bytes, selected: list[Mod], config: PackConfig) -> bytes:
    text = data.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    begin = next((i for i, line in enumerate(lines) if line == BEGIN), None)
    end = next((i for i, line in enumerate(lines) if line == END), None)
    if (begin is None) != (end is None) or (begin is not None and begin >= end):
        raise ValueError("Bloc généré incohérent dans mod.info")
    if begin is not None:
        del lines[begin : end + 1]
    original = read_info_from_lines(lines)
    required = list(parse_ids(original.get("require", "")))
    after = list(parse_ids(original.get("loadModAfter", "")))
    mapping = {alias: mod.new_id(config.prefix) for mod in selected for alias in mod.aliases}
    required = [mapping.get(x, x) for x in required]
    after = [mapping.get(x, x) for x in after]
    for mod in selected:
        if mod.new_id(config.prefix) not in required:
            required.append(mod.new_id(config.prefix))
        if mod.new_id(config.prefix) not in after:
            after.append(mod.new_id(config.prefix))
    lines = [line for line in lines if not line.startswith(("require=", "loadModAfter="))]
    lines.extend([BEGIN, "require=" + ",".join("\\" + x for x in required),
                  "loadModAfter=" + ",".join("\\" + x for x in after), END])
    return (newline.join(lines) + newline).encode("utf-8")


def read_info_from_lines(lines: list[str]) -> dict[str, str]:
    result = {}
    for line in lines:
        if "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def load_pack_components(catalog: list[Mod], config: PackConfig) -> tuple[list[Mod], list[str]]:
    manifest_path = config.variant / MANIFEST_NAME
    if not manifest_path.exists():
        return [], []
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = data["components"]
        if not isinstance(entries, list):
            raise ValueError("components doit être une liste")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [], [f"Manifeste illisible : {exc}"]
    by_source = {str(mod.folder.resolve()): mod for mod in catalog}
    existing: list[Mod] = []
    problems: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append("Entrée invalide dans le manifeste.")
            continue
        mod = by_source.get(str(entry.get("source", "")))
        if mod is None:
            problems.append(f"Source absente du catalogue : {entry.get('source', '')}")
            continue
        if (entry.get("old_id") != mod.mod_id or entry.get("new_id") != mod.new_id(config.prefix)
                or entry.get("destination") != str(mod.destination(config))):
            problems.append(f"IDs ou destination modifiés pour {mod.name}.")
            continue
        destination = mod.destination(config)
        variant = choose_variant(destination) if destination.is_dir() else None
        if variant is None or read_info(variant / "mod.info").get("id") != mod.new_id(config.prefix):
            problems.append(f"Copie absente ou ID incorrect : {destination}")
            continue
        existing.append(mod)
    if len({mod.key for mod in existing}) != len(existing):
        problems.append("Le manifeste contient un mod en double.")
    return existing, problems


def build(
    selected: list[Mod], catalog: list[Mod], config: PackConfig, *, allow_internal_refs: bool,
    progress: Callable[[float, str], None] | None = None,
) -> dict:
    def report(fraction: float, message: str) -> None:
        if progress is not None:
            progress(fraction, message)

    report(0.0, "Démarrage : validation du projet…")
    config_errors = validate_config(config)
    if config_errors:
        raise ValueError("\n".join(config_errors))
    existing, manifest_problems = load_pack_components(catalog, config)
    existing_keys = frozenset(mod.key for mod in existing)
    if manifest_problems:
        raise ValueError("\n".join(manifest_problems))
    if any(mod.key in existing_keys for mod in selected):
        raise ValueError("La sélection contient un mod déjà présent dans le pack.")
    all_selected = existing + selected
    report(0.0, "Vérification des dépendances et des fichiers avant copie…")
    errors, warnings = inspect_selection(
        all_selected, catalog, config, existing_keys=existing_keys,
        progress=lambda done, total: report(
            0.25 * done / max(total, 1), f"Vérification : {done}/{total} mods analysés"
        ),
    )
    if warnings and not allow_internal_refs:
        errors.append("Des références internes sont signalées ; confirmer leur examen dans l'interface.")
    if errors:
        raise ValueError("\n".join(errors))
    target = config.variant.resolve()
    parent = config.mods_root.resolve()
    fresh = not target.exists()
    if not fresh and not target.is_dir():
        raise ValueError("Le point d'entrée du pack n'est pas un dossier.")
    if not fresh and not (target / "mod.info").is_file():
        raise ValueError("Le point d'entrée du pack ne contient pas mod.info.")
    pack_info = target / "mod.info"
    original_info = (f"name={config.pack_id}\nid={config.pack_id}\nversionMin=42.20.0\n").encode("utf-8") if fresh else pack_info.read_bytes()
    if not fresh and read_info(pack_info).get("id") != config.pack_id:
        raise ValueError(f"modId principal différent de {config.pack_id} dans {pack_info}.")
    backup = target / "mod.info.before-modpack-builder.bak"
    if existing:
        if not backup.is_file():
            raise ValueError(f"Sauvegarde du mod.info introuvable : {backup}")
        base_info = backup.read_bytes()
        if original_info != update_pack_info(base_info, existing, config):
            raise ValueError("mod.info a changé depuis la dernière génération ; arrêt pour préserver ces changements.")
    else:
        if backup.exists() or (target / MANIFEST_NAME).exists():
            raise ValueError("Sauvegarde ou manifeste présent sans composants valides ; examen manuel nécessaire.")
        base_info = original_info
    new_info = update_pack_info(base_info, all_selected, config)
    mapping = {alias: mod.new_id(config.prefix) for mod in all_selected for alias in mod.aliases}
    manifest = {
        "format": 1,
        "target_version": "42.20",
        "prefix": config.prefix,
        "project": str(config.project),
        "pack_id": config.pack_id,
        "components": [
            {"workshop_id": m.workshop_id, "source": str(m.folder), "name": m.name,
             "old_id": m.mod_id, "new_id": m.new_id(config.prefix), "variant": m.variant,
             "destination": str(m.destination(config))} for m in all_selected
        ],
        "warnings": warnings,
    }
    # Prepare copies before touching the user's mod folder.
    with tempfile.TemporaryDirectory(prefix="batman-modpack-") as temporary:
        staging = Path(temporary)
        staged: list[tuple[Path, Path]] = []
        copy_fraction = 0.25
        copy_label = ""
        last_copy_update = 0.0

        def copy_file(source: str, destination: str) -> str:
            nonlocal last_copy_update
            now = time.monotonic()
            if now - last_copy_update >= 0.2:
                report(copy_fraction, f"{copy_label} · {Path(source).name}")
                last_copy_update = now
            return shutil.copy2(source, destination)

        for index, mod in enumerate(selected):
            copy_fraction = 0.25 + 0.35 * index / max(len(selected), 1)
            copy_label = f"Préparation temporaire : {index + 1}/{len(selected)} · {mod.name}"
            report(copy_fraction, copy_label)
            stage = staging / mod.destination(config).name
            shutil.copytree(mod.folder, stage, symlinks=False, copy_function=copy_file)
            (stage / "common").mkdir(exist_ok=True)
            for info_file in stage.rglob("mod.info"):
                info_file.write_bytes(rewrite_info(info_file.read_bytes(), mod.aliases, mapping))
            if mod.variant == "racine":
                versioned = stage / "42.20"
                versioned.mkdir(exist_ok=True)
                shutil.copy2(stage / "mod.info", versioned / "mod.info")
                if (stage / "media").is_dir():
                    shutil.copytree(stage / "media", versioned / "media", copy_function=copy_file)
            staged.append((stage, mod.destination(config)))
        for _, destination in staged:
            if destination.exists() or destination.resolve().parent != parent:
                raise ValueError(f"Destination invalide ou existante : {destination}")
        # No destructive replacement: the pack info is backed up and component folders are new.
        if fresh:
            target.mkdir(parents=True)
            (target.parent / "common").mkdir(exist_ok=True)
            if not (config.project / "workshop.txt").exists():
                (config.project / "workshop.txt").write_text(
                    f"version=1\ntitle={config.pack_id}\ndescription=Pack généré localement\ntags=Build 42\nvisibility=private\n",
                    encoding="utf-8",
                )
            if not (config.project / "preview.png").exists():
                write_preview(config.project / "preview.png")
        if not existing:
            backup.write_bytes(original_info)
        try:
            for index, (stage, destination) in enumerate(staged):
                copy_fraction = 0.60 + 0.30 * index / max(len(staged), 1)
                copy_label = f"Copie vers le pack : {index + 1}/{len(staged)} · {selected[index].name}"
                report(copy_fraction, copy_label)
                shutil.copytree(stage, destination, copy_function=copy_file)
            report(0.90, "Écriture du mod.info et du manifeste…")
            pack_info.write_bytes(new_info)
            (target / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pack_info.write_bytes(original_info)
            raise
    report(0.95, "Vérification des fichiers générés…")
    if pack_info.read_bytes() != new_info:
        raise RuntimeError("Vérification du mod.info généré échouée")
    for mod in all_selected:
        destination = mod.destination(config)
        generated_variant = choose_variant(destination)
        if generated_variant is None or read_info(generated_variant / "mod.info").get("id") != mod.new_id(config.prefix):
            raise RuntimeError(f"ID généré incorrect : {destination}")
    report(1.0, f"Terminé : {len(selected)} mod(s) ajouté(s) dans {config.project}")
    return manifest


def run_mod_git(root: Path, *args: str, timeout: int = 60) -> str:
    """Run Git without a shell; never change the caller's working directory."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), *args],
            capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except FileNotFoundError as exc:
        raise ValueError("Git est introuvable. Installez Git, puis relancez l'application.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Git a dépassé le délai prévu. Réessayez ou examinez ce dépôt dans un terminal.") from exc
    if result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "La commande Git a échoué.")
    return result.stdout


def mod_repository_root(root: Path) -> bool:
    """Require an explicit repository root, never silently use a parent repo."""
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("Choisissez un chemin absolu vers un dossier de mods existant.")
    if not (root / ".git").exists():
        return False
    actual = Path(run_mod_git(root, "rev-parse", "--show-toplevel").strip()).resolve()
    if actual != root.resolve():
        raise ValueError(f"La racine du dépôt Git est {actual}. Sélectionnez ce dossier.")
    return True


def initialize_mod_repository(root: Path) -> None:
    if mod_repository_root(root):
        return
    # Avoid creating a nested repository within another working tree.
    if any((parent / ".git").exists() for parent in root.parents):
        raise ValueError("Ce dossier appartient à un dépôt parent. Choisissez sa racine pour l'examiner.")
    run_mod_git(root, "init")


def mod_git_revisions(root: Path) -> list[tuple[str, str]]:
    if not mod_repository_root(root):
        raise ValueError("Initialisez d'abord le dépôt Git.")
    try:
        run_mod_git(root, "rev-parse", "--verify", "HEAD")
    except ValueError:
        branch = run_mod_git(root, "symbolic-ref", "HEAD").strip()
        refs = run_mod_git(root, "for-each-ref", "--format=%(refname)").splitlines()
        if branch not in refs:
            return []
        raise
    lines = run_mod_git(root, "log", "-50", "--format=%H%x09%cs %h %s").splitlines()
    return [tuple(line.split("\t", 1)) for line in lines]


def create_initial_mod_snapshot(root: Path) -> str:
    if mod_git_revisions(root):
        raise ValueError("Ce dépôt possède déjà un historique ; son état de référence est conservé.")
    run_mod_git(root, "add", "--all", "--", ".", timeout=600)
    run_mod_git(
        root, "-c", "user.name=Modpack Builder", "-c", "user.email=modpack-builder@localhost",
        "commit", "--allow-empty", "-m", "Record initial mod files", timeout=600,
    )
    return run_mod_git(root, "rev-parse", "HEAD").strip()


def collect_mod_changes(root: Path, revision: str) -> list[dict[str, str]]:
    if not mod_repository_root(root):
        raise ValueError("Le dossier ne contient pas de dépôt Git.")
    commit = run_mod_git(root, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}").strip()
    fields = run_mod_git(root, "diff", "--no-ext-diff", "--no-renames", "--name-status", "-z", commit, "--").split("\0")
    changes = [(fields[i], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]
    changes.extend(
        ("?", path) for path in run_mod_git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if path
    )
    states = {"A": "Ajouté", "M": "Modifié", "D": "Supprimé", "T": "Type modifié", "U": "Conflit", "?": "Non suivi"}
    rows = []
    for state, path in changes:
        parts = Path(path).parts
        # Workshop sources: <Workshop ID>/mods/<folder>/... ; pack: Contents/mods/<folder>/...
        if "mods" in parts and parts.index("mods") + 1 < len(parts):
            index = parts.index("mods")
            mod = "/".join(parts[:index + 2])
        elif (root / "mod.info").is_file():
            mod = root.name
        elif parts and ((root / parts[0]).is_dir() or len(parts) > 1):
            mod = parts[0]
        else:
            mod = "Fichiers du dépôt"
        rows.append({"Mod / dossier": mod, "État": states.get(state, state), "Fichier": path})
    return sorted(rows, key=lambda row: (row["Mod / dossier"], row["Fichier"]))


def mod_change_diff(root: Path, revision: str, row: dict[str, str]) -> str:
    path = row["Fichier"]
    if row["État"] == "Non suivi":
        source = root / path
        if not source.resolve().is_relative_to(root.resolve()) or source.is_symlink():
            return "Aperçu indisponible pour un lien extérieur au dépôt."
        with source.open("rb") as stream:
            data = stream.read(64_001)
        if b"\0" in data:
            return "Fichier binaire ajouté (aperçu indisponible)."
        return data[:64_000].decode("utf-8", errors="replace") + ("\n… Aperçu tronqué." if len(data) > 64_000 else "")
    result = run_mod_git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", revision, "--", f":(literal){path}")
    return result[:64_000] + ("\n… Différence tronquée." if len(result) > 64_000 else "")


def render_mod_updates(source_root: Path | None = None) -> None:
    import streamlit as st

    st.subheader("Mises à jour des mods")
    st.caption(
        "Compare les fichiers locaux à un état Git enregistré. Steam doit avoir téléchargé les mises à jour. "
        "Cette comparaison ne détermine pas encore quels changements ont été intégrés à chaque pack."
    )
    root = Path(st.text_input("Dossier des mods à suivre avec Git", value=str(source_root or WORKSHOP), key="updates_root").strip())
    try:
        if not mod_repository_root(root):
            st.info("Aucun dépôt Git à la racine de ce dossier. Initialisez-le pour commencer le suivi.")
            if st.button("Initialiser Git dans ce dossier"):
                with st.spinner("Initialisation du dépôt Git local…"):
                    initialize_mod_repository(root)
                st.success("Dépôt Git initialisé. Enregistrez maintenant le premier état de référence.")
            else:
                return
        revisions = mod_git_revisions(root)
        if not revisions:
            st.info(
                "Aucun état de référence enregistré. Le premier enregistrement indexe les fichiers non ignorés "
                "et crée un commit local ; il peut être long et occuper beaucoup d'espace. Les fichiers des mods restent inchangés."
            )
            if st.button("Enregistrer le premier état de référence"):
                with st.spinner("Enregistrement des fichiers dans Git…"):
                    commit = create_initial_mod_snapshot(root)
                st.success(f"État de référence enregistré : {commit[:12]}. Les futurs changements pourront être détectés.")
                revisions = mod_git_revisions(root)
            else:
                return
        labels = dict(revisions)
        revision = st.selectbox("Comparer les fichiers actuels à", list(labels), format_func=labels.get)
        st.caption("Inclut les changements indexés, non indexés et les nouveaux fichiers non ignorés. Les renommages apparaissent comme suppression et ajout.")
        scope = (str(root.resolve()), revision)
        if st.session_state.get("updates_scope") != scope:
            st.session_state["updates_scope"] = scope
            st.session_state.pop("updates_changes", None)
        if st.button("Rechercher les changements"):
            st.session_state.pop("updates_changes", None)
            with st.spinner("Comparaison des fichiers avec Git…"):
                st.session_state["updates_changes"] = collect_mod_changes(root, revision)
        changes = st.session_state.get("updates_changes")
        if changes is None:
            return
        if not changes:
            st.success("Aucun changement local par rapport à cet état de référence.")
            return
        groups: dict[str, int] = {}
        for row in changes:
            groups[row["Mod / dossier"]] = groups.get(row["Mod / dossier"], 0) + 1
        st.write(f"{len(changes)} fichier(s) changé(s) dans {len(groups)} mod(s) ou dossier(s).")
        st.dataframe(
            [{"Mod / dossier": mod, "Fichiers changés": count} for mod, count in groups.items()],
            hide_index=True, width="stretch",
        )
        chosen = st.multiselect("Mods à examiner", list(groups), key=f"updates_selection_{scope}")
        rows = [row for row in changes if not chosen or row["Mod / dossier"] in chosen]
        if not rows:
            st.info("Aucun fichier pour ces filtres. Sélectionnez un autre mod à examiner.")
            return
        st.dataframe(rows, hide_index=True, width="stretch")
        file_index = st.selectbox("Fichier à comparer", range(len(rows)), format_func=lambda index: rows[index]["Fichier"])
        if st.button("Afficher les différences"):
            st.code(mod_change_diff(root, revision, rows[file_index]), language="diff")
        st.caption("Consultation uniquement : aucune copie du pack n'est remplacée et aucun changement n'est publié sur GitHub.")
    except (ValueError, OSError) as exc:
        st.error(f"Suivi Git impossible : {exc}")


def load_source_settings() -> Path:
    if not SETTINGS_FILE.exists():
        return WORKSHOP
    data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("source_root"), str):
        raise ValueError("Format du fichier de paramètres invalide.")
    root = Path(data["source_root"])
    if not root.is_absolute():
        raise ValueError("Le dossier enregistré doit être un chemin absolu.")
    return root


def save_source_settings(root: Path) -> None:
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("Choisissez un chemin absolu vers un dossier existant.")
    temporary = SETTINGS_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"source_root": str(root.resolve())}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(SETTINGS_FILE)


def steam_installation_paths() -> set[Path]:
    steam_roots = {Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Steam"}
    if os.name == "nt":
        import ctypes
        import winreg

        for hive, key, value in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    steam_roots.add(Path(winreg.QueryValueEx(handle, value)[0]))
            except OSError:
                pass
        drives = ctypes.windll.kernel32.GetLogicalDrives()
        for index, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
            drive = Path(f"{letter}:/")
            if drives & (1 << index) and ctypes.windll.kernel32.GetDriveTypeW(str(drive)) == 3:
                steam_roots.update((drive / "Steam", drive / "SteamLibrary", drive / "Program Files (x86)" / "Steam"))
    steam_roots.update((Path.home() / ".steam" / "steam", Path.home() / ".local" / "share" / "Steam"))
    return steam_roots


def discover_workshop_paths(steam_roots: set[Path] | None = None) -> list[Path]:
    """Inspect Steam installation hints and library manifests, without walking disks."""
    steam_roots = steam_installation_paths() if steam_roots is None else steam_roots
    libraries = set(steam_roots)
    for steam_root in steam_roots:
        manifest = steam_root / "steamapps" / "libraryfolders.vdf"
        try:
            contents = manifest.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        libraries.update(Path(value.replace("\\\\", "\\")) for value in re.findall(r'"path"\s*"([^"\r\n]+)"', contents))
    candidates = {library / "steamapps" / "workshop" / "content" / "108600" for library in libraries}
    candidates.add(WORKSHOP)
    return sorted({path.resolve() for path in candidates if path.is_dir()}, key=str)


def search_workshop_paths(root: Path, *, limit: int = 20_000) -> tuple[list[Path], bool, int]:
    """Bounded, read-only search; avoid links, system folders and Git metadata."""
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("Choisissez un dossier de recherche existant avec un chemin absolu.")
    found: set[Path] = set()
    visited = 0
    inaccessible = 0

    def on_error(error: OSError) -> None:
        nonlocal inaccessible
        inaccessible += 1

    skipped = {".git", ".batman_modpack_deps", "$recycle.bin", "system volume information", "windows", "node_modules"}
    for directory, names, _ in os.walk(root, followlinks=False, onerror=on_error):
        visited += 1
        if visited > limit:
            return sorted(found, key=str), True, inaccessible
        current = Path(directory)
        if current.name == "108600" and current.parent.name == "content":
            found.add(current.resolve())
            names[:] = []
            continue
        names[:] = [name for name in names if name.casefold() not in skipped and not (current / name).is_symlink()
                    and not (getattr(os.path, "isjunction", lambda path: False)(current / name))]
    return sorted(found, key=str), False, inaccessible


def render_settings() -> Path:
    import streamlit as st

    if "source_root" not in st.session_state:
        try:
            st.session_state["source_root"] = str(load_source_settings())
        except (OSError, ValueError) as exc:
            st.warning(f"Paramètres illisibles : {exc}. Le chemin par défaut est proposé.")
            st.session_state["source_root"] = str(WORKSHOP)
    st.subheader("Dossier des mods source")
    st.caption("Sélectionnez le dossier Workshop de Project Zomboid : steamapps/workshop/content/108600, contenant les dossiers numériques des mods.")
    st.write(f"Dossier actif : {st.session_state['source_root']}")

    def apply_root(value: str) -> None:
        root = Path(value.strip().strip('"')).expanduser()
        save_source_settings(root)
        st.session_state["source_root"] = str(root.resolve())
        st.session_state["settings_source_input"] = str(root.resolve())
        for key in ("selected_roots", "pending_mods", "catalog_editor", "editor_scope", "inspection_key",
                    "inspection_result", "reviewed_refs", "build_success", "updates_root", "updates_scope",
                    "updates_changes", "import_report", "import_issues", "dependency_notice"):
            st.session_state.pop(key, None)
        st.session_state["settings_saved"] = True

    # Callbacks run before widgets are rendered, allowing both path fields to be reset safely.
    def save_manual() -> None:
        try:
            apply_root(st.session_state["settings_source_input"])
            st.session_state.pop("settings_error", None)
        except (OSError, ValueError) as exc:
            st.session_state["settings_error"] = str(exc)

    def save_detected() -> None:
        try:
            apply_root(st.session_state["settings_detected"])
            st.session_state.pop("settings_error", None)
        except (OSError, ValueError) as exc:
            st.session_state["settings_error"] = str(exc)

    if "settings_source_input" not in st.session_state:
        st.session_state["settings_source_input"] = st.session_state["source_root"]
    st.text_input("Chemin du dossier Workshop", key="settings_source_input")
    st.button("Enregistrer le dossier", on_click=save_manual)
    if st.session_state.pop("settings_saved", False):
        st.success("Dossier enregistré. Le catalogue et le suivi Git utilisent ce chemin ; la sélection précédente a été réinitialisée.")
    if st.session_state.get("settings_error"):
        st.error(st.session_state["settings_error"])
    if st.button("Détecter les bibliothèques Steam"):
        try:
            with st.spinner("Recherche des bibliothèques Steam…"):
                st.session_state["settings_candidates"] = [str(path) for path in discover_workshop_paths()]
        except OSError as exc:
            st.error(f"Recherche impossible : {exc}")
    with st.expander("Rechercher dans un autre emplacement"):
        search_root = st.text_input("Dossier ou disque à parcourir", value=str(Path.home()))
        st.caption("Recherche limitée à 20 000 dossiers. Les dossiers protégés et les liens sont ignorés.")
        if st.button("Scanner cet emplacement"):
            try:
                with st.spinner("Recherche des dossiers Workshop de Project Zomboid…"):
                    candidates, truncated, inaccessible = search_workshop_paths(Path(search_root.strip().strip('"')))
                st.session_state["settings_candidates"] = [str(path) for path in candidates]
                if truncated:
                    st.warning("Limite de recherche atteinte. Choisissez un sous-dossier plus précis pour poursuivre.")
                if inaccessible:
                    st.warning(f"{inaccessible} dossier(s) inaccessible(s) ont été ignorés.")
            except (OSError, ValueError) as exc:
                st.error(str(exc))
    if "settings_candidates" in st.session_state:
        candidates = st.session_state["settings_candidates"]
        if candidates:
            st.selectbox("Dossiers trouvés", candidates, key="settings_detected")
            st.button("Utiliser ce dossier", on_click=save_detected)
        else:
            st.info("Aucun dossier trouvé. Essayez un autre emplacement ou saisissez le chemin manuellement.")
    return Path(st.session_state["source_root"])


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="Créateur de modpacks PZ", layout="wide")
    st.markdown("""
    <style>
      html { font-size: 14px; }
      .block-container { padding-top: 4rem !important; padding-bottom: 1rem; }
      h1 { font-size: 1.45rem !important; margin-bottom: .3rem !important; }
      h2, h3 { font-size: 1.05rem !important; margin-bottom: .2rem !important; }
      [data-testid="stVerticalBlock"] { gap: .45rem; }
      [data-testid="stAlert"] { padding: .45rem .65rem; }
    </style>
    """, unsafe_allow_html=True)
    st.title("Créateur de modpacks Project Zomboid")
    builder_tab, updates_tab, settings_tab = st.tabs(["Création du pack", "Mises à jour", "Paramètres"])
    with settings_tab:
        source_root = render_settings()
    with updates_tab:
        render_mod_updates(source_root)
    with builder_tab:
        render_pack_builder(source_root)


def render_pack_builder(source_root: Path | None = None) -> None:
    import streamlit as st

    source_root = source_root or WORKSHOP
    with st.expander("Projet et identifiants", expanded=False):
        st.caption(f"Sources Workshop : {source_root}")
        workshop_root_text = st.text_input(
            "Dossier parent des projets Workshop",
            value=str(DEFAULT_WORKSHOP_ROOT),
            help="Chaque modpack sera créé dans un sous-dossier indépendant de ce répertoire.",
        ).strip()
        project_folder = st.text_input(
            "Nom du dossier de ce modpack", value=DEFAULT_PROJECT_FOLDER,
            help="Choisir un nom distinct pour chaque modpack, par exemple voitures-42-20.",
        ).strip()
        project = Path(workshop_root_text) / project_folder
        folder_slug = re.sub(r"[^A-Za-z0-9]+", "_", project_folder).strip("_").lower() or "pack"
        default_prefix = f"{folder_slug}_" if folder_slug[0].isalpha() else f"pack_{folder_slug}_"
        prefix = st.text_input(
            "Préfixe des modId et des dossiers copiés", value=default_prefix,
            key=f"prefix_{workshop_root_text}_{project_folder}",
        ).strip()
        default_pack_id = f"{prefix.rstrip('_-')}_pack"
        pack_id = st.text_input(
            "modId du pack principal", value=default_pack_id,
            key=f"pack_id_{workshop_root_text}_{project_folder}_{prefix}",
        ).strip()
    config = PackConfig(project, prefix, pack_id, source_root)
    st.caption(f"Projet : {config.project} · Préfixe : {config.prefix} · modId : {config.pack_id}")
    config_errors = validate_config(config)
    if not PROJECT_FOLDER_RE.fullmatch(project_folder):
        config_errors.append("Nom de dossier invalide : utiliser lettres, chiffres, espaces, _ ou - ; sans séparateur de chemin.")
    if not Path(workshop_root_text).is_absolute():
        config_errors.append("Le dossier parent Workshop doit être un chemin absolu.")
    for error in config_errors:
        st.error(error)

    @st.cache_data(show_spinner="Lecture des mod.info du Workshop…")
    def catalog_cached(schema: int, source_path: str) -> tuple[list[dict], list[str]]:
        mods, warnings = scan_catalog(Path(source_path))
        return [asdict(mod) for mod in mods], warnings

    catalog_data, catalog_warnings = catalog_cached(CATALOG_SCHEMA, str(source_root))
    if any("aliases" not in record for record in catalog_data):
        catalog_cached.clear()
        catalog_data, catalog_warnings = catalog_cached(CATALOG_SCHEMA, str(source_root))
    catalog = [Mod(**record) for record in catalog_data]
    existing, manifest_problems = load_pack_components(catalog, config)
    existing_keys = {mod.key for mod in existing}
    if "selected_roots" not in st.session_state:
        st.session_state["selected_roots"] = st.session_state.get("pending_mods", [])
    roots = set(st.session_state["selected_roots"])
    roots.intersection_update(mod.key for mod in catalog)
    roots.difference_update(existing_keys)
    pending, automatic, dependency_issues = resolve_dependencies(roots, catalog, existing, config)
    st.session_state["selected_roots"] = sorted(roots)
    st.session_state["pending_mods"] = sorted(pending)
    with st.expander("Importer favoris ou liste enregistrée", expanded=False):
        try:
            saved_lists = read_saved_lists()
        except OSError as exc:
            saved_lists = {}
            st.warning(f"Lecture impossible de {SAVED_LISTS_FILE} : {exc}")
        if saved_lists:
            names = list(saved_lists)
            preferred = next((name for name in names if saved_lists[name]), names[0])
            chosen_name = st.selectbox(
                "Favoris ou liste enregistrée dans pz_modlist_settings.cfg",
                names, index=names.index(preferred),
                format_func=lambda name: f"{name} ({len(saved_lists[name])} IDs)",
            )
            if "Favoris" in saved_lists and not saved_lists["Favoris"]:
                st.caption("La ligne !fav!: ne contient actuellement aucun mod ; les listes nommées restent disponibles.")
            if st.button("Cocher les mods de cette liste", disabled=not saved_lists[chosen_name]):
                matched, missing, ambiguous = match_saved_ids(saved_lists[chosen_name], catalog)
                eligible = [mod for mod in matched if mod.key not in existing_keys and not mod.destination(config).exists()]
                roots.update(mod.key for mod in eligible)
                st.session_state["selected_roots"] = sorted(roots)
                st.session_state["import_report"] = (
                    f"{len(eligible)} mod(s) cochés depuis « {chosen_name} ». "
                    f"{len(matched) - len(eligible)} déjà présents dans le pack ou sur disque."
                )
                st.session_state["import_issues"] = (missing, ambiguous)
                st.session_state.pop("catalog_editor", None)
                st.rerun()
    if st.session_state.get("import_report"):
        st.success(st.session_state.pop("import_report"))
        missing, ambiguous = st.session_state.pop("import_issues", ([], {}))
        if missing:
            st.warning(f"{len(missing)} ID(s) introuvables dans le catalogue : {', '.join(missing[:20])}")
        if ambiguous:
            st.warning(
                f"{len(ambiguous)} ID(s) ambigus, non cochés : "
                + ", ".join(f"{mod_id} ({len(choices)} copies)" for mod_id, choices in list(ambiguous.items())[:20])
            )
    if st.session_state.get("build_success"):
        st.success(st.session_state["build_success"])
    if catalog_warnings:
        with st.expander(f"{len(catalog_warnings)} entrées non analysées"):
            st.code("\n".join(catalog_warnings[:100]))
    for problem in manifest_problems:
        st.error(problem)
    filter_col, existing_col, refresh_col = st.columns([5, 2, 1], vertical_alignment="bottom")
    with filter_col:
        query = st.text_input(
            "Rechercher : nom, modId ou ID Workshop", type="search", live="250ms"
        ).strip()
    with existing_col:
        only_existing = st.checkbox("Déjà dans le pack")
    with refresh_col:
        if st.button("Actualiser"):
            catalog_cached.clear()
            st.rerun()
    visible = [
        m for m in catalog
        if (not only_existing or m.key in existing_keys)
        and matches_search(m, query)
    ]
    selected_now = existing + [mod for mod in catalog if mod.key in pending]
    conflicts = declared_conflicts(selected_now)
    conflicting_with = conflict_names(visible, selected_now)
    st.caption(f"{len(visible)} résultat(s) affiché(s) sur {len(catalog)} mods indexés.")
    editor_scope = (query, only_existing, str(config.project), config.prefix, config.pack_id)
    if st.session_state.get("editor_scope") != editor_scope:
        st.session_state.pop("catalog_editor", None)
        st.session_state["editor_scope"] = editor_scope
    rows = [
        {"Ajouter": m.key in pending,
         "État": (
             "✓ Dans le pack" if m.key in existing_keys else (
                 "Copie présente hors manifeste" if m.destination(config).exists() else (
                     "Dépendance automatique" if m.key in automatic else "Disponible"
                 )
             )
         ) + (" · Incompatible" if conflicting_with.get(m.key) else ""),
         "Nom": m.name, "modId": workshop_mod_link(m), "Workshop ID": m.workshop_id,
         "Variante": m.variant, "versionMin": m.version_min,
         "Incompatibles déclarés": ", ".join(m.incompatible),
         "Conflit avec sélection": ", ".join(mod.name for mod in conflicting_with.get(m.key, ())),
         "Autres IDs de version": ", ".join(x for x in m.aliases if x != m.mod_id),
         "Dossier": m.folder.name} for m in visible
    ]
    edited = st.data_editor(
        rows, hide_index=True, width="stretch", height=430, key="catalog_editor",
        column_config={
            "Ajouter": st.column_config.CheckboxColumn("Ajouter", default=False, pinned=True),
            "modId": st.column_config.LinkColumn(
                "modId", display_text=r"#modid=(.*)$", width="medium",
                help="Ouvrir la page Steam Workshop dans un nouvel onglet ; un premier clic peut sélectionner la cellule.",
            ),
        },
        disabled=[key for key in rows[0] if key != "Ajouter"] if rows else False,
    )
    changed = False
    for mod, row in zip(visible, edited):
        if mod.key in existing_keys or mod.destination(config).exists():
            roots.discard(mod.key)
        elif bool(row["Ajouter"]) != (mod.key in pending):
            changed = True
            if row["Ajouter"]:
                roots.add(mod.key)
            else:
                roots.discard(mod.key)
                if mod.key in automatic:
                    st.session_state["dependency_notice"] = (
                        f"{mod.name} reste coché car un autre mod sélectionné en dépend. "
                        "Décochez d'abord le mod qui l'exige."
                    )
    if changed:
        st.session_state["selected_roots"] = sorted(roots)
        st.session_state.pop("catalog_editor", None)
        st.rerun()
    st.session_state["pending_mods"] = sorted(pending)
    if st.session_state.get("dependency_notice"):
        st.info(st.session_state.pop("dependency_notice"))
    st.caption(
        f"{len(catalog)} mods détectés · {len(existing)} déjà dans le pack · "
        f"{len(pending)} à ajouter, dont {len(automatic)} dépendance(s) automatique(s). "
        "Variantes compatibles avec 42.20.4."
    )
    if automatic:
        with st.expander("Dépendances ajoutées automatiquement"):
            by_key = {mod.key: mod for mod in catalog}
            st.write(", ".join(by_key[key].name for key in sorted(automatic)))
    if dependency_issues:
        st.warning(f"{len(dependency_issues)} dépendance(s) à résoudre avant génération.")
        with st.expander("Détails des dépendances non résolues"):
            for issue in dependency_issues:
                st.write(issue)
    if conflicts:
        st.error(f"{len(conflicts)} paire(s) de mods sélectionnés déclarées incompatibles.")
        with st.expander("Détails des incompatibilités"):
            for left, right in conflicts:
                st.write(f"{left.name} ({left.mod_id}) ↔ {right.name} ({right.mod_id})")
    indexed = {mod.key: mod for mod in catalog}
    selected = [indexed[key] for key in sorted(pending)]
    if not selected:
        st.caption("Cochez « Ajouter » pour préparer un modpack.")
        return
    with st.expander(f"Détails des {len(selected)} mods sélectionnés"):
        st.dataframe(
            [{"Nom": m.name, "ID original": workshop_mod_link(m), "ID généré": m.new_id(config.prefix),
              "Dossier généré": str(m.destination(config)), "Dépendances": ", ".join(m.requires)} for m in selected],
            hide_index=True, width="stretch", height=260,
            column_config={"ID original": st.column_config.LinkColumn("ID original", display_text=r"#modid=(.*)$")},
        )
        st.caption("Chaque copie est un mod frère sous Contents/mods ; le mod principal référence ces composants.")
    all_selected = existing + selected
    inspection_key = (
        tuple(sorted(mod.key for mod in all_selected)),
        str(config.project), config.prefix, config.pack_id,
    )
    if st.session_state.get("inspection_key") != inspection_key:
        st.session_state["inspection_key"] = inspection_key
        st.session_state.pop("inspection_result", None)
        st.session_state.pop("reviewed_refs", None)
    if st.button("Vérifier la sélection", disabled=bool(config_errors or manifest_problems or dependency_issues)):
        with st.spinner("Analyse des dépendances, collisions et références internes…"):
            progress_bar = st.progress(0, text=f"Analyse des fichiers : 0/{len(all_selected)} mods")
            last_progress_update = 0.0

            def show_progress(done: int, total: int) -> None:
                nonlocal last_progress_update
                now = time.monotonic()
                if done == total or now - last_progress_update >= 0.2:
                    progress_bar.progress(done / total, text=f"Analyse des fichiers : {done}/{total} mods")
                    last_progress_update = now

            try:
                st.session_state["inspection_result"] = inspect_selection(
                    all_selected, catalog, config, existing_keys=frozenset(existing_keys),
                    progress=show_progress,
                )
            except Exception as exc:
                st.session_state.pop("inspection_result", None)
                st.error(f"Échec de la vérification : {type(exc).__name__} : {exc}")
            finally:
                progress_bar.empty()
    inspection = st.session_state.get("inspection_result")
    if inspection is None:
        st.caption("Vérifiez la sélection avant de générer le modpack.")
    errors, warnings = inspection if inspection is not None else ([], [])
    errors = [*errors, *config_errors, *manifest_problems, *dependency_issues]
    for error in errors:
        st.error(error)
    if warnings:
        with st.expander(f"{len(warnings)} références à examiner"):
            st.code("\n".join(warnings[:150]))
    reviewed = st.checkbox(
        "J'ai examiné les références internes signalées et accepte ce prototype pour cette sélection",
        disabled=not warnings,
        key="reviewed_refs",
    )
    with st.expander("Limites de la copie"):
        st.caption(
            "Les modId et leurs dépendances déclarées sont préfixés. Les noms d'objets, "
            "IDs de tuiles, packs, traductions et références Lua ne sont pas réécrits. "
            "Désactiver les originaux évite leurs collisions directes ; tester sur une copie de sauvegarde."
        )
    if errors:
        st.warning("Ajout indisponible : corrigez les erreurs ci-dessus, puis vérifiez à nouveau la sélection.")
    elif inspection is None:
        st.info("Ajout indisponible : cliquez d'abord sur « Vérifier la sélection ».")
    elif warnings and not reviewed:
        st.info("Ajout indisponible : examinez les avertissements, puis cochez la case de confirmation ci-dessus.")
    if st.button("Ajouter les mods cochés au pack", disabled=bool(inspection is None or errors or (warnings and not reviewed))):
        st.session_state.pop("build_success", None)
        status = st.status(f"Ajout de {len(selected)} mod(s) en cours…", expanded=True)
        with status:
            st.write(f"Destination : {config.project}")
            build_progress = st.progress(0.0, text="Démarrage de la génération…")

        def show_build_progress(fraction: float, message: str) -> None:
            build_progress.progress(fraction, text=message)

        try:
            build(selected, catalog, config, allow_internal_refs=reviewed, progress=show_build_progress)
        except Exception as exc:
            status.update(label="Échec de l'ajout des mods", state="error", expanded=True)
            st.error(f"Échec de la génération : {type(exc).__name__} : {exc}")
        else:
            st.session_state["pending_mods"] = []
            st.session_state["selected_roots"] = []
            st.session_state.pop("catalog_editor", None)
            st.session_state["build_success"] = (
                f"{len(selected)} mod(s) ajouté(s) dans {config.project}. "
                "Redémarrer le jeu pour vérifier le chargement."
            )
            status.update(label="Ajout terminé", state="complete")
            st.rerun()


if __name__ == "__main__":
    main()
