# Copyright (c) 2026 W. Augusto Andreoli
# Standard unittest assertions keep this suite consistent with the reader tests.
# ruff: noqa: D102, PT009
"""Exercise the discovered CLI with synthetic inventory and no live credentials."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import typer.main
from click.testing import CliRunner

from tests.test_homebox_export import ITEM_ID, SyntheticAPI
from vessel.homebox import HomeBoxError
from vessel.main import app


class ExportCommandTests(unittest.TestCase):
    """Verify options, dry runs, real package output, and clean errors."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "export"
        self.runner = CliRunner()
        self.command = typer.main.get_command(app)
        self.arguments = ["homebox", "export", "--item", ITEM_ID, "--output", str(self.output)]

    def test_help_lists_options_and_browse(self) -> None:
        result = self.runner.invoke(self.command, ["homebox", "export", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("--item", result.output)
        self.assertIn("--output", result.output)
        group = self.runner.invoke(self.command, ["homebox", "--help"])
        self.assertIn("browse", group.output)

    def test_no_item_exports_all_sell_items(self) -> None:
        with patch("vessel.homebox.HomeBoxAPI", return_value=SyntheticAPI()):
            result = self.runner.invoke(self.command, ["homebox", "export", "--output", str(self.output)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(json.loads((self.output / "inventory.json").read_text())["items"]), 1)

    def test_repeated_and_comma_separated_items_export_each_id_once(self) -> None:
        second_id = "77777777-7777-4777-8777-777777777777"
        for selection in (
            ["--item", f"{ITEM_ID}, {second_id}"],
            ["--item", ITEM_ID, "--item", second_id],
            ["--item", f"{ITEM_ID},{second_id}", "--item", ITEM_ID],
        ):
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as directory:
                api = SyntheticAPI()
                second = json.loads(json.dumps(api.item))
                second["id"] = second_id
                second["attachments"] = []
                api.extra_items[second_id] = second
                output = Path(directory) / "export"
                with patch("vessel.homebox.HomeBoxAPI", return_value=api):
                    result = self.runner.invoke(
                        self.command, ["homebox", "export", *selection, "--output", str(output)]
                    )
                self.assertEqual(result.exit_code, 0, result.output)
                payload = json.loads((output / "inventory.json").read_text())
                self.assertEqual([item["id"] for item in payload["items"]], [ITEM_ID, second_id])
                self.assertEqual(api.item_reads, 2)

    def test_batch_dry_run_needs_no_credentials_or_requests(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("vessel.homebox.HomeBoxAPI") as api:
            result = self.runner.invoke(self.command, ["--dry", "homebox", "export", "--output", str(self.output)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("sell", result.output)
        api.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_bad_id_anywhere_in_selection_fails_before_api(self) -> None:
        for value in (f"{ITEM_ID},bad", f"{ITEM_ID},", "", f"{ITEM_ID},,{ITEM_ID}"):
            with self.subTest(value=value), patch("vessel.homebox.HomeBoxAPI") as api:
                result = self.runner.invoke(
                    self.command, ["homebox", "export", "--item", value, "--output", str(self.output)]
                )
                self.assertNotEqual(result.exit_code, 0)
                self.assertIn("UUID", result.output)
                api.assert_not_called()

    def test_command_writes_complete_package(self) -> None:
        with patch("vessel.homebox.HomeBoxAPI", return_value=SyntheticAPI()):
            result = self.runner.invoke(self.command, self.arguments)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(str(self.output), result.output)
        self.assertTrue((self.output / "inventory.json").is_file())
        self.assertTrue((self.output / "listings" / f"{ITEM_ID}.de.txt").is_file())

    def test_dry_run_needs_no_credentials_and_writes_nothing(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("vessel.homebox.HomeBoxAPI") as api:
            result = self.runner.invoke(self.command, ["--dry", *self.arguments])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("DRY-RUN", result.output)
        api.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_missing_options_and_bad_uuid_fail_before_api(self) -> None:
        for arguments in (["homebox", "export"], ["homebox", "export", "--item", "bad", "--output", str(self.output)]):
            with self.subTest(arguments=arguments), patch("vessel.homebox.HomeBoxAPI") as api:
                result = self.runner.invoke(self.command, arguments)
                self.assertNotEqual(result.exit_code, 0)
                self.assertIn("Error:", result.output)
                api.assert_not_called()

    def test_nonempty_destination_rejected_before_api(self) -> None:
        self.output.mkdir()
        existing = self.output / "keep.txt"
        existing.write_text("keep")
        with patch("vessel.homebox.HomeBoxAPI") as api:
            result = self.runner.invoke(self.command, self.arguments)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("empty", result.output)
        api.assert_not_called()
        self.assertEqual(existing.read_text(), "keep")

    def test_api_failure_is_clean_error(self) -> None:
        with patch("vessel.homebox.HomeBoxAPI", side_effect=HomeBoxError("HomeBox unavailable.")):
            result = self.runner.invoke(self.command, self.arguments)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Error: HomeBox unavailable.", result.output)
        self.assertFalse(self.output.exists())

    def test_missing_credentials_is_clean_error(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = self.runner.invoke(self.command, self.arguments)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("HOMEBOX_API_KEY", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertFalse(self.output.exists())

    def test_destination_access_failure_is_clean_error(self) -> None:
        self.output.mkdir()
        with (
            patch("pathlib.Path.iterdir", side_effect=PermissionError("private path detail")),
            patch("vessel.homebox.HomeBoxAPI") as api,
        ):
            result = self.runner.invoke(self.command, self.arguments)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Error:", result.output)
        self.assertIn("destination", result.output)
        self.assertNotIn("private path detail", result.output)
        api.assert_not_called()
