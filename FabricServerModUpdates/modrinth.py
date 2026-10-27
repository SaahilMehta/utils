"""Modrinth API access and mod version/update checking."""

import json
import os
import re
import tempfile
import time
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional
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
            raise FileNotFoundError(
                f"Mods directory '{folder_path}' does not exist."
            )

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
