"""Tests for PatternLoader (Step 2) and Adapter (Step 4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rgen.adapter import Adapter, PatternLoader
from rgen.models import ProjectProfile


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kb_dir(tmp_path: Path) -> Path:
    """A knowledge_base dir with one minimal valid pattern."""
    p = tmp_path / "knowledge_base" / "test_pattern"
    p.mkdir(parents=True)
    (p / "metadata.json").write_text(json.dumps({
        "id": "test_pattern",
        "name": "Test Pattern",
        "tech_stack": ["python", "docker"],
        "agents": ["developer", "ops"],
    }), encoding="utf-8")
    routing = {
        "_base_autoloaded": {"note": "ignored"},
        "scenario_a": {
            "agent": "developer",
            "keywords": ["python", "fastapi"],
            "files": [".github/esperti/esperto_developer.md"],
            "context": "API layer",
            "priority": "high",
        },
        "scenario_b": {
            "agent": "ops",
            "keywords": ["docker", "compose"],
            "files": [".github/esperti/esperto_ops.md"],
            "context": "Infrastructure",
            "priority": "medium",
        },
    }
    (p / "routing-map.json").write_text(json.dumps(routing), encoding="utf-8")
    return tmp_path / "knowledge_base"


# ---------------------------------------------------------------------------
# list_patterns
# ---------------------------------------------------------------------------

def test_list_patterns_returns_pattern_ids(kb_dir: Path) -> None:
    loader = PatternLoader(kb_dir)
    assert loader.list_patterns() == ["test_pattern"]


def test_list_patterns_empty_when_no_kb(tmp_path: Path) -> None:
    loader = PatternLoader(tmp_path / "missing_kb")
    assert loader.list_patterns() == []


def test_list_patterns_ignores_dirs_without_metadata(kb_dir: Path) -> None:
    (kb_dir / "incomplete_pattern").mkdir()  # no metadata.json
    loader = PatternLoader(kb_dir)
    assert "incomplete_pattern" not in loader.list_patterns()


# ---------------------------------------------------------------------------
# pattern_dir
# ---------------------------------------------------------------------------

def test_pattern_dir_returns_correct_path(kb_dir: Path) -> None:
    loader = PatternLoader(kb_dir)
    assert loader.pattern_dir("test_pattern") == kb_dir / "test_pattern"


def test_pattern_dir_raises_for_missing(kb_dir: Path) -> None:
    loader = PatternLoader(kb_dir)
    with pytest.raises(FileNotFoundError, match="Pattern not found"):
        loader.pattern_dir("nonexistent")


# ---------------------------------------------------------------------------
# load — happy path
# ---------------------------------------------------------------------------

def test_load_returns_metadata(kb_dir: Path) -> None:
    loader = PatternLoader(kb_dir)
    result = loader.load("test_pattern")
    assert result["metadata"]["id"] == "test_pattern"
    assert result["metadata"]["tech_stack"] == ["python", "docker"]


def test_load_returns_routing_map_without_base_autoloaded(kb_dir: Path) -> None:
    loader = PatternLoader(kb_dir)
    result = loader.load("test_pattern")
    assert "_base_autoloaded" not in result["routing_map"]
    assert "scenario_a" in result["routing_map"]
    assert "scenario_b" in result["routing_map"]


def test_load_returns_pattern_dir(kb_dir: Path) -> None:
    loader = PatternLoader(kb_dir)
    result = loader.load("test_pattern")
    assert result["pattern_dir"] == kb_dir / "test_pattern"


# ---------------------------------------------------------------------------
# load — error cases
# ---------------------------------------------------------------------------

def test_load_raises_when_pattern_missing(kb_dir: Path) -> None:
    loader = PatternLoader(kb_dir)
    with pytest.raises(FileNotFoundError):
        loader.load("ghost_pattern")


def test_load_raises_when_routing_map_missing(kb_dir: Path) -> None:
    extra = kb_dir / "no_map"
    extra.mkdir()
    (extra / "metadata.json").write_text(json.dumps({
        "id": "no_map", "name": "X", "tech_stack": [], "agents": []
    }), encoding="utf-8")
    loader = PatternLoader(kb_dir)
    with pytest.raises(FileNotFoundError, match="routing-map.json missing"):
        loader.load("no_map")


def test_load_raises_when_required_metadata_missing(kb_dir: Path) -> None:
    bad = kb_dir / "bad_meta"
    bad.mkdir()
    (bad / "metadata.json").write_text(json.dumps({"id": "bad_meta"}), encoding="utf-8")
    (bad / "routing-map.json").write_text("{}", encoding="utf-8")
    loader = PatternLoader(kb_dir)
    with pytest.raises(ValueError, match="missing fields"):
        loader.load("bad_meta")


def test_load_raises_on_id_mismatch(kb_dir: Path) -> None:
    mismatch = kb_dir / "real_name"
    mismatch.mkdir()
    (mismatch / "metadata.json").write_text(json.dumps({
        "id": "wrong_name", "name": "X", "tech_stack": [], "agents": []
    }), encoding="utf-8")
    (mismatch / "routing-map.json").write_text("{}", encoding="utf-8")
    loader = PatternLoader(kb_dir)
    with pytest.raises(ValueError, match="id mismatch"):
        loader.load("real_name")


# ---------------------------------------------------------------------------
# Integration: load real psm_stack pattern
# ---------------------------------------------------------------------------

def test_load_psm_stack_real(request: pytest.FixtureRequest) -> None:
    """Verifies the actual psm_stack pattern in knowledge_base/ loads correctly."""
    project_root = Path(request.fspath).parent.parent # type: ignore
    kb = project_root / "knowledge_base"
    if not (kb / "psm_stack").exists():
        pytest.skip("psm_stack pattern not found")
    loader = PatternLoader(kb)
    result = loader.load("psm_stack")
    assert result["metadata"]["id"] == "psm_stack"
    assert len(result["routing_map"]) >= 10
    assert "agents" in result["metadata"]
    assert "_base_autoloaded" not in result["routing_map"]


# ===========================================================================
# Adapter tests (Step 4)
# ===========================================================================

@pytest.fixture
def adapter_kb(tmp_path: Path) -> Path:
    """knowledge_base with one pattern ready for Adapter tests."""
    p = tmp_path / "knowledge_base" / "base_pattern"
    experts = p / "esperti"
    experts.mkdir(parents=True)
    (p / "metadata.json").write_text(json.dumps({
        "id": "base_pattern",
        "name": "Base Pattern",
        "tech_stack": ["python", "docker"],
        "agents": ["developer", "ops"],
        "domain_scenarios": ["domain_only"],
    }), encoding="utf-8")
    routing = {
        "generic_a": {"agent": "developer", "keywords": ["python"], "files": [".github/esperti/esperto_developer.md"], "context": "generic", "priority": "high"},
        "generic_b": {"agent": "ops", "keywords": ["docker"], "files": [".github/esperti/esperto_ops.md"], "context": "infra", "priority": "medium"},
        "domain_only": {"agent": "developer", "keywords": ["specific"], "files": [".github/esperti/esperto_developer.md"], "context": "domain", "priority": "low"},
    }
    (p / "routing-map.json").write_text(json.dumps(routing), encoding="utf-8")
    (experts / "esperto_developer.md").write_text("# Developer\n## Stack\n{{TECH}}\nProject: {{PROJECT_NAME}}", encoding="utf-8")
    (experts / "esperto_developer_extended.md").write_text("# Developer Extended\nSee esperto_developer.md", encoding="utf-8")
    (experts / "esperto_ops.md").write_text("# Ops\n## Stack\ndocker", encoding="utf-8")
    (experts / "MODULARIZATION-STRATEGY.md").write_text("# Strategy\nPattern details", encoding="utf-8")
    return tmp_path / "knowledge_base"


@pytest.fixture
def base_profile(tmp_path: Path) -> ProjectProfile:
    return ProjectProfile(
        project_name="acme",
        target_path=tmp_path / "output",
        pattern_id="base_pattern",
        template_vars={"PROJECT_NAME": "acme", "TECH": "python3.12"},
    )


# ---------------------------------------------------------------------------
# adapt — pattern path
# ---------------------------------------------------------------------------

def test_adapt_produces_routing_map_file(adapter_kb: Path, base_profile: ProjectProfile) -> None:
    adapter = Adapter(adapter_kb)
    result = adapter.adapt(base_profile)
    assert ".github/routing-map.json" in result


def test_adapt_routing_map_excludes_domain_scenarios(adapter_kb: Path, base_profile: ProjectProfile) -> None:
    adapter = Adapter(adapter_kb)
    result = adapter.adapt(base_profile)
    routing = json.loads(result[".github/routing-map.json"])
    assert "domain_only" not in routing
    assert "generic_a" in routing
    assert "generic_b" in routing


def test_adapt_renames_agent_in_routing_map(adapter_kb: Path, tmp_path: Path) -> None:
    profile = ProjectProfile(
        project_name="x",
        target_path=tmp_path,
        pattern_id="base_pattern",
        template_vars={"PROJECT_NAME": "x", "RENAME_DEVELOPER": "backend"},
    )
    adapter = Adapter(adapter_kb)
    result = adapter.adapt(profile)
    routing = json.loads(result[".github/routing-map.json"])
    assert routing["generic_a"]["agent"] == "backend"


def test_adapt_generates_expert_file_with_substitution(adapter_kb: Path, base_profile: ProjectProfile) -> None:
    adapter = Adapter(adapter_kb)
    result = adapter.adapt(base_profile)
    expert = result.get(".github/esperti/esperto_developer.md", "")
    assert "acme" in expert
    assert "python3.12" in expert
    assert "{{PROJECT_NAME}}" not in expert
    assert "{{TECH}}" not in expert


def test_adapt_copies_all_expert_markdown_files(adapter_kb: Path, base_profile: ProjectProfile) -> None:
    adapter = Adapter(adapter_kb)
    result = adapter.adapt(base_profile)

    assert ".github/esperti/esperto_developer_extended.md" in result
    assert ".github/esperti/MODULARIZATION-STRATEGY.md" in result


def test_adapt_renames_extended_expert_filename(adapter_kb: Path, tmp_path: Path) -> None:
    profile = ProjectProfile(
        project_name="x",
        target_path=tmp_path,
        pattern_id="base_pattern",
        template_vars={"PROJECT_NAME": "x", "RENAME_DEVELOPER": "backend"},
    )
    adapter = Adapter(adapter_kb)
    result = adapter.adapt(profile)

    assert ".github/esperti/esperto_backend_extended.md" in result
    assert ".github/esperti/esperto_developer_extended.md" not in result


def test_adapt_generates_agent_registry(adapter_kb: Path, base_profile: ProjectProfile) -> None:
    adapter = Adapter(adapter_kb)
    result = adapter.adapt(base_profile)
    assert ".github/AGENT_REGISTRY.md" in result
    registry = result[".github/AGENT_REGISTRY.md"]
    assert "developer" in registry
    assert "ops" in registry


def test_adapt_generates_copilot_instructions(adapter_kb: Path, base_profile: ProjectProfile) -> None:
    adapter = Adapter(adapter_kb)
    result = adapter.adapt(base_profile)
    assert ".github/copilot-instructions.md" in result
    ci = result[".github/copilot-instructions.md"]
    assert "acme" in ci
    assert "router.py" in ci
    assert ".github/standard/README.md" in ci


def test_adapt_generates_subagent_brief(adapter_kb: Path, base_profile: ProjectProfile) -> None:
    adapter = Adapter(adapter_kb)
    result = adapter.adapt(base_profile)
    assert ".github/subagent-brief.md" in result


def test_adapt_generates_standard_pack_from_pattern_stack(adapter_kb: Path, base_profile: ProjectProfile) -> None:
    adapter = Adapter(adapter_kb)
    result = adapter.adapt(base_profile)

    assert ".github/standard/README.md" in result
    assert ".github/standard/general-style.md" in result
    assert ".github/standard/python-style-guide.md" in result
    assert ".github/standard/template.py" in result
    assert ".github/standard/bash-style-guide.md" in result
    assert ".github/standard/template.sh" in result


# ---------------------------------------------------------------------------
# adapt — scratch path
# ---------------------------------------------------------------------------

def test_adapt_scratch_no_pattern_id(adapter_kb: Path, tmp_path: Path) -> None:
    profile = ProjectProfile(
        project_name="scratch",
        target_path=tmp_path,
        pattern_id="",
        tech_stack=["python"],
        domain_keywords=["api", "auth"],
    )
    adapter = Adapter(adapter_kb)
    result = adapter.adapt(profile)
    routing = json.loads(result[".github/routing-map.json"])
    assert "api" in routing
    assert "auth" in routing
    assert "troubleshooting" in routing


def test_adapt_scratch_generates_structured_expert_markdown(adapter_kb: Path, tmp_path: Path) -> None:
    profile = ProjectProfile(
        project_name="scratch",
        target_path=tmp_path,
        pattern_id="",
        tech_stack=["python", "fastapi"],
        domain_keywords=["api"],
    )
    adapter = Adapter(adapter_kb)
    result = adapter.adapt(profile)

    expert_md = result[".github/esperti/esperto_developer.md"]
    assert "## Missione" in expert_md
    assert "## Ambiti Coperti" in expert_md
    assert "## Workflow Operativo" in expert_md
    assert "## Deliverable Attesi" in expert_md
    assert "## Capability Blocks" in expert_md
    assert "<!-- CAPABILITY:DEBUG -->" in expert_md
    assert "<!-- CAPABILITY:OPTIMIZE -->" in expert_md
    assert "<!-- CAPABILITY:SECURITY_AUDIT -->" in expert_md
    assert "<!-- CAPABILITY:TESTING -->" in expert_md

    assert ".github/standard/README.md" in result
    assert ".github/standard/python-style-guide.md" in result
    assert ".github/standard/template.py" in result


def test_adapt_scratch_adds_db_performance_only_for_database_stack(adapter_kb: Path, tmp_path: Path) -> None:
    with_db = ProjectProfile(
        project_name="db-app",
        target_path=tmp_path / "with-db",
        pattern_id="",
        tech_stack=["python", "postgresql"],
        domain_keywords=["api"],
    )
    without_db = ProjectProfile(
        project_name="web-app",
        target_path=tmp_path / "without-db",
        pattern_id="",
        tech_stack=["python", "fastapi"],
        domain_keywords=["api"],
    )

    adapter = Adapter(adapter_kb)

    with_db_result = adapter.adapt(with_db)
    without_db_result = adapter.adapt(without_db)

    assert "<!-- CAPABILITY:DB_PERFORMANCE -->" in with_db_result[".github/esperti/esperto_developer.md"]
    assert "<!-- CAPABILITY:DB_PERFORMANCE -->" not in without_db_result[".github/esperti/esperto_developer.md"]
    assert ".github/standard/sql-style-guide.md" in with_db_result
    assert ".github/standard/template.sql" in with_db_result
    assert ".github/standard/sql-style-guide.md" not in without_db_result


def test_generated_registry_contains_overview_and_scenario_column(adapter_kb: Path, base_profile: ProjectProfile) -> None:
    adapter = Adapter(adapter_kb)
    result = adapter.adapt(base_profile)

    registry = result[".github/AGENT_REGISTRY.md"]
    assert "## Panoramica" in registry
    assert "| Agent | Domain | Version | Status | Scenari | File |" in registry


def test_generated_subagent_brief_contains_task_template(adapter_kb: Path, base_profile: ProjectProfile) -> None:
    adapter = Adapter(adapter_kb)
    result = adapter.adapt(base_profile)

    brief = result[".github/subagent-brief.md"]
    assert "## Contesto" in brief
    assert "## Agenti Disponibili" in brief
    assert "## Template di Incarico" in brief


# ---------------------------------------------------------------------------
# adapt_routing_map unit test
# ---------------------------------------------------------------------------

def test_adapt_routing_map_cleans_non_esperti_files(adapter_kb: Path, tmp_path: Path) -> None:
    profile = ProjectProfile(project_name="x", target_path=tmp_path, pattern_id="base_pattern")
    adapter = Adapter(adapter_kb)
    source = {
        "s1": {
            "agent": "developer",
            "keywords": ["k"],
            "files": [".github/esperti/esperto_developer.md", ".github/subdetail/something.md"],
            "context": "",
            "priority": "high",
        }
    }
    adapted = adapter.adapt_routing_map(source, profile, {})
    assert ".github/subdetail/something.md" not in adapted["s1"]["files"]
    assert ".github/esperti/esperto_developer.md" in adapted["s1"]["files"]

