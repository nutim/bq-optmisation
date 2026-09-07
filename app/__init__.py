"""BigQuery FinOps Agent application package."""

from typing import Any

__all__ = ["app", "root_agent"]


def __getattr__(name: str) -> Any:
    """Expose ADK objects lazily so utility modules remain independently testable."""
    if name in __all__:
        from app.agent import app, root_agent

        return {"app": app, "root_agent": root_agent}[name]
    raise AttributeError(name)
