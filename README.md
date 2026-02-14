# Google Takeout Extract & Merge

A CLI tool to extract and merge multiple Google Takeout zip files into a single `Takeout/` folder.

## Why

Google splits Takeout exports into multiple ~2GB zips. Extracting them manually has problems:

- macOS `unzip` and Finder silently drop files with Unicode characters (Polish, Norwegian, etc.)
- Finder creates `Takeout 2/`, `Takeout 3/` instead of merging
- Albums span multiple zips with duplicate metadata files
- No way to verify all files were extracted

This tool uses Python's `zipfile` which handles all of the above correctly.

## Requirements

Python 3.8+ (no external dependencies)

## Usage

```bash
# Extract and merge all zips into <source_dir>/Takeout/
python3 google-takeout-merge.py <source_dir>

# Extract to a custom output directory
python3 google-takeout-merge.py <source_dir> --output ~/Photos/Takeout

# Scan and report without extracting anything
python3 google-takeout-merge.py <source_dir> --dry-run

# Verify an existing extraction against the zip files
python3 google-takeout-merge.py <source_dir> --verify-only

# Merge new takeout zips into an existing folder (deduplicates)
python3 google-takeout-merge.py <new_zips_dir> --merge-into ~/Photos/Takeout

# Preview what a merge would do without extracting
python3 google-takeout-merge.py <new_zips_dir> --merge-into ~/Photos/Takeout --dry-run

# Disable colored output
python3 google-takeout-merge.py <source_dir> --no-color
```

## What it does

### Fresh extraction

1. Finds all `takeout-*.zip` files in the source directory
2. Reads zip manifests and reports contents (file counts, detected Google products)
3. Checks zip integrity (CRC verification)
4. Extracts and merges everything into a single `Takeout/` folder
5. Verifies every file from every zip exists on disk
6. Prints a summary

### Merge with deduplication (`--merge-into`)

When you have an existing Takeout folder and download a newer export:

1. Indexes all files in the existing folder
2. Compares each file in the new zips against the index:
   - **Exact path + same size** — skipped as duplicate
   - **Same filename + same size in any folder** — skipped as moved/reorganized
   - **Everything else** — extracted as new
3. Reports new, duplicate, and moved files

## Development

Lint and format with [ruff](https://docs.astral.sh/ruff/) (via [uv](https://docs.astral.sh/uv/)):

```bash
uvx ruff check .        # lint
uvx ruff format .       # format
```

## Exit codes

- `0` — success, all files verified
- `1` — completed with errors (missing or corrupt files)
- `2` — invalid arguments or no zip files found
