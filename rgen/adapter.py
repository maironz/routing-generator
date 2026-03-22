"""Pattern loader and adapter -- transforms a pattern into a new project's routing system."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from rgen.models import ProjectProfile


class PatternLoader:
    """Loads and validates patterns from the knowledge_base directory.

    Args:
        knowledge_base_dir: Path containing pattern subdirectories.
    """

    REQUIRED_META_FIELDS = ("id", "name", "tech_stack", "agents")

    def __init__(self, knowledge_base_dir: Path) -> None:
        self._kb_dir = Path(knowledge_base_dir)

    def list_patterns(self) -> list[str]:
        """Returns all available pattern IDs (subdirectory names)."""
        if not self._kb_dir.exists():
            return []
        return sorted(
            d.name
            for d in self._kb_dir.iterdir()
            if d.is_dir() and (d / "metadata.json").exists()
        )

    def pattern_dir(self, pattern_id: str) -> Path:
        """Returns the directory for *pattern_id*.

        Raises:
            FileNotFoundError: If no pattern with that ID exists.
        """
        p = self._kb_dir / pattern_id
        if not p.exists():
            raise FileNotFoundError(f"Pattern not found: {pattern_id!r} in {self._kb_dir}")
        return p

    def load(self, pattern_id: str) -> dict[str, Any]:
        """Loads and validates a pattern.

        Returns a dict with keys:
        - ``metadata``: parsed metadata.json
        - ``routing_map``: parsed routing-map.json (without ``_base_autoloaded``)
        - ``pattern_dir``: Path to the pattern directory

        Args:
            pattern_id: ID of the pattern to load.

        Returns:
            Pattern data dictionary.

        Raises:
            FileNotFoundError: If pattern or required files are missing.
            ValueError: If metadata is missing required fields.
        """
        p_dir = self.pattern_dir(pattern_id)

        metadata = self._load_json(p_dir / "metadata.json")
        self._validate_metadata(metadata, pattern_id)

        routing_map_path = p_dir / "routing-map.json"
        if not routing_map_path.exists():
            raise FileNotFoundError(f"routing-map.json missing for pattern {pattern_id!r}")
        routing_map = self._load_json(routing_map_path)
        routing_map.pop("_base_autoloaded", None)

        return {
            "metadata": metadata,
            "routing_map": routing_map,
            "pattern_dir": p_dir,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_json(self, path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {path}: {exc}") from exc

    def _validate_metadata(self, meta: dict, pattern_id: str) -> None:
        missing = [f for f in self.REQUIRED_META_FIELDS if f not in meta]
        if missing:
            raise ValueError(
                f"Pattern {pattern_id!r} metadata missing fields: {missing}"
            )
        if meta.get("id") != pattern_id:
            raise ValueError(
                f"Pattern id mismatch: directory={pattern_id!r}, metadata={meta.get('id')!r}"
            )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class Adapter:
    """Transforms a loaded pattern into files ready to be written for a new project.

    Args:
        knowledge_base_dir: Path to the ``knowledge_base/`` directory.
    """

    # Files allowed in scenario files[] after adaptation (relative .github/ paths)
    _CORE_FILE_STEMS = frozenset({"router.py", "router_audit.py", "router_planner.py",
                                   "interventions.py", "mcp_server.py"})

    def __init__(self, knowledge_base_dir: Path) -> None:
        self._loader = PatternLoader(knowledge_base_dir)

    def adapt(self, profile: ProjectProfile) -> dict[str, str]:
        """Returns ``{relative_path: content}`` for all files to generate.

        The paths are relative to ``profile.target_path`` (typically the project root).

        Args:
            profile: Describes the target project.

        Returns:
            Dict mapping relative paths to file contents.

        Raises:
            FileNotFoundError: If pattern_id is set but not found.
        """
        if profile.pattern_id:
            pattern = self._loader.load(profile.pattern_id)
            return self._adapt_from_pattern(profile, pattern)
        return self._adapt_from_scratch(profile)

    # ------------------------------------------------------------------
    # Path A — from existing pattern
    # ------------------------------------------------------------------

    def _adapt_from_pattern(self, profile: ProjectProfile, pattern: dict) -> dict[str, str]:
        meta = pattern["metadata"]
        source_map = pattern["routing_map"]
        pattern_dir = pattern["pattern_dir"]

        agent_map = self._build_agent_map(profile, meta)
        adapted_map = self.adapt_routing_map(source_map, profile, agent_map, meta)
        result: dict[str, str] = {}

        # routing-map.json
        result[".github/routing-map.json"] = json.dumps(
            {"_base_autoloaded": {"note": "auto-generated by routing-generator"},
             **adapted_map},
            indent=2, ensure_ascii=True,
        )

        # Expert files from pattern
        experts_dir = pattern_dir / "esperti"
        for src_agent in meta.get("agents", []):
            src_file = experts_dir / f"esperto_{src_agent}.md"
            if src_file.exists():
                content = src_file.read_text(encoding="utf-8")
                adapted = self.adapt_expert_file(content, profile, agent_map)
                new_agent = agent_map.get(src_agent, src_agent)
                dest = f".github/esperti/esperto_{new_agent}.md"
                result[dest] = adapted

        # Meta files (generated)
        new_agents = [agent_map.get(a, a) for a in meta.get("agents", [])]
        result[".github/AGENT_REGISTRY.md"] = self._gen_agent_registry(adapted_map, profile, new_agents)
        result[".github/copilot-instructions.md"] = self._gen_copilot_instructions(adapted_map, profile, new_agents)
        result[".github/subagent-brief.md"] = self._gen_subagent_brief(profile, new_agents)

        return result

    # ------------------------------------------------------------------
    # Path B — from scratch (minimal scaffold)
    # ------------------------------------------------------------------

    def _adapt_from_scratch(self, profile: ProjectProfile) -> dict[str, str]:
        agents = profile.template_vars.get("AGENTS", "developer,documentazione,orchestratore")
        agent_list = [a.strip() for a in agents.split(",") if a.strip()]
        if not agent_list:
            agent_list = ["developer", "documentazione", "orchestratore"]

        tech_label = ", ".join(profile.tech_stack) if profile.tech_stack else "non specificato"
        domains = profile.domain_keywords or ["general"]

        routing_map: dict[str, Any] = {}
        for i, domain in enumerate(profile.domain_keywords):
            scenario_id = domain.lower().replace(" ", "_")
            routing_map[scenario_id] = {
                "agent": agent_list[0],
                "keywords": [domain] + (profile.tech_stack[:3] if i == 0 else []),
                "files": [f".github/esperti/esperto_{agent_list[0]}.md"],
                "context": f"{domain} domain",
                "priority": "high",
            }
        if not routing_map:
            routing_map["general"] = {
                "agent": agent_list[0],
                "keywords": profile.tech_stack or ["code"],
                "files": [f".github/esperti/esperto_{agent_list[0]}.md"],
                "context": "General development",
                "priority": "medium",
            }
        routing_map["troubleshooting"] = {
            "agent": agent_list[-1],
            "keywords": ["error", "debug", "fix", "problem", "issue"],
            "files": [f".github/esperti/esperto_{agent_list[-1]}.md"],
            "context": "Troubleshooting",
            "priority": "high",
        }

        result: dict[str, str] = {}
        result[".github/routing-map.json"] = json.dumps(
            {"_base_autoloaded": {"note": "auto-generated by routing-generator"}, **routing_map},
            indent=2, ensure_ascii=True,
        )
        for agent in agent_list:
            domain_line = ", ".join(domains)
            result[f".github/esperti/esperto_{agent}.md"] = (
                f"# Esperto {agent} - {profile.project_name}\n\n"
                f"## Missione\n"
                f"Guidare le decisioni tecniche per l'area {agent} del progetto {profile.project_name}.\n\n"
                f"## Ambito Operativo\n"
                f"- Dominio principale: {agent}\n"
                f"- Domini progetto: {domain_line}\n"
                f"- Stack di riferimento: {tech_label}\n\n"
                f"## Workflow\n"
                f"1. Analizza la richiesta e identifica vincoli e priorita.\n"
                f"2. Proponi una soluzione minima e verificabile.\n"
                f"3. Evidenzia rischi, test e impatti su file/configurazioni.\n\n"
                f"## Deliverable Attesi\n"
                f"- Piano operativo sintetico\n"
                f"- Modifiche implementative coerenti con il routing-map\n"
                f"- Verifica finale con test/check pertinenti\n"
            )
        result[".github/AGENT_REGISTRY.md"] = self._gen_agent_registry(routing_map, profile, agent_list)
        result[".github/copilot-instructions.md"] = self._gen_copilot_instructions(routing_map, profile, agent_list)
        result[".github/subagent-brief.md"] = self._gen_subagent_brief(profile, agent_list)
        return result

    # ------------------------------------------------------------------
    # Core adaptation logic (public for testing)
    # ------------------------------------------------------------------

    def adapt_routing_map(
        self,
        source_map: dict,
        profile: ProjectProfile,
        agent_map: dict[str, str],
        meta: dict | None = None,
    ) -> dict:
        """Filters and renames a source routing-map for the new project.

        - Removes scenarios listed in ``meta.domain_scenarios``
        - Renames agent fields using ``agent_map``
        - Cleans files[] to only keep .github/esperti/ and core files
        """
        domain_exclude = set((meta or {}).get("domain_scenarios", []))
        out: dict[str, Any] = {}
        for scenario_id, scenario in source_map.items():
            if scenario_id in domain_exclude:
                continue
            adapted = dict(scenario)
            old_agent = adapted.get("agent", "")
            adapted["agent"] = agent_map.get(old_agent, old_agent)
            adapted["files"] = self._remap_files(adapted.get("files", []), agent_map)
            out[scenario_id] = adapted
        return out

    def adapt_expert_file(
        self,
        content: str,
        profile: ProjectProfile,
        agent_map: dict[str, str],
    ) -> str:
        """Applies template substitution and agent renames to an expert file.

        Replaces ``{{VAR_NAME}}`` with values from ``profile.template_vars``
        and substitutes old agent names with new ones in the text.
        """
        result = self._substitute_vars(content, profile.template_vars)
        for old, new in agent_map.items():
            result = result.replace(f"esperto_{old}", f"esperto_{new}")
            result = result.replace(f"**{old}**", f"**{new}**")
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_agent_map(self, profile: ProjectProfile, meta: dict) -> dict[str, str]:
        """Builds {old_agent_name: new_agent_name} from profile.template_vars RENAME_* keys."""
        agent_map: dict[str, str] = {}
        for key, value in profile.template_vars.items():
            if key.startswith("RENAME_"):
                old = key[len("RENAME_"):].lower()
                agent_map[old] = value
        return agent_map

    def _remap_files(self, files: list[str], agent_map: dict[str, str]) -> list[str]:
        """Keeps only esperti/ and core files; renames agents in paths."""
        kept: list[str] = []
        for f in files:
            p = Path(f)
            if p.parent.name == "esperti":
                new_name = p.stem  # e.g. "esperto_fullstack"
                for old, new in agent_map.items():
                    new_name = new_name.replace(f"esperto_{old}", f"esperto_{new}")
                kept.append((p.parent / f"{new_name}{p.suffix}").as_posix())
            elif p.name in self._CORE_FILE_STEMS:
                kept.append(f)
        return kept

    @staticmethod
    def _substitute_vars(content: str, template_vars: dict[str, str]) -> str:
        for key, value in template_vars.items():
            content = content.replace(f"{{{{{key}}}}}", value)
        return content

    @staticmethod
    def _find_leftover_vars(content: str) -> list[str]:
        return re.findall(r"\{\{[A-Z_]+\}\}", content)

    def _gen_agent_registry(
        self, routing_map: dict, profile: ProjectProfile, agents: list[str]
    ) -> str:
        scenario_count = len(routing_map)
        scenario_by_agent = {
            a: sum(1 for s in routing_map.values() if s.get("agent") == a)
            for a in agents
        }
        rows = "\n".join(
            f"| {a} | {profile.project_name} domain | 1.0.0 | STABLE | {scenario_by_agent.get(a, 0)} "
            f"| .github/esperti/esperto_{a}.md |"
            for a in agents
        )
        return (
            f"# Agent Registry -- {profile.project_name}\n\n"
            f"## Panoramica\n"
            f"- Agenti totali: {len(agents)}\n"
            f"- Scenari instradati: {scenario_count}\n"
            f"- Stack: {', '.join(profile.tech_stack) or 'generic'}\n\n"
            f"## Tabella Agenti\n"
            f"| Agent | Domain | Version | Status | Scenari | File |\n"
            f"|-------|--------|---------|--------|---------|------|\n"
            f"{rows}\n\n"
            f"## Routing\n"
            f"Fonte: `.github/routing-map.json`\n"
        )

    def _gen_copilot_instructions(
        self, routing_map: dict, profile: ProjectProfile, agents: list[str]
    ) -> str:
        agent_table = "\n".join(
            f"| `{a}` | {profile.project_name} -- {a} domain |"
            for a in agents
        )
        scenario_list = "\n".join(f"- `{s}`" for s in list(routing_map)[:8])
        return (
            f"# {profile.project_name} -- AI Dispatcher\n\n"
            f"## DISPATCHER\n\n"
            f"### Session bootstrap\n"
            f"1. Run `python .github/router.py --stats` at session start.\n"
            f"2. Show header with model/agent/priority/routing summary.\n"
            f"3. Route each user request before implementing changes.\n\n"
            f"### Agents\n| Agent | Domain |\n|-------|--------|\n{agent_table}\n\n"
            f"### Key scenarios\n{scenario_list}\n\n"
            f"### Router commands\n"
            f"```\n"
            f"python .github/router.py --direct \"<query>\"\n"
            f"python .github/router.py --follow-up \"<query>\"\n"
            f"python .github/router.py --stats\n"
            f"python .github/router.py --audit\n"
            f"```\n\n"
            f"## PROJECT\n\n"
            f"**{profile.project_name}** | Stack: {', '.join(profile.tech_stack) or 'generic'}\n\n"
            f"## Postflight\n"
            f"- Verify chosen agent is coherent with request\n"
            f"- Run relevant tests before closing task\n"
            f"- Keep documentation and routing artifacts aligned\n"
        )

    def _gen_subagent_brief(self, profile: ProjectProfile, agents: list[str]) -> str:
        stack = ", ".join(profile.tech_stack) if profile.tech_stack else "generic"
        domain_list = ", ".join(profile.domain_keywords) if profile.domain_keywords else "general"
        return (
            f"# {profile.project_name} -- Subagent Brief\n\n"
            f"## Contesto\n"
            f"- Stack: {stack}\n"
            f"- Domini: {domain_list}\n"
            f"- Routing: `.github/routing-map.json`\n\n"
            f"## Agenti Disponibili\n"
            + "\n".join(f"- {a}" for a in agents)
            + "\n\n"
            f"## Template di Incarico\n"
            f"Obiettivo: <descrizione sintetica>\n\n"
            f"Vincoli:\n"
            f"- <vincolo 1>\n"
            f"- <vincolo 2>\n\n"
            f"Output richiesto:\n"
            f"- <modifiche>\n"
            f"- <test/verifiche>\n"
        )
