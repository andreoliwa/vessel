# Copyright (c) 2026 W. Augusto Andreoli
# ruff: noqa: D102, PT009, PT027
"""Check due maintenance using synthetic responses and fixed calendar dates."""

from __future__ import annotations

import json
import os
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import typer.main
from click.testing import CliRunner
from homebox import tasks

from vessel.homebox import HomeBoxAPI, HomeBoxError
from vessel.main import app

ITEM_ID = "11111111-1111-4111-8111-111111111111"
BASE_URL = "https://inventory.example.org/homebox"


def entry(scheduled: str, completed: str = "", title: str = "Example item") -> dict:
    """Mirror a maintenance entry without real inventory or credentials."""
    return {
        "id": "22222222-2222-4222-8222-222222222222",
        "itemID": ITEM_ID,
        "itemName": title,
        "name": "Example maintenance",
        "description": "Check the example item.",
        "cost": "0",
        "scheduledDate": scheduled,
        "completedDate": completed,
    }


class DueMaintenanceTests(unittest.TestCase):
    """Catch date-boundary, sorting, formatting, and API validation mistakes."""

    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ, {"HOMEBOX_API_KEY": "synthetic-token", "HOMEBOX_URL": BASE_URL, "COLUMNS": "240"}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def rows(self, entries: object) -> list:
        """Keep date arithmetic real while replacing only the HTTP read."""
        with patch.object(HomeBoxAPI, "_read", return_value=json.dumps(entries).encode()):
            return tasks.due_maintenance_rows(HomeBoxAPI(), date(2026, 3, 2))

    def test_filters_sorts_and_counts_calendar_days(self) -> None:
        rows = self.rows(
            [
                entry("2026-03-02", title="Due today"),
                entry("2026-03-03", title="Future"),
                entry("2026-03-01", completed="2026-03-01", title="Completed"),
                entry("", title="Unscheduled"),
                entry("2026-03-01", title="Due yesterday"),
                entry("2026-02-27", title="Due last month"),
            ]
        )
        link = f"{BASE_URL}/item/{ITEM_ID}"
        self.assertEqual(
            rows,
            [
                (link, "Due last month", "2026-02-27", "3 days"),
                (link, "Due yesterday", "2026-03-01", "1 day"),
                (link, "Due today", "2026-03-02", "Today"),
            ],
        )

    def test_keeps_multiple_due_entries_for_same_item(self) -> None:
        rows = self.rows([entry("2026-03-01"), entry("2026-03-02")])
        self.assertEqual(len(rows), 2)

    def test_invalid_dates_and_item_data_fail_cleanly(self) -> None:
        for field, value in (
            ("scheduledDate", "2026-02-30"),
            ("scheduledDate", 123),
            ("itemID", "bad"),
            ("itemName", None),
        ):
            record = entry("2026-03-01")
            record[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(HomeBoxError):
                self.rows([record])

    def test_invalid_response_shapes_fail_cleanly(self) -> None:
        body: object
        for body in ({}, None, ["bad"]):
            with self.subTest(body=body), self.assertRaises(HomeBoxError):
                self.rows(body)
        with patch.object(HomeBoxAPI, "_read", return_value=b"not json"), self.assertRaises(HomeBoxError):
            tasks.due_maintenance_rows(HomeBoxAPI(), date(2026, 3, 2))

    def test_empty_results(self) -> None:
        self.assertEqual(self.rows([]), [])

    def test_titles_cannot_break_table_rows(self) -> None:
        self.assertEqual(self.rows([entry("2026-03-01", title="Example\nitem\tname")])[0][1], "Example item name")

    def test_cli_displays_links_dates_and_overdue_column(self) -> None:
        today = date.today()  # noqa: DTZ011 - Match the command's local calendar date.
        records = [entry(today.isoformat()), entry((today - timedelta(days=1)).isoformat(), title="Yesterday")]
        with patch.object(HomeBoxAPI, "_read", return_value=json.dumps(records).encode()):
            result = CliRunner().invoke(typer.main.get_command(app), ["homebox", "maintenance-due"])
        self.assertEqual(result.exit_code, 0, result.output)
        for text in (
            "Item link",
            "Item title",
            "Maintenance date",
            "Overdue",
            "Today",
            "1 day",
            today.isoformat(),
            f"{BASE_URL}/item/{ITEM_ID}",
        ):
            self.assertIn(text, result.output)
        self.assertLess(result.output.index("Yesterday"), result.output.index("Example item"))

    def test_json_output_contains_only_structured_data(self) -> None:
        today = date.today()  # noqa: DTZ011 - Match the command's local calendar date.
        records = [entry(today.isoformat(), title="Example [bold]café[/bold]")]
        with patch.object(HomeBoxAPI, "_read", return_value=json.dumps(records).encode()):
            result = CliRunner().invoke(typer.main.get_command(app), ["homebox", "maintenance-due", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            json.loads(result.stdout),
            [
                {
                    "item_url": f"{BASE_URL}/item/{ITEM_ID}",
                    "item_title": "Example [bold]café[/bold]",
                    "maintenance_date": today.isoformat(),
                    "overdue": "Today",
                }
            ],
        )

    def test_json_empty_results_are_an_empty_array(self) -> None:
        with patch.object(HomeBoxAPI, "_read", return_value=b"[]"):
            result = CliRunner().invoke(typer.main.get_command(app), ["homebox", "maintenance-due", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout), [])

    def test_json_dry_run_is_parseable_without_http_or_credentials(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = CliRunner().invoke(typer.main.get_command(app), ["--dry", "homebox", "maintenance-due", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout), [])
        self.assertIn("DRY-RUN", result.stderr)

    def test_rich_table_preserves_literal_item_titles(self) -> None:
        records = [entry("2026-03-01", title="Example [bold]café[/bold]")]
        with patch.object(HomeBoxAPI, "_read", return_value=json.dumps(records).encode()):
            result = CliRunner().invoke(typer.main.get_command(app), ["homebox", "maintenance-due"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Example [bold]café[/bold]", result.output)
        self.assertIn("┏", result.output)
        self.assertIn("┃", result.output)

    def test_cli_empty_results_are_clear(self) -> None:
        with patch.object(HomeBoxAPI, "_read", return_value=b"[]"):
            result = CliRunner().invoke(typer.main.get_command(app), ["homebox", "maintenance-due"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("No maintenance due", result.output)

    def test_dry_run_needs_no_credentials_or_http(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = CliRunner().invoke(typer.main.get_command(app), ["--dry", "homebox", "maintenance-due"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("DRY-RUN", result.output)

    def test_cli_missing_credentials_and_api_errors_are_clean(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = CliRunner().invoke(typer.main.get_command(app), ["homebox", "maintenance-due"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("HOMEBOX_API_KEY", result.output)
        with patch.object(HomeBoxAPI, "_read", side_effect=HomeBoxError("HomeBox unavailable.")):
            result = CliRunner().invoke(typer.main.get_command(app), ["homebox", "maintenance-due"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Error: HomeBox unavailable.", result.output)
