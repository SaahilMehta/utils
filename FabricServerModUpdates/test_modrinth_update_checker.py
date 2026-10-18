import json
import tempfile
import zipfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

from modrinth_update_checker import ModrinthUpdateChecker, detect_minecraft_version


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
