#!/usr/bin/env python3
"""
Modrinth Update Checker for Minecraft Fabric/Java Mods
Scans a mods folder and queries Modrinth API to detect version updates.
No authentication required for read-only operations like this.
"""


from importlib.metadata import metadata
import os
import sys
import json
import re
import argparse
import shutil
import subprocess
import time
import tempfile
import zipfile
from pathlib import Path
from typing import Optional, Dict, List, Any
from functools import lru_cache
from urllib.parse import urlparse
import requests


MODRINTH_PROJECT_ID_OVERRIDES = {
    "treeharvester": "abooMhox",
}

MODRINTH_LOADER_LABELS = {"fabric", "forge", "neoforge", "neo", "quilt"}


def _versions_match(
    installed_version: str, published_version: str, minecraft_version: str
) -> bool:
    """Compare mod versions while ignoring Minecraft and loader labels."""
    if installed_version.casefold() == published_version.casefold():
        return True

    minecraft_version = minecraft_version.casefold()
    minecraft_labels = {
        minecraft_version,
        f"mc{minecraft_version}",
        f"{minecraft_version}.0",
        f"mc{minecraft_version}.0",
    }

    def version_components(version: str) -> tuple[str, ...]:
        return tuple(
            component
            for component in re.split(r"[-+]", version.casefold())
            if component
            and component not in MODRINTH_LOADER_LABELS
            and component not in minecraft_labels
        )

    installed_components = version_components(installed_version)
    published_components = version_components(published_version)
    return bool(installed_components) and installed_components == published_components


def detect_minecraft_version(server_dir: Path) -> Optional[str]:
    """Read the Minecraft version from server JARs in the directory."""
    versions = set()
    for server_jar in server_dir.glob("*.jar"):
        try:
            with zipfile.ZipFile(server_jar) as archive:
                metadata = json.loads(archive.read("version.json"))
        except KeyError:
            continue
        except (OSError, zipfile.BadZipFile):
            continue
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            print(f"[WARN] Could not read version metadata from '{server_jar.name}': {error}")
            continue

        if isinstance(metadata, dict):
            version = metadata.get("id")
            if isinstance(version, str) and version:
                versions.add(version)

    if len(versions) > 1:
        print(
            f"[WARN] Found multiple Minecraft versions in server JARs under "
            f"'{server_dir}': {', '.join(sorted(versions))}."
        )
        return None
    return next(iter(versions), None)


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


