import json
import tempfile
import zipfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from modrinth import (
    ModrinthUpdateChecker,
    detect_minecraft_version,
)
from update_operations import (
    _is_minecraft_server_command,
    install_downloaded_updates,
)


class GetLatestVersionTests(TestCase):
    def test_selects_latest_fabric_release_for_requested_minecraft_version(self):
        releases = [
            {
                "version_number": "old-mc",
                "date_published": "2025-01-01",
                "game_versions": ["1.20.1"],
                "loaders": ["fabric"],
            },
            {
                "version_number": "new-mc",
                "date_published": "2026-10-01",
                "game_versions": ["26.4"],
                "loaders": ["fabric"],
            },
            {
                "version_number": "forge-only",
                "date_published": "2026-11-01",
                "game_versions": ["1.20.1"],
                "loaders": ["forge"],
            },
        ]
        checker = ModrinthUpdateChecker()
        checker._request = Mock(return_value=(releases, 200))

        selected = checker.get_latest_version("fabric-api", "1.20.1")

        self.assertEqual(selected["version_number"], "old-mc")
        checker._request.assert_called_once_with(
            "/project/fabric-api/version",
            {"loaders": '["fabric"]', "game_versions": '["1.20.1"]'},
        )

    def test_cache_is_separate_for_each_minecraft_version(self):
        releases = [
            {
                "version_number": "old-mc",
                "date_published": "2025-01-01",
                "game_versions": ["1.20.1"],
                "loaders": ["fabric"],
            },
            {
                "version_number": "new-mc",
                "date_published": "2026-10-01",
                "game_versions": ["26.4"],
                "loaders": ["fabric"],
            },
        ]
        checker = ModrinthUpdateChecker()
        checker._request = Mock(return_value=(releases, 200))

        self.assertEqual(
            checker.get_latest_version("fabric-api", "1.20.1")["version_number"],
            "old-mc",
        )
        self.assertEqual(
            checker.get_latest_version("fabric-api", "26.4")["version_number"],
            "new-mc",
        )
        self.assertEqual(checker._request.call_count, 2)

    def test_does_not_select_release_for_another_minecraft_version(self):
        checker = ModrinthUpdateChecker()
        checker._request = Mock(
            return_value=(
                [
                    {
                        "version_number": "wrong-mc",
                        "date_published": "2026-10-01",
                        "game_versions": ["26.4"],
                        "loaders": ["fabric"],
                    }
                ],
                200,
            )
        )

        self.assertIsNone(checker.get_latest_version("fabric-api", "1.20.1"))


class InstalledVersionComparisonTests(TestCase):
    def test_ignores_minecraft_and_loader_labels_in_release_version(self):
        matching_versions = (
            ("26.3.159", "26.3.159+fabric"),
            ("8.58", "26.3.0-8.58-fabric+forge+neo"),
            ("9.0.0", "9.0.0-fabric"),
            ("0.26.2+mc26.3", "mc26.3-0.26.2-fabric"),
            ("26.3-v2.21.0", "Fabric-26.3-v2.21.0"),
            ("5.7", "26.3.0-5.7-fabric+forge+neo"),
            ("1.10.187", "1.10.187-fabric"),
            ("10.3", "26.3.0-10.3-fabric+forge+neo"),
        )
        checker = ModrinthUpdateChecker()
        checker.resolve_project_id = Mock(return_value="project")
        checker.get_mod_info = Mock(return_value={"id": "project"})

        for index, (installed, published) in enumerate(matching_versions):
            with self.subTest(installed=installed, published=published):
                checker.get_latest_version = Mock(
                    return_value={"version_number": published}
                )
                results = checker.check_for_updates(
                    [
                        {
                            "filename": f"mod-{index}.jar",
                            "mod_id": "mod",
                            "current_version": installed,
                        }
                    ],
                    "26.3",
                )

                self.assertEqual(results[f"mod-{index}.jar"]["status"], "up-to-date")

    def test_still_reports_a_different_mod_version_as_an_update(self):
        checker = ModrinthUpdateChecker()
        checker.resolve_project_id = Mock(return_value="project")
        checker.get_mod_info = Mock(return_value={"id": "project"})
        checker.get_latest_version = Mock(
            return_value={"version_number": "1.2.1-fabric"}
        )

        results = checker.check_for_updates(
            [
                {
                    "filename": "mod-1.2.0.jar",
                    "mod_id": "mod",
                    "current_version": "1.2.0",
                }
            ],
            "26.3",
        )

        self.assertEqual(results["mod-1.2.0.jar"]["status"], "update_available")


