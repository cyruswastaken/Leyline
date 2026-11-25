"""Sign in to Azure Managed Redis with the container's managed identity.

Redis takes the identity's object id as the user name and a short-lived Entra access
token as the password. redis-py asks this provider for credentials whenever it opens a
connection (including a reconnect after Redis closed one whose token expired), so no
token is stored beyond its own lifetime.
"""

from __future__ import annotations

from redis.credentials import CredentialProvider

from leyline.auth.azure_identity import REDIS, ManagedIdentity


class EntraRedisCredentials(CredentialProvider):
    def __init__(self, identity: ManagedIdentity, username: str) -> None:
        self._identity = identity
        self._username = username

    def get_credentials(self) -> tuple[str, str]:
        return self._username, self._identity.token(REDIS).token
