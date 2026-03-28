"""CLI entry point for routing-generator."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Project-root relative paths (resolved at import time)
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent
_PROJECT_ROOT = _HERE.parent
_DEFAULT_KB = _PROJECT_ROOT / "knowledge_base"
_DEFAULT_CORE = _PROJECT_ROOT / "core"


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``rgen`` command.

    Returns:
        Exit code (0 = success, non-zero = failure).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.list_patterns:
            return _cmd_list_patterns(args)
        if args.check:
            return _cmd_check(args)
        if args.restore:
            return _cmd_restore(args)
        if args.update:
            return _cmd_update(args)
        if args.direct or args.dry_run:
            return _cmd_direct(args)
        return _cmd_interactive(args)
    except KeyboardInterrupt:
        print("\nInterrotto dall'utente.")
        return 1
    except Exception as exc:
        print(f"[ERRORE] {exc}", file=sys.stderr)
        return 2


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _cmd_list_patterns(args: argparse.Namespace) -> int:
    from rgen.adapter import PatternLoader

    kb = Path(args.kb or _DEFAULT_KB)
    loader = PatternLoader(kb)
    patterns = loader.list_patterns()
    if not patterns:
        print(f"Nessun pattern trovato in: {kb}")
        return 0
    print(f"Pattern disponibili in {kb}:\n")
    for pid in patterns:
        data = loader.load(pid)
        meta = data["metadata"]
        tech = ", ".join(meta.get("tech_stack", []))
        print(f"  {pid:<25} {meta['name']}")
        print(f"  {'':25} Stack: {tech}\n")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    from rgen.self_checker import SelfChecker

    target = Path(args.target or ".")
    if not target.exists():
        print(f"[ERRORE] Directory non trovata: {target}", file=sys.stderr)
        return 2

    print(f"Self-check su: {target}")
    checker = SelfChecker(target)
    report = checker.run_all()

    for item in report.passed:
        print(f"  [OK] {item}")
    for item in report.warnings:
        print(f"  [WARN] {item}")
    for item in report.errors:
        print(f"  [FAIL] {item}", file=sys.stderr)

    overall = "OK" if report.overall else "FAILED"
    print(f"\nRisultato: {overall} — {len(report.passed)} pass, {len(report.warnings)} warn, {len(report.errors)} errori")
    return 0 if report.overall else 1


def _cmd_update(args: argparse.Namespace) -> int:
    """Copies updated core files to an existing project without full regeneration.

    Uses ``--flat`` for projects where core files live directly in the target
    directory (not in ``.github/``), as in legacy flat-layout projects.
    """
    import shutil
    from rgen.backup import BackupEngine
    from rgen.writer import Writer

    target = Path(args.target or ".")
    core = Path(args.core or _DEFAULT_CORE)
    flat = getattr(args, "flat", False)

    if flat:
        # Legacy flat layout: core files live directly in target_dir
        backup_engine = BackupEngine(target / ".rgen-backups")
        written, errors = [], []
        for name in Writer.CORE_FILES:
            src = core / name
            if not src.exists():
                print(f"  [SKIP]   {name} (non trovato in core/)")
                continue
            dest = target / name
            try:
                backup_engine.backup_if_exists(dest)
                shutil.copy2(src, dest)
                written.append(name)
                print(f"  [UPDATE] {name}")
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                print(f"  [ERRORE] {name}: {exc}", file=sys.stderr)
        if errors:
            return 1
        print(f"\n{len(written)} file aggiornati in {target}  (backup in .rgen-backups/)")
        return 0

    github_dir = target / ".github"
    if not github_dir.exists():
        print(
            f"[ERRORE] Nessuna directory .github trovata in: {target}\n"
            "Usa 'rgen --direct' per creare un nuovo progetto, o '--flat' per layout root.",
            file=sys.stderr,
        )
        return 2

    writer = Writer(core)
    result = writer.copy_core_files(target)

    for f in result.files_written:
        print(f"  [UPDATE] {f.name}")
    for f in result.files_skipped:
        print(f"  [SKIP]   {Path(f).name} (non trovato in core/)")
    for err in result.errors:
        print(f"  [ERRORE] {err}", file=sys.stderr)

    if result.errors:
        return 1

    n = len(result.files_written)
    print(f"\n{n} file aggiornati in {github_dir}  (backup in .github/.rgen-backups/)")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    from rgen.backup import BackupEngine

    target = Path(args.target or ".")
    backup_root = target / ".github" / ".rgen-backups"
    engine = BackupEngine(backup_root)

    if args.timestamp:
        try:
            restored = engine.restore(args.timestamp, target / ".github")
            print(f"Ripristinati {len(restored)} file da '{args.timestamp}'")
        except FileNotFoundError as exc:
            print(f"[ERRORE] {exc}", file=sys.stderr)
            return 2
    else:
        backups = engine.list_backups()
        if not backups:
            print(f"Nessun backup trovato in: {backup_root}")
            return 0
        print(f"Backup disponibili in {backup_root}:\n")
        for b in backups:
            files = list(b.iterdir())
            print(f"  {b.name} ({len(files)} file)")
        print(f"\nUso: rgen --restore --target {target} --timestamp <nome>")
    return 0


def _cmd_direct(args: argparse.Namespace) -> int:
    """Non-interactive generation using CLI arguments."""
    from rgen.questionnaire import Questionnaire

    overrides: dict[str, str] = {"use_pattern": "y" if args.pattern else "n"}
    if args.pattern:
        overrides["pattern_id"] = args.pattern
    if args.name:
        overrides["project_name"] = args.name
    if args.target:
        overrides["target_path"] = args.target
    if args.tech:
        overrides["tech_stack"] = args.tech
    if args.domains:
        overrides["domain_keywords"] = args.domains

    kb = Path(args.kb or _DEFAULT_KB)
    core = Path(args.core or _DEFAULT_CORE)

    q = Questionnaire(kb)
    profile = q.run_with_defaults(overrides)

    return _run_generation(profile, core, dry_run=args.dry_run)


def _cmd_interactive(args: argparse.Namespace) -> int:
    """Full interactive interview."""
    from rgen.questionnaire import Questionnaire

    kb = Path(args.kb or _DEFAULT_KB)
    core = Path(args.core or _DEFAULT_CORE)

    q = Questionnaire(kb)
    profile = q.run()

    print(f"\nProfilo raccolto:")
    print(f"  Progetto: {profile.project_name}")
    print(f"  Pattern:  {profile.pattern_id or 'da zero'}")
    print(f"  Output:   {profile.target_path}")

    confirm = input("\nProcedere con la generazione? [Y/n]: ").strip().lower()
    if confirm in ("n", "no"):
        print("Annullato.")
        return 0
    return _run_generation(profile, core, dry_run=False)


# ---------------------------------------------------------------------------
# Shared generation pipeline
# ---------------------------------------------------------------------------

def _run_generation(profile, core: Path, dry_run: bool = False) -> int:
    from rgen.adapter import Adapter
    from rgen.writer import Writer
    from rgen.self_checker import SelfChecker

    kb = profile.target_path.parent.parent / "knowledge_base" if not profile.pattern_id else _DEFAULT_KB
    adapter = Adapter(_DEFAULT_KB)
    files = adapter.adapt(profile)

    if dry_run:
        print(f"\n[DRY-RUN] Verrebbero scritti {len(files)} file in: {profile.target_path}")
        for path in sorted(files):
            size = len(files[path])
            print(f"  {path} ({size} byte)")
        return 0

    writer = Writer(core)
    result = writer.generate(files, profile.target_path)

    print(f"\nGenerazione completata:")
    print(f"  Scritti:  {len(result.files_written)} file")
    print(f"  Saltati:  {len(result.files_skipped)} file")
    if result.errors:
        for err in result.errors:
            print(f"  [ERRORE] {err}", file=sys.stderr)
        return 1

    # Post-generation self-check
    print("\nSelf-check in corso...")
    checker = SelfChecker(profile.target_path)
    report = checker.run_all()
    for w in report.warnings:
        print(f"  [WARN] {w}")
    for e in report.errors:
        print(f"  [FAIL] {e}", file=sys.stderr)
    overall = "OK" if report.overall else "FAILED"
    print(f"  Risultato: {overall} ({len(report.passed)}/8 check)")

    return 0 if result.success and report.overall else 1


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rgen",
        description="routing-generator -- genera sistemi di routing AI per qualsiasi progetto",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--direct", action="store_true", help="Generazione non interattiva")
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", help="Mostra cosa verrebbe generato senza scrivere")
    mode.add_argument("--check", action="store_true", help="Esegui self-check su progetto esistente")
    mode.add_argument("--restore", action="store_true", help="Ripristina da backup")
    mode.add_argument("--update", action="store_true", help="Aggiorna i core files in un progetto esistente (senza rigenerare)")
    mode.add_argument("--list-patterns", dest="list_patterns", action="store_true", help="Mostra pattern disponibili")

    parser.add_argument("--pattern", help="Pattern ID (es: psm_stack)")
    parser.add_argument("--name", help="Nome del progetto")
    parser.add_argument("--target", help="Directory di output")
    parser.add_argument("--flat", action="store_true", help="Per --update: copia i core files nella root del target (layout piatto, es. progetti legacy)")
    parser.add_argument("--timestamp", help="Timestamp backup per --restore")
    parser.add_argument("--tech", help="Tecnologie (virgola-separate) per --direct senza pattern")
    parser.add_argument("--domains", help="Domini (virgola-separati) per --direct senza pattern")
    parser.add_argument("--kb", help=f"Directory knowledge_base (default: {_DEFAULT_KB})")
    parser.add_argument("--core", help=f"Directory core files (default: {_DEFAULT_CORE})")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
