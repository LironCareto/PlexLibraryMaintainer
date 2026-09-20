# PlexLibraryMaintainer

Conservative maintenance tools for Plex libraries.

The first tool in this repository normalizes **movie folder names** from Plex's own metadata. Instead of trying to reverse release-style names with fuzzy rules, it asks Plex what the movie is and uses the canonical title and year that Plex already knows.

Example:

```text
Before:
A.Hologram.for.the.King.2016.BRRip.XviD.AC3-EVO/
└── A.Hologram.for.the.King.2016.BRRip.XviD.AC3-EVO.avi

After:
A Hologram for the King (2016)/
└── A.Hologram.for.the.King.2016.BRRip.XviD.AC3-EVO.avi
```

**Only the containing folder is renamed. Files inside it keep their original names.**

## Safety model

PlexLibraryMaintainer is deliberately conservative:

- Plex's SQLite database is opened with `mode=ro`.
- `PRAGMA query_only = ON` adds a second read-only safeguard.
- Dry-run is the default.
- No folder is renamed unless `--write` is explicitly supplied.
- Only the **first directory immediately below a selected library root** is ever considered for renaming.
- Files and nested folders inside movie folders are never renamed or moved.
- Library roots are never renamed.
- Destination folders are never merged or overwritten.
- Ambiguous folders are skipped and reported for review.
- Only Plex libraries of type **movie** are eligible for folder normalization.
- Machine-specific paths and library choices can live in local `config.json`, which is ignored by Git.

The script changes the filesystem only when `--write` is used. It never writes to Plex's databases.

## Requirements

- Python 3.8+
- No third-party Python packages

## Configuration

Copy the example:

```bash
cp config.example.json config.json
```

On PowerShell:

```powershell
Copy-Item config.example.json config.json
```

Then edit the local file:

```json
{
  "database_folder": "/path/to/Plex Media Server/Plug-in Support/Databases",
  "libraries": [
    "Movies"
  ],
  "path_maps": [
    "/plex/media=/local/media"
  ]
}
```

`config.json` is ignored by Git and should remain local.

Library entries may be exact Plex library names or numeric library IDs. Command-line `--library` values override the configured list.

If Plex and the script see the same filesystem paths, `path_maps` can be an empty array.

## Usage

### List Plex libraries

```bash
python3 plex_library_maintainer.py --list-libraries
```

Example:

```text
ID   Type          Name
1    movie         Movies
2    unsupported   TV Shows
3    movie         Documentaries
```

### Dry-run

With libraries configured in `config.json`:

```bash
python3 plex_library_maintainer.py
```

Or select libraries explicitly:

```bash
python3 plex_library_maintainer.py \
  --library "Movies" \
  --library "Documentaries"
```

A dry-run prints every proposed rename but changes nothing.

### M1 folder-selection rule

M1 deliberately operates only on an existing top-level movie folder: the first
directory immediately below the Plex library root.

For example, if Plex reports:

```text
/library/Movies/Foo.Release/CD1/foo.avi
/library/Movies/Foo.Release/CD2/foo.avi
```

both media files map to the single source folder:

```text
/library/Movies/Foo.Release/
```

Only that folder may be renamed. `CD1`, `CD2`, the video files, subtitles, and
anything else below it are left untouched.

If a movie file is directly in the library root, M1 reports it as
`[NO FOLDER]` and skips it. Creating or selecting a destination folder for such
files is intentionally deferred to a later milestone.

### Apply the renames

Only after reviewing the dry-run:

```bash
python3 plex_library_maintainer.py --write
```

The only filesystem operation performed in M1 is renaming the movie's first directory immediately below the selected library root from its current name to:

```text
<Plex title> (<Plex year>)
```

For example:

```text
The.Matrix.1999.1080p.BluRay.x265/
```

becomes:

```text
The Matrix (1999)/
```

while files inside remain untouched.

### Override the database location

```bash
python3 plex_library_maintainer.py \
  --database-folder "/path/to/Databases"
```

### Override path mappings

```bash
python3 plex_library_maintainer.py \
  --path-map "/plex/media=/local/media"
```

Command-line path mappings replace mappings from `config.json`.

## What is skipped

The tool refuses to guess when a rename is not clearly safe. Examples include:

- Plex metadata without a title or year.
- A selected library that is not a movie library.
- A movie file stored directly in the library root (reported as `[NO FOLDER]` in M1).
- A media path that is not contained by any configured root for its Plex library.
- A missing or inaccessible source folder.
- A symlinked movie folder.
- One source folder associated with more than one Plex title/year.
- Two source folders that would normalize to the same destination.
- A destination folder that already exists.

These cases are reported instead of being modified.

For M1 reporting, these categories are kept separate:

- `[NO FOLDER]`: a movie file is directly in the library root. This is a known
  structural case, not a generic review error.
- `[REVIEW]`: something is genuinely ambiguous or unsafe and needs inspection.
- `[COLLISION]`: two or more source folders want the same canonical target, or
  a target folder already exists. Collisions are reported once per target and
  list every source folder involved. M1 never chooses a winner or merges them.

The summary therefore reports `No folder`, `Needs review`, and
`Collision groups` independently.

## Plex after a rename

Renaming a movie folder changes its filesystem path. Plex may temporarily show the old path as unavailable until it detects or scans the changed files. Run a normal Plex library scan after applying folder renames.

## What this does not do

- It does not rename movie files.
- It does not rename subtitle files.
- It does not move files between folders.
- It does not merge folders.
- It does not use fuzzy title parsing.
- It does not call TMDB, IMDb, or any external metadata service.
- It does not modify Plex metadata or Plex databases.
- It does not normalize TV show or music libraries.

## License

MIT. See [LICENSE](LICENSE).

---

Plex is a trademark of Plex, Inc. This project is not affiliated with or endorsed by Plex.
