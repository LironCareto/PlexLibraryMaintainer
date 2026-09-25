#!/usr/bin/env python3
"""Normalize Plex movie folder names using Plex metadata.

Plex's database is always opened read-only. Dry-run is the default. With
--write, the tool can rename the first movie folder immediately below a
selected library root and can place Plex-indexed movie files that live directly
in the library root into their canonical movie folder.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ffmpeg_probe import probe_media_file_ffmpeg

LIBRARY_DB = "com.plexapp.plugins.library.db"
DEFAULT_CONFIG = Path("config.json")
SUSPICIOUS_SIMILARITY_THRESHOLD = 0.55
DUPLICATE_DURATION_TOLERANCE_SECONDS = 5.0

VIDEO_EXTENSIONS = {
    ".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg",
    ".mpg", ".ts", ".webm", ".wmv",
}
SUBTITLE_EXTENSIONS = {".ass", ".idx", ".smi", ".srt", ".ssa", ".sub", ".sup", ".vtt"}
GENERIC_SIDECAR_EXTENSIONS = {".jpg", ".jpeg", ".nfo", ".png", ".webp"}
SUBTITLE_DIRECTORY_NAMES = {"subs", "subtitles"}
PASSIVE_LEFTOVER_DIRECTORY_NAMES = {"cover-screens"}
SYSTEM_METADATA_DIRECTORY_NAMES = {"@eadir"}
SYSTEM_METADATA_FILE_NAMES = {".ds_store", "thumbs.db"}


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


def conflicting_source_years(plan: FolderPlan) -> set[int]:
    """Return explicit parenthesized source years that disagree with Plex."""
    if plan.year is None:
        return set()

    source_years = {
        int(match)
        for match in re.findall(r"\(((?:19|20)\d{2})\)", plan.comparison_name)
    }
    if source_years and plan.year not in source_years:
        return source_years
    return set()


def suspicious_title_match(plan: FolderPlan):
    """Return a similarity score when a proposed mapping looks suspicious.

    This is a guardrail, not a title parser. An explicit parenthesized year in
    the source must agree with Plex. Title similarity is then checked using
    whole-phrase containment, meaningful shared tokens, or character similarity.
    """
    if conflicting_source_years(plan):
        return 0.0

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

    source_meaningful = {token for token in source_tokens if len(token) >= 5}
    title_meaningful = {token for token in title_tokens if len(token) >= 5}
    if source_meaningful & title_meaningful:
        return None

    source_words = {token for token in source_tokens if len(token) >= 3}
    title_words = {token for token in title_tokens if len(token) >= 3}
    if len(source_words & title_words) >= 2:
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
                continue

            if name.casefold() in SYSTEM_METADATA_DIRECTORY_NAMES:
                entries.append(("system_dir", relative))
                # DSM's @eaDir tree contains generated indexes, thumbnails and
                # streams. It is never movie content, so do not recurse into it
                # or let it affect M3 safety classification.
                dir_names.remove(name)
                continue

            entries.append(("directory", relative))

        for name in file_names:
            path = root / name
            relative = path.relative_to(source)

            if path.is_symlink():
                entries.append(("symlink_file", relative))
                continue

            if name.casefold() in SYSTEM_METADATA_FILE_NAMES:
                entries.append(("system_file", relative))
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
    system_metadata = [
        relative
        for kind, relative in entries
        if kind in {"system_dir", "system_file"}
    ]
    ambiguous = [
        relative
        for kind, relative in entries
        if kind in {"unknown_file", "symlink_file", "symlink_dir"}
    ]

    lines.append(
        "      summary: "
        f"{len(videos)} video(s), {len(subtitles)} subtitle(s), "
        f"{len(sidecars)} known generic sidecar(s), "
        f"{len(system_metadata)} system metadata item(s), "
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

        if kind == "system_dir":
            lines.append(
                f"      [SYSTEM METADATA DIR] {relative}"
                " -> ignored for merge safety and not scanned"
            )
            continue

        if kind == "system_file":
            lines.append(
                f"      [SYSTEM METADATA FILE] {relative}"
                " -> ignored for merge safety"
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


def subtitle_matches_video(subtitle: Path, video: Path) -> bool:
    """Return whether a subtitle filename is clearly associated with a video."""
    subtitle_base = subtitle.name[:-len(subtitle.suffix)] if subtitle.suffix else subtitle.name
    video_stem = video.stem

    subtitle_folded = subtitle_base.casefold()
    video_folded = video_stem.casefold()

    if subtitle_folded == video_folded:
        return True

    if not subtitle_folded.startswith(video_folded):
        return False

    tail = subtitle_base[len(video_stem):]
    if not tail:
        return True

    # Accept a subtitle qualifier only when the boundary is explicit: either
    # the qualifier itself starts with a separator, or the video stem already
    # ends with one (for example "video_.mp4" + "video_eng.srt").
    return tail[0] in ".-_ " or video_stem[-1] in ".-_ "


def subtitle_tail_for_video(subtitle: Path, video: Path) -> str:
    """Return the subtitle suffix to append to the chosen video stem.

    A subtitle already named after the video keeps its existing qualifiers.
    A subtitle from Subs/Subtitles that only carries a language/label name is
    preserved as that label rather than guessed or translated.
    """
    subtitle_base = subtitle.name[:-len(subtitle.suffix)] if subtitle.suffix else subtitle.name
    subtitle_folded = subtitle_base.casefold()
    video_folded = video.stem.casefold()

    if subtitle_matches_video(subtitle, video):
        return subtitle_base[len(video.stem):] + subtitle.suffix
    return f".{subtitle_base}{subtitle.suffix}"


def is_subtitle_directory_name(name: str) -> bool:
    """Recognize conventional subtitle directories without guessing their contents."""
    folded = name.casefold()
    if folded in SUBTITLE_DIRECTORY_NAMES:
        return True

    return (
        folded.endswith("]")
        and (
            folded.startswith("subtitles [")
            or folded.startswith("subs [")
        )
    )


def inspect_subtitle_directory(subtitle_dir: Path, video: Path):
    """Inspect a flat Subs/Subtitles directory for one unambiguous video.

    Only recognized subtitle files and ignorable system metadata are accepted.
    The returned mapping contains the exact suffix each subtitle will receive
    after the video stem, for example '.eng.srt' or '.English.ass'.
    """
    subtitle_tails: dict[Path, str] = {}
    blockers: list[str] = []

    try:
        entries = sorted(subtitle_dir.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        return None, [f"cannot list subtitle directory {subtitle_dir.name}: {exc}"]

    for path in entries:
        name_folded = path.name.casefold()

        if path.is_symlink():
            blockers.append(f"symlink in subtitle directory: {path.name}")
            continue

        if path.is_dir():
            if name_folded in SYSTEM_METADATA_DIRECTORY_NAMES:
                continue
            blockers.append(f"nested directory in subtitle directory: {path.name}")
            continue

        if name_folded in SYSTEM_METADATA_FILE_NAMES:
            continue

        if path.suffix.casefold() not in SUBTITLE_EXTENSIONS:
            blockers.append(f"non-subtitle file in subtitle directory: {path.name}")
            continue

        subtitle_tails[path] = subtitle_tail_for_video(path, video)

    if not subtitle_tails:
        blockers.append(
            f"subtitle directory {subtitle_dir.name} contains no recognized subtitle files"
        )

    folded_targets: dict[str, Path] = {}
    for subtitle, tail in subtitle_tails.items():
        target_name = f"{video.stem}{tail}"
        folded = target_name.casefold()
        previous = folded_targets.get(folded)
        if previous is not None:
            blockers.append(
                "subtitle directory would produce duplicate target names: "
                f"{previous.name} and {subtitle.name} -> {target_name}"
            )
        else:
            folded_targets[folded] = subtitle

    if blockers:
        return None, blockers

    return subtitle_tails, []


def file_matches_video(path: Path, video: Path) -> bool:
    """Return whether a sidecar filename is clearly associated with a video."""
    base = path.name[:-len(path.suffix)] if path.suffix else path.name
    base_folded = base.casefold()
    video_folded = video.stem.casefold()
    return base_folded == video_folded or base_folded.startswith(video_folded + ".")


def associated_file_tail(path: Path, video: Path) -> str:
    """Return the suffix that preserves an associated file's qualifiers."""
    base = path.name[:-len(path.suffix)] if path.suffix else path.name
    if base.casefold() == video.stem.casefold():
        return path.suffix
    return base[len(video.stem):] + path.suffix