class ModrinthUpdateChecker:
    def __init__(self, api_token: Optional[str] = None):
        self.base_url = "https://api.modrinth.com/v2"
        self.api_token = api_token or os.getenv("MODRINTH_API_TOKEN")

        # Build headers only if token is provided
        self.headers = {}
        if self.api_token:
            self.headers["Authorization"] = f"Bearer {self.api_token}"

        # Cache for API responses (project info and latest versions)
        self._cache: Dict[str, Any] = {}

    def _request(
        self, endpoint: str, params: Optional[Dict] = None, retry_rate_limit: bool = True
    ) -> tuple:
        """Make API request and return (data, status_code)."""
        url = f"{self.base_url}{endpoint}"
        try:
            response = requests.get(url, headers=self.headers, params=params, timeout=10)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                if retry_rate_limit:
                    print(f"[WARN] Rate limited. Waiting {retry_after} seconds...")
                    time.sleep(retry_after)
                    return self._request(endpoint, params, retry_rate_limit=False)
                print(f"[ERROR] Still rate limited after retry for {url}")
                return {}, 429

            data = response.json() if response.content else {}

            # Check for rate limit headers and warn the user
            if "X-RateLimit-Remaining" in response.headers:
                remaining = int(response.headers["X-RateLimit-Remaining"])
                if remaining <= 5:
                    print(f"[WARN] Rate limit approaching ({remaining} requests left)")

            return data, response.status_code

        except requests.exceptions.RequestException as e:
            print(f"[ERROR] Request failed for {url}: {e}")
            return {}, 0

    def _rate_limit(self):
        """Enforce a small delay between API calls to stay within Modrinth's free tier limits."""
        time.sleep(1.2)  # ~15% buffer over Modrinth's 10 req/min limit

    @lru_cache(maxsize=128)
    def resolve_project_id(
        self, candidate_id: str, candidate_name: Optional[str] = None
    ) -> Optional[str]:
        """Search for a Modrinth project by name/ID and return its actual project ID."""
        # Try direct lookup first (faster if ID is already valid)
        data, status = self._request(f"/project/{candidate_id}")
        if status == 200 and isinstance(data, dict):
            return data.get("id")

        if status != 200:
            print(
                f"[WARN] Direct lookup for candidate ID '{candidate_id}' failed with status {status}; "
                f"falling back to search."
            )

        # Fall back to search API
        search_query = candidate_name or candidate_id
        results, status = self._request(
            "/search",
            {
                "query": search_query,
                "facets": json.dumps([["project_type:mod"]]),
                "limit": 10,
            },
        )
        if status >= 400 or not isinstance(results, dict) or "hits" not in results:
            print(
                f"[WARN] Could not resolve project ID for '{candidate_id}' via search API. "
                f"Query='{search_query}', status={status}."
            )
            return None

        hits = [
            hit for hit in results["hits"]
            if isinstance(hit, dict)
            and hit.get("project_type") == "mod"
            and isinstance(hit.get("project_id"), str)
        ]
        def normalize(value: str) -> str:
            return "".join(
                character for character in value.casefold() if character.isalnum()
            )

        expected_names = {normalize(candidate_id)}
        if candidate_name:
            expected_names.add(normalize(candidate_name))
        exact_hit = next(
            (
                hit for hit in hits
                if normalize(hit.get("slug", "")) in expected_names
                or normalize(hit.get("title", "")) in expected_names
            ),
            None,
        )
        if not exact_hit:
            print(
                f"[WARN] No Modrinth project match found for '{candidate_id}' "
                f"(name='{candidate_name}', expected={sorted(expected_names)})"
            )
            return None

        return exact_hit.get("project_id")

    def get_mod_info(self, project_id: str) -> Optional[Dict]:
        """Fetch mod/project info from Modrinth API."""
        # Check cache first
        if project_id in self._cache:
            return self._cache[project_id]

        data, status = self._request(f"/project/{project_id}")
        if not data or "error" in data or status >= 400:
            print(f"[WARN] Failed to fetch info for project ID '{project_id}'")
            return None

        # Cache the result by project ID (more stable than filename-derived IDs)
        self._cache[project_id] = data
        return data

    def get_latest_version(
        self, project_id: str, minecraft_version: Optional[str] = None
    ) -> Optional[Dict]:
        """Fetch the latest Fabric version compatible with Minecraft."""
        if not minecraft_version:
            print("[WARN] A Minecraft version is required to select compatible versions.")
            return None

        cache_key = f"latest:{minecraft_version}"
        # Check cache first
        if project_id in self._cache and cache_key in self._cache[project_id]:
            return self._cache[project_id][cache_key]

        versions, status = self._request(
            f"/project/{project_id}/version",
            {
                "loaders": json.dumps(["fabric"]),
                "game_versions": json.dumps([minecraft_version]),
            },
        )
        if not isinstance(versions, list) or not versions or status >= 400:
            print(f"[WARN] No versions found for project '{project_id}'")
            return None

        compatible_versions = [
            version
            for version in versions
            if isinstance(version, dict)
            and isinstance(version.get("game_versions"), list)
            and minecraft_version in version["game_versions"]
            and isinstance(version.get("loaders"), list)
            and "fabric" in version["loaders"]
        ]
        if not compatible_versions:
            print(
                f"[WARN] No Fabric versions found for project '{project_id}' "
                f"and Minecraft {minecraft_version}"
            )
            return None

        latest = max(
            compatible_versions,
            key=lambda version: version.get("date_published", ""),
        )
        self._cache.setdefault(project_id, {})[cache_key] = latest
        return latest

    def download_file(self, url: str, destination: Path) -> None:
        """Download a Modrinth file to a destination without leaving partial files."""
        parsed_url = urlparse(url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "cdn.modrinth.com":
            raise ValueError(f"Unexpected Modrinth download URL: {url}")

        temporary_path = None
        try:
            with requests.get(
                url, stream=True, timeout=(10, 60)
            ) as response:
                response.raise_for_status()
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=destination.parent,
                    prefix=f".{destination.name}.",
                    suffix=".part",
                    delete=False,
                ) as temporary_file:
                    temporary_path = Path(temporary_file.name)
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            temporary_file.write(chunk)

            if temporary_path.stat().st_size == 0:
                raise ValueError("The downloaded file is empty.")
            temporary_path.rename(destination)
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()

    def _read_fabric_metadata(self, file_path: Path) -> Dict[str, Optional[str]]:
        """Read the Fabric mod ID and installed version from a mod archive."""
        try:
            with zipfile.ZipFile(file_path) as archive:
                metadata = json.loads(archive.read("fabric.mod.json"))
        except KeyError:
            return {}
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError) as error:
            print(f"[WARN] Could not read Fabric metadata from '{file_path.name}': {error}")
            return {}

        if not isinstance(metadata, dict):
            return {}

        mod_id = metadata.get("id")
        name = metadata.get("name")
        version = metadata.get("version")
        return {
            "mod_id": mod_id if isinstance(mod_id, str) and mod_id else None,
            "name": name if isinstance(name, str) and name else None,
            "version": version if isinstance(version, str) and version else None,
        }

    def extract_mod_id(self, filename: str) -> Optional[str]:
        """Extract Modrinth mod ID from a jar/zip filename."""
        base = Path(filename).stem  # removes .jar or similar extension
        parts = base.split("-")

        if len(parts) < 2:
            print(f"[WARN] Could not infer Modrinth project ID from filename '{filename}': too few '-' segments.")
            return None

        candidate_id = parts[0].lower()
        # Validate against Modrinth ID rules (alphanumeric, hyphens/underscores allowed)
        if all(c.isalnum() or c in "-_" for c in candidate_id):
            return candidate_id

        print(
            f"[WARN] Candidate Modrinth project ID extracted from '{filename}' is invalid: '{candidate_id}'."
        )
        return None

    def scan_mods_folder(self, folder_path: Path) -> List[Dict]:
        """Scan a directory for mod files and extract their IDs."""
        mods = []
        if not folder_path.exists():
            print(f"[ERROR] Mods directory '{folder_path}' does not exist.")
            print("[ERROR] Aborting scan because the target folder could not be found.")
            sys.exit(1)

        for file in folder_path.iterdir():
            if file.is_file() and file.suffix.lower() in [".jar", ".zip"]:
                metadata = self._read_fabric_metadata(file)
                mod_id = metadata.get("mod_id") or self.extract_mod_id(file.name)
                if not mod_id:
                    print(
                        f"[WARN] Skipping '{file.name}' because no Modrinth ID could be determined from metadata or filename."
                    )
                mods.append({
                    "path": str(file),
                    "filename": file.name,
                    "mod_id": mod_id,
                    "mod_name": metadata.get("name"),
                    "current_version": metadata.get("version"),
                })

        print(f"[INFO] Found {len(mods)} potential mods in '{folder_path}'")
        if not mods:
            print("[WARN] No .jar/.zip files were found in the specified mods folder.")
        return mods

    def check_for_updates(
        self, mods: List[Dict], minecraft_version: Optional[str] = None
    ) -> Dict[str, Dict]:
        """Check each mod for available updates on Modrinth."""
        results = {}
        for mod in mods:
            if not mod["mod_id"]:
                continue

            if not minecraft_version:
                results[mod["filename"]] = {
                    "status": "unknown",
                    "current": mod.get("current_version"),
                    "details": (
                        "Could not determine the Minecraft version. "
                        "Specify --minecraft-version to check compatible releases."
                    ),
                }
                continue

            print(f"[CHECK] {mod['filename']} (ID: {mod['mod_id']})")

            # Resolve the actual Modrinth project ID from filename-derived ID
            resolved_id = MODRINTH_PROJECT_ID_OVERRIDES.get(
                mod["mod_id"].casefold()
            ) or self.resolve_project_id(mod["mod_id"], mod.get("mod_name"))
            if not resolved_id:
                results[mod["filename"]] = {
                    "status": "unknown",
                    "details": f"Could not resolve Modrinth project for '{mod['mod_id']}'"
                }
                continue

            info = self.get_mod_info(resolved_id)
            if not info or "error" in info:
                results[mod["filename"]] = {
                    "status": "unknown",
                    "details": f"Failed to fetch project info for '{resolved_id}'"
                }
                continue

            current_version = mod.get("current_version")
            latest = self.get_latest_version(resolved_id, minecraft_version)

            if not latest:
                results[mod["filename"]] = {
                    "status": "no_versions",
                    "current": current_version,
                    "details": (
                        f"No Fabric versions found for Minecraft {minecraft_version}"
                    ),
                }
                continue

            if not current_version:
                results[mod["filename"]] = {
                    "status": "unknown",
                    "details": "Could not read installed version from fabric.mod.json"
                }
                continue

            latest_version = latest.get("version_number")
            if not isinstance(latest_version, str) or not latest_version:
                results[mod["filename"]] = {
                    "status": "unknown",
                    "current": current_version,
                    "details": "Could not read the published version from Modrinth",
                }
                continue

            status = (
                "up-to-date"
                if _versions_match(current_version, latest_version, minecraft_version)
                else "update_available"
            )

            result = {
                "status": status,
                "current": current_version,
                "latest": latest_version,
                "project_id": resolved_id,
                "details": f"{status}: Current={current_version}, Latest={latest_version}"
            }
            files = latest.get("files")
            valid_files = [
                file for file in files
                if isinstance(file, dict)
                and isinstance(file.get("filename"), str)
                and file["filename"]
                and Path(file["filename"]).name == file["filename"]
                and "/" not in file["filename"]
                and "\\" not in file["filename"]
                and isinstance(file.get("url"), str)
            ] if isinstance(files, list) else []
            if valid_files:
                download_file = next(
                    (file for file in valid_files if file.get("primary") is True),
                    valid_files[0],
                )
                result["download_filename"] = download_file["filename"]
                result["download_url"] = download_file["url"]
            results[mod["filename"]] = result

        return results


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
