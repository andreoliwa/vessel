# Copyright (c) 2026 W. Augusto Andreoli
# Loaded directly by Vessel discovery, not as an importable application package.
"""Invoke tasks for HomeBox home inventory."""

import copy
import io
import json
import mimetypes
import re
import tempfile
import warnings
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import click
from invoke import Context, task
from invoke.exceptions import Exit
from PIL import Image, ImageOps

from vessel import homebox
from vessel.homebox import HomeBoxAPI, HomeBoxError

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_MAX_DESCRIPTION_BYTES = 1000


@dataclass
class Listing:
    """Parsed listing fields alongside a separate copy of all public item data."""

    item: dict[str, Any]
    purpose: str
    description_de: str
    description_en: str
    asking_price: str | None
    currency: str = "EUR"


def _listing_id(item: dict[str, Any]) -> str:
    """Validate the native ID before using it as an export identity."""
    try:
        return str(UUID(str(item.get("id"))))
    except ValueError:
        message = "Use a valid HomeBox item UUID."
        raise HomeBoxError(message) from None


def normalize_listing(item: dict[str, Any]) -> Listing:
    """Validate tag eligibility, split approved copy, and normalize EUR pricing."""
    _listing_id(item)
    if not isinstance(item.get("name"), str) or not item["name"].strip():
        message = "Give the HomeBox item a name before exporting."
        raise HomeBoxError(message)
    tags = item.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(tag, dict) for tag in tags):
        message = "HomeBox returned invalid item tags."
        raise HomeBoxError(message)
    purposes = {tag.get("name") for tag in tags if tag.get("name") in ("sell", "donate")}
    if len(purposes) != 1:
        message = "Attach exactly one sell or donate tag before exporting; do not use both."
        raise HomeBoxError(message)
    description = item.get("description")
    if not isinstance(description, str):
        message = "Save the bilingual description as Unicode text before exporting."
        raise HomeBoxError(message)
    try:
        description_bytes = description.encode("utf-8")
    except UnicodeError:
        message = "Save valid Unicode text in the bilingual description before exporting."
        raise HomeBoxError(message) from None
    if len(description_bytes) > _MAX_DESCRIPTION_BYTES:
        message = "Keep the bilingual description within 1,000 UTF-8 bytes."
        raise HomeBoxError(message)
    lines = description.splitlines(keepends=True)
    separators = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == "English:"]
    if len(separators) != 1:
        message = "Separate German and English copy with exactly one standalone English: line."
        raise HomeBoxError(message)
    index = separators[0]
    german = "".join(lines[:index]).strip("\r\n")
    english = "".join(lines[index + 1 :]).strip("\r\n")
    if not german.strip() or not english.strip():
        message = "Save nonempty German and English copy before exporting."
        raise HomeBoxError(message)
    purpose = purposes.pop()
    asking_price = None
    if purpose == "sell":
        asking_price = _sale_price(item)
    return Listing(copy.deepcopy(item), purpose, german, english, asking_price)


def _sale_price(item: dict[str, Any]) -> str:
    """Require a single exact decimal asking price without float conversion."""
    fields = item.get("fields", [])
    if not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields):
        message = "HomeBox returned invalid custom fields; check asking_price."
        raise HomeBoxError(message)
    prices = [field.get("textValue") for field in fields if field.get("name") == "asking_price"]
    if len(prices) != 1 or not isinstance(prices[0], str):
        message = "Add exactly one asking_price text field for a sale item."
        raise HomeBoxError(message)
    value = prices[0].strip()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value):
        message = "Set asking_price to a nonnegative decimal amount with at most two decimal places."
        raise HomeBoxError(message)
    return f"{Decimal(value):.2f}"


def validate_destination(directory: str | Path) -> Path:
    """Reject repository paths, symlinks, files, and nonempty directories."""
    requested = Path(directory).expanduser()
    destination = requested.resolve()
    if destination.is_relative_to(_REPOSITORY_ROOT):
        message = "Choose an export directory outside the Vessel repository."
        raise HomeBoxError(message)
    if requested.is_symlink():
        message = "Choose a real export directory, not a symlink."
        raise HomeBoxError(message)
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        message = "Choose an empty export directory or a new destination."
        raise HomeBoxError(message)
    return destination


