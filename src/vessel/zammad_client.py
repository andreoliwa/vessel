"""Zammad REST API client.

Shared by vessel' own Invoke tasks and by external projects
that add vessel as a uv editable path dependency and
import this module as ``vessel.zammad_client``.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import requests


class ZammadAPIError(Exception):
    """Raised when a Zammad API call returns a non-2xx response."""

    def __init__(self, msg: str, status: int = 0) -> None:
        """Store the error status code alongside the exception message."""
        super().__init__(msg)
        self.status = status


class ZammadAPI:
    """Thin wrapper around the Zammad REST API with retry/backoff on transient errors."""

    def __init__(self, base_url: str, token: str, dry_run: bool = False) -> None:
        """Configure the authenticated session used by all requests below."""
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token token={token}",
                "Content-Type": "application/json",
            }
        )
        retry = Retry(
            total=4,
            backoff_factor=1,  # sleeps 1s, 2s, 4s between retries
            status_forcelist=[HTTPStatus.BAD_GATEWAY, HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.GATEWAY_TIMEOUT],
            allowed_methods={"GET", "POST", "PUT", "DELETE"},
            raise_on_status=False,  # let callers inspect the response
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.dry_run = dry_run

    def _raise_for_status(self, resp: requests.Response) -> None:
        if not resp.ok:
            msg = f"{resp.status_code} {resp.reason}: {resp.text[:300]}"
            raise ZammadAPIError(msg, status=resp.status_code)

    def get(self, endpoint: str) -> dict:
        """Send a GET request and return the decoded JSON body."""
        resp = self.session.get(f"{self.base_url}/api/v1/{endpoint}")
        self._raise_for_status(resp)
        return resp.json()  # type: ignore[return-value]

    def search(self, endpoint: str) -> list:
        """Send a GET request and return the decoded JSON body as a list (empty if not a list)."""
        resp = self.session.get(f"{self.base_url}/api/v1/{endpoint}")
        self._raise_for_status(resp)
        result = resp.json()
        return result if isinstance(result, list) else []

    def post(self, endpoint: str, data: dict) -> dict:
        """Send a POST request with a JSON body; no-op and log in dry-run mode."""
        if self.dry_run:
            print(f"  [DRY-RUN] POST /api/v1/{endpoint}: {json.dumps(data, default=str)[:200]}")
            return {"id": -1}
        resp = self.session.post(f"{self.base_url}/api/v1/{endpoint}", json=data)
        self._raise_for_status(resp)
        return resp.json()

    def put(self, endpoint: str, data: dict) -> dict:
        """Send a PUT request with a JSON body; no-op and log in dry-run mode."""
        if self.dry_run:
            print(f"  [DRY-RUN] PUT /api/v1/{endpoint}: {json.dumps(data, default=str)[:200]}")
            return {"id": -1}
        resp = self.session.put(f"{self.base_url}/api/v1/{endpoint}", json=data)
        self._raise_for_status(resp)
        return resp.json()

    def delete(self, endpoint: str) -> None:
        """Send a DELETE request; no-op and log in dry-run mode."""
        if self.dry_run:
            print(f"  [DRY-RUN] DELETE /api/v1/{endpoint}")
            return
        resp = self.session.delete(f"{self.base_url}/api/v1/{endpoint}")
        self._raise_for_status(resp)
