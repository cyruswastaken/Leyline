from __future__ import annotations

import threading

from leyline.ports.blob import BlobNotFoundError


class InMemoryBlobStore:
    """Process-local BlobStore for tests."""

    def __init__(self) -> None:
        self._blobs: dict[str, tuple[bytes, str]] = {}
        self._lock = threading.Lock()

    def put(self, key: str, data: bytes, content_type: str) -> None:
        with self._lock:
            self._blobs[key] = (bytes(data), content_type)

    def get(self, key: str) -> bytes:
        with self._lock:
            try:
                return self._blobs[key][0]
            except KeyError:
                raise BlobNotFoundError(key) from None

    def delete(self, key: str) -> None:
        with self._lock:
            self._blobs.pop(key, None)

    def ping(self) -> bool:
        return True

    # -- test helpers -------------------------------------------------------

    def keys(self) -> set[str]:
        with self._lock:
            return set(self._blobs)
