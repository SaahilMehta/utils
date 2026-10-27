"""Shared validation and metadata helpers for Fabric mod archives."""

import json
import re
import zipfile
from pathlib import Path
from typing import Dict, Optional

MODRINTH_LOADER_LABELS = {"fabric", "forge", "neoforge", "neo", "quilt"}


def is_safe_filename(filename: object) -> bool:
    """Return whether a value is a single, non-empty filename."""
    return (
        isinstance(filename, str)
        and bool(filename)
        and Path(filename).name == filename
        and "/" not in filename
        and "\\" not in filename
    )


def read_fabric_metadata(file_path: Path) -> Dict[str, Optional[str]]:
    """Read the Fabric mod ID, name, and version from an archive."""
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


def versions_match(
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