class DetectMinecraftVersionTests(TestCase):
    def test_reads_version_from_server_jar(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            server_dir = Path(temporary_directory)
            with zipfile.ZipFile(server_dir / "server.jar", "w") as archive:
                archive.writestr("version.json", json.dumps({"id": "1.21.4"}))

            self.assertEqual(detect_minecraft_version(server_dir), "1.21.4")

    def test_returns_none_when_server_jars_have_conflicting_versions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            server_dir = Path(temporary_directory)
            for filename, version in (
                ("server.jar", "1.21.4"),
                ("fabric-server-launch.jar", "1.20.1"),
            ):
                with zipfile.ZipFile(server_dir / filename, "w") as archive:
                    archive.writestr("version.json", json.dumps({"id": version}))

            self.assertIsNone(detect_minecraft_version(server_dir))


class MinecraftServerProcessTests(TestCase):
    def test_recognizes_common_server_launch_commands(self):
        self.assertTrue(
            _is_minecraft_server_command(
                "java -Xmx4G -jar C:\\Minecraft\\fabric-server-launch.jar nogui"
            )
        )
        self.assertTrue(
            _is_minecraft_server_command(
                "java net.minecraft.server.Main --nogui"
            )
        )

    def test_does_not_treat_a_client_process_as_a_server(self):
        self.assertFalse(
            _is_minecraft_server_command(
                "java -cp minecraft-client.jar net.minecraft.client.main.Main"
            )
        )

    def test_recognizes_server_jar_names_from_the_server_directory(self):
        self.assertTrue(
            _is_minecraft_server_command(
                "java -Xmx4G -jar C:\\Minecraft\\custom-launcher.jar",
                {"custom-launcher.jar"},
            )
        )


class InstallDownloadedUpdatesTests(TestCase):
    @staticmethod
    def _write_mod(path: Path, mod_id: str, version: str) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "fabric.mod.json",
                json.dumps({"id": mod_id, "version": version}),
            )

    def test_backs_up_replaced_mod_and_keeps_unrelated_mods(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            server_dir = Path(temporary_directory)
            mods_folder = server_dir / "mods"
            download_dir = server_dir / "mods_updates"
            mods_folder.mkdir()
            download_dir.mkdir()
            old_mod = mods_folder / "example-1.0.jar"
            untouched_mod = mods_folder / "unrelated.jar"
            downloaded_mod = download_dir / "example-2.0.jar"
            self._write_mod(old_mod, "example", "1.0")
            untouched_mod.write_bytes(b"untouched")
            self._write_mod(downloaded_mod, "example", "2.0")
            old_contents = old_mod.read_bytes()

            with patch(
                "update_operations.is_minecraft_server_running",
                return_value=False,
            ):
                install_downloaded_updates(
                    ModrinthUpdateChecker(),
                    mods_folder,
                    download_dir,
                    {
                        "example-1.0.jar": {
                            "status": "update_available",
                            "latest": "2.0",
                            "download_filename": "example-2.0.jar",
                        }
                    },
                    "1.21.4",
                )

            self.assertFalse(old_mod.exists())
            self.assertEqual(untouched_mod.read_bytes(), b"untouched")
            self.assertTrue((mods_folder / "example-2.0.jar").is_file())
            self.assertFalse(downloaded_mod.exists())
            backup_dirs = list(server_dir.glob("mods_backup_*"))
            self.assertEqual(len(backup_dirs), 1)
            self.assertEqual(
                (backup_dirs[0] / "example-1.0.jar").read_bytes(),
                old_contents,
            )

    def test_does_not_change_files_when_server_is_running(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            server_dir = Path(temporary_directory)
            mods_folder = server_dir / "mods"
            download_dir = server_dir / "mods_updates"
            mods_folder.mkdir()
            download_dir.mkdir()
            old_mod = mods_folder / "example-1.0.jar"
            downloaded_mod = download_dir / "example-2.0.jar"
            self._write_mod(old_mod, "example", "1.0")
            self._write_mod(downloaded_mod, "example", "2.0")
            old_contents = old_mod.read_bytes()
            download_contents = downloaded_mod.read_bytes()

            with patch(
                "update_operations.is_minecraft_server_running",
                side_effect=[False, False, True],
            ) as process_check:
                with self.assertRaisesRegex(RuntimeError, "server appears to be running"):
                    install_downloaded_updates(
                        ModrinthUpdateChecker(),
                        mods_folder,
                        download_dir,
                        {
                            "example-1.0.jar": {
                                "status": "update_available",
                                "latest": "2.0",
                                "download_filename": "example-2.0.jar",
                            }
                        },
                        "1.21.4",
                    )

            self.assertEqual(process_check.call_count, 3)
            self.assertEqual(old_mod.read_bytes(), old_contents)
            self.assertEqual(downloaded_mod.read_bytes(), download_contents)
            self.assertEqual(list(server_dir.glob("mods_backup_*")), [])

    def test_rejects_stale_download_before_moving_any_mods(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            server_dir = Path(temporary_directory)
            mods_folder = server_dir / "mods"
            download_dir = server_dir / "mods_updates"
            mods_folder.mkdir()
            download_dir.mkdir()
            old_mod = mods_folder / "example-1.0.jar"
            downloaded_mod = download_dir / "example-2.0.jar"
            self._write_mod(old_mod, "example", "1.0")
            self._write_mod(downloaded_mod, "example", "1.5")
            old_contents = old_mod.read_bytes()

            with patch(
                "update_operations.is_minecraft_server_running",
                return_value=False,
            ):
                with self.assertRaisesRegex(ValueError, "does not match the expected"):
                    install_downloaded_updates(
                        ModrinthUpdateChecker(),
                        mods_folder,
                        download_dir,
                        {
                            "example-1.0.jar": {
                                "status": "update_available",
                                "latest": "2.0",
                                "download_filename": "example-2.0.jar",
                            }
                        },
                        "1.21.4",
                    )

            self.assertEqual(old_mod.read_bytes(), old_contents)
            self.assertTrue(downloaded_mod.is_file())
            self.assertEqual(list(server_dir.glob("mods_backup_*")), [])
