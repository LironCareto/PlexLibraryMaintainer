#!/usr/bin/env python3
"""Normalize Plex movie folder names using Plex metadata.

Plex's database is always opened read-only. Dry-run is the default. With
--write, the tool can rename the first movie folder immediately below a
selected library root and can place Plex-indexed movie files that live directly
in the library root into their canonical movie folder.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

LIBRARY_DB = "com.plexapp.plugins.library.db"
DEFAULT_CONFIG = Path("config.json")
SUSPICIOUS_SIMILARITY_THRESHOLD = 0.55

VIDEO_EXTENSIONS = {
    ".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg",
    ".mpg", ".ts", ".webm", ".wmv",
}
SUBTITLE_EXTENSIONS = {".ass", ".idx", ".smi", ".srt", ".ssa", ".sub", ".sup", ".vtt"}
GENERIC_SIDECAR_EXTENSIONS = {".jpg", ".jpeg", ".nfo", ".png", ".webp"}
SUBTITLE_DIRECTORY_NAMES = {"subs", "subtitles"}


@dataclass(frozen=True)
class Library:
    id: int
    name: str
    type_code: int

    @property
    def kind(self) -> str:
        return "movie" if self.type_code == 1 else "unsupported"


@dataclass(frozen=True)
class FolderPlan:
    source: Path
    target: Path
    title: str
    year: int | None
    library_id: int
    library_name: str

    @property
    def comparison_name(self) -> str:
        return self.source.name


@dataclass(frozen=True)
class RootFilePlan:
    source: Path
    target: Path
    title: str
    year: int | None
    library_id: int
    library_name: str

    @property
    def comparison_name(self) -> str:
        return self.source.stem


def open_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")

    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def parse_path_map(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("path mapping must have the form FROM=TO")

    source, target = value.split("=", 1)
    source = source.rstrip("/\\")
    target = target.rstrip("/\\")

    if not source or not target:
        raise argparse.ArgumentTypeError("path mapping must have the form FROM=TO")

    return source, target


def load_config(config_path: Path) -> dict:
    if not config_path.is_file():
        return {}

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read config file {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Config file {config_path} must contain a JSON object")

    return data


def config_path_maps(config: dict) -> list[tuple[str, str]]:
    raw = config.get("path_maps", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("'path_maps' in config must be a JSON array")

    mappings: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("Each 'path_maps' entry must be a string in FROM=TO form")
        mappings.append(parse_path_map(item))
    return mappings


def apply_path_maps(path: str, mappings: list[tuple[str, str]]) -> Path:
    for source, target in mappings:
        if path == source:
            return Path(target)

        for separator in ("/", "\\"):
            prefix = source + separator
            if path.startswith(prefix):
                remainder = path[len(source):].lstrip("/\\")
                return Path(target) / Path(remainder)

    return Path(path)


def canonical_title_component(title: str) -> str:
    """Apply only the explicit title substitutions approved for M1."""
    return title.replace(":", ";").replace("?", "¿")


def unsafe_component_reason(value: str):
    """Return a reason if a target folder component is unsafe for M1.

    M1 deliberately refuses to invent replacements beyond the two explicit
    conventions above. The remaining checks are conservative for DSM/SMB use.
    """
    if not value:
        return "empty path component"

    if value in {".", ".."}:
        return "reserved path component"

    if value.startswith("._"):
        return "names starting with '._' are reserved by DSM"

    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "contains a control character"

    for char in '<>"/\\|*':
        if char in value:
            return f"contains unsupported character {char!r}"

    if value.endswith(" ") or value.endswith("."):
        return "names ending in a space or dot are not safe for SMB/Windows clients"

    return None


def trailing_edition_marker(source_name: str):
    """Return a trailing {edition-...} marker exactly as written.

    M1 does not interpret edition text. It only preserves a well-formed marker
    already present at the end of the source folder name.
    """
    lowered = source_name.casefold()
    start = lowered.rfind("{edition-")
    if start == -1:
        return None

    end = source_name.find("}", start)
    if end == -1:
        return None

    if source_name[end + 1:].strip():
        return None

    marker = source_name[start:end + 1]
    payload = marker[len("{edition-"):-1].strip()
    if not payload:
        return None

    return marker


def display_title_year(title: str, year: int | None) -> str:
    return f"{title} ({year})" if year is not None else title


def canonical_folder_name(title: str, year: int | None, edition_marker=None) -> str:
    title_component = canonical_title_component(title)
    if year is None:
        # With no year suffix, a trailing dot or space from Plex would become
        # the final character of the folder name and is unsafe on DSM/SMB.
        title_component = title_component.rstrip(" .")
        name = title_component
    else:
        name = f"{title_component} ({year})"

    if edition_marker:
        name += f" {edition_marker}"
    return name


def available_file_target(target: Path, reserved: set[Path] | None = None) -> Path:
    """Return target, or target with (n) before its extension, without clobbering."""
    reserved = reserved or set()
    if not target.exists() and target not in reserved:
        return target

    index = 1
    while True:
        candidate = target.with_name(f"{target.stem} ({index}){target.suffix}")
        if not candidate.exists() and candidate not in reserved:
            return candidate
        index += 1


def comparison_text(value: str) -> str:
    """Normalize text only for the M1 mismatch safety check."""
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(
        char
        for char in value
        if unicodedata.category(char) != "Mn"
    )
    value = "".join(char if char.isalnum() else " " for char in value)
    return " ".join(value.split())


def suspicious_title_match(plan: FolderPlan):
    """Return a similarity score when a proposed rename looks suspicious.

    This is a guardrail, not a title parser. A rename is considered plausible
    when the normalized Plex title appears as a whole phrase in the source
    folder name, when the source phrase appears in the title, when they share a
    meaningful token, or when their character similarity is reasonably high.
    """
    year_token = str(plan.year) if plan.year is not None else None
    source_tokens = [
        token
        for token in comparison_text(plan.comparison_name).split()
        if year_token is None or token != year_token
    ]
    title_tokens = comparison_text(plan.title).split()

    source_text = " ".join(source_tokens)
    title_text = " ".join(title_tokens)

    if not source_text or not title_text:
        return 0.0

    padded_source = f" {source_text} "
    padded_title = f" {title_text} "
    if padded_title in padded_source or padded_source in padded_title:
        return None

    source_meaningful = {token for token in source_tokens if len(token) >= 4}
    title_meaningful = {token for token in title_tokens if len(token) >= 4}
    if source_meaningful & title_meaningful:
        return None

    score = SequenceMatcher(None, source_text, title_text).ratio()
    if score >= SUSPICIOUS_SIMILARITY_THRESHOLD:
        return None

    return score


def create_rename_log():
    """Open the single append-only JSON-lines rename audit history."""
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    log_path = log_dir / "renames.log"
    handle = log_path.open("a", encoding="utf-8")
    run_id = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")

    header = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "START",
        "run_id": run_id,
        "tool": "PlexLibraryMaintainer",
        "mode": "write",
    }
    handle.write(json.dumps(header, ensure_ascii=False) + "\n")
    handle.flush()
    return log_path, handle, run_id


def write_rename_log(handle, run_id: str, status: str, plan, error=None, target=None):
    record = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": status,
        "run_id": run_id,
        "source": str(plan.source),
        "target": str(target if target is not None else plan.target),
        "library": plan.library_name,
        "title": plan.title,
        "year": plan.year,
    }
    if error is not None:
        record["error"] = str(error)

    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    handle.flush()


def split_suspicious_plans(
    plans,
    milestone: str,
):
    safe = []
    suspicious: list[str] = []

    for plan in plans:
        score = suspicious_title_match(plan)
        if score is None:
            safe.append(plan)
            continue

        suspicious.append(
            f"[SUSPICIOUS] {plan.source}\n"
            f"  Plex title: {display_title_year(plan.title, plan.year)}\n"
            f"  proposed target: {plan.target}\n"
            f"  similarity: {score:.2f}; skipped in {milestone}"
        )

    return safe, suspicious


def list_libraries(conn: sqlite3.Connection) -> list[Library]:
    rows = conn.execute(
        "SELECT id, name, section_type FROM library_sections ORDER BY id"
    ).fetchall()
    return [
        Library(id=int(row["id"]), name=row["name"], type_code=int(row["section_type"]))
        for row in rows
    ]


def resolve_libraries(
    available: list[Library],
    requested: list[str],
) -> tuple[list[Library], list[str]]:
    by_name = {library.name.casefold(): library for library in available}
    by_id = {str(library.id): library for library in available}

    resolved: list[Library] = []
    errors: list[str] = []

    for item in requested:
        library = by_id.get(item) or by_name.get(item.casefold())
        if library is None:
            errors.append(f"Unknown Plex library: {item}")
            continue
        if library.type_code != 1:
            errors.append(
                f"Unsupported Plex library type for {library.name!r}: "
                f"section_type={library.type_code}"
            )
            continue
        if library not in resolved:
            resolved.append(library)

    return resolved, errors


def library_root_paths(
    conn: sqlite3.Connection,
    library_ids: list[int],
    path_maps: list[tuple[str, str]],
) -> dict[int, set[Path]]:
    roots: dict[int, set[Path]] = defaultdict(set)
    if not library_ids:
        return roots

    placeholders = ",".join("?" for _ in library_ids)
    rows = conn.execute(
        f"""
        SELECT library_section_id, root_path
        FROM section_locations
        WHERE library_section_id IN ({placeholders})
        """,
        library_ids,
    ).fetchall()

    for row in rows:
        roots[int(row["library_section_id"])].add(
            apply_path_maps(row["root_path"], path_maps)
        )
    return roots


def movie_source_folder(file_path: Path, roots: set[Path]):
    """Return the library root and first folder below it for a movie file.

    The match is lexical and conservative: the media path must be inside one of
    the configured Plex roots. If the file is directly in the root, the second
    return value is None.
    """
    matches = []
    for root in roots:
        try:
            relative = file_path.relative_to(root)
        except ValueError:
            continue
        matches.append((root, relative))

    if not matches:
        return None, None

    # If roots overlap, prefer the most specific matching root.
    root, relative = max(matches, key=lambda item: len(item[0].parts))

    # A media file directly in the library root has only the filename relative
    # to that root, so there is no folder for M1 to rename.
    if len(relative.parts) <= 1:
        return root, None

    return root, root / relative.parts[0]


def movie_rows(
    conn: sqlite3.Connection,
    library_ids: list[int],
) -> list[sqlite3.Row]:
    if not library_ids:
        return []

    placeholders = ",".join("?" for _ in library_ids)
    return conn.execute(
        f"""
        SELECT
            metadata.library_section_id,
            metadata.id AS metadata_id,
            metadata.title,
            metadata.year,
            parts.file
        FROM metadata_items AS metadata
        INNER JOIN media_items AS media
            ON media.metadata_item_id = metadata.id
        INNER JOIN media_parts AS parts
            ON parts.media_item_id = media.id
        WHERE metadata.library_section_id IN ({placeholders})
          AND metadata.metadata_type = 1
        ORDER BY metadata.library_section_id, metadata.id, parts.id
        """,
        library_ids,
    ).fetchall()


def build_plans(
    conn: sqlite3.Connection,
    libraries: list[Library],
    path_maps: list[tuple[str, str]],
) -> tuple[list[FolderPlan], list[RootFilePlan], list[str], list[str], int]:
    library_by_id = {library.id: library for library in libraries}
    roots = library_root_paths(conn, list(library_by_id), path_maps)

    folder_metadata: dict[Path, set[tuple[str, int | None, int]]] = defaultdict(set)
    root_file_metadata: dict[Path, set[tuple[str, int | None, int]]] = defaultdict(set)
    skipped = 0
    review: list[str] = []
    unsafe_names: list[str] = []

    for row in movie_rows(conn, list(library_by_id)):
        title = row["title"]
        year = row["year"]
        if not title:
            skipped += 1
            review.append(
                f"[REVIEW] metadata id {row['metadata_id']}: missing title"
            )
            continue

        file_path = apply_path_maps(row["file"], path_maps)
        library_id = int(row["library_section_id"])
        library_root, source = movie_source_folder(
            file_path,
            roots.get(library_id, set()),
        )

        if library_root is None:
            skipped += 1
            review.append(
                f"[REVIEW] {file_path}: media path is not under any configured "
                "root for this library"
            )
            continue

        year_value = int(year) if year is not None else None
        metadata = (str(title), year_value, library_id)
        if source is None:
            root_file_metadata[file_path].add(metadata)
            continue

        folder_metadata[source].add(metadata)

    folder_plans: list[FolderPlan] = []

    for source, metadata_set in folder_metadata.items():
        if len(metadata_set) != 1:
            skipped += 1
            values = ", ".join(
                display_title_year(title, year)
                for title, year, _ in sorted(
                    metadata_set,
                    key=lambda item: (
                        item[0].casefold(),
                        item[1] is None,
                        item[1] if item[1] is not None else -1,
                        item[2],
                    ),
                )
            )
            review.append(
                f"[REVIEW] {source}: multiple Plex identities share this folder: {values}"
            )
            continue

        title, year, library_id = next(iter(metadata_set))

        edition_marker = trailing_edition_marker(source.name)
        if "{edition-" in source.name.casefold() and edition_marker is None:
            skipped += 1
            review.append(
                f"[REVIEW] {source}: malformed or non-trailing edition marker; "
                "M1 will not discard or reinterpret it"
            )
            continue

        target_name = canonical_folder_name(title, year, edition_marker)
        unsafe_reason = unsafe_component_reason(target_name)
        if unsafe_reason is not None:
            skipped += 1
            unsafe_names.append(
                f"[UNSAFE NAME] {source}\n"
                f"  target name: {target_name}\n"
                f"  reason: {unsafe_reason}"
            )
            continue

        target = source.with_name(target_name)
        library = library_by_id[library_id]
        folder_plans.append(
            FolderPlan(
                source=source,
                target=target,
                title=title,
                year=year,
                library_id=library_id,
                library_name=library.name,
            )
        )

    root_file_plans: list[RootFilePlan] = []
    reserved_targets: set[Path] = set()

    for source, metadata_set in root_file_metadata.items():
        if len(metadata_set) != 1:
            skipped += 1
            values = ", ".join(
                display_title_year(title, year)
                for title, year, _ in sorted(
                    metadata_set,
                    key=lambda item: (
                        item[0].casefold(),
                        item[1] is None,
                        item[1] if item[1] is not None else -1,
                        item[2],
                    ),
                )
            )
            review.append(
                f"[REVIEW] {source}: multiple Plex identities share this root file: {values}"
            )
            continue

        title, year, library_id = next(iter(metadata_set))

        if not source.exists():
            skipped += 1
            review.append(f"[REVIEW] root movie file does not exist: {source}")
            continue
        if not source.is_file():
            skipped += 1
            review.append(f"[REVIEW] root movie path is not a file: {source}")
            continue
        if source.is_symlink():
            skipped += 1
            review.append(f"[REVIEW] refusing to move symlinked root movie file: {source}")
            continue

        edition_marker = trailing_edition_marker(source.stem)
        if "{edition-" in source.stem.casefold() and edition_marker is None:
            skipped += 1
            review.append(
                f"[REVIEW] {source}: malformed or non-trailing edition marker; "
                "M2 will not discard or reinterpret it"
            )
            continue

        target_folder_name = canonical_folder_name(title, year, edition_marker)
        unsafe_reason = unsafe_component_reason(target_folder_name)
        if unsafe_reason is not None:
            skipped += 1
            unsafe_names.append(
                f"[UNSAFE NAME] {source}\n"
                f"  target folder: {target_folder_name}\n"
                f"  reason: {unsafe_reason}"
            )
            continue

        target_dir = source.parent / target_folder_name
        if target_dir.exists():
            if not target_dir.is_dir():
                skipped += 1
                review.append(
                    f"[REVIEW] target movie folder path is not a directory: {target_dir}"
                )
                continue
            if target_dir.is_symlink():
                skipped += 1
                review.append(
                    f"[REVIEW] refusing to move into symlinked target folder: {target_dir}"
                )
                continue

        preferred_target = target_dir / source.name
        target = available_file_target(preferred_target, reserved_targets)
        reserved_targets.add(target)

        library = library_by_id[library_id]
        root_file_plans.append(
            RootFilePlan(
                source=source,
                target=target,
                title=title,
                year=year,
                library_id=library_id,
                library_name=library.name,
            )
        )

    return folder_plans, root_file_plans, review, unsafe_names, skipped

def collision_source_inventory(source: Path) -> list[str]:
    """Describe a collision source without proposing or performing mutations."""
    lines: list[str] = []

    if not source.exists():
        return ["      [MISSING] source does not exist"]
    if source.is_symlink():
        return ["      [AMBIGUOUS SYMLINK] source folder itself is a symlink"]
    if not source.is_dir():
        return ["      [AMBIGUOUS] source is not a directory"]

    entries: list[tuple[str, Path]] = []
    walk_errors: list[str] = []

    def on_walk_error(exc):
        walk_errors.append(str(exc))

    for root_text, dir_names, file_names in os.walk(
        source,
        topdown=True,
        onerror=on_walk_error,
        followlinks=False,
    ):
        root = Path(root_text)

        for name in list(dir_names):
            path = root / name
            relative = path.relative_to(source)
            if path.is_symlink():
                entries.append(("symlink_dir", relative))
                dir_names.remove(name)
            else:
                entries.append(("directory", relative))

        for name in file_names:
            path = root / name
            relative = path.relative_to(source)

            if path.is_symlink():
                entries.append(("symlink_file", relative))
                continue

            suffix = path.suffix.casefold()
            if suffix in VIDEO_EXTENSIONS:
                kind = "video"
            elif suffix in SUBTITLE_EXTENSIONS:
                kind = "subtitle"
            elif suffix in GENERIC_SIDECAR_EXTENSIONS:
                kind = "sidecar"
            else:
                kind = "unknown_file"
            entries.append((kind, relative))

    entries.sort(key=lambda item: str(item[1]).casefold())

    videos = [relative for kind, relative in entries if kind == "video"]
    subtitles = [relative for kind, relative in entries if kind == "subtitle"]
    sidecars = [relative for kind, relative in entries if kind == "sidecar"]
    ambiguous = [
        relative
        for kind, relative in entries
        if kind in {"unknown_file", "symlink_file", "symlink_dir"}
    ]

    lines.append(
        "      summary: "
        f"{len(videos)} video(s), {len(subtitles)} subtitle(s), "
        f"{len(sidecars)} known generic sidecar(s), "
        f"{len(ambiguous)} immediately ambiguous item(s)"
    )

    sole_video = videos[0] if len(videos) == 1 else None

    for kind, relative in entries:
        if kind == "video":
            lines.append(f"      [VIDEO] {relative}")
            continue

        if kind == "subtitle":
            in_subtitle_tree = (
                bool(relative.parts)
                and relative.parts[0].casefold() in SUBTITLE_DIRECTORY_NAMES
            )
            location_note = " in subtitle directory" if in_subtitle_tree else ""

            if sole_video is not None:
                lines.append(
                    f"      [POTENTIAL SUBTITLE]{location_note} {relative}"
                    f" -> sole video is {sole_video}"
                )
            else:
                lines.append(
                    f"      [AMBIGUOUS SUBTITLE]{location_note} {relative}"
                    f" -> source contains {len(videos)} video files"
                )
            continue

        if kind == "sidecar":
            lines.append(
                f"      [GENERIC SIDECAR] {relative}"
                " -> association is not assumed"
            )
            continue

        if kind == "directory":
            if (
                bool(relative.parts)
                and relative.parts[0].casefold() in SUBTITLE_DIRECTORY_NAMES
            ):
                lines.append(f"      [SUBTITLE DIR] {relative}")
            else:
                lines.append(
                    f"      [AMBIGUOUS DIR] {relative}"
                    " -> contents must be reviewed before any merge"
                )
            continue

        if kind == "symlink_dir":
            lines.append(
                f"      [AMBIGUOUS SYMLINK DIR] {relative}"
                " -> never follow automatically"
            )
            continue

        if kind == "symlink_file":
            lines.append(
                f"      [AMBIGUOUS SYMLINK FILE] {relative}"
                " -> never move automatically"
            )
            continue

        lines.append(
            f"      [AMBIGUOUS FILE] {relative}"
            " -> unrecognized file type"
        )

    for error in walk_errors:
        lines.append(f"      [SCAN ERROR] {error}")

    if not entries and not walk_errors:
        lines.append("      [EMPTY] folder contains no entries")

    return lines


def analyze_collision_plans(plans: list[FolderPlan]) -> list[str]:
    """Inventory multi-folder Plex collisions for M3a; never mutate anything."""
    destination_sources: dict[Path, list[Path]] = defaultdict(list)
    for plan in plans:
        destination_sources[plan.target].append(plan.source)

    reports: list[str] = []

    for target in sorted(destination_sources, key=str):
        sources = sorted(set(destination_sources[target]), key=str)
        if len(sources) <= 1:
            continue

        lines = [
            f"[M3 ANALYSIS] canonical target: {target}",
            "  mode: diagnostic only; no merge, rename, move, or delete is planned",
            f"  Plex source folders: {len(sources)}",
            f"  canonical target currently exists: {'yes' if target.exists() else 'no'}",
        ]

        for source in sources:
            role = "canonical source" if source == target else "source"
            lines.append(f"  {role}: {source}")
            lines.extend(collision_source_inventory(source))

        if target.exists() and target not in sources:
            lines.append(
                "  existing target not represented as a Plex source in this collision:"
                f" {target}"
            )
            lines.extend(collision_source_inventory(target))

        reports.append("\n".join(lines))

    return reports


def validate_plans(
    plans: list[FolderPlan],
) -> tuple[list[FolderPlan], list[str], list[str], int]:
    actionable: list[FolderPlan] = []
    review: list[str] = []
    collision_reports: list[str] = []
    already_normalized = 0

    destination_sources: dict[Path, list[Path]] = defaultdict(list)
    for plan in plans:
        destination_sources[plan.target].append(plan.source)

    # A collision is reported once per destination, with every source shown.
    multi_source_targets = {
        target
        for target, sources in destination_sources.items()
        if len(sources) > 1
    }

    for target in sorted(multi_source_targets, key=str):
        sources = sorted(destination_sources[target], key=str)
        lines = [f"[COLLISION] target: {target}"]
        lines.extend(f"  source: {source}" for source in sources)
        collision_reports.append("\n".join(lines))

    # Track single-source destinations that already exist. These are also one
    # collision group each, but are distinct from duplicate Plex destinations.
    existing_target_collisions: dict[Path, list[Path]] = defaultdict(list)

    for plan in plans:
        if plan.source == plan.target:
            already_normalized += 1
            continue

        if plan.target in multi_source_targets:
            continue

        if not plan.source.exists():
            review.append(f"[REVIEW] source folder does not exist: {plan.source}")
            continue

        if not plan.source.is_dir():
            review.append(f"[REVIEW] source is not a directory: {plan.source}")
            continue

        if plan.source.is_symlink():
            review.append(f"[REVIEW] refusing to rename symlinked folder: {plan.source}")
            continue

        if plan.target.exists():
            existing_target_collisions[plan.target].append(plan.source)
            continue

        actionable.append(plan)

    for target in sorted(existing_target_collisions, key=str):
        sources = sorted(existing_target_collisions[target], key=str)
        lines = [f"[COLLISION] destination already exists: {target}"]
        lines.extend(f"  source: {source}" for source in sources)
        collision_reports.append("\n".join(lines))

    return actionable, review, collision_reports, already_normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Normalize movie folders and organize root-level movie files from Plex metadata. "
            "Dry-run is the default."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Local JSON configuration file. Defaults to ./config.json if present.",
    )
    parser.add_argument(
        "-d",
        "--database-folder",
        type=Path,
        help=(
            "Plex 'Plug-in Support/Databases' directory. Overrides database_folder "
            "from config.json."
        ),
    )
    parser.add_argument(
        "--library",
        action="append",
        default=[],
        help=(
            "Exact Plex library name or numeric library ID. May be repeated. "
            "Command-line values replace configured libraries."
        ),
    )
    parser.add_argument(
        "--list-libraries",
        action="store_true",
        help="List Plex libraries and exit without changing anything.",
    )
    parser.add_argument(
        "--path-map",
        action="append",
        default=[],
        type=parse_path_map,
        metavar="FROM=TO",
        help=(
            "Map a path stored by Plex to a path visible to this machine. "
            "May be repeated. Command-line mappings replace config mappings."
        ),
    )
    parser.add_argument(
        "--analyze-collisions",
        action="store_true",
        help=(
            "M3a diagnostic: inventory every multi-folder collision in detail. "
            "This mode is read-only and cannot be combined with --write."
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually apply folder renames and root-file moves. Without this flag nothing is changed.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.analyze_collisions and args.write:
        print(
            "[FATAL] --analyze-collisions is diagnostic-only and cannot be combined with --write.",
            file=sys.stderr,
        )
        return 2

    try:
        config = load_config(args.config)
        configured_database = config.get("database_folder")

        if args.database_folder is not None:
            database_folder = args.database_folder
        elif configured_database:
            if not isinstance(configured_database, str):
                raise ValueError("'database_folder' in config must be a string")
            database_folder = Path(configured_database)
        else:
            raise ValueError(
                "No database folder configured. Set database_folder in config.json "
                "or pass --database-folder."
            )

        path_maps = args.path_map if args.path_map else config_path_maps(config)

        if args.library:
            requested_libraries = args.library
        else:
            raw_libraries = config.get("libraries", [])
            if not isinstance(raw_libraries, list) or not all(
                isinstance(item, (str, int)) for item in raw_libraries
            ):
                raise ValueError("'libraries' in config must be an array of names or IDs")
            requested_libraries = [str(item) for item in raw_libraries]
    except (ValueError, argparse.ArgumentTypeError) as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 2

    library_db = database_folder / LIBRARY_DB

    try:
        conn = open_readonly(library_db)
    except (OSError, sqlite3.Error) as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 2

    try:
        available = list_libraries(conn)

        if args.list_libraries:
            print(f"{'ID':<5}{'Type':<14}Name")
            for library in available:
                print(f"{library.id:<5}{library.kind:<14}{library.name}")
            return 0

        if not requested_libraries:
            print(
                "[FATAL] No libraries selected. Set 'libraries' in config.json "
                "or pass --library.",
                file=sys.stderr,
            )
            return 2

        libraries, selection_errors = resolve_libraries(
            available,
            requested_libraries,
        )
        if selection_errors:
            for error in selection_errors:
                print(f"[FATAL] {error}", file=sys.stderr)
            return 2

        plans, root_file_plans, build_review, unsafe_names, build_skipped = build_plans(
            conn,
            libraries,
            path_maps,
        )
    except sqlite3.Error as exc:
        print(f"[FATAL] Plex database query failed: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    actionable, validation_review, collision_reports, already_normalized = validate_plans(plans)
    m3_analysis_reports = analyze_collision_plans(plans) if args.analyze_collisions else []
    actionable, suspicious = split_suspicious_plans(actionable, "M1")
    root_actionable, root_suspicious = split_suspicious_plans(root_file_plans, "M2")
    suspicious = suspicious + root_suspicious
    review = build_review + validation_review

    print("PlexLibraryMaintainer")
    print("====================")
    print(f"Library DB : {library_db}")
    print("DB access  : READ ONLY (SQLite mode=ro + PRAGMA query_only)")
    print(f"Output     : {'WRITE' if args.write else 'DRY RUN'}")
    print("Libraries  : " + ", ".join(library.name for library in libraries))
    print()

    for plan in actionable:
        print(f"[RENAME] [{plan.library_name}] {plan.source}")
        print(f"      -> {plan.target}")
        if not args.write:
            print("         [DRY RUN: not renamed]")
        print()

    for plan in root_actionable:
        print(f"[MOVE] [{plan.library_name}] {plan.source}")
        print(f"    -> {plan.target}")
        if not args.write:
            print("       [DRY RUN: not moved]")
        print()

    for line in unsafe_names:
        print(line)

    for line in suspicious:
        print(line)

    for line in review:
        print(line)

    for report in collision_reports:
        print(report)

    for report in m3_analysis_reports:
        print()
        print(report)

    renamed = 0
    moved = 0
    errors = 0
    rename_log_path = None
    rename_log_handle = None
    rename_run_id = None

    if args.write:
        try:
            rename_log_path, rename_log_handle, rename_run_id = create_rename_log()
        except OSError as exc:
            print(
                f"[FATAL] Could not create rename audit log; refusing to write: {exc}",
                file=sys.stderr,
            )
            return 2

        print(f"Rename audit log: {rename_log_path}")

        try:
            # Deepest paths first, so a parent rename cannot invalidate a child source path.
            for plan in sorted(
                actionable,
                key=lambda item: len(item.source.parts),
                reverse=True,
            ):
                try:
                    os.rename(plan.source, plan.target)
                    renamed += 1
                    write_rename_log(rename_log_handle, rename_run_id, "RENAMED", plan)
                    print(f"[RENAMED] {plan.source} -> {plan.target}")
                except OSError as exc:
                    errors += 1
                    write_rename_log(rename_log_handle, rename_run_id, "ERROR", plan, error=exc)
                    print(
                        f"[ERROR] Could not rename {plan.source} -> {plan.target}: {exc}",
                        file=sys.stderr,
                    )

            runtime_reserved: set[Path] = set()
            for plan in root_actionable:
                try:
                    target_dir = plan.target.parent
                    if target_dir.exists():
                        if not target_dir.is_dir() or target_dir.is_symlink():
                            raise OSError(f"unsafe target folder: {target_dir}")
                    else:
                        target_dir.mkdir()

                    preferred_target = target_dir / plan.source.name
                    actual_target = available_file_target(preferred_target, runtime_reserved)
                    runtime_reserved.add(actual_target)

                    os.rename(plan.source, actual_target)
                    moved += 1
                    write_rename_log(
                        rename_log_handle,
                        rename_run_id,
                        "MOVED",
                        plan,
                        target=actual_target,
                    )
                    print(f"[MOVED] {plan.source} -> {actual_target}")
                except OSError as exc:
                    errors += 1
                    write_rename_log(
                        rename_log_handle,
                        rename_run_id,
                        "ERROR",
                        plan,
                        error=exc,
                    )
                    print(
                        f"[ERROR] Could not move {plan.source}: {exc}",
                        file=sys.stderr,
                    )
        finally:
            rename_log_handle.close()

    print()
    print("Summary")
    print("=======")
    print(f"Items examined      : {len(plans) + len(root_file_plans) + build_skipped}")
    print(f"Already normalized  : {already_normalized}")
    print(f"Would rename        : {len(actionable) if not args.write else 0}")
    print(f"Would move          : {len(root_actionable) if not args.write else 0}")
    print(f"Renamed             : {renamed}")
    print(f"Moved               : {moved}")
    print(f"Unsafe names        : {len(unsafe_names)}")
    print(f"Suspicious matches  : {len(suspicious)}")
    print(f"Needs review        : {len(review)}")
    print(f"Collision groups    : {len(collision_reports)}")
    print(f"M3 analyses         : {len(m3_analysis_reports)}")
    print(f"Errors              : {errors}")
    if rename_log_path is not None:
        print(f"Rename audit log    : {rename_log_path}")

    if not args.write:
        print("\nDRY RUN ONLY. Nothing was changed. Add --write to apply folder renames and root-file moves.")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