def _attachment_plan(item: dict[str, Any], item_id: str) -> list[tuple[dict[str, Any], Path | None]]:
    """Prepare stable local paths from native IDs, never remote filenames."""
    attachments = item.get("attachments", [])
    if not isinstance(attachments, list) or any(not isinstance(attachment, dict) for attachment in attachments):
        message = "HomeBox returned an invalid attachment list."
        raise HomeBoxError(message)
    plan: list[tuple[dict[str, Any], Path | None]] = []
    seen = set()
    for attachment in attachments:
        try:
            attachment_id = str(UUID(str(attachment.get("id"))))
        except ValueError:
            message = "HomeBox returned an invalid attachment ID."
            raise HomeBoxError(message) from None
        if attachment_id in seen:
            message = "HomeBox returned duplicate attachment IDs; check the item attachments."
            raise HomeBoxError(message)
        seen.add(attachment_id)
        mime = attachment.get("mimeType", "application/octet-stream")
        path = attachment.get("path", "")
        if not isinstance(mime, str) or not isinstance(path, str):
            message = "HomeBox returned invalid attachment metadata."
            raise HomeBoxError(message)
        # External links are public metadata, not permission to fetch websites.
        if mime == "link/url" or urlsplit(path).scheme in ("http", "https"):
            plan.append((attachment, None))
        elif attachment.get("type") == "photo":
            plan.append((attachment, Path("images") / item_id / f"{attachment_id}.png"))
        else:
            suffix = mimetypes.guess_extension(mime, strict=False) or ".bin"
            plan.append((attachment, Path("attachments") / item_id / f"{attachment_id}{suffix}"))
    return plan


def _write_photo(data: bytes, destination: Path) -> None:
    """Keep oriented image pixels while dropping embedded metadata from copies."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if getattr(image, "n_frames", 1) != 1:
                    message = "Use a still photo; animated or multi-page images are not supported."
                    # Validate decoded content inside the same error boundary as other image failures.
                    raise HomeBoxError(message)  # noqa: TRY301
                oriented = ImageOps.exif_transpose(image)
                mode = "RGBA" if "A" in oriented.getbands() or "transparency" in oriented.info else "RGB"
                pixels = oriented.convert(mode)
                # A fresh pixel-only image cannot carry EXIF, comments, or other source metadata.
                with Image.frombytes(mode, pixels.size, pixels.tobytes()) as clean:
                    clean.save(destination, format="PNG")
    except HomeBoxError:
        raise
    except (OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        message = "Could not write a photo copy; check the image format, dimensions, and available disk space."
        raise HomeBoxError(message) from None


def _write_text(listing: Listing, item_id: str, stage: Path) -> None:
    """Write exact language bodies with title and price in separate sections."""
    directory = stage / "listings"
    directory.mkdir(exist_ok=True)
    title = listing.item["name"]
    for language, body, labels, donation in (
        ("de", listing.description_de, ("Titel", "Beschreibung", "Preis"), "Zu verschenken"),
        ("en", listing.description_en, ("Title", "Description", "Price"), "Donation"),
    ):
        price = f"{listing.asking_price} {listing.currency}" if listing.asking_price is not None else donation
        text = f"{labels[0]}:\n{title}\n\n{labels[1]}:\n{body}\n\n{labels[2]}:\n{price}\n"
        (directory / f"{item_id}.{language}.txt").write_text(text, encoding="utf-8", newline="")


def _read_listings(api: HomeBoxAPI, item_ids: list[str]) -> list[Listing]:
    """Select explicit items or all sale items, then validate complete records."""
    selected = list(dict.fromkeys(item_ids))
    if not item_ids:
        for summary in api.list_items():
            tags = summary.get("tags", [])
            if not isinstance(tags, list) or any(not isinstance(tag, dict) for tag in tags):
                message = "HomeBox returned invalid item tags."
                raise HomeBoxError(message)
            if any(tag.get("name") == "sell" for tag in tags):
                selected.append(summary["id"])
    if not selected:
        message = "No items with the sell tag found; no export written."
        raise HomeBoxError(message)
    listings = [normalize_listing(api.read_item(item_id)) for item_id in selected]
    # Recheck full records: a sale tag may have changed since the summary read.
    if not item_ids and any(listing.purpose != "sell" for listing in listings):
        message = "An item's sell tag changed during export; retry after checking its tags."
        raise HomeBoxError(message)
    return listings


def _write_item(api: HomeBoxAPI, listing: Listing, stage: Path) -> dict[str, Any]:
    """Write one item's files and return its inventory entry."""
    stable_id = _listing_id(listing.item)
    item = copy.deepcopy(listing.item)
    plan = _attachment_plan(copy.deepcopy(item), stable_id)
    images = []
    for attachment, relative_path in plan:
        if relative_path is None:
            continue
        target = stage / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        content = api.read_attachment(stable_id, attachment["id"])
        if attachment.get("type") == "photo":
            _write_photo(content, target)
            images.append(relative_path.as_posix())
        else:
            target.write_bytes(content)
        attachment["path"] = relative_path.as_posix()
    _write_text(listing, stable_id, stage)
    return {
        "id": stable_id,
        "title": item["name"],
        "descriptions": {"de": listing.description_de, "en": listing.description_en},
        "pricing": {"mode": listing.purpose, "currency": listing.currency, "asking_price": listing.asking_price},
        "images": images,
        "attachments": [attachment for attachment, _ in plan],
        "data": item,
    }


