"""Informational package and image identity; no compatibility gates."""

from importlib.metadata import PackageNotFoundError, version
from uuid import uuid4


def release_identifier() -> str:
    try:
        return version("firm-sandbox")
    except PackageNotFoundError as exc:
        raise RuntimeError("firm-sandbox distribution metadata is unavailable") from exc


def image_identity() -> dict:
    """Informational release and immutable identity for a newly built image artifact."""
    return {
        "created_with_release": release_identifier(),
        "image_id": uuid4().hex,
    }
