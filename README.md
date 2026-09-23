# PlexLibraryMaintainer

Conservative maintenance tools for Plex libraries.

The first tool in this repository normalizes **movie folder names** from Plex's own metadata. Instead of trying to reverse release-style names with fuzzy rules, it asks Plex what the movie is and uses the canonical title and year that Plex already knows.

Example:

```text
Before:
Example Movie/
└── video.mkv

After:
Example Movie (2016)/
└── video.mkv
```

Existing movie folders are normalized in place. Plex-indexed movie files found directly in a library root can also be placed into their canonical movie folder. Movie filenames themselves are preserved unless a filename collision requires a numbered suffix.

## Safety model

PlexLibraryMaintainer is deliberately conservative:

- Plex's SQLite database is opened with `mode=ro`.
- `PRAGMA query_only = ON` adds a second read-only safeguard.
- Dry-run is the default.
- No folder is renamed unless `--write` is explicitly supplied.
- Only the **first directory immediately below a selected library root** is ever considered for renaming.
- Files and nested folders inside existing movie folders are never renamed or moved by M1.
- Library roots are never renamed.
- M2 can move a Plex-indexed movie file that lives directly in the library root into its canonical movie folder.
- If that canonical folder already exists, M2 reuses it.
- Existing files are never overwritten. If the same filename already exists in the target folder, M2 uses the first free numbered name such as `video (1).mkv`, then `video (2).mkv`.
- Destination folders are never merged by the M1 folder-renaming operation.
- A trailing Plex-style `{edition-...}` marker already present in the source
  folder is preserved literally in the target name.
- M1 applies only two explicit title substitutions: `:` → `;` and `?` → `¿`.
- Other unsafe DSM/SMB target names are skipped rather than guessed or rewritten.
- Proposed renames with an extremely weak textual relationship between the current
  folder name and Plex title are reported as `[SUSPICIOUS]` and skipped.
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
/library/Movies/Example Movie/CD1/video-part1.mkv
/library/Movies/Example Movie/CD2/video-part2.mkv
```

both media files map to the single source folder:

```text
/library/Movies/Example Movie/
```

Only that folder may be renamed. `CD1`, `CD2`, the video files, subtitles, and
anything else below it are left untouched.

Movie files that Plex indexes directly in the library root are handled separately by M2. M1 still never treats the library root itself as a movie folder.

### Apply changes

Only after reviewing the dry-run:

```bash
python3 plex_library_maintainer.py --write
```

This applies both safe M1 folder renames and safe M2 root-file moves.

All write runs append to one mandatory audit history:

```text
logs/renames.log
```

The file is never truncated or replaced by the script. It is JSON Lines, so the
complete rename history can be searched in one place. Each write run adds a
`START` record with a `run_id`, and every rename records its own timestamp,
the same `run_id`, original source path, resulting target path, library, title,
and year. Failed rename attempts are recorded as `ERROR` entries.

Each entry is flushed immediately, so the history remains useful even if a run
is interrupted. If the audit log cannot be opened for append, the script refuses
to perform any rename. The `*.log` pattern is ignored by Git, so this local
history is not committed.

The filesystem operation performed in M1 is renaming the movie's first directory immediately below the selected library root from its current name to:

```text
<Plex title> (<Plex year>)
```

Before building that folder name, M1 applies only these explicit conventions:

```text
:  -> ;
?  -> ¿
```

For example, `Example: Subtitle (2016)` becomes
`Example; Subtitle (2016)`. M1 does not invent substitutions for other
problematic characters.

Existing trailing edition markers are part of the folder identity and are kept
exactly as written:

```text
Example Movie (2016) {edition-Director's Cut}
-> Example Movie (2016) {edition-Director's Cut}

Example Movie - old folder (2016) {edition-Special Edition}
-> Example Movie (2016) {edition-Special Edition}
```

M1 does not infer edition names from Plex metadata. It only preserves a
well-formed trailing `{edition-...}` marker that already exists. A malformed or
non-trailing edition marker is reported as `[REVIEW]` and skipped.

For example:

```text
Example Movie - old folder/
```

becomes:

```text
Example Movie (2016)/
```

while files inside remain untouched.

### M2 root-file organization

When Plex indexes a movie file that is directly in a selected movie library root, M2 plans a move into the same canonical folder format used by M1:

```text
/library/Movies/video.mkv
-> /library/Movies/Example Movie (2016)/video.mkv
```

If the canonical folder already exists, it is reused rather than treated as a collision.

If the target folder already contains a file with the same name, M2 never overwrites it. The moved file receives the first free numbered suffix before the extension:

```text
video.mkv
video (1).mkv
video (2).mkv
```

M2 moves only the Plex-indexed movie file itself. It does not guess which unrelated files in the library root might be sidecars. Unsafe or suspicious metadata cases are skipped for review, just as with M1.

Dry-run output uses `[MOVE]`; successful write-mode operations are recorded as `MOVED` in the same cumulative audit history.

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
- A media path that is not contained by any configured root for its Plex library.
- A missing or inaccessible source folder.
- A symlinked movie folder.
- A malformed or non-trailing `{edition-...}` marker that M1 cannot preserve
  without interpretation.
- One source folder associated with more than one Plex title/year.
- Two source folders that would normalize to the same destination.
- A destination folder that already exists.
- A target name that remains unsafe after the explicit `:` → `;` and
  `?` → `¿` substitutions. M1 conservatively rejects remaining path
  separators/reserved characters, control characters, DSM-reserved `._`
  prefixes, and unsafe trailing space/dot cases.
- A proposed rename whose current folder name and Plex title have no whole-title
  containment, no shared meaningful token, and low normalized character
  similarity. This check is only a safety veto: it never guesses or changes a
  title.

These cases are reported instead of being modified.

For reporting, these categories are kept separate:

- `[MOVE]`: M2 proposes moving a Plex-indexed movie file from the library root
  into its canonical movie folder.
- `[UNSAFE NAME]`: Plex's title would produce a target that M1 refuses to
  create after the two explicit title substitutions.
- `[SUSPICIOUS]`: Plex metadata may be correct, translated, transliterated, or
  simply wrong, but the current folder and Plex title are too dissimilar for M1
  to rename automatically.
- `[REVIEW]`: something is genuinely ambiguous and needs inspection.
- `[COLLISION]`: two or more source folders want the same canonical target, or
  a target folder already exists. Collisions are reported once per target and
  list every source folder involved. M1 never chooses a winner or merges them.

The summary reports planned and completed folder renames and root-file moves separately, alongside `Unsafe names`, `Suspicious matches`, `Needs review`, and `Collision groups`.

## Plex after a rename

Renaming a movie folder changes its filesystem path. Plex may temporarily show the old path as unavailable until it detects or scans the changed files. Run a normal Plex library scan after applying folder renames.

## What this does not do

- It does not rename movie files.
- It does not rename subtitle files.
- It does not move files between existing movie folders. M2 only moves Plex-indexed movie files that are directly in a selected library root into their canonical movie folder.
- It does not merge folders.
- It does not use fuzzy title parsing to choose or rewrite titles. A conservative
  text-similarity check is used only to veto suspicious renames.
- It does not call TMDB, IMDb, or any external metadata service.
- It does not modify Plex metadata or Plex databases.
- It does not normalize TV show or music libraries.

## License

MIT. See [LICENSE](LICENSE).

---

Plex is a trademark of Plex, Inc. This project is not affiliated with or endorsed by Plex.
