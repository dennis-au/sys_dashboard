from collections.abc import Generator

from fastapi import HTTPException

from forgejo_client import ForgejoClient, ForgejoConfigurationError


def get_forgejo_client() -> Generator[ForgejoClient, None, None]:
    """Construct the client per request so startup does not require Forgejo secrets."""
    try:
        client = ForgejoClient.from_environment()
    except ForgejoConfigurationError as exc:
        raise HTTPException(status_code=503, detail="Forgejo configuration is unavailable.") from exc
    try:
        yield client
    finally:
        client.close()