def merge_source_files(source: Path):
    """Classify a source folder for conservative M3b planning.

    System metadata and unrelated leftovers do not block a safe merge because
    the original source folder can later be moved intact to quarantine. Real
    subdirectories, symlinks, ambiguous subtitles, and unsafe Subs/Subtitles
    structures still block the source.
    """
    if not source.exists():
        return None, [f"source does not exist: {source}"]
    if source.is_symlink():
        return None, [f"source folder is a symlink: {source}"]
    if not source.is_dir():
        return None, [f"source is not a directory: {source}"]

    videos: list[Path] = []
    subtitles: list[Path] = []
    sidecars: list[Path] = []
    subtitle_dirs: list[Path] = []
    leftovers: list[Path] = []
    blockers: list[str] = []

    try:
        entries = sorted(source.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        return None, [f"cannot list source folder: {exc}"]

    for path in entries:
        name_folded = path.name.casefold()

        if path.is_symlink():
            blockers.append(f"symlink present: {path.name}")
            continue

        if path.is_dir():
            if name_folded in SYSTEM_METADATA_DIRECTORY_NAMES:
                continue
            if is_subtitle_directory_name(path.name):
                subtitle_dirs.append(path)
                continue
            if name_folded in PASSIVE_LEFTOVER_DIRECTORY_NAMES:
                leftovers.append(path)
                continue
            blockers.append(f"real subdirectory present: {path.name}")
            continue

        if name_folded in SYSTEM_METADATA_FILE_NAMES:
            continue

        suffix = path.suffix.casefold()
        if suffix in VIDEO_EXTENSIONS:
            videos.append(path)
        elif suffix in SUBTITLE_EXTENSIONS:
            subtitles.append(path)
        elif suffix in GENERIC_SIDECAR_EXTENSIONS:
            sidecars.append(path)
        else:
            leftovers.append(path)

    if not videos:
        blockers.append("no recognized video files")

    if subtitle_dirs and len(videos) != 1:
        blockers.append(
            "Subs/Subtitles association is ambiguous because the source does not "
            f"contain exactly one video ({len(videos)} found)"
        )

    if blockers:
        return None, blockers

    subtitle_to_video: dict[Path, Path] = {}
    subtitle_tails: dict[Path, str] = {}

    for subtitle in subtitles:
        matches = [
            video
            for video in videos
            if subtitle_matches_video(subtitle, video)
        ]
        if len(matches) != 1:
            if not matches:
                blockers.append(
                    f"subtitle has no unambiguous video stem match: {subtitle.name}"
                )
            else:
                names = ", ".join(video.name for video in matches)
                blockers.append(
                    f"subtitle matches multiple video stems: {subtitle.name} -> {names}"
                )
            continue

        video = matches[0]
        subtitle_to_video[subtitle] = video
        subtitle_tails[subtitle] = subtitle_tail_for_video(subtitle, video)

    subtitle_dir_files: dict[Path, list[Path]] = {}
    if subtitle_dirs:
        video = videos[0]
        for subtitle_dir in subtitle_dirs:
            inspected, directory_blockers = inspect_subtitle_directory(
                subtitle_dir,
                video,
            )
            if directory_blockers:
                blockers.extend(directory_blockers)
                continue

            subtitle_dir_files[subtitle_dir] = sorted(
                inspected,
                key=lambda item: item.name.casefold(),
            )
            for subtitle, tail in inspected.items():
                subtitle_to_video[subtitle] = video
                subtitle_tails[subtitle] = tail

    sidecar_to_video: dict[Path, Path] = {}
    sidecar_tails: dict[Path, str] = {}

    for sidecar in sidecars:
        matches = [
            video
            for video in videos
            if file_matches_video(sidecar, video)
        ]
        if len(matches) == 1:
            video = matches[0]
            sidecar_to_video[sidecar] = video
            sidecar_tails[sidecar] = associated_file_tail(sidecar, video)
        else:
            leftovers.append(sidecar)

    folded_targets_by_video: dict[Path, dict[str, Path]] = defaultdict(dict)
    companions = []
    companions.extend(
        (subtitle, video, subtitle_tails[subtitle])
        for subtitle, video in subtitle_to_video.items()
    )
    companions.extend(
        (sidecar, video, sidecar_tails[sidecar])
        for sidecar, video in sidecar_to_video.items()
    )

    for companion, video, tail in companions:
        target_name = f"{video.stem}{tail}"
        folded = target_name.casefold()
        previous = folded_targets_by_video[video].get(folded)
        if previous is not None:
            blockers.append(
                "associated files would produce duplicate target names: "
                f"{previous} and {companion} -> {target_name}"
            )
        else:
            folded_targets_by_video[video][folded] = companion

    if blockers:
        return None, blockers

    return {
        "videos": videos,
        "subtitles": list(subtitle_to_video),
        "subtitle_to_video": subtitle_to_video,
        "subtitle_tails": subtitle_tails,
        "sidecars": list(sidecar_to_video),
        "sidecar_to_video": sidecar_to_video,
        "sidecar_tails": sidecar_tails,
        "subtitle_dirs": subtitle_dir_files,
        "leftovers": sorted(leftovers, key=lambda item: item.name.casefold()),
    }, []


def available_video_bundle_targets(
    target_dir: Path,
    video: Path,
    companions: list[Path],
    companion_tails: dict[Path, str],
    reserved: set[Path],
):
    """Choose one suffix index that keeps a video and all companions together."""
    index = 0

    while True:
        if index == 0:
            video_name = video.name
            video_stem = video.stem
        else:
            video_name = f"{video.stem} ({index}){video.suffix}"
            video_stem = f"{video.stem} ({index})"

        video_target = target_dir / video_name
        companion_targets: dict[Path, Path] = {}
        candidates = [video_target]

        for companion in companions:
            companion_target = target_dir / f"{video_stem}{companion_tails[companion]}"
            companion_targets[companion] = companion_target
            candidates.append(companion_target)

        candidate_names = [path.name.casefold() for path in candidates]
        if len(candidate_names) != len(set(candidate_names)):
            raise ValueError("video bundle would create duplicate target filenames")

        if all(not path.exists() and path not in reserved for path in candidates):
            return video_target, companion_targets

        index += 1


def available_directory_target(target: Path, reserved: set[Path]) -> Path:
    """Return a non-existing directory target without overwriting anything."""
    if not target.exists() and target not in reserved:
        return target

    index = 1
    while True:
        candidate = target.with_name(f"{target.name} ({index})")
        if not candidate.exists() and candidate not in reserved:
            return candidate
        index += 1


def quarantine_root_for_target(target: Path) -> Path:
    """Keep quarantined source shells outside the Plex library root."""
    library_root = target.parent
    return library_root.parent / "_PlexLibraryMaintainer_Quarantine" / library_root.name


def plan_canonical_subtitle_flatten(
    target: Path,
    classified,
    reserved: set[Path],
):
    """Plan only Subs/Subtitles flattening for a canonical source already in place."""
    subtitle_dirs: dict[Path, list[Path]] = classified["subtitle_dirs"]
    if not subtitle_dirs:
        return [], [], []

    videos: list[Path] = classified["videos"]
    if len(videos) != 1:
        return [], [], [
            "canonical Subs/Subtitles cannot be flattened unless exactly one video is present"
        ]

    video = videos[0]
    subtitle_tails: dict[Path, str] = classified["subtitle_tails"]
    moves: list[tuple[Path, Path]] = []
    removals: list[tuple[Path, list[Path]]] = []
    blockers: list[str] = []

    for subtitle_dir in sorted(subtitle_dirs, key=str):
        directory_moves: list[tuple[Path, Path]] = []
        for subtitle in subtitle_dirs[subtitle_dir]:
            target_path = target / f"{video.stem}{subtitle_tails[subtitle]}"
            if target_path.exists() or target_path in reserved:
                blockers.append(
                    f"subtitle target already exists: {target_path}"
                )
                continue
            directory_moves.append((subtitle, target_path))

        if blockers:
            continue

        for source_path, target_path in directory_moves:
            moves.append((source_path, target_path))
            reserved.add(target_path)
        removals.append(
            (subtitle_dir, [target_path for _, target_path in directory_moves])
        )

    if blockers:
        return [], [], blockers

    return moves, removals, []


def plan_collision_merges(plans: list[FolderPlan]):
    """Build exact M3b move and quarantine plans without mutating the filesystem."""
    destination_plans: dict[Path, list[FolderPlan]] = defaultdict(list)
    for plan in plans:
        destination_plans[plan.target].append(plan)

    reports: list[str] = []
    planned_moves = 0
    blocked_sources = 0
    quarantine_reserved: set[Path] = set()

    for target in sorted(destination_plans, key=str):
        group = destination_plans[target]
        sources = sorted({plan.source for plan in group}, key=str)
        if len(sources) <= 1:
            continue

        lines = [
            f"[M3 PLAN] canonical target: {target}",
            "  mode: plan only; --write cannot execute M3 moves",
        ]

        target_exists = target.exists()
        if target_exists:
            if target.is_symlink() or not target.is_dir():
                lines.append("  [M3 BLOCKED] canonical target is not a safe real directory")
                blocked_sources += max(1, len(sources) - 1)
                reports.append("\n".join(lines))
                continue

            if target not in sources:
                lines.append(
                    "  [M3 BLOCKED] canonical target exists but is not one of the Plex source folders"
                )
                blocked_sources += len(sources)
                reports.append("\n".join(lines))
                continue
        else:
            if (
                not target.parent.exists()
                or not target.parent.is_dir()
                or target.parent.is_symlink()
            ):
                lines.append(
                    "  [M3 BLOCKED] canonical target parent is not a safe real directory"
                )
                blocked_sources += len(sources)
                reports.append("\n".join(lines))
                continue

            lines.append(f"  [M3 MKDIR] {target}")

        reserved: set[Path] = set()

        for source in sources:
            classified, blockers = merge_source_files(source)
            if blockers:
                blocked_sources += 1
                role = "canonical source" if source == target else "source"
                lines.append(f"  [M3 BLOCKED {role.upper()}] {source}")
                for blocker in blockers:
                    lines.append(f"    reason: {blocker}")
                continue

            if source == target:
                flatten_moves, removals, flatten_blockers = plan_canonical_subtitle_flatten(
                    target,
                    classified,
                    reserved,
                )
                if flatten_blockers:
                    blocked_sources += 1
                    lines.append(f"  [M3 BLOCKED CANONICAL SOURCE] {source}")
                    for blocker in flatten_blockers:
                        lines.append(f"    reason: {blocker}")
                    continue

                lines.append(f"  canonical source stays in place: {source}")
                for source_path, target_path in flatten_moves:
                    lines.append(f"    [M3 MOVE SUBTITLE] {source_path}")
                    lines.append(f"                      -> {target_path}")
                    planned_moves += 1
                canonical_quarantine_dir = quarantine_root_for_target(target) / target.name
                for subtitle_dir, target_paths in removals:
                    lines.append(
                        f"    [M3 VERIFY SUBS] {subtitle_dir}: verify "
                        f"{len(target_paths)} moved subtitle(s) before quarantine"
                    )
                    subtitle_quarantine = available_directory_target(
                        canonical_quarantine_dir / subtitle_dir.name,
                        quarantine_reserved,
                    )
                    quarantine_reserved.add(subtitle_quarantine)
                    lines.append(f"    [M3 QUARANTINE SUBS] {subtitle_dir}")
                    lines.append(f"                         -> {subtitle_quarantine}")
                continue

            videos: list[Path] = classified["videos"]
            subtitle_to_video: dict[Path, Path] = classified["subtitle_to_video"]
            subtitle_tails: dict[Path, str] = classified["subtitle_tails"]
            sidecar_to_video: dict[Path, Path] = classified["sidecar_to_video"]
            sidecar_tails: dict[Path, str] = classified["sidecar_tails"]
            subtitle_dirs: dict[Path, list[Path]] = classified["subtitle_dirs"]
            leftovers: list[Path] = classified["leftovers"]

            companions_by_video: dict[Path, list[Path]] = defaultdict(list)
            companion_tails: dict[Path, str] = {}

            for subtitle, video in subtitle_to_video.items():
                companions_by_video[video].append(subtitle)
                companion_tails[subtitle] = subtitle_tails[subtitle]

            for sidecar, video in sidecar_to_video.items():
                companions_by_video[video].append(sidecar)
                companion_tails[sidecar] = sidecar_tails[sidecar]

            source_moves: list[tuple[Path, Path, str]] = []
            target_by_subtitle: dict[Path, Path] = {}

            try:
                for video in sorted(videos, key=lambda item: item.name.casefold()):
                    companions_for_video = sorted(
                        companions_by_video.get(video, []),
                        key=lambda item: str(item).casefold(),
                    )
                    video_target, companion_targets = available_video_bundle_targets(
                        target,
                        video,
                        companions_for_video,
                        companion_tails,
                        reserved,
                    )

                    source_moves.append((video, video_target, "VIDEO"))
                    reserved.add(video_target)

                    for companion in companions_for_video:
                        companion_target = companion_targets[companion]
                        if companion in subtitle_to_video:
                            kind = "SUBTITLE"
                            target_by_subtitle[companion] = companion_target
                        else:
                            kind = "SIDECAR"
                        source_moves.append((companion, companion_target, kind))
                        reserved.add(companion_target)
            except ValueError as exc:
                blocked_sources += 1
                lines.append(f"  [M3 BLOCKED SOURCE] {source}")
                lines.append(f"    reason: {exc}")
                continue

            lines.append(f"  [M3 SOURCE READY] {source}")
            for source_path, target_path, kind in source_moves:
                lines.append(f"    [M3 MOVE {kind}] {source_path}")
                lines.append(f"                     -> {target_path}")
                planned_moves += 1

            for subtitle_dir in sorted(subtitle_dirs, key=str):
                moved_targets = [
                    target_by_subtitle[subtitle]
                    for subtitle in subtitle_dirs[subtitle_dir]
                ]
                lines.append(
                    f"    [M3 VERIFY SUBS] {subtitle_dir}: verify "
                    f"{len(moved_targets)} moved subtitle(s) before source quarantine"
                )

            for leftover in leftovers:
                lines.append(
                    f"    [M3 LEFTOVER] {leftover} "
                    "-> retained inside the source shell"
                )

            quarantine_root = quarantine_root_for_target(target)
            quarantine_target = available_directory_target(
                quarantine_root / source.name,
                quarantine_reserved,
            )
            quarantine_reserved.add(quarantine_target)

            lines.append(
                f"    [M3 QUARANTINE MKDIR] {quarantine_root} "
                "(if it does not already exist)"
            )
            lines.append(f"    [M3 QUARANTINE SOURCE] {source}")
            lines.append(f"                           -> {quarantine_target}")
            lines.append(
                "      only after every planned move succeeds and every Subs/Subtitles "
                "directory has been verified and removed; the source shell is moved intact, "
                "never deleted"
            )

        reports.append("\n".join(lines))

    return reports, planned_moves, blocked_sources


def collision_groups(plans: list[FolderPlan]) -> dict[Path, list[FolderPlan]]:
    """Return only Plex targets currently backed by more than one source folder."""
    groups: dict[Path, list[FolderPlan]] = defaultdict(list)
    for plan in plans:
        groups[plan.target].append(plan)

    return {
        target: group
        for target, group in groups.items()
        if len({plan.source for plan in group}) > 1
    }


def selected_collision(plans: list[FolderPlan], selector: str):
    """Return exactly one collision group selected by canonical name or path."""
    groups = collision_groups(plans)

    key = selector.casefold()
    matches = [
        (target, group)
        for target, group in groups.items()
        if target.name.casefold() == key or str(target).casefold() == key
    ]

    if not matches:
        raise ValueError(f"no collision found for {selector!r}")
    if len(matches) != 1:
        raise ValueError(f"collision selector is ambiguous: {selector!r}")

    return matches[0]


def build_collision_execution_plan(
    plans: list[FolderPlan],
    selector: str,
    accept_title_mismatch: bool = False,
):
    """Build a complete, read-only preflight plan for one selected collision."""
    target, group = selected_collision(plans, selector)

    year_conflicts = []
    for plan in group:
        conflicts = conflicting_source_years(plan)
        if conflicts:
            years = ", ".join(str(year) for year in sorted(conflicts))
            year_conflicts.append(
                f"{plan.source} has source year(s) {years}, Plex year {plan.year}"
            )
    if year_conflicts:
        raise ValueError(
            "collision year guardrail rejected: " + "; ".join(year_conflicts)
        )

    if not accept_title_mismatch:
        suspicious_sources = []
        for plan in group:
            score = suspicious_title_match(plan)
            if score is not None:
                suspicious_sources.append(
                    f"{plan.source} -> {display_title_year(plan.title, plan.year)}"
                )
        if suspicious_sources:
            raise ValueError(
                "collision identity guardrail rejected: "
                + "; ".join(suspicious_sources)
            )

    sources = sorted({plan.source for plan in group}, key=str)

    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise ValueError(f"canonical target is not a safe directory: {target}")
        if target not in sources:
            raise ValueError(
                "canonical target exists but is not one of the Plex source folders"
            )
        create_target = False
    else:
        if (
            not target.parent.exists()
            or not target.parent.is_dir()
            or target.parent.is_symlink()
        ):
            raise ValueError(
                f"canonical target parent is not a safe directory: {target.parent}"
            )
        create_target = True

    reserved: set[Path] = set()
    quarantine_reserved: set[Path] = set()
    actions = []

    for source in sources:
        classified, blockers = merge_source_files(source)
        if blockers:
            raise ValueError(f"{source}: " + "; ".join(blockers))

        if source == target:
            flatten_moves, removals, flatten_blockers = plan_canonical_subtitle_flatten(
                target,
                classified,
                reserved,
            )
            if flatten_blockers:
                raise ValueError(f"{source}: " + "; ".join(flatten_blockers))

            subtitle_quarantine_targets = []
            quarantine_root = quarantine_root_for_target(target)
            canonical_quarantine_dir = quarantine_root / target.name
            if canonical_quarantine_dir.exists() and (
                canonical_quarantine_dir.is_symlink()
                or not canonical_quarantine_dir.is_dir()
            ):
                raise ValueError(
                    "canonical quarantine path is not a safe directory: "
                    f"{canonical_quarantine_dir}"
                )

            for subtitle_dir, _ in removals:
                quarantine_target = available_directory_target(
                    canonical_quarantine_dir / subtitle_dir.name,
                    quarantine_reserved,
                )
                quarantine_reserved.add(quarantine_target)
                subtitle_quarantine_targets.append(
                    (subtitle_dir, quarantine_target)
                )

            actions.append(
                {
                    "source": source,
                    "canonical": True,
                    "moves": [
                        (source_path, target_path, "SUBTITLE")
                        for source_path, target_path in flatten_moves
                    ],
                    "subtitle_dirs": [path for path, _ in removals],
                    "subtitle_quarantine_targets": subtitle_quarantine_targets,
                    "leftovers": classified["leftovers"],
                    "quarantine_target": None,
                }
            )
            continue

        subtitle_to_video: dict[Path, Path] = classified["subtitle_to_video"]
        subtitle_tails: dict[Path, str] = classified["subtitle_tails"]
        sidecar_to_video: dict[Path, Path] = classified["sidecar_to_video"]
        sidecar_tails: dict[Path, str] = classified["sidecar_tails"]
        subtitle_dirs: dict[Path, list[Path]] = classified["subtitle_dirs"]

        companions_by_video: dict[Path, list[Path]] = defaultdict(list)
        companion_tails: dict[Path, str] = {}

        for subtitle, video in subtitle_to_video.items():
            companions_by_video[video].append(subtitle)
            companion_tails[subtitle] = subtitle_tails[subtitle]

        for sidecar, video in sidecar_to_video.items():
            companions_by_video[video].append(sidecar)
            companion_tails[sidecar] = sidecar_tails[sidecar]

        moves: list[tuple[Path, Path, str]] = []

        for video in sorted(classified["videos"], key=lambda item: item.name.casefold()):
            companions = sorted(
                companions_by_video.get(video, []),
                key=lambda item: str(item).casefold(),
            )
            video_target, companion_targets = available_video_bundle_targets(
                target,
                video,
                companions,
                companion_tails,
                reserved,
            )

            moves.append((video, video_target, "VIDEO"))
            reserved.add(video_target)

            for companion in companions:
                companion_target = companion_targets[companion]
                kind = "SUBTITLE" if companion in subtitle_to_video else "SIDECAR"
                moves.append((companion, companion_target, kind))
                reserved.add(companion_target)

        quarantine_root = quarantine_root_for_target(target)
        quarantine_target = available_directory_target(
            quarantine_root / source.name,
            quarantine_reserved,
        )
        quarantine_reserved.add(quarantine_target)

        actions.append(
            {
                "source": source,
                "canonical": False,
                "moves": moves,
                "subtitle_dirs": sorted(subtitle_dirs, key=str),
                "subtitle_quarantine_targets": [],
                "leftovers": classified["leftovers"],
                "quarantine_target": quarantine_target,
            }
        )

    return {
        "target": target,
        "create_target": create_target,
        "quarantine_root": quarantine_root_for_target(target),
        "metadata_plan": group[0],
        "accept_title_mismatch": accept_title_mismatch,
        "actions": actions,
    }


def build_ready_collision_executions(plans: list[FolderPlan]):
    """Preflight every collision, returning executable groups and refused groups."""
    ready = []
    skipped = []

    for target in sorted(collision_groups(plans), key=str):
        try:
            ready.append(build_collision_execution_plan(plans, str(target)))
        except (OSError, ValueError) as exc:
            skipped.append((target, str(exc)))

    return ready, skipped


def print_batch_collision_preflight(ready, skipped, will_write: bool) -> None:
    print("PlexLibraryMaintainer M3 batch preflight")
    print("=======================================")
    print(f"Collision groups     : {len(ready) + len(skipped)}")
    print(f"Ready                : {len(ready)}")
    print(f"Skipped              : {len(skipped)}")
    print(f"Execution            : {'WRITE ready groups' if will_write else 'DRY RUN'}")
    print()

    for execution in ready:
        print(f"[M3 READY] {execution['target']}")

    for target, reason in skipped:
        print(f"[M3 SKIP] {target}")
        print(f"          reason: {reason}")


def execute_ready_collision_batch(ready) -> int:
    """Execute preflighted collision groups sequentially; stop on first runtime error."""
    completed = 0

    for index, execution in enumerate(ready, start=1):
        print()
        print(f"M3 batch group {index}/{len(ready)}")
        print("==============================")
        print_collision_execution_plan(execution)
        result = execute_collision_execution_plan(execution)
        if result != 0:
            print(
                f"[FATAL] M3 batch stopped after {completed} completed group(s).",
                file=sys.stderr,
            )
            return result
        completed += 1

    print()
    print("M3 batch summary")
    print("================")
    print(f"Groups completed     : {completed}")
    print("Runtime errors       : 0")
    return 0


def print_collision_execution_plan(execution) -> None:
    """Print the frozen shape M3c would execute; this function never writes."""
    target: Path = execution["target"]

    print("PlexLibraryMaintainer M3c preflight")
    print("===================================")
    print(f"Selected collision : {target}")
    print("Scope              : this collision only")
    print("Execution          : single-collision write requested")
    if execution.get("accept_title_mismatch"):
        print("Title identity     : explicitly accepted for this collision")
    print()

    if execution["create_target"]:
        print(f"[M3 MKDIR] {target}")

    for action in execution["actions"]:
        source = action["source"]
        role = "CANONICAL SOURCE" if action["canonical"] else "SOURCE"
        print(f"[M3 {role}] {source}")

        for source_path, target_path, kind in action["moves"]:
            print(f"  [M3 MOVE {kind}] {source_path}")
            print(f"                   -> {target_path}")

        for subtitle_dir in action["subtitle_dirs"]:
            print(
                f"  [M3 VERIFY SUBS] {subtitle_dir} "
                "after moving every planned subtitle"
            )

        for subtitle_dir, quarantine_target in action["subtitle_quarantine_targets"]:
            print(f"  [M3 QUARANTINE SUBS] {subtitle_dir}")
            print(f"                       -> {quarantine_target}")

        for leftover in action["leftovers"]:
            print(f"  [M3 LEFTOVER] {leftover}")

        quarantine_target = action["quarantine_target"]
        if quarantine_target is not None:
            print(f"  [M3 QUARANTINE SOURCE] {source}")
            print(f"                         -> {quarantine_target}")

        print()


def write_m3_audit(handle, run_id: str, status: str, source: Path, target: Path | None, plan):
    record = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": status,
        "run_id": run_id,
        "source": str(source),
        "target": str(target) if target is not None else None,
        "library": plan.library_name,
        "title": plan.title,
        "year": plan.year,
    }
    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    handle.flush()


