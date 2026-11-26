"""Azure Blob Storage adapter (also works against the Azurite emulator)."""

from __future__ import annotations

import contextlib

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContentSettings, ExponentialRetry

from leyline.ports.blob import BlobNotFoundError

_TIMEOUT_SECONDS = 20
# The SDK's default retry policy backs off for over a minute, which would
# stall web requests (uploads) and health checks when storage is down.
# Data operations get a few quick retries; the readiness probe gets none.
_PROBE_TIMEOUT_SECONDS = 3


class AzureBlobStore:
    """Signs in with a connection string (Azurite, local use) or, given an account
    address and a credential, with a token credential such as the managed identity."""

    def __init__(
        self,
        connection_string: str | None,
        container: str,
        *,
        account_url: str | None = None,
        credential: TokenCredential | None = None,
    ) -> None:
        data_policy = ExponentialRetry(
            initial_backoff=1, increment_base=2, retry_total=2
        )
        probe_policy = ExponentialRetry(retry_total=0)
        if connection_string:
            self._service = BlobServiceClient.from_connection_string(
                connection_string,
                connection_timeout=_TIMEOUT_SECONDS,
                retry_policy=data_policy,
            )
            self._probe = BlobServiceClient.from_connection_string(
                connection_string,
                connection_timeout=_PROBE_TIMEOUT_SECONDS,
                retry_policy=probe_policy,
            )
        elif account_url and credential is not None:
            self._service = BlobServiceClient(
                account_url,
                credential=credential,
                connection_timeout=_TIMEOUT_SECONDS,
                retry_policy=data_policy,
            )
            self._probe = BlobServiceClient(
                account_url,
                credential=credential,
                connection_timeout=_PROBE_TIMEOUT_SECONDS,
                retry_policy=probe_policy,
            )
        else:
            raise ValueError("a connection string, or an account URL and a credential")
        self._container = self._service.get_container_client(container)
        self._container_ready = False

    def _ensure_container(self) -> None:
        if self._container_ready:
            return
        with contextlib.suppress(ResourceExistsError):
            self._container.create_container()
        self._container_ready = True

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._ensure_container()
        self._container.upload_blob(
            key,
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
            timeout=_TIMEOUT_SECONDS,
        )

    def get(self, key: str) -> bytes:
        try:
            downloader = self._container.download_blob(key, timeout=_TIMEOUT_SECONDS)
            return bytes(downloader.readall())
        except ResourceNotFoundError:
            raise BlobNotFoundError(key) from None

    def delete(self, key: str) -> None:
        with contextlib.suppress(ResourceNotFoundError):
            self._container.delete_blob(key, timeout=_TIMEOUT_SECONDS)

    def ping(self) -> bool:
        """Can we reach our container? Asks only what the least-privilege data role
        allows: the account's service properties are refused to a managed identity."""
        try:
            self._probe.get_container_client(
                self._container.container_name
            ).get_container_properties(timeout=_PROBE_TIMEOUT_SECONDS)
        except ResourceNotFoundError:
            return True  # storage answered; the first write creates the container
        except Exception:
            return False
        return True
