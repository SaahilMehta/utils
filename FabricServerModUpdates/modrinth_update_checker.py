#!/usr/bin/env python3
"""
Modrinth Update Checker for Minecraft Fabric/Java Mods
Scans a mods folder and queries Modrinth API to detect version updates.
No authentication required for read-only operations like this.
"""

import argparse
import sys
from pathlib import Path
from typing import Dict

from modrinth import ModrinthUpdateChecker, detect_minecraft_version
from update_operations import (
    download_updates,
    install_downloaded_updates,
    is_minecraft_server_running,
)


def print_results(results: Dict[str, Dict], summary_only: bool = False):
    """Format and display the update check results."""
    if not results:
        print("[INFO] No mods to report.")
        return

    updates = []
    outdated = []
    ok = []

    for filename, data in results.items():
        status = data.get("status", "unknown")
        if status == "update_available":
            updates.append((filename, data))
        elif status == "up-to-date":
            ok.append((filename, data))
        else:
            outdated.append((filename, data))

    print("\n" + "=" * 60)
    print("MODRINTH UPDATE CHECKER RESULTS")
    print("=" * 60)

    if updates:
        print(f"\n[UPDATES] {len(updates)} mod(s) have newer versions:\n")
        for filename, data in updates:
            print(f"  - {filename}")
            print(f"      Current: {data.get('current', 'N/A')} → Latest: {data.get('latest', 'N/A')}")

    if outdated:
        print(f"\n[WARNINGS] {len(outdated)} mod(s) with issues:\n")
        for filename, data in outdated:
            print(f"  - {filename}")
            print(f"      Details: {data.get('details', 'N/A')}")

    if ok:
        print(f"\n[OK] {len(ok)} mod(s) are up-to-date.")

    if summary_only:
        print("\n--- SUMMARY ---")
        print(f"  Updates available: {len(updates)}")
        print(f"  Up-to-date:       {len(ok)}")
        print(f"  Issues/Unknown:   {len(outdated)}")


def main():
    parser = argparse.ArgumentParser(
        description="Check Minecraft Fabric mods for updates via Modrinth API."
    )
    parser.add_argument("mods_folder", nargs="?", default=None, help="Path to the 'mods' folder.")
    parser.add_argument(
        "--minecraft-version",
        help=(
            "Minecraft version to target. If omitted, the checker tries to read "
            "version.json from a server JAR beside the mods folder."
        ),
    )
    parser.add_argument("-t", "--token", help="Modrinth API token (optional).")
    parser.add_argument("-s", "--summary", action="store_true", help="Show only a summary report.")
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download available updates to a separate folder (default: sibling '<mods-folder>_updates').",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help=(
            "Download and install available updates; replaced mods are moved to "
            "a sibling backup folder. Installation is blocked while a Minecraft "
            "server is running."
        ),
    )
    parser.add_argument(
        "--download-dir",
        type=Path,
        help="Destination folder for downloaded updates; requires --download or --install.",
    )

    args = parser.parse_args()

    if args.download_dir and not (args.download or args.install):
        parser.error("--download-dir requires --download or --install.")

    if not args.mods_folder:
        print("[ERROR] Please specify the mods folder path.")
        sys.exit(1)

    minecraft_version = (
        args.minecraft_version.strip() if args.minecraft_version else None
    )
    if args.minecraft_version is not None and not minecraft_version:
        parser.error("--minecraft-version must not be empty.")

    checker = ModrinthUpdateChecker(api_token=args.token)
    mods_folder = Path(args.mods_folder)
    if args.install:
        try:
            if is_minecraft_server_running(mods_folder.parent):
                print(
                    "[ERROR] A Minecraft server appears to be running. "
                    "No mods were downloaded or installed; stop the server and try again."
                )
                sys.exit(1)
        except RuntimeError as error:
            print(f"[ERROR] {error}")
            sys.exit(1)

    if not minecraft_version:
        minecraft_version = detect_minecraft_version(mods_folder.parent)
    if minecraft_version:
        print(f"[INFO] Checking for Minecraft {minecraft_version} compatible versions.")
    else:
        print(
            "[WARN] Could not determine the Minecraft version. "
            "No updates will be reported or downloaded; use --minecraft-version."
        )

    mods = checker.scan_mods_folder(mods_folder)
    results = checker.check_for_updates(mods, minecraft_version)
    print_results(results, summary_only=args.summary)
    if args.download or args.install:
        download_dir = args.download_dir or (
            mods_folder.resolve().parent / f"{mods_folder.name}_updates"
        )
        download_updates(checker, results, download_dir)
        if args.install:
            if not minecraft_version:
                print("[ERROR] A Minecraft version is required to validate updates before installation.")
                sys.exit(1)
            try:
                install_downloaded_updates(
                    checker,
                    mods_folder,
                    download_dir,
                    results,
                    minecraft_version,
                )
            except (OSError, RuntimeError, ValueError) as error:
                print(f"[ERROR] {error}")
                sys.exit(1)


if __name__ == "__main__":
    main()
