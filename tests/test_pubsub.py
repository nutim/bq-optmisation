"""Pub/Sub push handler semantics: auth, permanent rejection, and retry mapping."""

import base64
import json
from typing import Any

import pytest
from fastapi import HTTPException

import app.api as api
from app.config import Settings
from app.events import verify_pubsub_oidc


def envelope(project_id: str, reason: str = "scheduled") -> dict[str, Any]:
    data = json.dumps({"project_id": project_id, "reason": reason}).encode()
    return {"message": {"data": base64.b64encode(data).decode("ascii")}}


class FakeRepo:
    def __init__(self, project: dict[str, Any] | None) -> None:
        self.project = project

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        return self.project if self.project and self.project["project_id"] == project_id else None


class FakeCollector:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def collect(
        self, project: dict[str, Any], locations: list[str] | None = None
    ) -> dict[str, int]:
        if self.error:
            raise self.error
        return {"jobs": 1, "tables": 1, "recommendations": 0}


class FakeDetectors:
    def run(self, project_id: str | None = None) -> dict[str, int]:
        return {"EXPENSIVE_RECURRING_QUERY": 1}


def test_invalid_envelope_is_acked_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "store", lambda: FakeRepo(None))
    response = api.collect_from_pubsub({"message": {}})
    assert response["status"] == "rejected"


def test_unregistered_project_is_acked_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "store", lambda: FakeRepo(None))
    response = api.collect_from_pubsub(envelope("analytics-production"))
    assert response["status"] == "rejected"
    assert "not registered" in response["reason"]


def test_transient_collection_failure_returns_500(monkeypatch: pytest.MonkeyPatch) -> None:
    project = {"project_id": "analytics-production", "enabled": True}
    monkeypatch.setattr(api, "store", lambda: FakeRepo(project))
    monkeypatch.setattr(
        api, "ProjectCollector", lambda *a, **k: FakeCollector(RuntimeError("boom"))
    )
    monkeypatch.setattr(api, "DetectorService", lambda *a, **k: FakeDetectors())
    with pytest.raises(HTTPException) as excinfo:
        api.collect_from_pubsub(envelope("analytics-production"))
    assert excinfo.value.status_code == 500


def test_successful_collection_runs_detectors(monkeypatch: pytest.MonkeyPatch) -> None:
    project = {"project_id": "analytics-production", "enabled": True}
    monkeypatch.setattr(api, "store", lambda: FakeRepo(project))
    monkeypatch.setattr(api, "ProjectCollector", lambda *a, **k: FakeCollector())
    monkeypatch.setattr(api, "DetectorService", lambda *a, **k: FakeDetectors())
    response = api.collect_from_pubsub(envelope("analytics-production"))
    assert response == {
        "collection": {"jobs": 1, "tables": 1, "recommendations": 0},
        "detections": {"EXPENSIVE_RECURRING_QUERY": 1},
    }


def test_missing_token_rejected_when_verification_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api, "settings", Settings(control_project_id="test-control", verify_pubsub_tokens=True)
    )
    with pytest.raises(HTTPException) as excinfo:
        api.collect_from_pubsub(envelope("analytics-production"))
    assert excinfo.value.status_code == 401


def test_verify_pubsub_oidc_rejects_wrong_invoker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.events.id_token.verify_oauth2_token",
        lambda token, request, audience=None: {"email": "other@project.iam.gserviceaccount.com"},
    )
    settings = Settings(
        control_project_id="test-control",
        pubsub_invoker_service_account="expected@control.iam.gserviceaccount.com",
    )
    with pytest.raises(PermissionError):
        verify_pubsub_oidc("token", settings)


def test_verify_pubsub_oidc_accepts_expected_invoker(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_verify(token: str, request: object, audience: str | None = None) -> dict[str, Any]:
        captured["token"] = token
        captured["audience"] = audience
        return {"email": "expected@control.iam.gserviceaccount.com"}

    monkeypatch.setattr("app.events.id_token.verify_oauth2_token", fake_verify)
    settings = Settings(
        control_project_id="test-control",
        service_url="https://agent-abc.run.app",
        pubsub_invoker_service_account="expected@control.iam.gserviceaccount.com",
    )
    verify_pubsub_oidc("token", settings)
    assert captured == {
        "token": "token",
        "audience": "https://agent-abc.run.app",
    }