def verify_drained_subtitle_directory(subtitle_dir: Path) -> None:
    """Require a processed Subs/Subtitles directory to contain only system metadata."""
    if not subtitle_dir.exists() or not subtitle_dir.is_dir() or subtitle_dir.is_symlink():
        raise OSError(f"subtitle directory is no longer safe: {subtitle_dir}")

    for entry in subtitle_dir.iterdir():
        if entry.is_symlink():
            raise OSError(f"symlink appeared in subtitle directory: {entry}")

        folded = entry.name.casefold()
        if entry.is_dir() and folded in SYSTEM_METADATA_DIRECTORY_NAMES:
            continue
        if entry.is_file() and folded in SYSTEM_METADATA_FILE_NAMES:
            continue

        raise OSError(f"unexpected content remains in subtitle directory: {entry}")


def execute_collision_execution_plan(execution) -> int:
    """Execute one preflighted collision, including verified Subs/Subtitles handling."""
    target: Path = execution["target"]
    metadata_plan = execution["metadata_plan"]
    actions = execution["actions"]

    frozen = {}
    try:
        for action in actions:
            for source_path, target_path, _ in action["moves"]:
                if (
                    not source_path.exists()
                    or not source_path.is_file()
                    or source_path.is_symlink()
                ):
                    raise OSError(f"planned source changed or disappeared: {source_path}")
                if target_path.exists():
                    raise OSError(f"refusing to overwrite existing target: {target_path}")

                stat = source_path.stat()
                frozen[source_path] = (stat.st_size, stat.st_mtime_ns)

        if execution["create_target"] and target.exists():
            raise OSError(f"canonical target appeared after preflight: {target}")
    except OSError as exc:
        print(f"[FATAL] M3c final preflight failed: {exc}", file=sys.stderr)
        return 2

    try:
        log_path, log_handle, run_id = create_rename_log()
    except OSError as exc:
        print(f"[FATAL] Could not create audit log; refusing M3c write: {exc}", file=sys.stderr)
        return 2

    moved = 0
    quarantined = 0
    errors = 0

    try:
        if execution["create_target"]:
            try:
                target.mkdir()
                write_m3_audit(
                    log_handle, run_id, "M3_MKDIR", target, target, metadata_plan
                )
                print(f"[M3 CREATED] {target}")
            except OSError as exc:
                errors += 1
                print(f"[M3 ERROR] Could not create {target}: {exc}", file=sys.stderr)
                return 1

        for action in actions:
            action_failed = False
            for source_path, target_path, kind in action["moves"]:
                try:
                    if (
                        not source_path.exists()
                        or not source_path.is_file()
                        or source_path.is_symlink()
                    ):
                        raise OSError(f"source changed since preflight: {source_path}")

                    stat = source_path.stat()
                    if (stat.st_size, stat.st_mtime_ns) != frozen[source_path]:
                        raise OSError(f"source changed since preflight: {source_path}")
                    if target_path.exists():
                        raise OSError(f"refusing to overwrite existing target: {target_path}")

                    os.rename(source_path, target_path)

                    if (
                        source_path.exists()
                        or not target_path.exists()
                        or not target_path.is_file()
                        or target_path.stat().st_size != frozen[source_path][0]
                    ):
                        raise OSError(f"move verification failed: {target_path}")

                    moved += 1
                    write_m3_audit(
                        log_handle,
                        run_id,
                        f"M3_MOVED_{kind}",
                        source_path,
                        target_path,
                        metadata_plan,
                    )
                    print(f"[M3 MOVED {kind}] {source_path} -> {target_path}")
                except OSError as exc:
                    errors += 1
                    action_failed = True
                    write_m3_audit(
                        log_handle,
                        run_id,
                        "M3_ERROR",
                        source_path,
                        target_path,
                        metadata_plan,
                    )
                    print(f"[M3 ERROR] {exc}", file=sys.stderr)
                    break

            if action_failed:
                break

            try:
                for subtitle_dir in action["subtitle_dirs"]:
                    planned_subtitles = [
                        (source_path, target_path)
                        for source_path, target_path, kind in action["moves"]
                        if kind == "SUBTITLE" and source_path.parent == subtitle_dir
                    ]
                    for source_path, target_path in planned_subtitles:
                        if source_path.exists():
                            raise OSError(
                                f"subtitle still exists at source after move: {source_path}"
                            )
                        if not target_path.exists() or not target_path.is_file():
                            raise OSError(
                                f"moved subtitle is missing at destination: {target_path}"
                            )
                    verify_drained_subtitle_directory(subtitle_dir)
                    print(f"[M3 VERIFIED SUBS] {subtitle_dir}")
            except OSError as exc:
                errors += 1
                write_m3_audit(
                    log_handle,
                    run_id,
                    "M3_ERROR",
                    action["source"],
                    None,
                    metadata_plan,
                )
                print(f"[M3 ERROR] {exc}", file=sys.stderr)
                break

            for subtitle_dir, subtitle_quarantine in action["subtitle_quarantine_targets"]:
                try:
                    quarantine_parent = subtitle_quarantine.parent
                    if quarantine_parent.exists() and (
                        quarantine_parent.is_symlink()
                        or not quarantine_parent.is_dir()
                    ):
                        raise OSError(
                            f"unsafe subtitle quarantine parent: {quarantine_parent}"
                        )
                    quarantine_parent.mkdir(parents=True, exist_ok=True)
                    if quarantine_parent.is_symlink() or not quarantine_parent.is_dir():
                        raise OSError(
                            f"unsafe subtitle quarantine parent: {quarantine_parent}"
                        )
                    if subtitle_quarantine.exists():
                        raise OSError(
                            f"refusing to overwrite subtitle quarantine target: "
                            f"{subtitle_quarantine}"
                        )
                    verify_drained_subtitle_directory(subtitle_dir)
                    os.rename(subtitle_dir, subtitle_quarantine)
                    if subtitle_dir.exists() or not subtitle_quarantine.is_dir():
                        raise OSError(
                            f"subtitle quarantine verification failed: "
                            f"{subtitle_quarantine}"
                        )

                    quarantined += 1
                    write_m3_audit(
                        log_handle,
                        run_id,
                        "M3_QUARANTINED_SUBTITLE_DIR",
                        subtitle_dir,
                        subtitle_quarantine,
                        metadata_plan,
                    )
                    print(
                        f"[M3 QUARANTINED SUBS] {subtitle_dir} "
                        f"-> {subtitle_quarantine}"
                    )
                except OSError as exc:
                    errors += 1
                    action_failed = True
                    write_m3_audit(
                        log_handle,
                        run_id,
                        "M3_ERROR",
                        subtitle_dir,
                        subtitle_quarantine,
                        metadata_plan,
                    )
                    print(f"[M3 ERROR] {exc}", file=sys.stderr)
                    break

            if action_failed:
                break

            source = action["source"]
            quarantine_target = action["quarantine_target"]
            if quarantine_target is None:
                continue

            try:
                quarantine_target.parent.mkdir(parents=True, exist_ok=True)
                if quarantine_target.exists():
                    raise OSError(
                        f"refusing to overwrite quarantine target: {quarantine_target}"
                    )
                if not source.exists() or not source.is_dir() or source.is_symlink():
                    raise OSError(f"unsafe source shell before quarantine: {source}")

                os.rename(source, quarantine_target)

                if source.exists() or not quarantine_target.is_dir():
                    raise OSError(f"quarantine verification failed: {quarantine_target}")

                quarantined += 1
                write_m3_audit(
                    log_handle,
                    run_id,
                    "M3_QUARANTINED",
                    source,
                    quarantine_target,
                    metadata_plan,
                )
                print(f"[M3 QUARANTINED] {source} -> {quarantine_target}")
            except OSError as exc:
                errors += 1
                write_m3_audit(
                    log_handle,
                    run_id,
                    "M3_ERROR",
                    source,
                    quarantine_target,
                    metadata_plan,
                )
                print(f"[M3 ERROR] {exc}", file=sys.stderr)
                break
    finally:
        log_handle.close()

    print()
    print("M3c summary")
    print("===========")
    print(f"Files moved         : {moved}")
    print(f"Sources quarantined : {quarantined}")
    print(f"Errors              : {errors}")
    print(f"Audit log           : {log_path}")
    return 1 if errors else 0



