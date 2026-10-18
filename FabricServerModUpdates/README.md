# Modrinth Update Checker

`modrinth_update_checker.py` checks a Minecraft Fabric server's `mods` folder
for newer mod versions available on Modrinth. It reads each mod's installed
version from its `fabric.mod.json` metadata, looks up the corresponding Modrinth
project, and compares that version with the latest published Fabric version.

By default, the script only reports results; it does not download, install, or
replace mods. It checks `.jar` and `.zip` files directly inside the folder you
provide; it does not search subfolders.

## Requirements

- Python
- Packages listed in [`requirements.txt`](requirements.txt)
- Internet access to query the Modrinth API

## Install

From this directory, create and activate a virtual environment if desired, then
install the dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

On macOS or Linux, activate the virtual environment with:

```sh
source .venv/bin/activate
```

## Run

Pass the path to the server's `mods` folder:

```powershell
py .\modrinth_update_checker.py "C:\Minecraft\FabricServer\mods"
```

The path can be relative or absolute. To include summary counts in the report,
add `--summary` (or `-s`):

```powershell
py .\modrinth_update_checker.py "C:\Minecraft\FabricServer\mods" --summary
```

To download available updates without changing the server's `mods` folder, add
`--download`. Files are saved to a sibling folder named
`<mods-folder>_updates` by default (for example, `mods_updates`), and existing
files in that destination are not overwritten:

```powershell
py .\modrinth_update_checker.py "C:\Minecraft\FabricServer\mods" --download
```

The checker filters releases to the server's Minecraft version. It tries to
detect that version from `version.json` in a server JAR directly beside the
`mods` folder. If detection is unavailable or ambiguous, specify the version
explicitly; without a known version, updates are not reported or downloaded:

```powershell
py .\modrinth_update_checker.py "C:\Minecraft\FabricServer\mods" `
  --minecraft-version 1.21.4 --download
```

Choose a different destination with `--download-dir`:

```powershell
py .\modrinth_update_checker.py "C:\Minecraft\FabricServer\mods" --download `
  --download-dir "C:\Minecraft\FabricServer\pending-mod-updates"
```

Only updates reported as available for the target Minecraft version are
downloaded. For each update, the primary file from the latest compatible Fabric
version is selected, or the first file if Modrinth does not mark one as primary.
The original installed mod remains unchanged.

Use `--help` to see the command-line options:

```powershell
py .\modrinth_update_checker.py --help
```

## Project lookup and version detection

For each archive, the checker first reads the mod ID, display name, and installed
version from `fabric.mod.json`. If the metadata does not provide a mod ID, it
tries the first part of the filename before a hyphen (for example,
`examplemod-1.2.0.jar` gives the candidate ID `examplemod`). It then resolves
that candidate to a Modrinth mod project, using the metadata name as a search
hint where available. One project ID override is currently configured for
`treeharvester`. Modrinth versions are filtered by both the target Minecraft
version and the Fabric loader.

The installed version must be present in `fabric.mod.json` for the checker to
compare versions. Project lookup requires a matching Modrinth mod project, and
only Fabric versions are considered. When comparing versions, the checker
ignores the selected Minecraft version and known loader labels that Modrinth may
include in its published version string.

## Authentication and rate limits

An API token is optional for these read-only API requests. If needed, provide
one with `--token` or set the `MODRINTH_API_TOKEN` environment variable:

```powershell
py .\modrinth_update_checker.py "C:\Minecraft\FabricServer\mods" --token "YOUR_TOKEN"
$env:MODRINTH_API_TOKEN = "YOUR_TOKEN"
py .\modrinth_update_checker.py "C:\Minecraft\FabricServer\mods"
```

Avoid putting a token directly in shell history or shared scripts. If Modrinth
rate-limits a request, the checker waits for the indicated interval and retries
once.

## Reading the report

- **Updates** — a newer Fabric version was found on Modrinth.
- **Up-to-date** — the installed version matches the latest published Fabric
  version.
- **Warnings / issues** — project lookup failed, metadata was unavailable, or
  no Fabric versions were found. Check the printed details for the specific
  reason.

The `--summary` option prints the detailed report as usual and adds totals for
updates, up-to-date mods, and issues/unknown results.
