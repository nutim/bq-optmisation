import base64
import json

import pytest
from pydantic import ValidationError

import app.models as models
from app.config import Settings
from app.events import decode_push_envelope
from app.models import AutonomyLevel, ProjectRegistration


def registration(reader_sa: str, project_id: str = "analytics-production") -> ProjectRegistration:
    return ProjectRegistration(
        project_id=project_id,
        locations=["eu"],
        reader_service_account=reader_sa,
        owner_email="owner@example.com",
        autonomy_level=AutonomyLevel.OBSERVE,
    )


def test_project_registration_rejects_cross_project_service_accounts() -> None:
    with pytest.raises(ValidationError):
        registration("bq-finops-reader@some-other-project.iam.gserviceaccount.com")


def test_project_registration_allows_allowlisted_sa_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        models,
        "get_settings",
        lambda: Settings(control_project_id="test-control", allowed_sa_projects="central-sa"),
    )
    registration("bq-finops-reader@central-sa.iam.gserviceaccount.com")


def test_project_registration_normalizes_locations() -> None:
    registration = ProjectRegistration(
        project_id="analytics-production",
        locations=["EU", "eu", "europe-west1"],
        reader_service_account=("bq-finops-reader@analytics-production.iam.gserviceaccount.com"),
        owner_email="owner@example.com",
        autonomy_level=AutonomyLevel.OBSERVE,
    )
    assert registration.locations == ["eu", "europe-west1"]


def test_project_registration_rejects_non_sa_identity() -> None:
    with pytest.raises(ValidationError):
        ProjectRegistration(
            project_id="analytics-production",
            locations=["eu"],
            reader_service_account="person@example.com",
            owner_email="owner@example.com",
        )


def test_pubsub_collection_event_decoding() -> None:
    data = json.dumps(
        {"project_id": "analytics-production", "reason": "project_registered"}
    ).encode()
    event = decode_push_envelope({"message": {"data": base64.b64encode(data).decode("ascii")}})
    assert event.project_id == "analytics-production"
    assert event.reason == "project_registered"