def duplicate_movie_groups(
    conn: sqlite3.Connection,
    libraries: list[Library],
    path_maps: list[tuple[str, str]],
):
    """Return Plex movie items that contain more than one media version."""
    library_by_id = {library.id: library for library in libraries}
    library_ids = list(library_by_id)
    if not library_ids:
        return []

    placeholders = ",".join("?" for _ in library_ids)
    rows = conn.execute(
        f"""
        SELECT
            metadata.library_section_id,
            metadata.id AS metadata_id,
            metadata.title,
            metadata.year,
            media.id AS media_id,
            parts.id AS part_id,
            parts.file
        FROM metadata_items AS metadata
        INNER JOIN media_items AS media
            ON media.metadata_item_id = metadata.id
        INNER JOIN media_parts AS parts
            ON parts.media_item_id = media.id
        WHERE metadata.library_section_id IN ({placeholders})
          AND metadata.metadata_type = 1
        ORDER BY metadata.library_section_id, metadata.id, media.id, parts.id
        """,
        library_ids,
    ).fetchall()

    movies = {}
    for row in rows:
        library_id = int(row["library_section_id"])
        metadata_id = int(row["metadata_id"])
        key = (library_id, metadata_id)
        group = movies.setdefault(
            key,
            {
                "library": library_by_id[library_id],
                "metadata_id": metadata_id,
                "title": str(row["title"] or f"metadata {metadata_id}"),
                "year": int(row["year"]) if row["year"] is not None else None,
                "versions": {},
            },
        )

        media_id = int(row["media_id"])
        version = group["versions"].setdefault(
            media_id,
            {
                "media_id": media_id,
                "files": [],
            },
        )
        path = apply_path_maps(row["file"], path_maps)
        if path not in version["files"]:
            version["files"].append(path)

    groups = [
        group
        for group in movies.values()
        if len(group["versions"]) > 1
    ]
    groups.sort(
        key=lambda group: (
            group["library"].name.casefold(),
            group["title"].casefold(),
            group["year"] if group["year"] is not None else -1,
            group["metadata_id"],
        )
    )
    return groups


