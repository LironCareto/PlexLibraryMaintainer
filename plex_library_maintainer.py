#!/usr/bin/env python3
"""Normalize Plex movie folder names using Plex metadata.

Plex's database is always opened read-only. Dry-run is the default. The only
filesystem mutation performed with --write is renaming the first movie folder
immediately below a selected library root. Files and nested folders inside it
are never renamed or moved individually.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

LIBRARY_DB = "com.plexapp.plugins.library.db"
DEFAULT_CONFIG = Path("config.json")


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
    year: int
    library_id: int
    library_name: str


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


def sanitize_component(value: str) -> str:
    """Make a title safe as one filesystem path component without over-normalizing."""
    value = re.sub(r"[\x00-\x1f]", " ", value)
    value = value.replace("/", "⁄").replace("\\", "⧵")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def canonical_folder_name(title: str, year: int) -> str:
    return f"{sanitize_component(title)} ({year})"


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
) -> tuple[list[FolderPlan], list[str], int]:
    library_by_id = {library.id: library for library in libraries}
    roots = library_root_paths(conn, list(library_by_id), path_maps)

    folder_metadata: dict[Path, set[tuple[str, int, int]]] = defaultdict(set)
    skipped = 0
    review: list[str] = []

    for row in movie_rows(conn, list(library_by_id)):
        title = row["title"]
        year = row["year"]
        if not title or not year:
            skipped += 1
            review.append(
                f"[REVIEW] metadata id {row['metadata_id']}: missing title or year"
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

        if source is None:
            skipped += 1
            review.append(
                f"[NO FOLDER] {file_path}: movie file is directly in the library "
                "root; skipped in M1"
            )
            continue

        folder_metadata[source].add((str(title), int(year), library_id))

    plans: list[FolderPlan] = []

    for source, metadata_set in folder_metadata.items():
        if len(metadata_set) != 1:
            skipped += 1
            values = ", ".join(
                f"{title} ({year})" for title, year, _ in sorted(metadata_set)
            )
            review.append(
                f"[REVIEW] {source}: multiple Plex identities share this folder: {values}"
            )
            continue

        title, year, library_id = next(iter(metadata_set))
        target = source.with_name(canonical_folder_name(title, year))
        library = library_by_id[library_id]
        plans.append(
            FolderPlan(
                source=source,
                target=target,
                title=title,
                year=year,
                library_id=library_id,
                library_name=library.name,
            )
        )

    return plans, review, skipped


def validate_plans(
    plans: list[FolderPlan],
) -> tuple[list[FolderPlan], list[str], int, int]:
    actionable: list[FolderPlan] = []
    review: list[str] = []
    already_normalized = 0
    collisions = 0

    destination_sources: dict[Path, list[Path]] = defaultdict(list)
    for plan in plans:
        destination_sources[plan.target].append(plan.source)

    for plan in plans:
        if plan.source == plan.target:
            already_normalized += 1
            continue

        if len(destination_sources[plan.target]) > 1:
            collisions += 1
            review.append(
                f"[COLLISION] multiple source folders target {plan.target}"
            )
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
            collisions += 1
            review.append(
                f"[COLLISION] destination already exists: {plan.target}"
            )
            continue

        actionable.append(plan)

    return actionable, review, already_normalized, collisions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Normalize top-level movie folder names from Plex metadata. "
            "Dry-run is the default; files and nested folders are never renamed."
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
        "--write",
        action="store_true",
        help="Actually rename folders. Without this flag nothing is changed.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

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

        plans, build_review, build_skipped = build_plans(
            conn,
            libraries,
            path_maps,
        )
    except sqlite3.Error as exc:
        print(f"[FATAL] Plex database query failed: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    actionable, validation_review, already_normalized, collisions = validate_plans(plans)
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

    for line in review:
        print(line)

    renamed = 0
    errors = 0

    if args.write:
        # Deepest paths first, so a parent rename cannot invalidate a child source path.
        for plan in sorted(
            actionable,
            key=lambda item: len(item.source.parts),
            reverse=True,
        ):
            try:
                os.rename(plan.source, plan.target)
                renamed += 1
                print(f"[RENAMED] {plan.source} -> {plan.target}")
            except OSError as exc:
                errors += 1
                print(
                    f"[ERROR] Could not rename {plan.source} -> {plan.target}: {exc}",
                    file=sys.stderr,
                )

    print()
    print("Summary")
    print("=======")
    print(f"Folders examined    : {len(plans) + build_skipped}")
    print(f"Already normalized  : {already_normalized}")
    print(f"Would rename        : {len(actionable) if not args.write else 0}")
    print(f"Renamed             : {renamed}")
    print(f"Needs review        : {len(review)}")
    print(f"Collisions          : {collisions}")
    print(f"Errors              : {errors}")

    if not args.write:
        print("\nDRY RUN ONLY. Nothing was renamed. Add --write to apply folder renames.")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
