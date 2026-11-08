# Copyright (c) 2026 W. Augusto Andreoli
# unittest avoids adding a test dependency; descriptive test names document behavior.
# ruff: noqa: D102, PT009, PT027
"""Synthetic checks for HomeBox listing preparation and read-only API access."""

from __future__ import annotations

import copy
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from unittest.mock import patch

from homebox.tasks import normalize_listing
from invoke.exceptions import Exit

from vessel.homebox import HomeBoxAPI, HomeBoxError


def sample_item() -> dict:
    """Return public synthetic inventory, never a saved API response."""
    return {
        "id": "11111111-1111-4111-8111-111111111111",
        "name": "Example item",
        "description": "Deutscher Text: Größe.\nZweite Zeile.\n\nEnglish:\nEnglish café.\nSecond line.",
        "tags": [{"id": "22222222-2222-4222-8222-222222222222", "name": "sell"}],
        "fields": [
            {"id": "33333333-3333-4333-8333-333333333333", "name": "asking_price", "type": "text", "textValue": "25.00"}
        ],
        "attachments": [
            {
                "id": "44444444-4444-4444-8444-444444444444",
                "type": "photo",
                "mimeType": "image/jpeg",
                "title": "Example",
                "path": "example.jpg",
            },
            {
                "id": "55555555-5555-4555-8555-555555555555",
                "type": "receipt",
                "mimeType": "application/pdf",
                "title": "Example receipt",
                "path": "example.pdf",
            },
        ],
        "notes": "Public example notes",
        "serialNumber": "SYNTHETIC-ONLY",
        "purchasePrice": 10,
        "quantity": 1,
    }