def human_size(value: int | None) -> str:
    if value is None:
        return "?"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} TiB"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def probe_media_file(ffprobe: str, path: Path):
    """Read technical media metadata with ffprobe without modifying the file."""
    if not path.exists():
        return {"path": path, "error": "file does not exist"}
    if not path.is_file():
        return {"path": path, "error": "path is not a regular file"}
    if path.is_symlink():
        return {"path": path, "error": "refusing to probe symlink"}

    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"path": path, "error": str(exc)}

    if completed.returncode != 0:
        message = completed.stderr.strip() or f"ffprobe exited {completed.returncode}"
        return {"path": path, "error": message}

    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"path": path, "error": f"invalid ffprobe JSON: {exc}"}

    format_info = data.get("format") or {}
    streams = data.get("streams") or []
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitle_streams = [
        stream for stream in streams if stream.get("codec_type") == "subtitle"
    ]

    duration = None
    raw_duration = format_info.get("duration")
    if raw_duration not in (None, "", "N/A"):
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            pass

    try:
        size = int(format_info.get("size"))
    except (TypeError, ValueError):
        try:
            size = path.stat().st_size
        except OSError:
            size = None

    video = None
    if video_streams:
        stream = video_streams[0]
        pix_fmt = str(stream.get("pix_fmt") or "")
        bit_depth = None
        for key in ("bits_per_raw_sample", "bits_per_sample"):
            raw = stream.get(key)
            if raw not in (None, "", "N/A", "0"):
                try:
                    bit_depth = int(raw)
                    break
                except (TypeError, ValueError):
                    pass
        if bit_depth is None:
            match = re.search(r"p(10|12|16)(?:le|be)?$", pix_fmt.casefold())
            if match:
                bit_depth = int(match.group(1))
            elif pix_fmt:
                bit_depth = 8

        transfer = str(stream.get("color_transfer") or "").casefold()
        side_data = json.dumps(stream.get("side_data_list") or []).casefold()
        if "dovi" in side_data or "dolby vision" in side_data:
            hdr = "Dolby Vision"
        elif transfer == "smpte2084":
            hdr = "HDR/PQ"
        elif transfer == "arib-std-b67":
            hdr = "HLG"
        else:
            hdr = "SDR/unknown"

        raw_video_bitrate = stream.get("bit_rate")
        try:
            video_bitrate = (
                int(raw_video_bitrate)
                if raw_video_bitrate not in (None, "", "N/A")
                else None
            )
        except (TypeError, ValueError):
            video_bitrate = None

        video = {
            "codec": str(stream.get("codec_name") or "?"),
            "profile": str(stream.get("profile") or ""),
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
            "pix_fmt": pix_fmt,
            "bit_depth": bit_depth,
            "hdr": hdr,
            "bitrate": video_bitrate,
        }

    audio = []
    for stream in audio_streams:
        tags = stream.get("tags") or {}
        raw_audio_bitrate = stream.get("bit_rate")
        try:
            audio_bitrate = (
                int(raw_audio_bitrate)
                if raw_audio_bitrate not in (None, "", "N/A")
                else None
            )
        except (TypeError, ValueError):
            audio_bitrate = None
        audio.append(
            {
                "language": str(tags.get("language") or "und").casefold(),
                "codec": str(stream.get("codec_name") or "?").casefold(),
                "channels": int(stream.get("channels") or 0),
                "layout": str(stream.get("channel_layout") or ""),
                "title": str(tags.get("title") or ""),
                "bitrate": audio_bitrate,
            }
        )

    subtitles = []
    for stream in subtitle_streams:
        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}
        subtitles.append(
            {
                "language": str(tags.get("language") or "und").casefold(),
                "codec": str(stream.get("codec_name") or "?").casefold(),
                "forced": bool(disposition.get("forced")),
                "hearing_impaired": bool(disposition.get("hearing_impaired")),
                "title": str(tags.get("title") or ""),
            }
        )

    return {
        "path": path,
        "error": None,
        "size": size,
        "duration": duration,
        "video": video,
        "audio": audio,
        "subtitles": subtitles,
    }


