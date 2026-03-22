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

        # Expert docs from pattern (all markdown files, including extended references)
        experts_dir = pattern_dir / "esperti"
        if experts_dir.exists():
            for src_file in sorted(experts_dir.rglob("*.md")):
                rel = src_file.relative_to(experts_dir)
                remapped_rel = rel.parent / self._remap_expert_filename(rel.name, agent_map)
                content = src_file.read_text(encoding="utf-8")
                adapted = self.adapt_expert_file(content, profile, agent_map)
                dest = f".github/esperti/{remapped_rel.as_posix()}"
                result[dest] = adapted

        # Meta files (generated)
        new_agents = [agent_map.get(a, a) for a in meta.get("agents", [])]
        result[".github/AGENT_REGISTRY.md"] = self._gen_agent_registry(adapted_map, profile, new_agents)
        result[".github/copilot-instructions.md"] = self._gen_copilot_instructions(adapted_map, profile, new_agents)
        result[".github/subagent-brief.md"] = self._gen_subagent_brief(profile, new_agents)
        result.update(self._generate_standard_pack(profile, list(meta.get("tech_stack", []))))

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
            role = self._scratch_role_profile(agent, profile.tech_stack)
            domain_line = ", ".join(domains)
            capability_blocks = self._render_capability_blocks(role["capabilities"])
            result[f".github/esperti/esperto_{agent}.md"] = (
                f"# Esperto {agent} - {profile.project_name}\n\n"
                f"**Ruolo**: {role['role_line']}\n"
                f"**Regola risposta**: quando agisci come {agent}, apri con `Agente {agent}:`.\n\n"
                f"---\n\n"
                f"## Missione\n"
                f"{role['mission']}\n\n"
                f"## Ambiti Coperti\n"
                f"- Dominio principale: {agent}\n"
                f"- Domini progetto: {domain_line}\n"
                f"- Stack di riferimento: {tech_label}\n"
                f"- Ciclo vita: analisi, implementazione, validazione, documentazione\n"
                f"- Cross-team: escalation verso orchestratore su task multi-layer\n\n"
                f"## Workflow Operativo\n"
                f"1. Triage: identifica layer, vincoli e priorita operative.\n"
                f"2. Diagnosi: esplicita root cause e impatto tecnico/funzionale.\n"
                f"3. Piano: proponi fix a basso rischio + alternativa strutturale.\n"
                f"4. Esecuzione: applica modifiche minimali e tracciabili.\n"
                f"5. Validazione: test funzionali/tecnici e verifica regressioni.\n"
                f"6. Chiusura: documenta decisioni, rischi residui e next steps.\n\n"
                f"## Checklist Qualita\n"
                f"- [ ] Requisiti e vincoli confermati\n"
                f"- [ ] Impatti su sicurezza/performance valutati\n"
                f"- [ ] Test minimi eseguiti o motivazione del limite\n"
                f"- [ ] Routing/agent selection coerente con il task\n"
                f"- [ ] Note operative aggiornate\n\n"
                f"## Deliverable Attesi\n"
                f"- Diagnosi sintetica con causa radice\n"
                f"- Piano di intervento con priorita\n"
                f"- Patch o proposta operativa verificabile\n"
                f"- Evidenze di test/check\n"
                f"- Raccomandazioni post-intervento\n\n"
                f"---\n\n"
                f"## Capability Blocks\n\n"
                f"{capability_blocks}"
            )
        result[".github/AGENT_REGISTRY.md"] = self._gen_agent_registry(routing_map, profile, agent_list)
        result[".github/copilot-instructions.md"] = self._gen_copilot_instructions(routing_map, profile, agent_list)
        result[".github/subagent-brief.md"] = self._gen_subagent_brief(profile, agent_list)
        result.update(self._generate_standard_pack(profile))
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
    def _remap_expert_filename(name: str, agent_map: dict[str, str]) -> str:
        """Renames expert markdown file name when it starts with ``esperto_<agent>``."""
        for old, new in agent_map.items():
            old_prefix = f"esperto_{old}"
            if name.startswith(old_prefix):
                return name.replace(old_prefix, f"esperto_{new}", 1)
        return name

    @staticmethod
    def _scratch_role_profile(agent: str, tech_stack: list[str]) -> dict[str, Any]:
        """Returns role metadata and capability set for scratch-generated agents."""
        a = agent.lower()
        normalized_stack = {item.lower() for item in tech_stack}
        db_stack = {"mariadb", "mysql", "postgres", "postgresql", "sqlite", "sqlserver", "oracle", "database", "db"}
        include_db_performance = bool(normalized_stack & db_stack)

        if a in {"orchestratore", "orchestrator"}:
            return {
                "role_line": "coordinamento tecnico e routing multi-agente",
                "mission": "Coordina specialisti, riduce ambiguita di routing e assicura esecuzione verificabile end-to-end.",
                "capabilities": ["TRIAGE", "COORDINATION", "ROUTING_AUDIT", "VALIDATE"],
            }
        if a in {"documentazione", "documentation", "docs"}:
            return {
                "role_line": "governance documentale, runbook e allineamento cross-file",
                "mission": "Mantiene documentazione affidabile, aggiornata e collegata ai cambiamenti tecnici reali.",
                "capabilities": ["DOC_SYNC", "RUNBOOK", "AUDIT", "VALIDATE"],
            }
        if a in {"sistemista", "ops", "devops", "infra"}:
            capabilities = ["INFRA_DEBUG", "CONFIG_REVIEW", "DISASTER_RECOVERY", "MONITORING"]
            if include_db_performance:
                capabilities.append("DB_PERFORMANCE")
            return {
                "role_line": "infrastruttura, deployment e affidabilita operativa",
                "mission": "Garantisce stabilita ambienti, configurazioni sicure e procedure di rollback testabili.",
                "capabilities": capabilities,
            }

        capabilities = ["DEBUG", "OPTIMIZE", "SECURITY_AUDIT", "TESTING", "VALIDATE"]
        if include_db_performance:
            capabilities.append("DB_PERFORMANCE")

        return {
            "role_line": "sviluppo applicativo, diagnostica e quality engineering",
            "mission": "Progetta e implementa soluzioni robuste, misurabili e sicure lungo tutto il ciclo di sviluppo.",
            "capabilities": capabilities,
        }

    @staticmethod
    def _render_capability_blocks(capability_ids: list[str]) -> str:
        """Builds markdown capability blocks for scratch-generated expert files."""
        library: dict[str, tuple[str, list[str]]] = {
            "TRIAGE": (
                "Modalita Triage",
                [
                    "Classifica il problema per layer (app, db, infra, docs).",
                    "Definisci ipotesi ordinate per probabilita prima del fix.",
                    "Raccogli evidenze minime riproducibili.",
                ],
            ),
            "COORDINATION": (
                "Modalita Coordination",
                [
                    "Assegna il task all'agente piu adatto con criterio esplicito.",
                    "Evita overlap tra specialisti e chiarisci ownership.",
                    "Consolida output in un piano operativo unico.",
                ],
            ),
            "ROUTING_AUDIT": (
                "Modalita Routing Audit",
                [
                    "Valuta coverage e overlap degli scenari di routing.",
                    "Preferisci interventi minimi e misurabili su keyword/scenari.",
                    "Riesegui stats/audit dopo ogni modifica al routing map.",
                ],
            ),
            "DOC_SYNC": (
                "Modalita Documentation Sync",
                [
                    "Allinea README, changelog e runbook ai cambiamenti tecnici.",
                    "Mantieni cross-reference e versioning coerenti.",
                    "Riduci duplicazioni, privilegia fonte unica di verita.",
                ],
            ),
            "RUNBOOK": (
                "Modalita Runbook",
                [
                    "Documenta pre-check, procedura, post-check e rollback.",
                    "Includi comandi ripetibili e output atteso.",
                    "Esplicita prerequisiti e rischi operativi.",
                ],
            ),
            "AUDIT": (
                "Modalita Audit",
                [
                    "Verifica coerenza tra stato reale e documentazione.",
                    "Segnala gap con severita e impatto.",
                    "Proponi azioni correttive con priorita.",
                ],
            ),
            "INFRA_DEBUG": (
                "Modalita Infra Debug",
                [
                    "Diagnostica servizi, rete e dipendenze runtime.",
                    "Valida configurazioni prima di riavvio/deploy.",
                    "Prevedi rollback immediato in caso di regressione.",
                ],
            ),
            "CONFIG_REVIEW": (
                "Modalita Config Review",
                [
                    "Controlla differenze tra stato atteso e stato configurato.",
                    "Verifica sintassi, path, credenziali e porte.",
                    "Esegui smoke test post-change.",
                ],
            ),
            "DISASTER_RECOVERY": (
                "Modalita Disaster Recovery",
                [
                    "Conferma backup disponibili e piano di restore.",
                    "Definisci RPO/RTO e sequenza di recovery.",
                    "Esegui test di integrita post-ripristino.",
                ],
            ),
            "MONITORING": (
                "Modalita Monitoring",
                [
                    "Definisci healthcheck e metriche minime di servizio.",
                    "Usa logging con segnali di errore ad alta priorita.",
                    "Traccia trend e anomalie rilevanti.",
                ],
            ),
            "DEBUG": (
                "Modalita Debug",
                [
                    "Isola root cause prima di proporre qualsiasi fix.",
                    "Distingui sintomo, causa e impatto utente.",
                    "Conferma ipotesi con evidenze verificabili.",
                ],
            ),
            "OPTIMIZE": (
                "Modalita Optimize",
                [
                    "Misura baseline prima di ottimizzare.",
                    "Intervieni sul collo di bottiglia reale.",
                    "Confronta metriche before/after.",
                ],
            ),
            "SECURITY_AUDIT": (
                "Modalita Security Audit",
                [
                    "Verifica input handling, autorizzazioni e esposizione dati.",
                    "Classifica finding per severita.",
                    "Proponi mitigazioni immediate e strutturali.",
                ],
            ),
            "TESTING": (
                "Modalita Testing",
                [
                    "Definisci criteri di successo prima dell'esecuzione.",
                    "Copri happy path ed edge case critici.",
                    "Evita fix senza test di regressione quando possibile.",
                ],
            ),
            "DB_PERFORMANCE": (
                "Modalita DB Performance",
                [
                    "Analizza query, indici e piani di esecuzione prima di intervenire.",
                    "Misura latenze e throughput before/after.",
                    "Distingui tra problemi di schema, query e carico runtime.",
                ],
            ),
            "VALIDATE": (
                "Modalita Validate",
                [
                    "Conferma il risultato con check riproducibili.",
                    "Segnala rischi residui e limiti della verifica.",
                    "Documenta chiaramente lo stato finale.",
                ],
            ),
        }

        blocks: list[str] = []
        for capability_id in capability_ids:
            if capability_id not in library:
                continue
            title, bullets = library[capability_id]
            bullet_lines = "\n".join(f"- {b}" for b in bullets)
            blocks.append(
                f"<!-- CAPABILITY:{capability_id} -->\n"
                f"### {title}\n"
                f"{bullet_lines}\n"
                f"<!-- END CAPABILITY -->"
            )
        return "\n\n".join(blocks) + "\n"

    def _generate_standard_pack(
        self,
        profile: ProjectProfile,
        tech_stack_override: list[str] | None = None,
    ) -> dict[str, str]:
        """Builds .github/standard docs and starter templates from detected languages."""
        stack = tech_stack_override if tech_stack_override is not None else profile.tech_stack
        detected = self._detect_languages(stack)
        files: dict[str, str] = {
            ".github/standard/README.md": self._gen_standard_readme(profile, detected),
            ".github/standard/general-style.md": self._gen_general_style(profile),
        }

        generators: dict[str, tuple[str, str, str]] = {
            "python": ("python-style-guide.md", self._gen_python_style(profile), self._gen_python_template()),
            "javascript": ("javascript-style-guide.md", self._gen_javascript_style(profile), self._gen_javascript_template()),
            "typescript": ("typescript-style-guide.md", self._gen_typescript_style(profile), self._gen_typescript_template()),
            "php": ("php-style-guide.md", self._gen_php_style(profile), self._gen_php_template()),
            "sql": ("sql-style-guide.md", self._gen_sql_style(profile), self._gen_sql_template()),
            "bash": ("bash-style-guide.md", self._gen_bash_style(profile), self._gen_bash_template()),
            "powershell": ("powershell-style-guide.md", self._gen_powershell_style(profile), self._gen_powershell_template()),
        }

        ext_map = {
            "python": "py",
            "javascript": "js",
            "typescript": "ts",
            "php": "php",
            "sql": "sql",
            "bash": "sh",
            "powershell": "ps1",
        }

        for language in detected:
            style_name, style_content, template_content = generators[language]
            files[f".github/standard/{style_name}"] = style_content
            files[f".github/standard/template.{ext_map[language]}"] = template_content

        return files

    @staticmethod
    def _detect_languages(tech_stack: list[str]) -> list[str]:
        """Infers language standards to generate from the project tech stack."""
        normalized = {item.lower() for item in tech_stack}
        language_map = {
            "python": {"python", "fastapi", "flask", "django", "celery", "pydantic"},
            "javascript": {"javascript", "node", "nodejs", "react", "vue", "svelte", "express", "next"},
            "typescript": {"typescript", "ts", "nestjs", "angular"},
            "php": {"php", "laravel", "symfony", "wordpress", "joomla"},
            "sql": {"postgres", "postgresql", "mysql", "mariadb", "sqlite", "sqlserver", "oracle", "database", "db"},
            "bash": {"bash", "shell", "linux", "docker", "kubernetes", "helm"},
            "powershell": {"powershell", "pwsh", "windows", "azure"},
        }
        ordered = ["python", "javascript", "typescript", "php", "sql", "bash", "powershell"]
        detected = [language for language in ordered if normalized & language_map[language]]
        return detected or ["python"]

    @staticmethod
    def _gen_standard_readme(profile: ProjectProfile, languages: list[str]) -> str:
        language_lines = "\n".join(f"- {language}" for language in languages)
        template_lines = "\n".join(
            f"- template.{ext}" for ext in [
                {"python": "py", "javascript": "js", "typescript": "ts", "php": "php", "sql": "sql", "bash": "sh", "powershell": "ps1"}[language]
                for language in languages
            ]
        )
        return (
            f"# Standard Pack - {profile.project_name}\n\n"
            f"## Scopo\n"
            f"Definisce stile di programmazione, convenzioni e template base per il progetto.\n\n"
            f"## Uso\n"
            f"- Consulta `general-style.md` per le regole trasversali.\n"
            f"- Usa la style guide del linguaggio prima di creare o rifattorizzare file.\n"
            f"- Parti dai template base per nuove classi, moduli, script o query.\n\n"
            f"## Linguaggi rilevati\n"
            f"{language_lines}\n\n"
            f"## Template disponibili\n"
            f"{template_lines}\n"
        )

    @staticmethod
    def _gen_general_style(profile: ProjectProfile) -> str:
        return (
            f"# General Coding Style - {profile.project_name}\n\n"
            f"## Principi\n"
            f"- Preferisci chiarezza, testabilita e cambi minimali.\n"
            f"- Fai emergere la root cause prima del fix.\n"
            f"- Evita accoppiamento nascosto e side effect impliciti.\n"
            f"- Nomina simboli, file e moduli in modo coerente con il dominio tecnico.\n"
            f"- Mantieni I/O, configurazione e log espliciti.\n\n"
            f"## Regole trasversali\n"
            f"- Input validati ai boundary del sistema.\n"
            f"- Error handling coerente e osservabile.\n"
            f"- Test o verifica riproducibile per ogni modifica sostanziale.\n"
            f"- Nessun hardcode di segreti o path sensibili.\n"
            f"- Commenti brevi solo se chiariscono una scelta non ovvia.\n"
        )

    @staticmethod
    def _gen_python_style(profile: ProjectProfile) -> str:
        return (
            f"# Python Style Guide - {profile.project_name}\n\n"
            f"- Type hints su funzioni pubbliche e return types espliciti.\n"
            f"- Docstring per moduli, classi e funzioni pubbliche.\n"
            f"- Funzioni piccole, I/O separato dalla logica.\n"
            f"- Preferisci pathlib, dataclass e stdlib quando sufficiente.\n"
            f"- Gestisci eccezioni ai boundary, non con catch generici silenziosi.\n"
        )

    @staticmethod
    def _gen_python_template() -> str:
        return (
            '"""Module summary."""\n\n'
            "from __future__ import annotations\n\n"
            "from dataclasses import dataclass\n\n\n"
            "@dataclass\n"
            "class ExampleService:\n"
            "    \"\"\"Small, testable service example.\"\"\"\n\n"
            "    name: str\n\n"
            "    def run(self, value: int) -> str:\n"
            "        if value < 0:\n"
            "            raise ValueError(\"value must be non-negative\")\n"
            "        return f\"{self.name}:{value}\"\n"
        )

    @staticmethod
    def _gen_javascript_style(profile: ProjectProfile) -> str:
        return (
            f"# JavaScript Style Guide - {profile.project_name}\n\n"
            f"- Usa const di default, let solo se serve mutazione.\n"
            f"- Funzioni piccole e naming esplicito.\n"
            f"- Async/await preferito a chain annidate.\n"
            f"- Valida input e gestisci errori di rete esplicitamente.\n"
            f"- Evita logica di business sparsa nel layer UI.\n"
        )

    @staticmethod
    def _gen_javascript_template() -> str:
        return (
            "export async function runTask(input) {\n"
            "  if (!input) {\n"
            "    throw new Error(\"input is required\");\n"
            "  }\n\n"
            "  return {\n"
            "    ok: true,\n"
            "    value: String(input).trim(),\n"
            "  };\n"
            "}\n"
        )

    @staticmethod
    def _gen_typescript_style(profile: ProjectProfile) -> str:
        return (
            f"# TypeScript Style Guide - {profile.project_name}\n\n"
            f"- Tipi espliciti ai boundary e modelli condivisi.\n"
            f"- Preferisci union types e object shapes piccoli.\n"
            f"- Evita any salvo casi eccezionali e localizzati.\n"
            f"- Separa types, transport layer e logica dominio.\n"
            f"- Usa result objects o error strategy coerente.\n"
        )

    @staticmethod
    def _gen_typescript_template() -> str:
        return (
            "type TaskResult = {\n"
            "  ok: boolean;\n"
            "  value: string;\n"
            "};\n\n"
            "export function runTask(input: string): TaskResult {\n"
            "  const value = input.trim();\n"
            "  if (!value) {\n"
            "    throw new Error(\"input is required\");\n"
            "  }\n\n"
            "  return { ok: true, value };\n"
            "}\n"
        )

    @staticmethod
    def _gen_php_style(profile: ProjectProfile) -> str:
        return (
            f"# PHP Style Guide - {profile.project_name}\n\n"
            f"- strict_types all'inizio del file.\n"
            f"- Type hints su parametri e return.\n"
            f"- Dipendenze esplicite via costruttore.\n"
            f"- Try/catch solo su operazioni rischiose con logging coerente.\n"
            f"- Metodi pubblici sopra helper privati, struttura prevedibile della classe.\n"
        )

    @staticmethod
    def _gen_php_template() -> str:
        return (
            "<?php\n"
            "declare(strict_types=1);\n\n"
            "final class ExampleService\n"
            "{\n"
            "    public function run(string $input): string\n"
            "    {\n"
            "        $value = trim($input);\n\n"
            "        if ($value === '') {\n"
            "            throw new InvalidArgumentException('Input is required');\n"
            "        }\n\n"
            "        return $value;\n"
            "    }\n"
            "}\n"
        )

    @staticmethod
    def _gen_sql_style(profile: ProjectProfile) -> str:
        return (
            f"# SQL Style Guide - {profile.project_name}\n\n"
            f"- Usa nomi tabelle e colonne coerenti e descrittivi.\n"
            f"- Evita SELECT * in query applicative.\n"
            f"- Indici e piani di esecuzione verificati prima di ottimizzare.\n"
            f"- DML multi-step dentro transazioni quando richiesto.\n"
            f"- Separa query operative, migrazioni e reportistica.\n"
        )

    @staticmethod
    def _gen_sql_template() -> str:
        return (
            "BEGIN;\n\n"
            "SELECT id, status\n"
            "FROM tasks\n"
            "WHERE status = 'pending'\n"
            "ORDER BY created_at DESC;\n\n"
            "COMMIT;\n"
        )

    @staticmethod
    def _gen_bash_style(profile: ProjectProfile) -> str:
        return (
            f"# Bash Style Guide - {profile.project_name}\n\n"
            f"- Usa set -euo pipefail negli script non banali.\n"
            f"- Quota sempre path e variabili shell potenzialmente ambigue.\n"
            f"- Controlla prerequisiti e dipendenze prima dell'esecuzione.\n"
            f"- Logga step rilevanti e fallisci in modo esplicito.\n"
            f"- Evita comandi distruttivi senza guardrail.\n"
        )

    @staticmethod
    def _gen_bash_template() -> str:
        return (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n\n"
            "main() {\n"
            "  local target=${1:-}\n"
            "  if [[ -z \"${target}\" ]]; then\n"
            "    echo \"usage: ./script.sh <target>\" >&2\n"
            "    exit 1\n"
            "  fi\n\n"
            "  echo \"processing ${target}\"\n"
            "}\n\n"
            "main \"$@\"\n"
        )

    @staticmethod
    def _gen_powershell_style(profile: ProjectProfile) -> str:
        return (
            f"# PowerShell Style Guide - {profile.project_name}\n\n"
            f"- Usa nomi cmdlet completi e parametri espliciti.\n"
            f"- Preferisci pipeline di oggetti a parsing testuale fragile.\n"
            f"- Valida input con param block e tipi forti dove utile.\n"
            f"- Gestisci errori con try/catch e -ErrorAction Stop.\n"
            f"- Mantieni script idempotenti quando possibile.\n"
        )

    @staticmethod
    def _gen_powershell_template() -> str:
        return (
            "param(\n"
            "    [Parameter(Mandatory = $true)]\n"
            "    [string]$Target\n"
            ")\n\n"
            "$ErrorActionPreference = 'Stop'\n\n"
            "try {\n"
            "    Write-Host \"Processing $Target\"\n"
            "}\n"
            "catch {\n"
            "    Write-Error $_\n"
            "    throw\n"
            "}\n"
        )

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
            f"### Programming standards\n"
            f"- Read `.github/standard/README.md` before adding new code.\n"
            f"- Use the language style guides under `.github/standard/`.\n"
            f"- Start from the generated templates when creating new modules or scripts.\n\n"
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