class NormalizeListingTests(unittest.TestCase):
    """Catch broken eligibility, parsing, pricing, and public-data preservation."""

    def test_sale_preserves_copy_and_all_public_item_data(self) -> None:
        item = sample_item()
        original = copy.deepcopy(item)
        listing = normalize_listing(item)
        self.assertEqual(listing.purpose, "sell")
        self.assertEqual(listing.description_de, "Deutscher Text: Größe.\nZweite Zeile.")
        self.assertEqual(listing.description_en, "English café.\nSecond line.")
        self.assertEqual(listing.asking_price, "25.00")
        self.assertEqual(listing.currency, "EUR")
        self.assertEqual(listing.item, original)
        self.assertEqual(item, original)
        listing.item["fields"].clear()
        self.assertEqual(item, original)

    def test_unrelated_or_missing_tags_cannot_publish_an_item(self) -> None:
        for tags in ([], [{"name": "work"}], [{"name": "Sell"}], [{"name": "seller"}]):
            with self.subTest(tags=tags):
                item = sample_item()
                item["tags"] = tags
                item["fields"].extend(
                    [
                        {"name": "purpose", "textValue": "sell"},
                        {"name": "export", "textValue": "true"},
                    ]
                )
                with self.assertRaisesRegex(HomeBoxError, "sell.*donate"):
                    normalize_listing(item)

    def test_donation_needs_no_price_and_does_not_invent_zero(self) -> None:
        item = sample_item()
        item["tags"] = [{"name": "donate"}]
        item["fields"] = []
        listing = normalize_listing(item)
        self.assertEqual(listing.purpose, "donate")
        self.assertIsNone(listing.asking_price)
        self.assertEqual(listing.currency, "EUR")

    def test_both_purpose_tags_need_a_clear_user_choice(self) -> None:
        item = sample_item()
        item["tags"].append({"name": "donate"})
        with self.assertRaisesRegex(HomeBoxError, "one|both"):
            normalize_listing(item)

    def test_bad_or_empty_language_sections_fail_without_echoing_copy(self) -> None:
        for text in (
            "No separator",
            "English:\nOnly English",
            "German\nEnglish:",
            "Deutsch\nEnglish:\nFirst\nEnglish:\nSecond",
        ):
            with self.subTest(text=text):
                item = sample_item()
                item["description"] = text
                with self.assertRaises(HomeBoxError) as caught:
                    normalize_listing(item)
                self.assertNotIn(text, str(caught.exception))

    def test_crlf_input_keeps_language_line_endings(self) -> None:
        item = sample_item()
        item["description"] = "Deutsch\r\nZweite Zeile\r\n\r\nEnglish:\r\nEnglish\r\nSecond line"
        listing = normalize_listing(item)
        self.assertEqual(listing.description_de, "Deutsch\r\nZweite Zeile")
        self.assertEqual(listing.description_en, "English\r\nSecond line")

    def test_missing_duplicate_or_invalid_sale_price_is_not_zero(self) -> None:
        for fields in (
            [],
            [{"name": "asking_price", "textValue": value} for value in ("1", "2")],
            *[
                [{"name": "asking_price", "textValue": value}]
                for value in ("", "-1", "NaN", "Infinity", "25,00", "1.001")
            ],
        ):
            with self.subTest(fields=fields):
                item = sample_item()
                item["fields"] = fields
                with self.assertRaisesRegex(HomeBoxError, "asking_price"):
                    normalize_listing(item)

    def test_decimal_money_is_normalized_without_float_rounding(self) -> None:
        for value, expected in (("25", "25.00"), ("0", "0.00"), ("0.10", "0.10"), ("1234567890.12", "1234567890.12")):
            with self.subTest(value=value):
                item = sample_item()
                item["fields"][0]["textValue"] = value
                self.assertEqual(normalize_listing(item).asking_price, expected)

    def test_missing_identity_or_name_fails(self) -> None:
        for field in ("id", "name"):
            item = sample_item()
            item[field] = ""
            with self.subTest(field=field), self.assertRaises(HomeBoxError):
                normalize_listing(item)

    def test_generic_custom_links_are_preserved_without_fetching(self) -> None:
        item = sample_item()
        item["fields"].append({"name": "My reference", "textValue": "https://example.org/ad"})
        self.assertEqual(
            normalize_listing(item).item["fields"][-1], {"name": "My reference", "textValue": "https://example.org/ad"}
        )

    def test_description_budget_counts_utf8_bytes_not_characters(self) -> None:
        item = sample_item()
        item["description"] = "ä" * 490 + "\nEnglish:\n" + "e" * 10
        self.assertEqual(len(item["description"].encode()), 1000)
        self.assertEqual(len(normalize_listing(item).description_de), 490)
        item["description"] += "e"
        with self.assertRaisesRegex(HomeBoxError, "1,000"):
            normalize_listing(item)

    def test_bad_tag_and_custom_field_shapes_have_useful_errors(self) -> None:
        for tags in (None, "sell", ["sell"], [{"name": ["sell"]}]):
            item = sample_item()
            item["tags"] = tags
            with self.subTest(tags=tags), self.assertRaises(HomeBoxError):
                normalize_listing(item)
        for fields in (None, "asking_price", ["asking_price"]):
            item = sample_item()
            item["fields"] = fields
            with self.subTest(fields=fields), self.assertRaises(HomeBoxError):
                normalize_listing(item)


