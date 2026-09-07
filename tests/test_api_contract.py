from app.api import api


def test_combined_adk_and_control_routes_exist() -> None:
    routes = {route.path for route in api.routes}
    assert "/list-apps" in routes
    assert "/run_sse" in routes
    assert "/v1/projects" in routes
    assert "/v1/events/collection" in routes
    assert "/v1/actions/{action_id}/execute" in routes
    assert "/v1/delivery/jira" in routes
    assert "/v1/delivery/confluence" in routes
    assert "/v1/delivery/gitlab-mr" in routes