def summarize_media_version(probe_binary: str, version, probe_backend: str):
    if probe_backend == "ffprobe":
        probes = [probe_media_file(probe_binary, path) for path in version["files"]]
    else:
        probes = [
            probe_media_file_ffmpeg(probe_binary, path)
            for path in version["files"]
        ]
    errors = [probe["error"] for probe in probes if probe.get("error")]
    valid = [probe for probe in probes if not probe.get("error")]

    total_size = None
    if valid and all(probe.get("size") is not None for probe in valid):
        total_size = sum(int(probe["size"]) for probe in valid)

    duration = None
    if valid and len(valid) == len(probes) and all(
        probe.get("duration") is not None for probe in valid
    ):
        duration = sum(float(probe["duration"]) for probe in valid)

    effective_bitrate = None
    if total_size is not None and duration and duration > 0:
        effective_bitrate = (total_size * 8.0) / duration

    videos = [probe.get("video") for probe in valid if probe.get("video")]
    video = videos[0] if videos else None
    mixed_video = any(item != video for item in videos[1:]) if video else False

    audio_signatures = {
        (
            stream["language"],
            stream["codec"],
            stream["channels"],
        )
        for probe in valid
        for stream in probe.get("audio", [])
    }
    audio_coverage_signatures = {
        (
            stream["language"],
            stream["channels"],
        )
        for probe in valid
        for stream in probe.get("audio", [])
    }
    subtitle_signatures = {
        (
            stream["language"],
            stream["codec"],
            stream["forced"],
            stream["hearing_impaired"],
        )
        for probe in valid
        for stream in probe.get("subtitles", [])
    }

    return {
        "media_id": version["media_id"],
        "files": version["files"],
        "probes": probes,
        "errors": errors,
        "multipart": len(version["files"]) > 1,
        "size": total_size,
        "duration": duration,
        "effective_bitrate": effective_bitrate,
        "video": video,
        "mixed_video": mixed_video,
        "audio_signatures": audio_signatures,
        "audio_coverage_signatures": audio_coverage_signatures,
        "subtitle_signatures": subtitle_signatures,
    }


def duration_clusters(versions):
    """Group versions whose total durations are within a conservative tolerance."""
    known = sorted(
        [version for version in versions if version["duration"] is not None],
        key=lambda version: version["duration"],
    )
    unknown = [version for version in versions if version["duration"] is None]

    clusters = []
    for version in known:
        placed = False
        for cluster in clusters:
            values = [item["duration"] for item in cluster]
            if (
                max(max(values), version["duration"])
                - min(min(values), version["duration"])
                <= DUPLICATE_DURATION_TOLERANCE_SECONDS
            ):
                cluster.append(version)
                placed = True
                break
        if not placed:
            clusters.append([version])

    clusters.extend([[version] for version in unknown])
    return clusters


def version_dominates(better, worse) -> bool:
    """Return True only for a deliberately strict technical dominance case."""
    if better["errors"] or worse["errors"]:
        return False
    if better["multipart"] or worse["multipart"]:
        return False
    if better["mixed_video"] or worse["mixed_video"]:
        return False

    better_video = better["video"]
    worse_video = worse["video"]
    if not better_video or not worse_video:
        return False

    if better_video["codec"] != worse_video["codec"]:
        return False
    if better_video["hdr"] != worse_video["hdr"]:
        return False

    better_pixels = better_video["width"] * better_video["height"]
    worse_pixels = worse_video["width"] * worse_video["height"]
    if not better_pixels or not worse_pixels or better_pixels < worse_pixels:
        return False

    better_depth = better_video["bit_depth"]
    worse_depth = worse_video["bit_depth"]
    if worse_depth is not None and (
        better_depth is None or better_depth < worse_depth
    ):
        return False

    better_rate = better["effective_bitrate"]
    worse_rate = worse["effective_bitrate"]
    if worse_rate is not None and (
        better_rate is None or better_rate < worse_rate * 0.98
    ):
        return False

    if not better["audio_coverage_signatures"].issuperset(
        worse["audio_coverage_signatures"]
    ):
        return False
    if not better["subtitle_signatures"].issuperset(worse["subtitle_signatures"]):
        return False

    strict = (
        better_pixels > worse_pixels
        or (
            better_rate is not None
            and worse_rate is not None
            and better_rate > worse_rate * 1.05
        )
        or (
            better_depth is not None
            and worse_depth is not None
            and better_depth > worse_depth
        )
        or better["audio_coverage_signatures"] > worse["audio_coverage_signatures"]
        or better["subtitle_signatures"] > worse["subtitle_signatures"]
    )
    return strict


def classify_duration_cluster(cluster):
    if len(cluster) == 1:
        version = cluster[0]
        if version["duration"] is None or version["errors"]:
            return {version["media_id"]: "REVIEW"}
        return {version["media_id"]: "DIFFERENT CUT"}

    if any(
        version["duration"] is None
        or version["errors"]
        or version["multipart"]
        or version["mixed_video"]
        for version in cluster
    ):
        return {version["media_id"]: "REVIEW" for version in cluster}

    dominated = set()
    for worse in cluster:
        for better in cluster:
            if better is worse:
                continue
            if version_dominates(better, worse):
                dominated.add(worse["media_id"])
                break

    survivors = [
        version for version in cluster if version["media_id"] not in dominated
    ]
    result = {
        version["media_id"]: "DOMINATED"
        for version in cluster
        if version["media_id"] in dominated
    }

    survivor_label = "BEST CANDIDATE" if len(survivors) == 1 else "TRADE-OFF"
    for version in survivors:
        result[version["media_id"]] = survivor_label
    return result