class HomeBoxAPITests(unittest.TestCase):
    """Exercise real HTTP requests against a synthetic loopback server."""

    requests: ClassVar[list[tuple[str, str, str | None]]]
    response_status: ClassVar[int]
    response_body: ClassVar[bytes]
    response_pages: ClassVar[dict[str, bytes]]
    response_headers: ClassVar[dict[str, str]]
    server: ClassVar[ThreadingHTTPServer]
    thread: ClassVar[threading.Thread]
    url: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        """Start one local service for all API boundary checks."""
        cls.requests = []
        cls.response_status = 200
        cls.response_body = b""
        cls.response_pages = {}
        cls.response_headers = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                cls.requests.append((self.command, self.path, self.headers.get("Authorization")))
                self.send_response(cls.response_status)
                for key, value in cls.response_headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(cls.response_pages.get(self.path, cls.response_body))

            def log_message(self, *_args: object) -> None:
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        """Release the loopback server without adding test cleanup to production."""
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self) -> None:
        """Reset synthetic responses and use only a fake credential."""
        type(self).requests = []
        type(self).response_status = 200
        type(self).response_pages = {}
        type(self).response_body = json.dumps(sample_item()).encode()
        type(self).response_headers = {"Content-Type": "application/json"}
        self.environment = patch.dict(os.environ, {"HOMEBOX_API_KEY": "synthetic-token"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_list_items_reads_every_page_with_authentication(self) -> None:
        first = {"id": "11111111-1111-4111-8111-111111111111", "tags": []}
        second = {"id": "77777777-7777-4777-8777-777777777777", "tags": [{"name": "sell"}]}
        type(self).response_pages = {
            "/api/v1/entities?page=1&pageSize=100": json.dumps({"items": [first], "page": 1, "total": 2}).encode(),
            "/api/v1/entities?page=2&pageSize=100": json.dumps({"items": [second], "page": 2, "total": 2}).encode(),
        }
        self.assertEqual(HomeBoxAPI(base_url=self.url).list_items(), [first, second])
        self.assertEqual(
            self.requests,
            [
                ("GET", "/api/v1/entities?page=1&pageSize=100", "Bearer synthetic-token"),
                ("GET", "/api/v1/entities?page=2&pageSize=100", "Bearer synthetic-token"),
            ],
        )

    def test_list_items_accepts_empty_inventory(self) -> None:
        type(self).response_body = b'{"items": [], "page": 1, "total": 0}'
        self.assertEqual(HomeBoxAPI(base_url=self.url).list_items(), [])

    def test_list_items_rejects_invalid_or_incomplete_pages(self) -> None:
        for page in (
            None,
            {"items": None, "page": 1, "total": 0},
            {"items": [], "page": 1, "total": 1},
            {"items": [], "page": 2, "total": 0},
            {"items": [], "page": 1, "total": -1},
            {"items": [], "page": 1, "total": True},
            {"items": [{"id": "bad"}], "page": 1, "total": 1},
        ):
            with self.subTest(page=page):
                type(self).response_body = json.dumps(page).encode()
                with self.assertRaises(HomeBoxError):
                    HomeBoxAPI(base_url=self.url).list_items()

    def test_list_items_rejects_repeated_pages(self) -> None:
        item = {"id": "11111111-1111-4111-8111-111111111111"}
        type(self).response_pages = {
            f"/api/v1/entities?page={page}&pageSize=100": json.dumps(
                {"items": [item], "page": page, "total": 2}
            ).encode()
            for page in (1, 2)
        }
        with self.assertRaises(HomeBoxError):
            HomeBoxAPI(base_url=self.url).list_items()
        self.assertEqual(len(self.requests), 2)

    def test_item_reader_authenticates_and_returns_complete_data(self) -> None:
        result = HomeBoxAPI(base_url=self.url).read_item("11111111-1111-4111-8111-111111111111")
        self.assertEqual(result, sample_item())
        self.assertEqual(
            self.requests, [("GET", "/api/v1/entities/11111111-1111-4111-8111-111111111111", "Bearer synthetic-token")]
        )

    def test_item_reader_supports_task_local_listing_validation(self) -> None:
        listing = normalize_listing(HomeBoxAPI(base_url=self.url).read_item("11111111-1111-4111-8111-111111111111"))
        self.assertEqual(listing.asking_price, "25.00")
        self.assertEqual(listing.item["notes"], "Public example notes")
        self.assertEqual(len(self.requests), 1)

    def test_optional_base_url_environment_is_respected(self) -> None:
        with patch.dict(os.environ, {"HOMEBOX_URL": self.url}):
            self.assertEqual(HomeBoxAPI().read_item("11111111-1111-4111-8111-111111111111")["name"], "Example item")

    def test_attachment_reader_uses_item_scoped_get(self) -> None:
        type(self).response_body = b"synthetic attachment bytes"
        data = HomeBoxAPI(base_url=self.url).read_attachment(
            "11111111-1111-4111-8111-111111111111", "44444444-4444-4444-8444-444444444444"
        )
        self.assertEqual(data, b"synthetic attachment bytes")
        self.assertEqual(
            self.requests,
            [
                (
                    "GET",
                    "/api/v1/entities/11111111-1111-4111-8111-111111111111/attachments/44444444-4444-4444-8444-444444444444",
                    "Bearer synthetic-token",
                )
            ],
        )

    def test_http_failures_do_not_echo_server_bodies_or_credentials(self) -> None:
        for status in (401, 403, 404, 500):
            with self.subTest(status=status):
                type(self).response_status = status
                type(self).response_body = b"synthetic-token PRIVATE SERVER BODY"
                with self.assertRaises(HomeBoxError) as caught:
                    HomeBoxAPI(base_url=self.url).read_item("11111111-1111-4111-8111-111111111111")
                self.assertNotIn("synthetic-token", str(caught.exception))
                self.assertNotIn("PRIVATE SERVER BODY", str(caught.exception))
                self.assertRegex(str(caught.exception), "auth|key|found|unavailable|HTTP")

    def test_attachment_failure_does_not_return_a_success_package(self) -> None:
        type(self).response_status = 404
        with self.assertRaisesRegex(HomeBoxError, "attachment"):
            HomeBoxAPI(base_url=self.url).read_attachment(
                "11111111-1111-4111-8111-111111111111", "44444444-4444-4444-8444-444444444444"
            )

    def test_bad_json_and_wrong_item_identity_fail_cleanly(self) -> None:
        for body in (b"not JSON", b"[]", b"null", b'{"id":"different"}'):
            with self.subTest(body=body):
                type(self).response_body = body
                with self.assertRaisesRegex(HomeBoxError, "JSON|item|response"):
                    HomeBoxAPI(base_url=self.url).read_item("11111111-1111-4111-8111-111111111111")

    def test_invalid_unicode_in_json_has_a_clean_listing_error(self) -> None:
        item = sample_item()
        item["description"] = "Deutsch\ud800\nEnglish:\nEnglish"
        type(self).response_body = json.dumps(item).encode()
        with self.assertRaisesRegex(HomeBoxError, "Unicode|UTF-8"):
            normalize_listing(HomeBoxAPI(base_url=self.url).read_item("11111111-1111-4111-8111-111111111111"))

    def test_redirect_is_not_followed_with_credentials(self) -> None:
        type(self).response_status = 302
        type(self).response_headers = {"Location": self.url + "/unexpected"}
        with self.assertRaisesRegex(HomeBoxError, "redirect|HTTP"):
            HomeBoxAPI(base_url=self.url).read_item("11111111-1111-4111-8111-111111111111")
        self.assertEqual(len(self.requests), 1)

    def test_invalid_ids_fail_before_any_network_request(self) -> None:
        api = HomeBoxAPI(base_url=self.url)
        for value in ("../users", "not-an-id", "https://example.org"):
            with self.subTest(value=value), self.assertRaises(HomeBoxError):
                api.read_item(value)
        with self.assertRaises(HomeBoxError):
            api.read_attachment("11111111-1111-4111-8111-111111111111", "../../secret")
        self.assertEqual(self.requests, [])

    def test_unreachable_api_has_secret_free_diagnostic(self) -> None:
        with (
            patch("urllib.request.OpenerDirector.open", side_effect=OSError("synthetic-token transport internals")),
            self.assertRaisesRegex(HomeBoxError, "unavailable|reach") as caught,
        ):
            HomeBoxAPI(base_url=self.url).read_item("11111111-1111-4111-8111-111111111111")
        self.assertNotIn("synthetic-token", str(caught.exception))

    def test_base_url_cannot_embed_credentials_or_query_secrets(self) -> None:
        for url in (
            "ftp://localhost",
            "http://user:password@localhost",
            "http://localhost?token=private",
            "http://localhost#private",
        ):
            with self.subTest(url=url), self.assertRaises(HomeBoxError):
                HomeBoxAPI(base_url=url)
        self.assertEqual(self.requests, [])

    def test_invalid_credentials_fail_before_any_network_request(self) -> None:
        for token in ("", "bad\nkey", "bad\rkey", "synthetic-🔑"):
            with (
                self.subTest(token=token),
                patch.dict(os.environ, {"HOMEBOX_API_KEY": token}),
                self.assertRaises(HomeBoxError),
            ):
                HomeBoxAPI(base_url=self.url).read_item("11111111-1111-4111-8111-111111111111")
        self.assertEqual(self.requests, [])

    def test_missing_credential_uses_standard_required_environment_error(self) -> None:
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(Exit):
            HomeBoxAPI(base_url=self.url)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
