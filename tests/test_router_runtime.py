"""Runtime tests for the generated router policy in .github/."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def runtime_router():
    repo_root = Path(__file__).parent.parent
    github_dir = repo_root / ".github"
    sys.path.insert(0, str(github_dir))
    try:
        spec = importlib.util.spec_from_file_location(
            "runtime_router_for_tests",
            github_dir / "router.py",
        )
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.remove(str(github_dir))


def test_route_query_fallback_allows_repo_exploration(runtime_router, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_router, "_load_routes", lambda: {})
    monkeypatch.setattr(runtime_router, "_enrich_with_prior", lambda result, query: result)

    result = runtime_router.route_query("zzqv unmatched semantic token")

    assert result["scenario"] == "_fallback"
    assert result["confidence"] == 0.0
    assert result["repo_exploration"]["allowed"] is True
    assert result["repo_exploration"]["recommended_scope"] == "repo-fallback"


def test_route_query_high_confidence_keeps_scope_restricted(runtime_router, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_router, "_enrich_with_prior", lambda result, query: result)
    monkeypatch.setattr(
        runtime_router,
        "_load_routes",
        lambda: {
            "backup_restore": {
                "agent": "sistemista",
                "keywords": ["backup", "restore", "snapshot"],
                "files": [".github/esperti/esperto_sistemista.md"],
                "context": "Backup operations",
                "priority": "high",
            }
        },
    )

    query = "backup restore snapshot"
    result = runtime_router.route_query(query)

    assert result["scenario"] != "_fallback"
    assert result["confidence"] >= runtime_router.CONFIDENCE_GATE
    assert result["repo_exploration"]["allowed"] is False
    assert result["repo_exploration"]["recommended_scope"] == "routed-files-only"