def describe_video(version) -> str:
    video = version["video"]
    if not video:
        return "video ?"

    resolution = (
        f"{video['width']}x{video['height']}"
        if video["width"] and video["height"]
        else "?"
    )
    depth = f"{video['bit_depth']}-bit" if video["bit_depth"] else "?-bit"
    profile = f" {video['profile']}" if video["profile"] else ""
    return (
        f"{resolution} {video['codec']}{profile} "
        f"{depth} {video['hdr']}"
    )


def describe_audio(version) -> str:
    audio_streams = [
        stream
        for probe in version["probes"]
        if not probe.get("error")
        for stream in probe.get("audio", [])
    ]
    if not audio_streams:
        return "none"

    items = []
    for stream in audio_streams:
        language = stream["language"]
        codec = stream["codec"]
        channels = stream["channels"]
        channel_text = f"{channels}ch" if channels else "?ch"
        bitrate = stream.get("bitrate")
        bitrate_text = (
            f"@{bitrate / 1000:.0f}kbps"
            if bitrate is not None
            else ""
        )
        items.append(f"{language}:{codec}/{channel_text}{bitrate_text}")
    return ", ".join(sorted(items))


def describe_subtitles(version) -> str:
    if not version["subtitle_signatures"]:
        return "none"
    items = []
    for language, codec, forced, hearing_impaired in sorted(
        version["subtitle_signatures"]
    ):
        flags = []
        if forced:
            flags.append("forced")
        if hearing_impaired:
            flags.append("HI")
        suffix = f"/{'/'.join(flags)}" if flags else ""
        items.append(f"{language}:{codec}{suffix}")
    return ", ".join(items)


