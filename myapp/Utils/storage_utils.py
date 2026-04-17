"""
Cloudinary helpers. The `cloudinary_storage` package wires up Django's
default storage automatically, so ImageField.upload_to paths behave as
usual. These helpers exist for direct uploads and URL signing.
"""
import logging
from typing import Optional

import cloudinary
import cloudinary.uploader
from django.conf import settings

log = logging.getLogger(__name__)


def ensure_configured():
    cfg = settings.CLOUDINARY_STORAGE
    cloudinary.config(
        cloud_name=cfg.get("CLOUD_NAME"),
        api_key=cfg.get("API_KEY"),
        api_secret=cfg.get("API_SECRET"),
        secure=True,
    )


def upload_file(file_obj, folder: str = "misc", public_id: Optional[str] = None) -> dict:
    """Synchronous upload. Returns Cloudinary response dict."""
    ensure_configured()
    try:
        res = cloudinary.uploader.upload(
            file_obj, folder=folder, public_id=public_id, resource_type="auto",
        )
        return res
    except Exception as e:
        log.exception("Cloudinary upload failed: %s", e)
        raise


def delete_file(public_id: str) -> None:
    ensure_configured()
    try:
        cloudinary.uploader.destroy(public_id, invalidate=True)
    except Exception as e:
        log.warning("Cloudinary delete failed for %s: %s", public_id, e)
