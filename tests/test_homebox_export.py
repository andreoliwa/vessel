# Copyright (c) 2026 W. Augusto Andreoli
# unittest keeps the suite dependency-free beyond the application image library.
# ruff: noqa: D102, PT009, PT027
"""Exercise package contents and failure cleanup using synthetic inventory only."""

from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from homebox.tasks import export_items
from PIL import Image, PngImagePlugin

from tests.test_homebox import sample_item
from vessel.homebox import HomeBoxAPI, HomeBoxError

ITEM_ID = "11111111-1111-4111-8111-111111111111"
PHOTO_ID = "44444444-4444-4444-8444-444444444444"
DOCUMENT_ID = "55555555-5555-4555-8555-555555555555"


def sample_photo() -> bytes:
    """Create a tiny rotated photo with synthetic embedded author metadata."""
    image = Image.new("RGB", (3, 2), "red")
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = "SYNTHETIC AUTHOR"
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "SYNTHETIC LOCATION")
    output = io.BytesIO()
    image.save(output, format="PNG", exif=exif, pnginfo=info)
    return output.getvalue()


class SyntheticAPI(HomeBoxAPI):
    """Replace only the external API boundary; use real listing validation."""

    def __init__(self) -> None:
        """Keep synthetic inventory and attachment responses in memory."""
        self.item = sample_item()
        self.item["attachments"][0]["mimeType"] = "image/png"
        self.item["attachments"][0]["path"] = "../../untrusted-name.png"
        self.attachments = {PHOTO_ID: sample_photo(), DOCUMENT_ID: b"%PDF-synthetic public document"}
        self.reads: list[str] = []
        self.item_reads = 0
        self.extra_items: dict[str, dict] = {}
        self.summaries = [{"id": ITEM_ID, "tags": [{"name": "sell"}]}]
        self.fail_attachment: str | None = None

    def list_items(self) -> list[dict]:
        """Return native summaries, including items that must not be exported."""
        return copy.deepcopy(self.summaries)

    def read_item(self, item_id: str) -> dict:
        """Return native synthetic data; the export task owns listing validation."""
        self.item_reads += 1
        return copy.deepcopy(self.item if item_id == ITEM_ID else self.extra_items[item_id])

    def read_attachment(self, item_id: str, attachment_id: str) -> bytes:
        """Serve only native attachment IDs, never source URLs or filenames."""
        if item_id != ITEM_ID:
            message = "Unexpected synthetic attachment item"
            raise AssertionError(message)
        self.reads.append(attachment_id)
        if attachment_id == self.fail_attachment:
            message = "Synthetic attachment unavailable"
            raise HomeBoxError(message)
        return self.attachments[attachment_id]


