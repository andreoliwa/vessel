# Copyright (c) 2026 W. Augusto Andreoli
"""Read HomeBox items and attachments without modifying remote data."""

from __future__ import annotations

import json
import os
import re
from http import HTTPStatus
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from conjuring.grimoire import lazy_env_variable


class HomeBoxError(ValueError):
    """Report a corrective action without echoing credentials or API bodies."""


class _NoRedirect(HTTPRedirectHandler):
    """Keep bearer credentials on the configured API, never on redirect targets."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        """Leave redirects as HTTP errors rather than forwarding authentication."""
        return


class HomeBoxAPI:
    """Read item JSON and attachment bytes with no mutation methods or retries."""

    def __init__(self, base_url: str | None = None) -> None:
        """Load required credentials only when an API reader is constructed."""
        self._base_url = (base_url or os.environ.get("HOMEBOX_URL", "http://localhost:8009")).rstrip("/")
        try:
            parts = urlsplit(self._base_url)
            valid_url = (
                parts.scheme in ("http", "https")
                and bool(parts.hostname)
                and parts.username is None
                and parts.password is None
                and not parts.query
                and not parts.fragment
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            message = "Set HOMEBOX_URL to an HTTP or HTTPS base URL without credentials, query, or fragment."
            raise HomeBoxError(message)
        self._token = lazy_env_variable("HOMEBOX_API_KEY", "HomeBox API key").strip()
        if not re.fullmatch(r"[\x21-\x7e]+", self._token):
            message = "Set HOMEBOX_API_KEY to a nonempty printable ASCII key without whitespace."
            raise HomeBoxError(message)
        self._opener = build_opener(_NoRedirect())

    def _read(self, path: str, kind: str) -> bytes:
        """Read one API resource without including response bodies in errors."""
        # The constructor permits only HTTP(S) URLs, and IDs are UUID path segments.
        request = Request(  # noqa: S310
            self._base_url + "/api/v1/" + path,
            headers={"Authorization": f"Bearer {self._token}"},
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=15) as response:
                return response.read()
        except HTTPError as error:
            error.close()
            if error.code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                message = "HomeBox authentication failed; check HOMEBOX_API_KEY and its expiry."
                raise HomeBoxError(message) from None
            if error.code == HTTPStatus.NOT_FOUND:
                message = f"HomeBox {kind} not found; check its ID."
                raise HomeBoxError(message) from None
            message = f"HomeBox {kind} read failed (HTTP {error.code}); check the service."
            raise HomeBoxError(message) from None
        except (OSError, HTTPException):
            message = f"HomeBox {kind} API unavailable; check HOMEBOX_URL and that the service is running."
            raise HomeBoxError(message) from None

    def _read_item_page(self, page: int) -> tuple[list[dict[str, Any]], int]:
        """Read and validate one native item-summary page."""
        data = self._read(f"entities?page={page}&pageSize=100", "item list")
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeError):
            message = "HomeBox returned invalid item-list JSON; check the API response format."
            raise HomeBoxError(message) from None
        if not isinstance(payload, dict):
            message = "HomeBox returned an invalid item-list response."
            raise HomeBoxError(message)
        items = payload.get("items")
        total = payload.get("total")
        if (
            not isinstance(items, list)
            or any(not isinstance(item, dict) for item in items)
            or type(total) is not int
            or total < 0
            or type(payload.get("page")) is not int
            or payload["page"] != page
        ):
            message = "HomeBox returned invalid item-list pagination; retry or check the API version."
            raise HomeBoxError(message)
        for item in items:
            _item_id(item.get("id"))
        return items, total

    def list_items(self) -> list[dict[str, Any]]:
        """Return all native item summaries, following every result page."""
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        page = 1
        expected_total = None
        while True:
            entries, total = self._read_item_page(page)
            if expected_total is not None and expected_total != total:
                message = "HomeBox inventory changed during pagination; retry the read."
                raise HomeBoxError(message)
            expected_total = total
            for entry in entries:
                item_id = _item_id(entry["id"])
                if item_id in seen:
                    message = "HomeBox repeated an item during pagination; retry the read."
                    raise HomeBoxError(message)
                seen.add(item_id)
            items.extend(entries)
            if len(items) > total or (not entries and len(items) < total):
                message = "HomeBox returned incomplete or inconsistent item pages; retry the read."
                raise HomeBoxError(message)
            if len(items) == total:
                return items
            page += 1

    def read_item(self, item_id: str) -> dict[str, Any]:
        """Return the complete selected item, rejecting invalid JSON or identity."""
        expected_id = _item_id(item_id)
        data = self._read("entities/" + expected_id, "item")
        try:
            item = json.loads(data)
        except (ValueError, UnicodeError):
            message = "HomeBox returned invalid item JSON; check the API response format."
            raise HomeBoxError(message) from None
        if not isinstance(item, dict) or item.get("id") != expected_id:
            message = "HomeBox returned an invalid item response; check the API version."
            raise HomeBoxError(message)
        return item

    def read_attachment(self, item_id: str, attachment_id: str) -> bytes:
        """Read a native attachment without trusting URLs from item metadata."""
        path = f"entities/{_item_id(item_id)}/attachments/{_item_id(attachment_id)}"
        return self._read(path, "attachment")


def _item_id(value: object) -> str:
    """Validate an ID before using it as an API path segment."""
    try:
        return str(UUID(str(value)))
    except ValueError:
        message = "Use a valid HomeBox item or attachment UUID."
        raise HomeBoxError(message) from None
