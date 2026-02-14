#!/usr/bin/env python3
"""
Google Takeout Extract & Merge Tool

Extracts and merges multiple Google Takeout zip files into a single folder.
Uses Python's zipfile module instead of system unzip to correctly handle
Unicode filenames (Polish, Norwegian, etc.) that macOS unzip silently drops.

Supports merging new takeout zips into an existing folder with deduplication,
comparing by filename and file size to avoid extracting duplicates.

Usage:
    python3 google-takeout-merge.py <source_dir> [options]
    python3 google-takeout-merge.py <source_dir> --merge-into <dir> [--dry-run]
"""

import argparse
import os
import sys
import zipfile
import zlib


# ANSI color codes
class Colors:
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


NO_COLOR = Colors()
for attr in vars(NO_COLOR):
    if not attr.startswith("_"):
        setattr(NO_COLOR, attr, "")

C = Colors()


def set_no_color():
    global C
    C = NO_COLOR


def is_path_safe(output_dir, filename):
    """Check that a zip entry won't escape the output directory (ZipSlip guard)."""
    dest = os.path.realpath(os.path.join(output_dir, filename))
    return dest.startswith(os.path.realpath(output_dir) + os.sep) or dest == os.path.realpath(
        output_dir
    )


def file_crc32(filepath):
    """Compute CRC32 of a file on disk, matching the format stored in zip entries."""
    crc = 0
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def find_takeout_zips(source_dir):
    """Find all takeout zip files in the source directory."""
    zips = sorted(
        f for f in os.listdir(source_dir) if f.startswith("takeout-") and f.endswith(".zip")
    )
    return [os.path.join(source_dir, z) for z in zips]


