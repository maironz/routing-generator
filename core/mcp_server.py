#!/usr/bin/env python3
"""
PSM Stack MCP Server — Model Context Protocol interface

Exposes the routing system and intervention memory as MCP tools
that AI agents can call natively (no CLI parsing needed).

Tools:
  1. route_query    — Route a user query to the appropriate agent
  2. search_history — Search intervention memory (FTS5)
  3. log_intervention — Record a completed intervention
  4. get_stats      — Health metrics for the routing system
  5. audit_coverage — Scan codebase for routing map gaps

Run:
  python .github/mcp_server.py          (stdio transport — for VS Code)
"""

import json
import sys
from pathlib import Path

# Ensure .github is on the path for sibling imports
sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP

from router_audit import audit_routing_coverage, get_health_stats

# Import routing functions — these need the router module
import importlib
router_mod = importlib.import_module("router")
route_query_fn = router_mod.route_query
route_follow_up_fn = router_mod.route_follow_up
route_subagent_fn = router_mod.route_subagent

from interventions import InterventionStore

# ─── Server Setup ───

mcp = FastMCP(
    "PSM Stack Router",
)


# ─── Tool 1: Route Query ───

@mcp.tool()
def route_query(
    query: str,
    mode: str = "direct",
) -> str:
    """
    Route a user query to the appropriate PSM Stack agent.

    Args:
        query: The user's question or task description
        mode: Routing mode - "direct" (default), "follow_up" (same session), or "subagent" (for runSubagent)

    Returns:
        JSON with agent, files, context, priority, scenario, capability,
        prior_interventions, and repo_exploration policy.
    """
    if mode == "follow_up":
        result = route_follow_up_fn(query)
    elif mode == "subagent":
        result = route_subagent_fn(query)
    else:
        result = route_query_fn(query)

    return json.dumps(result, indent=2, ensure_ascii=False)


# ─── Tool 2: Search History ───

@mcp.tool()
def search_history(
    query: str = "",
    limit: int = 10,
) -> str:
    """
    Search the intervention memory for past similar work.
    If no query is provided, returns aggregate statistics.

    Args:
        query: Search text (uses FTS5 full-text search with OR matching)
        limit: Maximum results to return (default: 10)

    Returns:
        JSON array of matching interventions, or stats object if no query
    """
    store = InterventionStore()
    try:
        if query:
            results = store.search(query, limit=limit)
            return json.dumps(results, indent=2, ensure_ascii=False)
        else:
            stats = store.stats()
            return json.dumps(stats, indent=2, ensure_ascii=False)
    finally:
        store.close()


# ─── Tool 3: Log Intervention ───

@mcp.tool()
def log_intervention(
    agent: str,
    scenario: str,
    query: str,
    resolution: str = "",
    files_touched: list[str] | None = None,
    tags: list[str] | None = None,
    duration_min: float | None = None,
    outcome: str = "success",
) -> str:
    """
    Record a completed intervention in the memory store.

    Args:
        agent: Agent that handled it (fullstack, sistemista, documentazione, orchestratore)
        scenario: Routing scenario that matched (e.g., database_optimization)
        query: Original user query/request
        resolution: Description of what was done
        files_touched: List of file paths that were modified
        tags: Categorization tags (e.g., ["performance", "sql"])
        duration_min: Estimated effort in minutes
        outcome: Result - "success", "partial", "failed", or "reverted"

    Returns:
        JSON with logged:true and the assigned ID
    """
    store = InterventionStore()
    try:
        rid = store.log(
            agent=agent,
            scenario=scenario,
            query=query,
            resolution=resolution,
            files_touched=files_touched,
            tags=tags,
            duration_min=duration_min,
            outcome=outcome,
        )
        return json.dumps({"logged": True, "id": rid}, indent=2)
    finally:
        store.close()


# ─── Tool 4: Get Stats ───

@mcp.tool()
def get_stats() -> str:
    """
    Get health metrics for the PSM Stack routing system.
    Includes scenario/keyword counts, overlap %, file sizes, and status indicators.

    Returns:
        JSON with overall status (ok/warn/crit), per-metric details, and thresholds
    """
    stats = get_health_stats()
    return json.dumps(stats, indent=2, ensure_ascii=False)


# ─── Tool 5: Audit Coverage ───

@mcp.tool()
def audit_coverage() -> str:
    """
    Scan the PSM Stack codebase for concepts (PHP namespaces, CLI scripts, DB tables)
    and check whether each is covered by at least one routing-map keyword.

    Returns:
        JSON with coverage percentage, covered concepts, and gap details with suggested keywords
    """
    result = audit_routing_coverage()
    # Exclude internal _covered_details from MCP output
    output = {k: v for k, v in result.items() if not k.startswith("_")}
    return json.dumps(output, indent=2, ensure_ascii=False)


# ─── Entry Point ───

if __name__ == "__main__":
    mcp.run(transport="stdio")
