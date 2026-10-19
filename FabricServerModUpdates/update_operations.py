"""Safe server-process checks and update download/installation operations."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Optional

import requests

from modrinth import ModrinthUpdateChecker, _versions_match


def _is_minecraft_server_command(
        command_line: str, server_jar_names: Optional[set[str]] = None
) -> bool:
    """Identify common Fabric and vanilla dedicated-server Java commands."""
    normalized = command_line.casefold().replace("\\", "/")
    server_markers = (
        "net.minecraft.server.main",
        "net.fabricmc.loader.impl.launch.server.fabricserverlauncher",
        "fabric-server-launch",
        "minecraft_server",
        " server.jar",
        "/server.jar",
    )
    return any(marker in normalized for marker in server_markers) or any(
        re.search(
            rf"(?<![\w.-]){re.escape(jar_name.casefold())}(?![\w.-])",
            normalized,
        )
        for jar_name in (server_jar_names or set())
    )


def is_minecraft_server_running(server_dir: Optional[Path] = None) -> bool:
    """Check Java process command lines for a Minecraft dedicated server."""
    server_jar_names = (
        {jar.name.casefold() for jar in server_dir.glob("*.jar")}
        if server_dir and server_dir.is_dir()
        else set()
    )
    if os.name == "nt":
        command = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$ErrorActionPreference = 'Stop'; "
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.Name -match '^javaw?\\.exe$' -and $_.CommandLine } | "
                "Select-Object -Property CommandLine | ConvertTo-Json -Compress"
            ),
        ]
    else:
        command = ["ps", "-eo", "args="]

    try:
        process_list = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(
            f"Could not check whether a Minecraft server is running: {error}"
        ) from error

    if os.name == "nt":
        try:
            process_data = json.loads(process_list.stdout) if process_list.stdout else []
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "Could not interpret the Windows process list while checking "
                "whether a Minecraft server is running."
            ) from error

        if isinstance(process_data, dict):
            process_data = [process_data]
        command_lines = [
            entry.get("CommandLine", "")
            for entry in process_data
            if isinstance(entry, dict)
        ] if isinstance(process_data, list) else []
    else:
        command_lines = process_list.stdout.splitlines()

    return any(
        isinstance(command_line, str)
        and _is_minecraft_server_command(command_line, server_jar_names)
        for command_line in command_lines
    )


def download_updates(
        checker: ModrinthUpdateChecker,
        results: Dict[str, Dict],
        download_dir: Path,
) -> None:
    """Download available updates into a directory separate from the mods folder."""
    updates = [
        (filename, data)
        for filename, data in results.items()
        if data.get("status") == "update_available"
    ]
    if not updates:
        print("[INFO] No available updates to download.")
        return

    try:
        download_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"[ERROR] Could not create download directory '{download_dir}': {error}")
        return

    for installed_filename, data in updates:
        download_filename = data.get("download_filename")
        download_url = data.get("download_url")
        if (
                not isinstance(download_filename, str)
                or not download_filename
                or Path(download_filename).name != download_filename
                or "/" in download_filename
                or "\\" in download_filename
                or not isinstance(download_url, str)
        ):
            print(
                f"[ERROR] No valid download file was provided for '{installed_filename}'."
            )
            continue

        destination = download_dir / download_filename
        if destination.exists():
            print(
                f"[SKIP] '{destination}' already exists; it was not overwritten."
            )
            continue

        try:
            checker.download_file(download_url, destination)
            print(f"[DOWNLOADED] {installed_filename} -> {destination}")
        except (requests.exceptions.RequestException, OSError, ValueError) as error:
            print(f"[ERROR] Could not download update for '{installed_filename}': {error}")


def install_downloaded_updates(
        checker: ModrinthUpdateChecker,
        mods_folder: Path,
        download_dir: Path,
        results: Dict[str, Dict],
        minecraft_version: str,
) -> None:
    """Back up replaced mods, then move downloaded updates into the mods folder."""
    updates = [
        (filename, data)
        for filename, data in results.items()
        if data.get("status") == "update_available"
    ]
    if not updates:
        print("[INFO] No available updates to install.")
        return

    if is_minecraft_server_running(mods_folder.parent):
        raise RuntimeError(
            "A Minecraft server appears to be running. No mods were installed; "
            "stop the server and try again."
        )

    resolved_mods = mods_folder.resolve()
    resolved_downloads = download_dir.resolve()
    try:
        resolved_downloads.relative_to(resolved_mods)
    except ValueError:
        pass
    else:
        raise ValueError("The download directory must not be inside the mods folder.")
    try:
        resolved_mods.relative_to(resolved_downloads)
    except ValueError:
        pass
    else:
        raise ValueError("The download directory must not contain the mods folder.")

    install_plan = []
    target_names = set()
    for installed_filename, data in updates:
        download_filename = data.get("download_filename")
        latest_version = data.get("latest")
        if (
                not isinstance(installed_filename, str)
                or not installed_filename
                or Path(installed_filename).name != installed_filename
                or "/" in installed_filename
                or "\\" in installed_filename
                or not isinstance(download_filename, str)
                or not download_filename
                or Path(download_filename).name != download_filename
                or "/" in download_filename
                or "\\" in download_filename
                or not isinstance(latest_version, str)
                or not latest_version
        ):
            raise ValueError(f"Invalid update information for '{installed_filename}'.")

        if download_filename.casefold() in target_names:
            raise ValueError(
                f"Multiple updates use the same download filename "
                f"'{download_filename}'; no mods were installed."
            )
        target_names.add(download_filename.casefold())

        installed_path = mods_folder / installed_filename
        download_path = download_dir / download_filename
        if not installed_path.is_file():
            raise ValueError(
                f"Installed mod '{installed_path}' is missing; no mods were installed."
            )
        if not download_path.is_file():
            raise ValueError(
                f"Downloaded update '{download_path}' is missing; no mods were installed."
            )

        downloaded_version = checker._read_fabric_metadata(download_path).get("version")
        if not downloaded_version or not _versions_match(
                downloaded_version, latest_version, minecraft_version
        ):
            raise ValueError(
                f"Downloaded file '{download_path}' does not match the expected "
                f"version '{latest_version}'; no mods were installed."
            )
        install_plan.append((installed_path, download_path, mods_folder / download_filename))

    paths_to_back_up = {
        installed_path
        for installed_path, _, _ in install_plan
    }
    paths_to_back_up.update(
        target_path
        for _, _, target_path in install_plan
        if target_path.exists()
    )
    for path in paths_to_back_up:
        if path.exists() and not path.is_file():
            raise ValueError(
                f"Cannot back up '{path}' because it is not a file; "
                "no mods were installed."
            )

    if is_minecraft_server_running(mods_folder.parent):
        raise RuntimeError(
            "A Minecraft server appears to be running. No mods were installed; "
            "stop the server and try again."
        )

    backup_dir = Path(
        tempfile.mkdtemp(
            prefix=f"{mods_folder.name}_backup_",
            dir=mods_folder.parent,
        )
    )
    backed_up = []
    installed = []
    try:
        for original_path in sorted(paths_to_back_up):
            if original_path.exists():
                backup_path = backup_dir / original_path.name
                shutil.move(str(original_path), str(backup_path))
                backed_up.append((original_path, backup_path))
                print(f"[BACKUP] {original_path} -> {backup_path}")

        if is_minecraft_server_running(mods_folder.parent):
            raise RuntimeError(
                "A Minecraft server appears to be running. No mods were installed."
            )

        for _, download_path, target_path in install_plan:
            shutil.move(str(download_path), str(target_path))
            installed.append((download_path, target_path))
            print(f"[INSTALLED] {download_path} -> {target_path}")
    except (OSError, RuntimeError) as error:
        rollback_errors = []
        for download_path, target_path in reversed(installed):
            try:
                if target_path.exists():
                    shutil.move(str(target_path), str(download_path))
            except OSError as rollback_error:
                rollback_errors.append(f"{target_path}: {rollback_error}")
        for original_path, backup_path in reversed(backed_up):
            try:
                if backup_path.exists():
                    shutil.move(str(backup_path), str(original_path))
            except OSError as rollback_error:
                rollback_errors.append(f"{original_path}: {rollback_error}")
        if not rollback_errors:
            try:
                backup_dir.rmdir()
            except OSError as rollback_error:
                rollback_errors.append(f"{backup_dir}: {rollback_error}")

        message = f"Could not install updates: {error}"
        if rollback_errors:
            message += "; rollback was incomplete: " + "; ".join(rollback_errors)
        else:
            message += "; the original mods were restored."
        raise RuntimeError(message) from error

    print(f"[INFO] Replaced mods were backed up to '{backup_dir}'.")
