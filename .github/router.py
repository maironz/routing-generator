#!/usr/bin/env python3
"""
PSM Stack Dynamic Router — with Planner Integration + Subagent Optimization

Modes:
  python .github/router.py "query"              → Planner workflow (first request)
  python .github/router.py --direct "query"     → Direct keyword routing (no planner)
  python .github/router.py --follow-up "query"  → Minimal context for same-session follow-ups
  python .github/router.py --subagent "query"   → Compact brief for runSubagent prompts
  python .github/router.py --audit              → Scan codebase for routing map gaps
  python .github/router.py --stats              → Health metrics
  python .github/router.py PLAN_APPROVED        → Execute approved plan
  python .github/router.py PLAN_REJECTED: reason → Replan

Note: copilot-instructions.md is auto-loaded by VS Code into the system prompt.
      It is NOT included in router output to avoid redundant reads.
"""

import json
import sys
import re
from pathlib import Path

# Modular imports (split from monolithic router.py)
from router_audit import audit_routing_coverage, get_health_stats
from router_planner import handle_plan_approved, handle_plan_rejected, handle_new_query
from interventions import InterventionStore

ROUTING_MAP = Path(__file__).parent / "routing-map.json"
SUBAGENT_BRIEF = Path(__file__).parent / "subagent-brief.md"
CONFIDENCE_GATE = 0.55

# Agent → expert file mapping (single source of truth)
AGENT_EXPERT_MAP = {
    "fullstack":      ".github/esperti/esperto_fullstack.md",
    "sistemista":     ".github/esperti/esperto_sistemista.md",
    "documentazione": ".github/esperti/esperto_documentazione.md",
    "orchestratore":  ".github/esperti/esperto_orchestratore.md",
}

# Critical constraints that subagents must always respect
SUBAGENT_CONSTRAINTS = [
    "Non toccare WInApp/ (progetto Visual Studio separato)",
    "Let's Encrypt: NON abilitare mTLS su router pubblici, porta 80 aperta per ACME",
    "Sync VM↔NAS disabilitato — VM è source of truth",
    "Samba/CIFS first: modificare file da Windows (Z:\\), SSH solo per runtime",
    "Backup prima di modifiche production",
]

REPO_EXPLORATION_TRIGGERS = [
    "nessun scenario matchato",
    "routing ambiguo",
    "confidence sotto soglia",
    "file instradati insufficienti o incoerenti con il repo reale",
]


def _load_routes() -> dict:
    """Load routing map, skip metadata entries."""
    with open(ROUTING_MAP, "r", encoding="utf-8") as f:
        routes = json.load(f)
    return {k: v for k, v in routes.items() if isinstance(v, dict) and "keywords" in v}


