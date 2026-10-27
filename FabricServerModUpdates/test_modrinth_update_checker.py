import json
import tempfile
import zipfile
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

import modrinth_update_checker
from modrinth import (
    ModrinthUpdateChecker,
    detect_minecraft_version,
)
from update_operations import (
    _is_minecraft_server_command,
    install_downloaded_updates,
)


class ScanModsFolderTests(TestCase):
    def test_metadata_mod_id_takes_precedence_over_filename(self):
        checker = ModrinthUpdateChecker()
        with tempfile.TemporaryDirectory() as temporary_directory:
            mods_folder = Path(temporary_directory)
            mod_path = mods_folder / "filename-id-1.2.3.jar"
            with zipfile.ZipFile(mod_path, "w") as archive:
                archive.writestr(
                    "fabric.mod.json",
                    json.dumps(
                        {"id": "metadata_id", "name": "Example", "version": "1.2.3"}
                    ),
                )

            mods = checker.scan_mods_folder(mods_folder)

        self.assertEqual(mods[0]["mod_id"], "metadata_id")

    def test_missing_directory_raises_file_not_found_error(self):
        checker = ModrinthUpdateChecker()
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing_folder = Path(temporary_directory) / "missing-mods"

            with self.assertRaisesRegex(
                FileNotFoundError,
                "Mods directory .* does not exist",
            ):
                checker.scan_mods_folder(missing_folder)

    def test_cli_reports_missing_directory_and_exits_with_error(self):
        checker = Mock()
        checker.scan_mods_folder.side_effect = FileNotFoundError(
            "Mods directory 'missing-mods' does not exist."
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            with ExitStack() as stack:
                stack.enter_context(
                    patch(
                        "modrinth_update_checker.ModrinthUpdateChecker",
                        return_value=checker,
                    )
                )
                stack.enter_context(
                    patch(
                        "sys.argv",
                        ["modrinth_update_checker.py", temporary_directory],
                    )
                )
                output = stack.enter_context(redirect_stdout(StringIO()))

                with self.assertRaises(SystemExit) as error:
                    modrinth_update_checker.main()

            self.assertEqual(error.exception.code, 1)
            self.assertIn("[ERROR] Mods directory", output.getvalue())


class ExtractModIdTests(TestCase):
    def test_extracts_hyphenated_id_before_version_and_loader_suffixes(self):
        examples = {
            "sodium-extra-fabric-0.5.8+mc1.20.1.jar": "sodium-extra",
            "my_mod-neoforge-1.2.3-build-4.zip": "my_mod",
            "fabric-api-0.92.2+1.20.1.jar": "fabric-api",
            "examplemod-1.2.0-forge.jar": "examplemod",
            "my-custom-mod-build-42.zip": "my-custom-mod",
            "mod-mc1.20.1-fabric.jar": "mod",
            "mod-quilt.jar": "mod",
        }

        for filename, expected_id in examples.items():
            with self.subTest(filename=filename):
                self.assertEqual(
                    ModrinthUpdateChecker().extract_mod_id(filename),
                    expected_id,
                )

    def test_preserves_hyphenated_ids_without_recognized_suffixes(self):
        self.assertEqual(
            ModrinthUpdateChecker().extract_mod_id("my-custom-mod.jar"),
            "my-custom-mod",
        )

    def test_does_not_infer_id_when_filename_starts_with_a_version(self):
        checker = ModrinthUpdateChecker()
        for filename in ("1.2.3-example-mod.jar", "1-example-mod.jar"):
            with self.subTest(filename=filename):
                self.assertIsNone(checker.extract_mod_id(filename))


class ResolveProjectIdTests(TestCase):
    def test_accepts_direct_lookup_only_for_a_valid_mod_project(self):
        checker = ModrinthUpdateChecker()
        checker._request = Mock(
            return_value=(
                {"id": "actual-project-id", "project_type": "mod"},
                200,
            )
        )

        self.assertEqual(
            checker.resolve_project_id("project-slug"),
            "actual-project-id",
        )
        checker._request.assert_called_once_with("/project/project-slug")

    def test_rejects_successful_direct_lookup_for_non_mod_project(self):
        checker = ModrinthUpdateChecker()
        checker._request = Mock(
            side_effect=[
                ({"id": "resource", "project_type": "resourcepack"}, 200),
                ({"hits": []}, 200),
            ]
        )

        self.assertIsNone(checker.resolve_project_id("resource"))

    def test_does_not_match_a_similar_but_different_project_name(self):
        checker = ModrinthUpdateChecker()
        checker._request = Mock(
            side_effect=[
                ({}, 404),
                (
                    {
                        "hits": [
                            {
                                "project_id": "different-project",
                                "project_type": "mod",
                                "slug": "widget-pro",
                                "title": "Widget Pro",
                            }
                        ]
                    },
                    200,
                ),
            ]
        )

        self.assertIsNone(checker.resolve_project_id("widget", "Widget"))

    def test_returns_unknown_for_multiple_exact_name_matches(self):
        checker = ModrinthUpdateChecker()
        checker._request = Mock(
            side_effect=[
                ({}, 404),
                (
                    {
                        "hits": [
                            {
                                "project_id": "first-project",
                                "project_type": "mod",
                                "slug": "example-mod",
                                "title": "Example Mod",
                            },
                            {
                                "project_id": "second-project",
                                "project_type": "mod",
                                "slug": "example-mod-fork",
                                "title": "Example Mod",
                            },
                        ]
                    },
                    200,
                ),
            ]
        )

        self.assertIsNone(
            checker.resolve_project_id("fabric_example", "Example Mod")
        )

    def test_resolves_a_unique_exact_name_match(self):
        checker = ModrinthUpdateChecker()
        checker._request = Mock(
            side_effect=[
                ({}, 404),
                (
                    {
                        "hits": [
                            {
                                "project_id": "similar-project",
                                "project_type": "mod",
                                "slug": "example-mod-plus",
                                "title": "Example Mod Plus",
                            },
                            {
                                "project_id": "exact-project",
                                "project_type": "mod",
                                "slug": "example-mod",
                                "title": "Example Mod",
                            },
                        ]
                    },
                    200,
                ),
            ]
        )

        self.assertEqual(
            checker.resolve_project_id("fabric_example", "Example Mod"),
            "exact-project",
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


class UpdateApprovalTests(TestCase):
    def run_cli(self, options, responses=(), results=None):
        checker = Mock()
        checker.scan_mods_folder.return_value = []
        checker.check_for_updates.return_value = results or {
            "example-1.0.jar": {
                "status": "update_available",
                "current": "1.0",
                "latest": "2.0",
            }
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            with ExitStack() as stack:
                stack.enter_context(
                    patch(
                        "modrinth_update_checker.ModrinthUpdateChecker",
                        return_value=checker,
                    )
                )
                stack.enter_context(
                    patch(
                        "modrinth_update_checker.detect_minecraft_version",
                        return_value="1.21.4",
                    )
                )
                stack.enter_context(
                    patch(
                        "modrinth_update_checker.is_minecraft_server_running",
                        return_value=False,
                    )
                )
                downloads = stack.enter_context(
                    patch("modrinth_update_checker.download_updates")
                )
                installs = stack.enter_context(
                    patch("modrinth_update_checker.install_downloaded_updates")
                )
                prompt = stack.enter_context(
                    patch("builtins.input", side_effect=responses)
                )
                stack.enter_context(
                    patch(
                        "sys.argv",
                        [
                            "modrinth_update_checker.py",
                            temporary_directory,
                            *options,
                        ],
                    )
                )
                modrinth_update_checker.main()

        return downloads, installs, prompt

    def test_download_is_skipped_when_user_declines(self):
        downloads, installs, prompt = self.run_cli(["--download"], ["n"])

        downloads.assert_not_called()
        installs.assert_not_called()
        prompt.assert_called_once()
        self.assertIn("download", prompt.call_args.args[0])

    def test_download_and_install_are_not_attempted_when_no_updates_exist(self):
        downloads, installs, prompt = self.run_cli(
            ["--download", "--install"],
            results={"example-1.0.jar": {"status": "up-to-date", "current": "1.0"}},
        )

        downloads.assert_not_called()
        installs.assert_not_called()
        prompt.assert_not_called()

    def test_empty_response_reprompts_until_user_answers(self):
        downloads, _, prompt = self.run_cli(["--download"], ["", "y"])

        downloads.assert_called_once()
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(
            prompt.call_args_list[0].args[0],
            prompt.call_args_list[1].args[0],
        )

    def test_install_requires_separate_download_and_install_approvals(self):
        downloads, installs, prompt = self.run_cli(
            ["--install"], ["y", "n"]
        )

        downloads.assert_called_once()
        installs.assert_not_called()
        self.assertEqual(prompt.call_count, 2)
        self.assertIn("download", prompt.call_args_list[0].args[0])
        self.assertIn("install", prompt.call_args_list[1].args[0])

    def test_yes_option_bypasses_both_prompts(self):
        downloads, installs, prompt = self.run_cli(["--install", "-y"])

        downloads.assert_called_once()
        installs.assert_called_once()
        prompt.assert_not_called()


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
