#!/usr/bin/env python3
"""
Modrinth Update Checker for Minecraft Fabric/Java Mods
Scans a mods folder and queries Modrinth API to detect version updates.
No authentication required for read-only operations like this.
"""


import os
import sys
import json
import argparse
import time
import zipfile
from pathlib import Path
from typing import Optional, Dict, List, Any
from functools import lru_cache
import requests


MODRINTH_PROJECT_ID_OVERRIDES = {
    "treeharvester": "abooMhox",
}



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

    def get_latest_version(self, project_id: str) -> Optional[Dict]:
        """Fetch the latest version of a mod from Modrinth API."""
        # Check cache first
        if project_id in self._cache and "latest" in self._cache[project_id]:
            return self._cache[project_id]["latest"]

        versions, status = self._request(
            f"/project/{project_id}/version",
            {"loaders": json.dumps(["fabric"])},
        )
        if not isinstance(versions, list) or not versions or status >= 400:
            print(f"[WARN] No versions found for project '{project_id}'")
            return None

        latest = max(versions, key=lambda version: version.get("date_published", ""))
        # Cache the latest version info
        self._cache.setdefault(project_id, {})["latest"] = latest
        return latest

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

    def check_for_updates(self, mods: List[Dict]) -> Dict[str, Dict]:
        """Check each mod for available updates on Modrinth."""
        results = {}
        for mod in mods:
            if not mod["mod_id"]:
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
            latest = self.get_latest_version(resolved_id)

            if not latest:
                results[mod["filename"]] = {
                    "status": "no_versions",
                    "current": current_version,
                    "details": "No versions found on Modrinth"
                }
                continue

            if not current_version:
                results[mod["filename"]] = {
                    "status": "unknown",
                    "details": "Could not read installed version from fabric.mod.json"
                }
                continue

            latest_version = latest.get("version_number")
            status = "up-to-date" if latest_version == current_version else "update_available"

            results[mod["filename"]] = {
                "status": status,
                "current": current_version,
                "latest": latest_version,
                "project_id": resolved_id,
                "details": f"{status}: Current={current_version}, Latest={latest_version}"
            }

        return results


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
    parser.add_argument("-t", "--token", help="Modrinth API token (optional).")
    parser.add_argument("-s", "--summary", action="store_true", help="Show only a summary report.")

    args = parser.parse_args()

    if not args.mods_folder:
        print("[ERROR] Please specify the mods folder path.")
        sys.exit(1)

    # Token is optional for read-only operations like this
    checker = ModrinthUpdateChecker(api_token=args.token)
    mods = checker.scan_mods_folder(Path(args.mods_folder))
    results = checker.check_for_updates(mods)
    print_results(results, summary_only=args.summary)


if __name__ == "__main__":
    main()
