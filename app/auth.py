"""Short-lived service-account impersonation without service-account keys."""

from functools import lru_cache

import google.auth
from google.auth import credentials, impersonated_credentials

BIGQUERY_READ_SCOPES = ("https://www.googleapis.com/auth/bigquery",)
BIGQUERY_WRITE_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)


@lru_cache(maxsize=256)
def impersonate(
    target_service_account: str,
    scopes: tuple[str, ...] = BIGQUERY_READ_SCOPES,
    lifetime_seconds: int = 1800,
) -> credentials.Credentials:
    """Return cached short-lived credentials for a registered target identity."""
    source_credentials, _ = google.auth.default(
        scopes=("https://www.googleapis.com/auth/cloud-platform",)
    )
    return impersonated_credentials.Credentials(  # type: ignore[no-untyped-call]
        source_credentials=source_credentials,
        target_principal=target_service_account,
        target_scopes=list(scopes),
        lifetime=lifetime_seconds,
    )


def default_credentials() -> credentials.Credentials:
    creds, _ = google.auth.default(scopes=BIGQUERY_WRITE_SCOPES)
    return creds