class ExportItemTests(unittest.TestCase):
    """Prove complete publication, safe failures, and stable public output."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "export"
        self.api = SyntheticAPI()

    def test_batch_exports_only_sell_tagged_items_in_one_package(self) -> None:
        second_id = "77777777-7777-4777-8777-777777777777"
        second = sample_item()
        second["id"] = second_id
        second["attachments"] = []
        self.api.extra_items[second_id] = second
        self.api.summaries.extend(
            [
                {"id": second_id, "tags": [{"name": "sell"}]},
                {"id": "88888888-8888-4888-8888-888888888888", "tags": []},
                {"id": "99999999-9999-4999-8999-999999999999", "tags": [{"name": "donate"}]},
            ]
        )
        export_items(self.api, [], self.output)
        payload = json.loads((self.output / "inventory.json").read_text())
        self.assertEqual([item["id"] for item in payload["items"]], [ITEM_ID, second_id])
        self.assertTrue((self.output / "listings" / f"{second_id}.en.txt").is_file())
        self.assertEqual(self.api.item_reads, 2)

    def test_batch_with_no_sell_items_writes_nothing(self) -> None:
        self.api.summaries = [{"id": ITEM_ID, "tags": [{"name": "donate"}]}]
        with self.assertRaisesRegex(HomeBoxError, "No.*sell"):
            export_items(self.api, [], self.output)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.api.item_reads, 0)

    def test_batch_failure_does_not_publish_earlier_items(self) -> None:
        second_id = "77777777-7777-4777-8777-777777777777"
        second = sample_item()
        second["id"] = second_id
        second["attachments"][0]["id"] = "bad"
        self.api.extra_items[second_id] = second
        self.api.summaries.append({"id": second_id, "tags": [{"name": "sell"}]})
        with self.assertRaises(HomeBoxError):
            export_items(self.api, [], self.output)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_batch_rechecks_sell_tag_on_complete_item(self) -> None:
        self.api.item["tags"] = [{"name": "donate"}]
        with self.assertRaisesRegex(HomeBoxError, "sell"):
            export_items(self.api, [], self.output)
        self.assertEqual(self.api.reads, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_complete_package_preserves_public_data_and_exact_copy(self) -> None:
        original = copy.deepcopy(self.api.item)
        result = export_items(self.api, [ITEM_ID], self.output)
        self.assertEqual(result, self.output.resolve())
        payload = json.loads((result / "inventory.json").read_text())
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(len(payload["items"]), 1)
        item = payload["items"][0]
        self.assertEqual(item["id"], ITEM_ID)
        self.assertEqual(item["title"], "Example item")
        self.assertEqual(
            item["descriptions"], {"de": "Deutscher Text: Größe.\nZweite Zeile.", "en": "English café.\nSecond line."}
        )
        self.assertEqual(item["pricing"], {"mode": "sell", "currency": "EUR", "asking_price": "25.00"})
        self.assertEqual(item["data"]["notes"], "Public example notes")
        self.assertEqual(item["data"]["serialNumber"], "SYNTHETIC-ONLY")
        self.assertEqual(item["data"]["purchasePrice"], 10)
        self.assertEqual(item["data"]["fields"], original["fields"])
        self.assertEqual(item["data"], original)
        self.assertEqual(self.api.item, original)
        self.assertEqual(item["images"], [f"images/{ITEM_ID}/{PHOTO_ID}.png"])
        document = item["attachments"][1]["path"]
        self.assertEqual(document, f"attachments/{ITEM_ID}/{DOCUMENT_ID}.pdf")
        self.assertEqual((result / document).read_bytes(), b"%PDF-synthetic public document")
        self.assertEqual(
            (result / "listings" / f"{ITEM_ID}.de.txt").read_text(),
            "Titel:\nExample item\n\nBeschreibung:\nDeutscher Text: Größe.\nZweite Zeile.\n\nPreis:\n25.00 EUR\n",
        )
        self.assertEqual(
            (result / "listings" / f"{ITEM_ID}.en.txt").read_text(),
            "Title:\nExample item\n\nDescription:\nEnglish café.\nSecond line.\n\nPrice:\n25.00 EUR\n",
        )
        self.assertEqual(self.api.reads, [PHOTO_ID, DOCUMENT_ID])
        self.assertEqual(list(self.root.glob(".export-*")), [])

    def test_exported_photo_strips_metadata_and_applies_orientation(self) -> None:
        original_bytes = self.api.attachments[PHOTO_ID]
        export_items(self.api, [ITEM_ID], self.output)
        with Image.open(self.output / "images" / ITEM_ID / f"{PHOTO_ID}.png") as image:
            self.assertEqual(image.size, (2, 3))
            self.assertEqual(dict(image.getexif()), {})
            self.assertNotIn("Comment", image.info)
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(self.api.attachments[PHOTO_ID], original_bytes)

    def test_external_link_attachments_are_recorded_without_fetching(self) -> None:
        link = {
            "id": "66666666-6666-4666-8666-666666666666",
            "type": "attachment",
            "mimeType": "link/url",
            "path": "https://example.org/reference",
            "title": "Public reference",
        }
        self.api.item["attachments"].append(link)
        export_items(self.api, [ITEM_ID], self.output)
        payload = json.loads((self.output / "inventory.json").read_text())
        self.assertEqual(payload["items"][0]["data"]["attachments"][-1], link)
        self.assertEqual(self.api.reads, [PHOTO_ID, DOCUMENT_ID])

    def test_donation_exports_without_inventing_a_price(self) -> None:
        self.api.item["tags"] = [{"name": "donate"}]
        self.api.item["fields"] = []
        self.api.item["attachments"] = []
        export_items(self.api, [ITEM_ID], self.output)
        data = json.loads((self.output / "inventory.json").read_text())["items"][0]
        self.assertEqual(data["pricing"], {"mode": "donate", "currency": "EUR", "asking_price": None})
        self.assertEqual(data["images"], [])
        self.assertIn("Zu verschenken", (self.output / "listings" / f"{ITEM_ID}.de.txt").read_text())
        self.assertEqual(self.api.reads, [])

    def test_repeated_fresh_exports_keep_ids_and_paths_stable(self) -> None:
        export_items(self.api, [ITEM_ID], self.output)
        other = self.root / "second"
        export_items(self.api, [ITEM_ID], other)
        self.assertEqual((self.output / "inventory.json").read_bytes(), (other / "inventory.json").read_bytes())

    def test_empty_existing_destination_is_supported(self) -> None:
        self.output.mkdir()
        export_items(self.api, [ITEM_ID], self.output)
        self.assertTrue((self.output / "inventory.json").is_file())

    def test_nonempty_destination_is_unchanged_without_api_calls(self) -> None:
        self.output.mkdir()
        existing = self.output / "keep.txt"
        existing.write_text("Keep this")
        with self.assertRaisesRegex(HomeBoxError, "empty"):
            export_items(self.api, [ITEM_ID], self.output)
        self.assertEqual(existing.read_text(), "Keep this")
        self.assertEqual(list(self.output.iterdir()), [existing])
        self.assertEqual(self.api.item_reads, 0)

    def test_existing_file_is_not_replaced(self) -> None:
        self.output.write_text("Keep file")
        with self.assertRaises(HomeBoxError):
            export_items(self.api, [ITEM_ID], self.output)
        self.assertEqual(self.output.read_text(), "Keep file")

    def test_repository_destination_and_symlink_alias_are_rejected(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        alias = self.root / "repo-link"
        alias.symlink_to(repository, target_is_directory=True)
        for destination in (repository, repository / "new-export", alias / "new-export"):
            with self.subTest(destination=destination), self.assertRaisesRegex(HomeBoxError, "repository"):
                export_items(self.api, [ITEM_ID], destination)
        self.assertEqual(self.api.item_reads, 0)

    def test_destination_symlink_is_not_replaced(self) -> None:
        other = self.root / "existing"
        other.mkdir()
        self.output.symlink_to(other, target_is_directory=True)
        with self.assertRaises(HomeBoxError):
            export_items(self.api, [ITEM_ID], self.output)
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(list(other.iterdir()), [])

    def test_download_failure_removes_staging_not_original_files(self) -> None:
        self.api.fail_attachment = DOCUMENT_ID
        keep = self.root / "unrelated.txt"
        keep.write_text("Keep this")
        with self.assertRaises(HomeBoxError):
            export_items(self.api, [ITEM_ID], self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.iterdir()), [keep])
        self.assertEqual(keep.read_text(), "Keep this")

    def test_missing_pillow_fails_cleanly_without_a_partial_export(self) -> None:
        with patch.dict(sys.modules, {"PIL": None}), self.assertRaisesRegex(HomeBoxError, "Pillow"):
            export_items(self.api, [ITEM_ID], self.output)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_image_never_publishes_a_partial_package(self) -> None:
        self.api.attachments[PHOTO_ID] = b"not an image"
        with self.assertRaisesRegex(HomeBoxError, "photo|image"):
            export_items(self.api, [ITEM_ID], self.output)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_native_attachment_ids_fail_before_downloads(self) -> None:
        self.api.item["attachments"][1]["id"] = "../../escape"
        with self.assertRaises(HomeBoxError):
            export_items(self.api, [ITEM_ID], self.output)
        self.assertEqual(self.api.reads, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_private_item_fails_before_downloads_or_output_writes(self) -> None:
        self.api.item["tags"] = []
        with self.assertRaises(HomeBoxError):
            export_items(self.api, [ITEM_ID], self.output)
        self.assertEqual(self.api.reads, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_destination_filled_during_download_is_preserved(self) -> None:
        read_attachment = self.api.read_attachment

        def fill_destination(item_id: str, attachment_id: str) -> bytes:
            self.output.mkdir(exist_ok=True)
            (self.output / "keep.txt").write_text("Concurrent user file")
            return read_attachment(item_id, attachment_id)

        with patch.object(self.api, "read_attachment", side_effect=fill_destination), self.assertRaises(HomeBoxError):
            export_items(self.api, [ITEM_ID], self.output)
        self.assertEqual((self.output / "keep.txt").read_text(), "Concurrent user file")
        self.assertEqual(list(self.root.iterdir()), [self.output])

    def test_bad_json_metadata_does_not_publish_or_leave_staging(self) -> None:
        for value in ("bad\ud800text", float("nan")):
            with self.subTest(value=value):
                self.api.item["notes"] = value
                with self.assertRaises(HomeBoxError):
                    export_items(self.api, [ITEM_ID], self.output)
                self.assertEqual(list(self.root.iterdir()), [])

    def test_duplicate_attachments_are_rejected_before_downloads(self) -> None:
        self.api.item["attachments"].append(copy.deepcopy(self.api.item["attachments"][0]))
        with self.assertRaisesRegex(HomeBoxError, "duplicate"):
            export_items(self.api, [ITEM_ID], self.output)
        self.assertEqual(self.api.reads, [])

    def test_png_transparency_survives_metadata_removal(self) -> None:
        photo = Image.new("RGBA", (2, 2), (1, 2, 3, 100))
        content = io.BytesIO()
        photo.save(content, format="PNG")
        self.api.attachments[PHOTO_ID] = content.getvalue()
        export_items(self.api, [ITEM_ID], self.output)
        with Image.open(self.output / "images" / ITEM_ID / f"{PHOTO_ID}.png") as output:
            self.assertEqual(output.getpixel((0, 0)), (1, 2, 3, 100))

    def test_jpeg_photo_is_exported_without_exif(self) -> None:
        photo = Image.new("RGB", (2, 2), "blue")
        content = io.BytesIO()
        exif = Image.Exif()
        exif[315] = "SYNTHETIC AUTHOR"
        photo.save(content, format="JPEG", exif=exif)
        self.api.attachments[PHOTO_ID] = content.getvalue()
        self.api.item["attachments"][0]["mimeType"] = "image/jpeg"
        export_items(self.api, [ITEM_ID], self.output)
        with Image.open(self.output / "images" / ITEM_ID / f"{PHOTO_ID}.png") as output:
            self.assertEqual(output.size, (2, 2))
            self.assertEqual(dict(output.getexif()), {})

    def test_animated_photos_fail_instead_of_silently_losing_frames(self) -> None:
        first = Image.new("RGB", (2, 2), "red")
        second = Image.new("RGB", (2, 2), "blue")
        content = io.BytesIO()
        first.save(content, format="GIF", save_all=True, append_images=[second], duration=100, loop=0)
        self.api.attachments[PHOTO_ID] = content.getvalue()
        with self.assertRaisesRegex(HomeBoxError, "still|animated"):
            export_items(self.api, [ITEM_ID], self.output)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
