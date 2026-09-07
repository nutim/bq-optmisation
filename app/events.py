"""Pub/Sub events used for immediate onboarding and scheduled collection."""

import base64
import json
from functools import lru_cache
from typing import cast

import google.auth.transport.requests
from google.cloud.pubsub_v1 import PublisherClient  # type: ignore[import-untyped]
from google.oauth2 import id_token

from app.config import Settings, get_settings
from app.logging import get_logger
from app.models import CollectionEvent

log = get_logger(__name__)


class EventPublisher:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.publisher = PublisherClient()

    def publish_collection(self, event: CollectionEvent) -> str:
        topic = self.publisher.topic_path(
            self.settings.control_project_id, self.settings.collection_topic
        )
        future = self.publisher.publish(
            topic,
            event.model_dump_json().encode("utf-8"),
            event_type="collect_project",
            project_id=event.project_id,
        )
        message_id = cast(str, future.result(timeout=30))
        log.info(
            "collection_published",
            topic=topic,
            project_id=event.project_id,
            reason=event.reason,
            message_id=message_id,
        )
        return message_id


@lru_cache
def get_publisher() -> EventPublisher:
    """Return a shared publisher so gRPC channels are not rebuilt per request."""
    return EventPublisher()


def decode_push_envelope(envelope: dict[str, object]) -> CollectionEvent:
    message = envelope.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("data"), str):
        raise ValueError("invalid Pub/Sub push envelope")
    decoded = base64.b64decode(message["data"]).decode("utf-8")
    return CollectionEvent.model_validate(json.loads(decoded))


def verify_pubsub_oidc(token: str, settings: Settings) -> None:
    """Verify a Google-signed OIDC token attached to a Pub/Sub push request.

    Rejects tokens that are not signed by Google, are expired, do not match the
    configured service URL audience, or are not minted for the expected invoker
    service account. This is defense in depth on top of Cloud Run IAM.
    """
    request = google.auth.transport.requests.Request()
    try:
        claims = id_token.verify_oauth2_token(
            token, request, audience=settings.service_url
        )  # type: ignore[no-untyped-call]
    except ValueError as exc:
        raise PermissionError(f"invalid Pub/Sub OIDC token: {exc}") from exc
    expected_sa = settings.pubsub_invoker_service_account
    if expected_sa and claims.get("email") != expected_sa:
        raise PermissionError(
            f"Pub/Sub token was minted for {claims.get('email')!r}, expected {expected_sa!r}"
        )