def configured_executable(value, key: str):
    """Validate an optional private executable path from local config."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"'{key}' in config must be a string")
    path = Path(value)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"Configured {key} is not an executable file: {path}")
    return str(path)


def find_ffprobe():
    """Locate ffprobe in PATH or common package locations."""
    found = shutil.which("ffprobe")
    if found:
        return found

    candidates = [
        Path("/bin/ffprobe"),
        Path("/usr/bin/ffprobe"),
        Path("/usr/local/bin/ffprobe"),
    ]
    candidates.extend(sorted(Path("/var/packages").glob("*/target/bin/ffprobe")))

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def print_duplicate_report(
    conn: sqlite3.Connection,
    libraries: list[Library],
    path_maps: list[tuple[str, str]],
    probe_media: bool,
    ffprobe_path: str | None = None,
    ffmpeg_path: str | None = None,
    tsv_path: Path | None = None,
) -> int:
    groups = duplicate_movie_groups(conn, libraries, path_maps)
    version_count = sum(len(group["versions"]) for group in groups)
    file_count = sum(
        len(version["files"])
        for group in groups
        for version in group["versions"].values()
    )

    probe_backend = None
    probe_binary = None
    if probe_media:
        probe_binary = ffprobe_path or find_ffprobe()
        if probe_binary is not None:
            probe_backend = "ffprobe"
        else:
            probe_binary = ffmpeg_path or shutil.which("ffmpeg")
            if probe_binary is not None:
                probe_backend = "ffmpeg"

        if probe_binary is None:
            print(
                "[FATAL] --probe-media requested but neither ffprobe nor ffmpeg "
                "could be found.",
                file=sys.stderr,
            )
            return 2

    print("PlexLibraryMaintainer M4 duplicate report")
    print("=========================================")
    print("Mode                : READ ONLY")
    print(f"Duplicate movies    : {len(groups)}")
    print(f"Media versions      : {version_count}")
    print(f"Media files         : {file_count}")
    print(f"Technical probe     : {probe_backend if probe_media else 'disabled'}")
    if probe_media:
        print(
            f"Duration tolerance  : {DUPLICATE_DURATION_TOLERANCE_SECONDS:.0f}s "
            "per same-cut cluster"
        )
    print()

    if not groups:
        print("No Plex movie items with multiple media versions were found.")
        return 0

    probe_errors = 0
    probe_error_details: list[str] = []
    tsv_rows: list[dict[str, object]] = []
    detailed_console = tsv_path is None
    processed_versions = 0

    for group_index, group in enumerate(groups, start=1):
        title = display_title_year(group["title"], group["year"])
        versions = [
            group["versions"][media_id]
            for media_id in sorted(group["versions"])
        ]
        if detailed_console:
            print(f"[DUPLICATE] [{group['library'].name}] {title}")
            print(
                f"  Plex metadata id {group['metadata_id']} | "
                f"{len(versions)} media versions"
            )

        if not probe_media:
            for index, version in enumerate(versions, start=1):
                sizes = []
                for path in version["files"]:
                    try:
                        sizes.append(path.stat().st_size if path.is_file() else None)
                    except OSError:
                        sizes.append(None)
                total_size = (
                    sum(size for size in sizes if size is not None)
                    if any(size is not None for size in sizes)
                    else None
                )
                tsv_rows.append(
                    {
                        "library": group["library"].name,
                        "title": group["title"],
                        "year": group["year"] or "",
                        "metadata_id": group["metadata_id"],
                        "version_count": len(versions),
                        "media_id": version["media_id"],
                        "assessment": "",
                        "cut_cluster": "",
                        "cut_class": "",
                        "files": " | ".join(str(path) for path in version["files"]),
                        "file_count": len(version["files"]),
                        "size_bytes": total_size if total_size is not None else "",
                        "size": human_size(total_size),
                        "duration_seconds": "",
                        "duration": "",
                        "bitrate_mbps": "",
                        "resolution": "",
                        "video_codec": "",
                        "video_profile": "",
                        "bit_depth": "",
                        "hdr": "",
                        "audio": "",
                        "subtitles": "",
                        "multipart": "",
                        "mixed_video": "",
                        "probe_errors": "",
                    }
                )
                if detailed_console:
                    print(f"  {index}. media id {version['media_id']}")
                    for path, size in zip(version["files"], sizes):
                        print(f"     {path}")
                        print(f"       size: {human_size(size)}")
            if detailed_console:
                print()
            processed_versions += len(versions)
            if tsv_path is not None:
                print(
                    f"Progress            : {group_index}/{len(groups)} movies | "
                    f"{processed_versions}/{version_count} versions",
                    end="\r",
                    flush=True,
                )
            continue

        summaries = [
            summarize_media_version(probe_binary, version, probe_backend)
            for version in versions
        ]
        clusters = duration_clusters(summaries)
        labels = {}
        cluster_by_media_id = {}
        for cluster_index, cluster in enumerate(clusters, start=1):
            cluster_labels = classify_duration_cluster(cluster)
            labels.update(cluster_labels)
            for version in cluster:
                cluster_by_media_id[version["media_id"]] = (
                    cluster_index,
                    "SAME CUT" if len(cluster) > 1 else (
                        "REVIEW" if version["duration"] is None else "DIFFERENT CUT"
                    ),
                )

        for index, version in enumerate(summaries, start=1):
            cluster_index, cluster_label = cluster_by_media_id[version["media_id"]]
            assessment = labels[version["media_id"]]
            video = version["video"] or {}
            width = video.get("width")
            height = video.get("height")
            resolution = f"{width}x{height}" if width and height else ""
            bitrate_mbps = (
                version["effective_bitrate"] / 1_000_000
                if version["effective_bitrate"] is not None
                else ""
            )
            tsv_rows.append(
                {
                    "library": group["library"].name,
                    "title": group["title"],
                    "year": group["year"] or "",
                    "metadata_id": group["metadata_id"],
                    "version_count": len(versions),
                    "media_id": version["media_id"],
                    "assessment": assessment,
                    "cut_cluster": cluster_index,
                    "cut_class": cluster_label,
                    "files": " | ".join(str(path) for path in version["files"]),
                    "file_count": len(version["files"]),
                    "size_bytes": version["size"] if version["size"] is not None else "",
                    "size": human_size(version["size"]),
                    "duration_seconds": (
                        f"{version['duration']:.3f}"
                        if version["duration"] is not None
                        else ""
                    ),
                    "duration": format_duration(version["duration"]),
                    "bitrate_mbps": (
                        f"{bitrate_mbps:.3f}" if bitrate_mbps != "" else ""
                    ),
                    "video_bitrate_mbps": (
                        f"{video.get('bitrate') / 1_000_000:.3f}"
                        if video.get("bitrate") is not None
                        else ""
                    ),
                    "resolution": resolution,
                    "video_codec": video.get("codec") or "",
                    "video_profile": video.get("profile") or "",
                    "bit_depth": video.get("bit_depth") or "",
                    "hdr": video.get("hdr") or "",
                    "audio": describe_audio(version),
                    "subtitles": describe_subtitles(version),
                    "multipart": "yes" if version["multipart"] else "no",
                    "mixed_video": "yes" if version["mixed_video"] else "no",
                    "probe_errors": " | ".join(version["errors"]),
                }
            )

            if detailed_console:
                print(
                    f"  {index}. [{assessment}] media id {version['media_id']} "
                    f"| cluster {cluster_index} [{cluster_label}]"
                )
                for path in version["files"]:
                    print(f"     {path}")
                print(
                    f"       size: {human_size(version['size'])} | "
                    f"duration: {format_duration(version['duration'])} | "
                    f"bitrate: "
                    + (
                        f"{version['effective_bitrate'] / 1_000_000:.2f} Mbps"
                        if version["effective_bitrate"] is not None
                        else "?"
                    )
                )
                print(f"       video: {describe_video(version)}")
                print(f"       audio: {describe_audio(version)}")
                print(f"       subs : {describe_subtitles(version)}")
                if version["multipart"]:
                    print(f"       note : MULTIPART ({len(version['files'])} files); auto-ranking disabled")
                if version["mixed_video"]:
                    print("       note : mixed video characteristics across parts; auto-ranking disabled")
            for error in version["errors"]:
                probe_errors += 1
                probe_error_details.append(
                    f"{title} | media id {version['media_id']} | {error}"
                )
                if detailed_console:
                    print(f"       [PROBE ERROR] {error}")

        processed_versions += len(versions)
        if tsv_path is not None:
            print(
                f"Progress            : {group_index}/{len(groups)} movies | "
                f"{processed_versions}/{version_count} versions",
                end="\r",
                flush=True,
            )

        if detailed_console:
            print("  Group assessment:")
            for cluster_index, cluster in enumerate(clusters, start=1):
                if len(cluster) > 1:
                    durations = [version["duration"] for version in cluster]
                    spread = max(durations) - min(durations)
                    print(
                        f"    cluster {cluster_index}: SAME CUT by duration "
                        f"({len(cluster)} versions, spread {spread:.2f}s)"
                    )
                else:
                    version = cluster[0]
                    if version["duration"] is None:
                        print(f"    cluster {cluster_index}: REVIEW (duration unavailable)")
                    else:
                        print(
                            f"    cluster {cluster_index}: DIFFERENT CUT candidate "
                            f"({format_duration(version['duration'])})"
                        )
            print()

    if tsv_path is not None:
        print()
        fieldnames = [
            "library", "title", "year", "metadata_id", "version_count",
            "media_id", "assessment", "cut_cluster", "cut_class", "files",
            "file_count", "size_bytes", "size", "duration_seconds", "duration",
            "bitrate_mbps", "video_bitrate_mbps", "resolution", "video_codec", "video_profile",
            "bit_depth", "hdr", "audio", "subtitles", "multipart",
            "mixed_video", "probe_errors",
        ]
        tsv_path.parent.mkdir(parents=True, exist_ok=True)
        with tsv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=fieldnames,
                delimiter="\t",
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(tsv_rows)

    print("M4 duplicate summary")
    print("====================")
    print(f"Duplicate movies    : {len(groups)}")
    print(f"Media versions      : {version_count}")
    print(f"Technical probe     : {probe_backend if probe_media else 'disabled'}")
    if probe_media:
        print(f"Probe binary        : {probe_binary}")
    print(f"Probe errors        : {probe_errors}")
    if tsv_path is not None:
        print(f"TSV report          : {tsv_path}")
        print(f"TSV rows            : {len(tsv_rows)}")
    if probe_error_details:
        print()
        print("Probe error details")
        print("===================")
        for error in probe_error_details:
            print(f"- {error}")
    return 1 if probe_errors else 0



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
        "--plan-collisions",
        action="store_true",
        help=(
            "M3b diagnostic: produce exact conservative merge move plans. "
            "This mode never executes those moves and cannot be combined with --write."
        ),
    )
    parser.add_argument(
        "--merge-collision",
        metavar="CANONICAL_FOLDER",
        help=(
            "M3c: execute exactly one collision group selected by canonical folder "
            "name or full path. Requires --write; M1/M2 writes are disabled."
        ),
    )
    parser.add_argument(
        "--merge-ready-collisions",
        action="store_true",
        help=(
            "M3 batch mode: preflight every collision and select only groups that pass "
            "all M3 write guardrails. Dry-run by default; add --write to execute them. "
            "Rejected groups are skipped and M1/M2 writes are disabled."
        ),
    )
    parser.add_argument(
        "--accept-title-mismatch",
        action="store_true",
        help=(
            "For one explicit --merge-collision only, accept a title/language mismatch "
            "that the automatic identity guardrail rejects. Explicit source/Plex year "
            "conflicts remain blocked."
        ),
    )
    parser.add_argument(
        "--report",
        choices=("duplicates",),
        help=(
            "Read-only M4 report. 'duplicates' lists Plex movie items that contain "
            "multiple media versions."
        ),
    )
    parser.add_argument(
        "--probe-media",
        action="store_true",
        help=(
            "With --report duplicates, inspect each duplicate version with ffprobe "
            "or fall back to ffmpeg, then classify duration clusters and conservative "
            "technical dominance."
        ),
    )
    parser.add_argument(
        "--tsv",
        type=Path,
        metavar="FILE",
        help=(
            "With --report duplicates, write one Google-Sheets-friendly TSV row per "
            "media version. Detailed per-movie console output is suppressed."
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually apply the selected write operation. Without this flag nothing is changed.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if (args.analyze_collisions or args.plan_collisions) and args.write:
        print(
            "[FATAL] M3 diagnostic modes cannot be combined with --write.",
            file=sys.stderr,
        )
        return 2

    if args.merge_collision and args.merge_ready_collisions:
        print(
            "[FATAL] --merge-collision and --merge-ready-collisions are mutually exclusive.",
            file=sys.stderr,
        )
        return 2

    if args.accept_title_mismatch and not args.merge_collision:
        print(
            "[FATAL] --accept-title-mismatch requires one explicit --merge-collision.",
            file=sys.stderr,
        )
        return 2

    if (args.merge_collision or args.merge_ready_collisions) and (
        args.analyze_collisions or args.plan_collisions
    ):
        print(
            "[FATAL] M3 merge modes cannot be combined with M3 diagnostic modes.",
            file=sys.stderr,
        )
        return 2

    if args.merge_collision and not args.write:
        print("[FATAL] --merge-collision requires --write.", file=sys.stderr)
        return 2

    if args.report and args.write:
        print("[FATAL] M4 reports are read-only and cannot use --write.", file=sys.stderr)
        return 2

    if args.probe_media and args.report != "duplicates":
        print(
            "[FATAL] --probe-media requires --report duplicates.",
            file=sys.stderr,
        )
        return 2

    if args.tsv is not None and args.report != "duplicates":
        print(
            "[FATAL] --tsv requires --report duplicates.",
            file=sys.stderr,
        )
        return 2

    if args.report and (
        args.analyze_collisions
        or args.plan_collisions
        or args.merge_collision
        or args.merge_ready_collisions
        or args.accept_title_mismatch
    ):
        print(
            "[FATAL] M4 reports cannot be combined with M3 modes.",
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
        ffprobe_path = configured_executable(config.get("ffprobe_path"), "ffprobe_path")
        ffmpeg_path = configured_executable(config.get("ffmpeg_path"), "ffmpeg_path")

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

        if args.report == "duplicates":
            return print_duplicate_report(
                conn,
                libraries,
                path_maps,
                probe_media=args.probe_media,
                ffprobe_path=ffprobe_path,
                ffmpeg_path=ffmpeg_path,
                tsv_path=args.tsv,
            )

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

    if args.merge_collision:
        try:
            execution = build_collision_execution_plan(
                plans,
                args.merge_collision,
                accept_title_mismatch=args.accept_title_mismatch,
            )
        except (OSError, ValueError) as exc:
            print(f"[FATAL] M3c preflight refused the operation: {exc}", file=sys.stderr)
            return 2

        print_collision_execution_plan(execution)
        return execute_collision_execution_plan(execution)

    if args.merge_ready_collisions:
        ready, skipped = build_ready_collision_executions(plans)
        print_batch_collision_preflight(ready, skipped, args.write)
        if not args.write:
            print()
            print("DRY RUN ONLY. Nothing was changed. Add --write to execute ready M3 groups.")
            return 0

        if not ready:
            print()
            print("No collision groups passed the M3 write preflight. Nothing was changed.")
            return 0

        return execute_ready_collision_batch(ready)

    actionable, validation_review, collision_reports, already_normalized = validate_plans(plans)
    m3_analysis_reports = analyze_collision_plans(plans) if args.analyze_collisions else []
    if args.plan_collisions:
        m3_plan_reports, m3_planned_moves, m3_blocked_sources = plan_collision_merges(plans)
    else:
        m3_plan_reports, m3_planned_moves, m3_blocked_sources = [], 0, 0
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

    for report in m3_plan_reports:
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
    print(f"M3 planned moves    : {m3_planned_moves}")
    print(f"M3 blocked sources  : {m3_blocked_sources}")
    print(f"Errors              : {errors}")
    if rename_log_path is not None:
        print(f"Rename audit log    : {rename_log_path}")

    if not args.write:
        print("\nDRY RUN ONLY. Nothing was changed. Add --write to apply folder renames and root-file moves.")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
