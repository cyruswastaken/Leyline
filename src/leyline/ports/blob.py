from __future__ import annotations

from typing import Protocol


class BlobNotFoundError(KeyError):
    """The requested blob does not exist."""


class BlobStore(Protocol):
    """Where original uploaded files live (Azure Blob Storage / Azurite)."""

    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def get(self, key: str) -> bytes:
        """Return the blob's bytes or raise ``BlobNotFoundError``."""
        ...

    def delete(self, key: str) -> None:
        """Delete a blob. Deleting a missing blob is not an error."""
        ...

    def ping(self) -> bool:
        """True when the store is reachable (used by readiness checks)."""
        ...
