"""Integration tests for CLI -- Step 8."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from rgen.cli import main, _build_parser


# ---------------------------------------------------------------------------
# --list-patterns
# ---------------------------------------------------------------------------

def test_list_patterns_finds_psm_stack(capsys: pytest.CaptureFixture) -> None:
    ret = main(["--list-patterns"])
    out = capsys.readouterr().out
    assert ret == 0
    assert "psm_stack" in out


def test_list_patterns_empty_kb(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    ret = main(["--list-patterns", "--kb", str(tmp_path / "empty_kb")])
    out = capsys.readouterr().out
    assert ret == 0
    assert "Nessun pattern" in out


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------

def test_check_passes_on_valid_project(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    gh = tmp_path / ".github"
    experts = gh / "esperti"
    experts.mkdir(parents=True)
    routing = {
        "s1": {"agent": "dev", "keywords": ["k"], "files": [".github/esperti/esperto_dev.md"], "context": "", "priority": "high"},
        "s2": {"agent": "dev", "keywords": ["k2"], "files": [], "context": "", "priority": "medium"},
        "s3": {"agent": "dev", "keywords": ["k3"], "files": [], "context": "", "priority": "low"},
    }
    (gh / "routing-map.json").write_text(json.dumps(routing))
    (experts / "esperto_dev.md").write_text("# Dev")
    (gh / "copilot-instructions.md").write_text("## DISPATCHER\nrouter.py")
    (gh / "subagent-brief.md").write_text("# Brief")
    (gh / "AGENT_REGISTRY.md").write_text("# Registry\ndev")
    (gh / "router.py").write_text("# stub")
    (gh / "interventions.py").write_text("# stub")
    (gh / "mcp_server.py").write_text("# stub")

    ret = main(["--check", "--target", str(tmp_path)])
    assert ret == 0


def test_check_fails_on_missing_target(capsys: pytest.CaptureFixture) -> None:
    ret = main(["--check", "--target", "/nonexistent/path"])
    assert ret == 2


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------

def test_dry_run_does_not_write_files(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    ret = main([
        "--dry-run",
        "--pattern", "psm_stack",
        "--name", "test-project",
        "--target", str(tmp_path / "output"),
    ])
    assert ret == 0
    # No files should have been written
    assert not (tmp_path / "output" / ".github").exists()

    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert ".github/routing-map.json" in out


# ---------------------------------------------------------------------------
# --restore
# ---------------------------------------------------------------------------

def test_restore_lists_backups_when_no_timestamp(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    ret = main(["--restore", "--target", str(tmp_path)])
    assert ret == 0
    out = capsys.readouterr().out
    assert "Nessun backup" in out


def test_restore_fails_on_missing_timestamp(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    ret = main(["--restore", "--target", str(tmp_path), "--timestamp", "99990101_999999"])
    assert ret == 2


# ---------------------------------------------------------------------------
# --direct (non-interactive generation)
# ---------------------------------------------------------------------------

def test_direct_generates_github_dir(tmp_path: Path) -> None:
    ret = main([
        "--direct",
        "--pattern", "psm_stack",
        "--name", "acme",
        "--target", str(tmp_path / "acme"),
    ])
    assert ret == 0
    assert (tmp_path / "acme" / ".github" / "routing-map.json").exists()
    assert (tmp_path / "acme" / ".github" / "router.py").exists()
    assert (tmp_path / "acme" / ".github" / "copilot-instructions.md").exists()
    assert (tmp_path / "acme" / ".github" / "standard" / "README.md").exists()


def test_direct_routing_map_is_valid_json(tmp_path: Path) -> None:
    ret = main([
        "--direct",
        "--pattern", "psm_stack",
        "--name", "acme",
        "--target", str(tmp_path / "acme"),
    ])
    assert ret == 0
    routing_map = json.loads(
        (tmp_path / "acme" / ".github" / "routing-map.json").read_text(encoding="utf-8")
    )
    scenarios = {k: v for k, v in routing_map.items() if not k.startswith("_")}
    assert len(scenarios) >= 5


def test_direct_scratch_generates_from_domains(tmp_path: Path) -> None:
    ret = main([
        "--direct",
        "--name", "scratch-app",
        "--target", str(tmp_path / "scratch"),
        "--tech", "python,fastapi",
        "--domains", "api,auth",
    ])
    assert ret == 0
    routing_map = json.loads(
        (tmp_path / "scratch" / ".github" / "routing-map.json").read_text(encoding="utf-8")
    )
    assert "api" in routing_map or "auth" in routing_map
    assert (tmp_path / "scratch" / ".github" / "standard" / "python-style-guide.md").exists()


# ---------------------------------------------------------------------------
# Parser structure
# ---------------------------------------------------------------------------

def test_parser_modes_are_mutually_exclusive() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--direct", "--dry-run"])


# ---------------------------------------------------------------------------
# --update
# ---------------------------------------------------------------------------

def test_update_copies_core_files_to_existing_project(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    (tmp_path / ".github").mkdir()
    ret = main(["--update", "--target", str(tmp_path)])
    assert ret == 0
    out = capsys.readouterr().out
    assert "aggiornati" in out
    assert (tmp_path / ".github" / "router.py").exists()


def test_update_fails_when_no_github_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    ret = main(["--update", "--target", str(tmp_path)])
    assert ret == 2
    err = capsys.readouterr().err
    assert ".github" in err


def test_update_creates_backup(tmp_path: Path) -> None:
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "router.py").write_text("# old content")
    ret = main(["--update", "--target", str(tmp_path)])
    assert ret == 0
    backup_root = gh / ".rgen-backups"
    assert backup_root.exists()
    backup_slots = list(backup_root.iterdir())
    assert len(backup_slots) >= 1


def test_update_flat_copies_to_root(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    ret = main(["--update", "--flat", "--target", str(tmp_path)])
    assert ret == 0
    out = capsys.readouterr().out
    assert "aggiornati" in out
    assert (tmp_path / "router.py").exists()


def test_update_flat_creates_backup_in_root(tmp_path: Path) -> None:
    (tmp_path / "router.py").write_text("# old")
    ret = main(["--update", "--flat", "--target", str(tmp_path)])
    assert ret == 0
    assert (tmp_path / ".rgen-backups").exists()