def inventory_zips(zip_paths):
    """Read manifests from all zips.

    Returns (unique_files, products, per_zip_counts, total_size).
    """
    unique_files = set()
    products = set()
    per_zip_counts = {}
    total_size = 0

    for zpath in zip_paths:
        zname = os.path.basename(zpath)
        total_size += os.path.getsize(zpath)
        count = 0
        with zipfile.ZipFile(zpath, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                unique_files.add(info.filename)
                count += 1
                # Detect product from path: Takeout/<Product>/...
                parts = info.filename.split("/")
                if len(parts) >= 2:
                    products.add(parts[1])
        per_zip_counts[zname] = count

    return unique_files, products, per_zip_counts, total_size


def check_integrity(zip_paths):
    """Test each zip for CRC errors. Returns list of (zip_name, error_file) tuples."""
    errors = []
    for zpath in zip_paths:
        zname = os.path.basename(zpath)
        print(f"  Testing {zname}...", end=" ", flush=True)
        with zipfile.ZipFile(zpath, "r") as zf:
            bad = zf.testzip()
            if bad:
                print(f"{C.RED}FAILED{C.RESET} (first bad file: {bad})")
                errors.append((zname, bad))
            else:
                print(f"{C.GREEN}OK{C.RESET}")
    return errors


def extract_and_merge(zip_paths, output_dir):
    """Extract all zips into a single output directory. Returns (extracted, skipped, errors)."""
    extracted = 0
    skipped = 0
    errors = []
    seen_paths = set()

    for zpath in zip_paths:
        zname = os.path.basename(zpath)
        zip_extracted = 0
        zip_skipped = 0
        print(f"  Extracting {C.BOLD}{zname}{C.RESET}...", flush=True)

        with zipfile.ZipFile(zpath, "r") as zf:
            entries = [info for info in zf.infolist() if not info.is_dir()]
            for info in entries:
                dest = os.path.join(output_dir, info.filename)

                if not is_path_safe(output_dir, info.filename):
                    errors.append((zname, info.filename, "path traversal detected"))
                    continue

                if info.filename in seen_paths:
                    zip_skipped += 1
                    skipped += 1
                    continue

                seen_paths.add(info.filename)

                if os.path.exists(dest):
                    zip_skipped += 1
                    skipped += 1
                    continue

                try:
                    zf.extract(info, output_dir)
                    zip_extracted += 1
                    extracted += 1
                except Exception as e:
                    errors.append((zname, info.filename, str(e)))

        print(
            f"    {C.GREEN}{zip_extracted} extracted{C.RESET}, "
            f"{C.DIM}{zip_skipped} skipped (duplicates){C.RESET}"
            + (
                f", {C.RED}{len([e for e in errors if e[0] == zname])} errors{C.RESET}"
                if any(e[0] == zname for e in errors)
                else ""
            )
        )

    return extracted, skipped, errors


def verify_extraction(zip_paths, output_dir):
    """Verify every file from every zip exists on disk. Returns list of missing files."""
    missing = []
    total = 0

    for zpath in zip_paths:
        zname = os.path.basename(zpath)
        with zipfile.ZipFile(zpath, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                total += 1
                dest = os.path.join(output_dir, info.filename)
                if not os.path.exists(dest):
                    missing.append((zname, info.filename))

    return total, missing


def index_existing_folder(folder_path):
    """Build an index of all files in an existing folder.

    Returns a dict mapping relative path -> (size, basename) and
    a dict mapping basename -> list of (relative_path, size) for fuzzy matching.
    """
    by_path = {}
    by_basename = {}

    for root, _dirs, files in os.walk(folder_path):
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, os.path.dirname(folder_path))
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            by_path[rel] = size
            by_basename.setdefault(fname, []).append((rel, size))

    return by_path, by_basename


def compare_and_merge(zip_paths, existing_dir, output_dir, dry_run=False):
    """Compare new takeout zips against an existing folder and extract only new files.

    Deduplication strategy:
    1. Exact path match + same size -> skip (duplicate)
    2. Same basename + same size in any folder -> skip (moved between albums)
    3. Everything else -> extract as new

    Returns (new_files, duplicates, moved, errors).
    """
    print("  Indexing existing folder...", flush=True)
    existing_by_path, existing_by_basename = index_existing_folder(existing_dir)
    print(f"  Found {C.BOLD}{len(existing_by_path)}{C.RESET} existing files")

    new_files = []
    duplicates = []
    moved = []
    errors = []
    seen_paths = set()

    for zpath in zip_paths:
        zname = os.path.basename(zpath)
        zip_new = 0
        zip_dup = 0
        zip_moved = 0
        print(f"\n  Comparing {C.BOLD}{zname}{C.RESET}...", flush=True)

        with zipfile.ZipFile(zpath, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue

                if not is_path_safe(output_dir, info.filename):
                    errors.append((zname, info.filename, "path traversal detected"))
                    continue

                if info.filename in seen_paths:
                    zip_dup += 1
                    duplicates.append(info.filename)
                    continue
                seen_paths.add(info.filename)

                basename = os.path.basename(info.filename)
                zip_file_size = info.file_size

                # Check 1: exact path match
                if info.filename in existing_by_path:
                    existing_size = existing_by_path[info.filename]
                    if existing_size == zip_file_size:
                        duplicates.append(info.filename)
                        zip_dup += 1
                        continue

                # Check 2: same basename + same size + same CRC32 anywhere
                if basename in existing_by_basename:
                    crc_match = None
                    for existing_rel, existing_size in existing_by_basename[basename]:
                        if existing_size != zip_file_size:
                            continue
                        existing_full = os.path.join(os.path.dirname(existing_dir), existing_rel)
                        try:
                            if file_crc32(existing_full) == info.CRC:
                                crc_match = existing_rel
                                break
                        except OSError:
                            continue
                    if crc_match:
                        moved.append((info.filename, crc_match))
                        zip_moved += 1
                        continue

                # New file — extract it
                new_files.append((zname, info.filename))
                zip_new += 1

                if not dry_run:
                    try:
                        zf.extract(info, output_dir)
                    except Exception as e:
                        errors.append((zname, info.filename, str(e)))

        print(
            f"    {C.GREEN}{zip_new} new{C.RESET}, "
            f"{C.DIM}{zip_dup} duplicates{C.RESET}, "
            f"{C.DIM}{zip_moved} moved{C.RESET}"
        )

    return new_files, duplicates, moved, errors


def format_size(size_bytes):
    """Format bytes into human readable string."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def main():
    parser = argparse.ArgumentParser(
        description="Extract and merge Google Takeout zip files into a single folder.",
        epilog="Example: python3 google-takeout-merge.py ~/Downloads/google-takeout",
    )
    parser.add_argument(
        "source_dir",
        help="Directory containing takeout-*.zip files",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Output directory (default: <source_dir>/Takeout)",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Scan and report without extracting",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--verify-only",
        action="store_true",
        help="Only verify existing extraction against zips",
    )
    mode_group.add_argument(
        "--merge-into",
        metavar="DIR",
        help="Merge new zips into an existing Takeout folder, skipping duplicates",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )
    args = parser.parse_args()

    if args.no_color:
        set_no_color()

    source_dir = os.path.abspath(args.source_dir)
    output_dir = args.output or os.path.join(source_dir, "Takeout")

    if not os.path.isdir(source_dir):
        print(f"{C.RED}Error:{C.RESET} {source_dir} is not a directory")
        sys.exit(2)

    # Step 1: Discover
    print(f"\n{C.BOLD}Step 1: Discovering takeout zip files{C.RESET}")
    zip_paths = find_takeout_zips(source_dir)

    if not zip_paths:
        print(f"{C.RED}No takeout-*.zip files found in {source_dir}{C.RESET}")
        sys.exit(2)

    print(f"  Found {C.BOLD}{len(zip_paths)}{C.RESET} zip files")

    # Step 2: Inventory
    print(f"\n{C.BOLD}Step 2: Reading zip manifests{C.RESET}")
    unique_files, products, per_zip_counts, total_size = inventory_zips(zip_paths)

    print(f"  Total zip size: {C.BOLD}{format_size(total_size)}{C.RESET}")
    print(f"  Unique files across all zips: {C.BOLD}{len(unique_files)}{C.RESET}")
    print(f"  Google products found: {C.BOLD}{', '.join(sorted(products))}{C.RESET}")
    print("  Per-zip file counts:")
    for zname, count in per_zip_counts.items():
        print(f"    {zname}: {count} files")

    # Step 3: Integrity check
    print(f"\n{C.BOLD}Step 3: Checking zip integrity{C.RESET}")
    integrity_errors = check_integrity(zip_paths)

    if integrity_errors:
        print(f"\n{C.RED}Integrity errors found in {len(integrity_errors)} zip(s):{C.RESET}")
        for zname, bad_file in integrity_errors:
            print(f"  {zname}: first bad file: {bad_file}")
        print(f"{C.RED}Aborting extraction. Fix or re-download the corrupted zips.{C.RESET}")
        sys.exit(1)

    print(f"  {C.GREEN}All zips passed integrity check{C.RESET}")

    if args.dry_run and not args.merge_into:
        print(f"\n{C.YELLOW}Dry run complete. No files were extracted.{C.RESET}")
        sys.exit(0)

    if args.merge_into:
        existing_dir = os.path.abspath(args.merge_into)
        merge_output = args.output or existing_dir
        if not os.path.isdir(existing_dir):
            print(f"{C.RED}Error:{C.RESET} {existing_dir} is not a directory")
            sys.exit(2)

        print(f"\n{C.BOLD}Step 4: Merging into {existing_dir} (deduplicating){C.RESET}")
        new_files, duplicates, moved, merge_errors = compare_and_merge(
            zip_paths, existing_dir, merge_output, dry_run=args.dry_run
        )

        print(f"\n{C.BOLD}{'=' * 50}{C.RESET}")
        print(f"{C.BOLD}  Merge Summary{C.RESET}")
        print(f"{C.BOLD}{'=' * 50}{C.RESET}")
        print(f"  Zip files processed: {len(zip_paths)}")
        print(f"  Exact duplicates skipped: {len(duplicates)}")
        print(f"  Moved/renamed skipped: {len(moved)}")
        verb = "found" if args.dry_run else "extracted"
        print(f"  {C.GREEN}New files {verb}: {len(new_files)}{C.RESET}")

        if merge_errors:
            print(f"  {C.RED}Errors: {len(merge_errors)}{C.RESET}")
            for zname, fname, err in merge_errors:
                print(f"    [{zname}] {fname}: {err}")

        if new_files and args.dry_run:
            print("\n  New files that would be extracted:")
            # Group by folder
            by_folder = {}
            for _zname, fname in new_files:
                parts = fname.split("/")
                folder = "/".join(parts[:3]) if len(parts) >= 3 else "/".join(parts[:-1])
                by_folder.setdefault(folder, []).append(fname)
            for folder in sorted(by_folder):
                files = by_folder[folder]
                print(f"    {folder}: {len(files)} files")
                for f in files[:3]:
                    print(f"      {os.path.basename(f)}")
                if len(files) > 3:
                    print(f"      ... and {len(files) - 3} more")

        if moved:
            print(f"\n  {C.DIM}Files found under different paths (skipped):{C.RESET}")
            for new_path, old_path in moved[:5]:
                print(f"    {C.DIM}{os.path.basename(new_path)}: {new_path} -> {old_path}{C.RESET}")
            if len(moved) > 5:
                print(f"    {C.DIM}... and {len(moved) - 5} more{C.RESET}")

        if args.dry_run:
            print(f"\n{C.YELLOW}Dry run complete. No files were extracted.{C.RESET}")
        elif not merge_errors:
            print(f"\n  {C.GREEN}Merge completed successfully.{C.RESET}")
        else:
            print(f"\n  {C.RED}Merge completed with errors.{C.RESET}")
            sys.exit(1)
        sys.exit(0)

    if args.verify_only:
        # Skip to verification
        print(f"\n{C.BOLD}Step 5: Verifying extraction{C.RESET}")
        total, missing = verify_extraction(zip_paths, output_dir)

        if missing:
            print(f"  {C.RED}{len(missing)} files missing out of {total} entries:{C.RESET}")
            # Group by zip
            by_zip = {}
            for zname, fname in missing:
                by_zip.setdefault(zname, []).append(fname)
            for zname, files in by_zip.items():
                print(f"    {zname}: {len(files)} missing")
                for f in files[:3]:
                    print(f"      {f}")
                if len(files) > 3:
                    print(f"      ... and {len(files) - 3} more")
            sys.exit(1)
        else:
            print(f"  {C.GREEN}All {total} file entries verified on disk{C.RESET}")
            sys.exit(0)

    # Step 4: Extract & merge
    print(f"\n{C.BOLD}Step 4: Extracting and merging into {output_dir}{C.RESET}")
    os.makedirs(output_dir, exist_ok=True)
    extracted, skipped, extract_errors = extract_and_merge(zip_paths, output_dir)

    print(f"\n  Total: {C.GREEN}{extracted} extracted{C.RESET}, {skipped} skipped (duplicates)")
    if extract_errors:
        print(f"  {C.RED}{len(extract_errors)} errors:{C.RESET}")
        for zname, fname, err in extract_errors:
            print(f"    [{zname}] {fname}: {err}")

    # Step 5: Verify
    print(f"\n{C.BOLD}Step 5: Verifying extraction{C.RESET}")
    total, missing = verify_extraction(zip_paths, output_dir)

    if missing:
        print(f"  {C.RED}{len(missing)} files missing out of {total} entries:{C.RESET}")
        by_zip = {}
        for zname, fname in missing:
            by_zip.setdefault(zname, []).append(fname)
        for zname, files in by_zip.items():
            print(f"    {zname}: {len(files)} missing")
            for f in files[:3]:
                print(f"      {f}")
            if len(files) > 3:
                print(f"      ... and {len(files) - 3} more")
    else:
        print(f"  {C.GREEN}All {total} file entries verified on disk{C.RESET}")

    # Step 6: Report
    print(f"\n{C.BOLD}{'=' * 50}{C.RESET}")
    print(f"{C.BOLD}  Summary{C.RESET}")
    print(f"{C.BOLD}{'=' * 50}{C.RESET}")
    print(f"  Zip files processed: {len(zip_paths)}")
    print(f"  Total zip size: {format_size(total_size)}")
    print(f"  Unique files: {len(unique_files)}")
    print(f"  Files extracted: {extracted}")
    print(f"  Duplicates skipped: {skipped}")
    print(f"  Products: {', '.join(sorted(products))}")
    print(f"  Output: {output_dir}")

    if extract_errors or missing:
        print(f"\n  {C.RED}Completed with errors.{C.RESET}")
        print(f"  Extraction errors: {len(extract_errors)}")
        print(f"  Missing after verification: {len(missing)}")
        sys.exit(1)
    else:
        print(f"\n  {C.GREEN}Completed successfully. All files verified.{C.RESET}")
        print("\n  You can now safely delete the zip files.")
        sys.exit(0)


if __name__ == "__main__":
    main()
