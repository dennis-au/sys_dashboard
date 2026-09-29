from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from forgejo_client import ForgejoClient, ForgejoConfigurationError, ForgejoError, ForgejoRequestError

from ..dependencies import get_forgejo_client
from ..profiles import public_profile
from ..records import database_connection, records
from ..reporting import summary


router = APIRouter(tags=["system"])


@router.get("/api/health")
def health() -> dict[str, str]:
    with database_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return {"status": "ok", "database": "connected"}


@router.get("/api/summary")
def system_summary() -> dict[str, Any]:
    """Return the stable, simulation-labelled summary used by the portal and future Grafana catalog."""

    return summary()


@router.get("/api/integrations/forgejo/readiness")
def forgejo_readiness(client: ForgejoClient = Depends(get_forgejo_client)) -> dict[str, Any]:
    try:
        readiness = client.readiness()
    except ForgejoConfigurationError as exc:
        raise HTTPException(status_code=503, detail="Forgejo service credential is unavailable.") from exc
    except ForgejoRequestError as exc:
        raise HTTPException(status_code=503, detail="Forgejo repository is not ready.") from exc
    except ForgejoError as exc:
        raise HTTPException(status_code=503, detail="Forgejo is not ready.") from exc
    return {
        "status": "ready",
        "repository": {
            "fullName": readiness.repository.full_name,
            "private": readiness.repository.private,
            "defaultBranch": readiness.repository.default_branch,
        },
    }


@router.get("/api/bootstrap")
def bootstrap() -> dict[str, list[dict[str, Any]]]:
    return {
        "hosts": records("hosts"),
        "runs": records("runs"),
        "credentials": records("credentials"),
        "managers": records("managers"),
        "collectionProfiles": [public_profile(profile) for profile in records("profiles")],
        "grafanaDashboards": records("dashboards"),
    }