def export_items(api: HomeBoxAPI, item_ids: list[str], output_directory: str | Path) -> Path:
    """Publish one complete package for selected items or all sale items."""
    try:
        destination = validate_destination(output_directory)
        listings = _read_listings(api, item_ids)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix=f".{destination.name}-", dir=destination.parent) as temporary:
            stage = Path(temporary)
            payload = {"schema_version": 1, "items": [_write_item(api, listing, stage) for listing in listings]}
            (stage / "inventory.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                encoding="utf-8",
            )
            # A directory rename publishes only complete packages and refuses a newly nonempty destination.
            validate_destination(output_directory)
            stage.rename(destination)
    except HomeBoxError:
        raise
    except (OSError, ValueError, TypeError):
        message = "Could not publish a complete export; check item data, destination access, and disk space."
        raise HomeBoxError(message) from None
    else:
        return destination


@task
def browse(c: Context) -> None:
    """Browse HomeBox."""
    c.run("open http://localhost:8009")


@task(
    iterable=["item"],
    help={
        "item": "Item UUIDs; repeat this option or separate with commas. Default: all sell-tagged items.",
        "output": "New or empty directory outside the repository (required).",
    },
)
def export(c: Context, item: tuple[str, ...] = (), output: str = "") -> None:
    """Export selected items, or all sell-tagged items when --item is omitted."""
    if not output:
        message = "Provide --output with a new or empty directory outside the repository."
        raise click.UsageError(message)
    try:
        item_ids = list(dict.fromkeys(str(UUID(value.strip())) for group in item for value in group.split(",")))
    except ValueError:
        message = "Every --item value must be a UUID. Separate multiple UUIDs with commas or repeat --item."
        raise click.UsageError(message) from None
    if c.config.run.dry:
        selection = ", ".join(item_ids) if item_ids else "all sell-tagged items"
        click.echo(f"[DRY-RUN] Would export {selection} to {output}; no requests or files written.")
        return
    try:
        destination = validate_destination(output)
        result = export_items(homebox.HomeBoxAPI(), item_ids, destination)
    except homebox.HomeBoxError as error:
        raise click.ClickException(str(error)) from None
    except OSError:
        message = "Could not access the export destination; check directory permissions and available disk space."
        raise click.ClickException(message) from None
    except Exit:
        message = "Set HOMEBOX_API_KEY to your HomeBox API key."
        raise click.ClickException(message) from None
    click.echo(f"Exported HomeBox items to {result}")