def extract_capability(content: str, capability: str) -> str:
    """
    Extract a capability block from an expert file.
    Blocks are delimited by <!-- CAPABILITY:NAME --> ... <!-- END CAPABILITY -->
    Returns the block content stripped, or empty string if not found.
    """
    if not capability:
        return ""
    pattern = rf"<!-- CAPABILITY:{re.escape(capability)} -->(.*?)<!-- END CAPABILITY -->"
    match = re.search(pattern, content, re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""


def _resolve_capability(scenario_data: dict, agent: str) -> tuple[str | None, str]:
    """
    Resolve capability for a scenario.
    Returns (capability_name, capability_instructions) tuple.
    capability_name is None if not defined in scenario.
    capability_instructions is empty string if block not found in expert file.
    """
    capability = scenario_data.get("capability")
    if not capability:
        return None, ""

    expert_file = AGENT_EXPERT_MAP.get(agent)
    if not expert_file:
        return capability, ""

    # Resolve expert file path relative to this script's directory
    expert_path = Path(__file__).parent.parent / expert_file
    if not expert_path.exists():
        # Try relative to workspace root
        expert_path = Path(expert_file)
    if not expert_path.exists():
        import warnings
        warnings.warn(f"Expert file not found for capability {capability}: {expert_file}", stacklevel=2)
        return capability, ""

    try:
        content = expert_path.read_text(encoding="utf-8")
    except Exception:
        return capability, ""

    instructions = extract_capability(content, capability)
    if not instructions:
        # Silent warning: capability declared but block not found in expert file
        import sys
        print(f"[WARN] Capability {capability} declared but no block found in {expert_file}", file=sys.stderr)

    return capability, instructions


def _enrich_with_prior(result: dict, query: str, max_results: int = 3) -> dict:
    """Enrich routing result with prior interventions from memory."""
    try:
        store = InterventionStore()
        prior = store.search(query, limit=max_results)
        store.close()
        if prior:
            result["prior_interventions"] = [
                {
                    "ts": p["ts"],
                    "scenario": p["scenario"],
                    "resolution": p["resolution"][:200],
                    "outcome": p["outcome"],
                }
                for p in prior
            ]
    except Exception:
        pass  # Memory is optional — never block routing
    return result


def _score_scenarios(query: str, routes: dict) -> list[dict]:
    """Score scenarios with decision traces for explainable routing."""
    q = (query or "").lower()
    scored = []
    for key, data in routes.items():
        keywords = data.get("keywords", [])
        matched = [kw for kw in keywords if kw.lower() in q]
        score = len(matched)
        if score > 0:
            ratio = round(score / max(len(keywords), 1), 3)
            scored.append({
                "score": score,
                "ratio": ratio,
                "scenario": key,
                "data": data,
                "matched_keywords": matched,
            })
    scored.sort(reverse=True, key=lambda x: (x["score"], x["ratio"]))
    return scored


def _compute_confidence(scored: list[dict]) -> float:
    """Compute confidence in [0, 1] from top candidates and score margin."""
    if not scored:
        return 0.0

    best = scored[0]
    second = scored[1] if len(scored) > 1 else None
    best_score = best["score"]
    second_score = second["score"] if second else 0

    # Balance score margin and coverage ratio to avoid overconfident ties.
    margin_component = (best_score - second_score) / max(best_score, 1)
    ratio_component = best["ratio"]

    confidence = (0.65 * margin_component) + (0.35 * ratio_component)
    return round(max(0.0, min(confidence, 1.0)), 3)


def _is_ambiguous(scored: list[dict], confidence: float) -> bool:
    """Detect routing ambiguity when top scenarios are too close."""
    if len(scored) < 2:
        return False

    best = scored[0]
    second = scored[1]
    close_scores = (best["score"] - second["score"]) <= 1
    close_ratio = abs(best["ratio"] - second["ratio"]) <= 0.08

    return confidence < 0.45 or (close_scores and close_ratio)


def _build_routing_debug(scored: list[dict], max_candidates: int = 3) -> list[dict]:
    """Return compact routing traces to explain why a scenario won."""
    out = []
    for c in scored[:max_candidates]:
        out.append({
            "scenario": c["scenario"],
            "score": c["score"],
            "ratio": c["ratio"],
            "matched_keywords": c["matched_keywords"][:8],
            "agent": c["data"].get("agent", "orchestratore"),
        })
    return out


def _build_clarification_payload(scored: list[dict], mode: str) -> dict:
    """Create an orchestrator handoff payload with deterministic clarifying questions."""
    cands = _build_routing_debug(scored, max_candidates=2)
    option_lines = [
        {
            "label": f"{c['scenario']} ({c['agent']})",
            "description": f"match:{c['score']} ratio:{c['ratio']} kw:{', '.join(c['matched_keywords'][:3])}",
        }
        for c in cands
    ]

    return {
        "agent": "orchestratore",
        "files": [AGENT_EXPERT_MAP["orchestratore"]],
        "context": "Ambiguità routing: scenario multipli con score simile",
        "priority": "medium",
        "scenario": "_ambiguity_router",
        "mode": mode,
        "needs_clarification": True,
        "clarification": {
            "reason": "Top scenario troppo vicini per routing affidabile automatico",
            "questions": [
                {
                    "header": "dominio",
                    "question": "Quale dominio vuoi privilegiare per questa richiesta?",
                    "options": option_lines,
                }
            ],
            "candidates": cands,
        },
        "repo_exploration": _build_repo_exploration_policy(
            mode=mode,
            confidence=0.0,
            ambiguous=True,
        ),
    }


def _build_repo_exploration_policy(
    mode: str,
    confidence: float,
    *,
    fallback: bool = False,
    ambiguous: bool = False,
) -> dict:
    """Describe when the agent may widen search from routed files to the full repo."""
    if fallback:
        return {
            "allowed": True,
            "recommended_scope": "repo-fallback",
            "reason": "Nessuno scenario ha matchato la query: e' consentita esplorazione repo per autocorrezione.",
            "confidence_gate": CONFIDENCE_GATE,
            "triggers": REPO_EXPLORATION_TRIGGERS,
        }

    if ambiguous:
        return {
            "allowed": True,
            "recommended_scope": "clarify-then-repo-search",
            "reason": "Routing ambiguo: chiarisci il dominio o amplia la ricerca se i file instradati non bastano.",
            "confidence_gate": CONFIDENCE_GATE,
            "triggers": REPO_EXPLORATION_TRIGGERS,
        }

    if confidence < CONFIDENCE_GATE:
        return {
            "allowed": True,
            "recommended_scope": "routed-files-then-repo-search",
            "reason": "Confidence sotto soglia: parti dai file instradati e allarga al repo solo se emergono contraddizioni o buchi.",
            "confidence_gate": CONFIDENCE_GATE,
            "triggers": REPO_EXPLORATION_TRIGGERS,
        }

    return {
        "allowed": False,
        "recommended_scope": "routed-files-only",
        "reason": "Confidence sufficiente: usa prima i file instradati e amplia solo se il contesto reale li smentisce.",
        "confidence_gate": CONFIDENCE_GATE,
        "triggers": REPO_EXPLORATION_TRIGGERS,
    }


def route_query(query: str) -> dict:
    """Direct keyword routing (no planner). Returns full context for first request."""
    routes = _load_routes()
    scored = _score_scenarios(query, routes)

    if not scored:
        return {
            "agent": "orchestratore",
            "files": [AGENT_EXPERT_MAP["orchestratore"]],
            "context": "Fallback generico — nessuno scenario matchato",
            "priority": "low",
            "scenario": "_fallback",
            "mode": "direct",
            "confidence": 0.0,
            "repo_exploration": _build_repo_exploration_policy(
                mode="direct",
                confidence=0.0,
                fallback=True,
            ),
        }

    top = scored[0]
    score = top["score"]
    scenario_key = top["scenario"]
    best = top["data"]
    agent = best.get("agent")
    confidence = _compute_confidence(scored)
    routing_debug = _build_routing_debug(scored)

    if _is_ambiguous(scored, confidence):
        amb = _build_clarification_payload(scored, mode="direct")
        amb["confidence"] = confidence
        amb["routing_debug"] = routing_debug
        amb["repo_exploration"] = _build_repo_exploration_policy(
            mode="direct",
            confidence=confidence,
            ambiguous=True,
        )
        amb = _enrich_with_prior(amb, query)
        return amb

    result = {
        "agent": agent,
        "files": best.get("files", []),
        "context": best.get("context", ""),
        "priority": best.get("priority", "medium"),
        "scenario": scenario_key,
        "score": score,
        "confidence": confidence,
        "routing_debug": routing_debug,
        "mode": "direct",
        "repo_exploration": _build_repo_exploration_policy(
            mode="direct",
            confidence=confidence,
        ),
    }

    # Capability layer: extract if defined
    cap_name, cap_instructions = _resolve_capability(best, agent)
    if cap_name:
        result["capability"] = cap_name
    if cap_instructions:
        result["capability_instructions"] = cap_instructions

    # Intervention memory: enrich with prior similar interventions
    result = _enrich_with_prior(result, query)

    return result


def route_follow_up(query: str) -> dict:
    """
    Follow-up mode: for subsequent requests in the same session.
    Returns ONLY the agent-specific expert file — base context is already loaded.
    Skips supplementary files (checklists, vision, subdetails) that were loaded on first call.
    """
    routes = _load_routes()
    scored = _score_scenarios(query, routes)

    if not scored:
        return {
            "agent": "orchestratore",
            "files": [AGENT_EXPERT_MAP["orchestratore"]],
            "context": "Follow-up fallback",
            "priority": "low",
            "mode": "follow-up",
            "confidence": 0.0,
            "repo_exploration": _build_repo_exploration_policy(
                mode="follow-up",
                confidence=0.0,
                fallback=True,
            ),
        }

    top = scored[0]
    scenario_key = top["scenario"]
    best = top["data"]
    agent = best.get("agent", "orchestratore")
    confidence = _compute_confidence(scored)
    routing_debug = _build_routing_debug(scored)

    if _is_ambiguous(scored, confidence):
        amb = _build_clarification_payload(scored, mode="follow-up")
        amb["confidence"] = confidence
        amb["routing_debug"] = routing_debug
        amb["repo_exploration"] = _build_repo_exploration_policy(
            mode="follow-up",
            confidence=confidence,
            ambiguous=True,
        )
        amb = _enrich_with_prior(amb, query)
        return amb

    # In follow-up mode: load ONLY the agent expert file
    # Supplementary files were already loaded in the initial request
    expert_file = AGENT_EXPERT_MAP.get(agent)
    files = [expert_file] if expert_file else []

    result = {
        "agent": agent,
        "files": files,
        "context": best.get("context", ""),
        "priority": best.get("priority", "medium"),
        "scenario": scenario_key,
        "confidence": confidence,
        "routing_debug": routing_debug,
        "mode": "follow-up",
        "note": "Solo file agente — contesto base già in sessione",
        "repo_exploration": _build_repo_exploration_policy(
            mode="follow-up",
            confidence=confidence,
        ),
    }

    # Capability layer: maintain from scenario if same session
    cap_name, cap_instructions = _resolve_capability(best, agent)
    if cap_name:
        result["capability"] = cap_name
    if cap_instructions:
        result["capability_instructions"] = cap_instructions

    # Intervention memory: enrich with prior similar interventions
    result = _enrich_with_prior(result, query)

    return result


def route_subagent(query: str) -> dict:
    """
    Subagent mode: returns a compact context blob for runSubagent prompts.
    Includes:
    - subagent_brief: path to the ultra-compact project context file
    - subagent_prompt_prefix: pre-built text to prepend to subagent prompts
    - constraints: critical rules the subagent must respect
    - files: minimal file set (just the expert file, no base)
    """
    routes = _load_routes()
    scored = _score_scenarios(query, routes)

    if not scored:
        agent = "orchestratore"
        context = "Generic subagent task"
        scenario_key = "_fallback"
        confidence = 0.0
    else:
        top = scored[0]
        scenario_key = top["scenario"]
        best = top["data"]
        agent = best.get("agent", "orchestratore")
        context = best.get("context", "")
        confidence = _compute_confidence(scored)

    expert_file = AGENT_EXPERT_MAP.get(agent)

    # Build the prompt prefix that the main agent should prepend to subagent prompts
    brief_path = str(SUBAGENT_BRIEF)
    prompt_prefix = _build_subagent_prompt_prefix(agent, context)

    return {
        "agent": agent,
        "files": [expert_file] if expert_file else [],
        "context": context,
        "scenario": scenario_key,
        "mode": "subagent",
        "subagent_brief": ".github/subagent-brief.md",
        "subagent_prompt_prefix": prompt_prefix,
        "constraints": SUBAGENT_CONSTRAINTS,
        "confidence": confidence,
        "repo_exploration": _build_repo_exploration_policy(
            mode="subagent",
            confidence=confidence,
            fallback=not scored,
        ),
        "usage": (
            "Include subagent_prompt_prefix at the start of your runSubagent prompt. "
            "Read subagent-brief.md ONLY if the subagent needs project structure knowledge. "
            "For pure code searches or simple edits, the prompt_prefix alone is sufficient."
        )
    }


def _build_subagent_prompt_prefix(agent: str, context: str) -> str:
    """Build a minimal context prefix for subagent prompts."""
    return (
        f"[Progetto PSM Stack — {context}]\n"
        f"Ruolo: {agent} | VM: <SERVER_IP> | App: <APP_SHARE> (Samba) | Docs: <BACKUP_PATH>\\proxmoxConfig\n"
        f"Stack: Docker (Traefik + Apache/Joomla + MariaDB) su Ubuntu/Proxmox\n"
        f"Vincoli: Non toccare WInApp/. Let's Encrypt attivo (no mTLS pubblico). VM è source of truth.\n"
    )



# ─── CLI ───

def main():
    args = sys.argv[1:]

    if not args:
        print("""
╔════════════════════════════════════════════════════════════════╗
║       PSM Stack Router + Planner + Subagent Optimization      ║
╚════════════════════════════════════════════════════════════════╝

MODES:
  python .github/router.py "query"              → Planner workflow
  python .github/router.py --direct "query"     → Direct routing (skip planner)
  python .github/router.py --follow-up "query"  → Minimal context (same session)
  python .github/router.py --subagent "query"   → Compact brief for subagents

HEALTH:
  python .github/router.py --stats              → Health metrics (session start)
  python .github/router.py --audit              → Scan codebase for routing gaps

MEMORY:
  python .github/router.py --history "query"    → Search intervention memory (FTS5)
  python .github/router.py --history            → Intervention stats
  python .github/router.py --log-intervention   → Log new intervention

PLANNER:
  python .github/router.py PLAN_APPROVED
  python .github/router.py "PLAN_REJECTED: motivo"

EXAMPLES:
  python .github/router.py --direct "fix login API"
  python .github/router.py --follow-up "aggiungi validazione input"
  python .github/router.py --subagent "cerca tutti i file PHP con query SQL"
        """)
        sys.exit(0)

    # Parse mode flag
    mode = None
    if args[0] in ("--direct", "--follow-up", "--subagent", "--audit", "--stats", "--history", "--log-intervention"):
        mode = args[0].lstrip("-").replace("-", "_")
        query = " ".join(args[1:]).strip() if len(args) > 1 else ""
    else:
        query = " ".join(args).strip()

    # Handle stats mode (no query needed)
    if mode == "stats":
        stats = get_health_stats()
        # Compact one-liner for session header
        icons = {"ok": "[OK]", "warn": "[!!]", "crit": "[XX]"}
        m = stats["metrics"]
        line = (
            f"Routing: {m['scenarios']['value']}scn/{m['keywords']['value']}kw "
            f"| overlap:{m['overlap_pct']['value']}% "
            f"| router:{m['router_lines']['value']}L "
            f"| map:{m['routing_map_kb']['value']}KB "
            f"| {icons[stats['overall']]} {stats['overall'].upper()}"
        )
        print(line)
        # Warnings detail
        warns = [(k, v) for k, v in m.items() if v["status"] != "ok"]
        if warns:
            for k, v in warns:
                th_w = stats['thresholds'].get(f'{k}_warn', '?')
                th_c = stats['thresholds'].get(f'{k}_crit', '?')
                print(f"  {icons[v['status']]} {k}: {v['value']} (warn:{th_w} crit:{th_c})")
        print(json.dumps(stats, indent=2, ensure_ascii=True))
        sys.exit(0)

    # Handle audit mode (no query needed)
    if mode == "audit":
        # Force UTF-8 output (avoid cp1252 encoding issues on Windows redirect)
        if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')

        result = audit_routing_coverage()
        # Pretty-print audit results
        print(f"\n{'='*60}")
        print(f"🔍 ROUTING MAP AUDIT")
        print(f"{'='*60}")
        print(f"Scenari: {result['total_scenarios']} | Keywords: {result['total_keywords']}")
        print(f"Concetti trovati: {result['total_concepts']} | Coperti: {result['covered']} | Gap: {result['gaps']}")
        print(f"Copertura: {result['coverage_pct']}%")
        if result['gap_details']:
            print(f"\n{'─'*60}")
            print("⚠️  CONCETTI NON COPERTI:")
            print(f"{'─'*60}")
            for g in result['gap_details']:
                print(f"  • {g['concept']} ({g['type']})")
                print(f"    File: {g['source']}")
                print(f"    Keywords suggerite: {', '.join(g['suggested_keywords'])}")
        else:
            print("\n✅ Tutti i concetti sono coperti dalla routing map.")

        if result['_covered_details']:
            print(f"\n{'─'*60}")
            print("📋 CONCETTI COPERTI:")
            print(f"{'─'*60}")
            for c in result['_covered_details']:
                print(f"  ✓ {c['concept']} ({c['type']}) → {', '.join(c['matched_by'])}")

        print(f"\n{'='*60}")
        # JSON output: exclude internal _covered_details
        json_result = {k: v for k, v in result.items() if not k.startswith('_')}
        print("\nJSON:")
        print(json.dumps(json_result, indent=2, ensure_ascii=True))
        sys.exit(0)

    # Handle intervention memory modes
    if mode == "history":
        store = InterventionStore()
        if query:
            results = store.search(query)
            if not results:
                print("Nessun intervento trovato.")
            else:
                for r in results:
                    print(f"  [{r['ts'][:10]}] {r['agent']}/{r['scenario']}: {r['query'][:80]}")
                    if r.get('resolution'):
                        print(f"    → {r['resolution'][:120]}")
        else:
            print(json.dumps(store.stats(), indent=2, ensure_ascii=True))
        store.close()
        sys.exit(0)

    if mode == "log_intervention":
        # Expected format: --log-intervention agent|scenario|query|resolution|files|tags|outcome
        parts = query.split("|")
        if len(parts) < 4:
            print("Formato: --log-intervention agent|scenario|query|resolution[|files_csv|tags_csv|outcome]")
            sys.exit(1)
        store = InterventionStore()
        files = parts[4].split(",") if len(parts) > 4 and parts[4] else []
        tags = parts[5].split(",") if len(parts) > 5 and parts[5] else []
        outcome = parts[6].strip() if len(parts) > 6 and parts[6].strip() else "success"
        rid = store.log(
            agent=parts[0].strip(),
            scenario=parts[1].strip(),
            query=parts[2].strip(),
            resolution=parts[3].strip(),
            files_touched=files,
            tags=tags,
            outcome=outcome,
        )
        print(json.dumps({"logged": True, "id": rid}, indent=2))
        store.close()
        sys.exit(0)

    # Handle planner commands
    if query.lower() == "plan_approved":
        result = handle_plan_approved()
    elif query.lower().startswith("plan_rejected:"):
        reason = query[len("plan_rejected:"):].strip()
        result = handle_plan_rejected(reason)
    # Handle mode-specific routing
    elif mode == "direct":
        result = route_query(query)
    elif mode == "follow_up":
        result = route_follow_up(query)
    elif mode == "subagent":
        result = route_subagent(query)
    else:
        # Default: planner workflow
        result = handle_new_query(query)

    print(json.dumps(result, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
