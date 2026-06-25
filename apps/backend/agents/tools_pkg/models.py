"""
Tool Models and Constants
==========================

Defines tool name constants and configuration for magestic-ai MCP tools.

This module is the single source of truth for all tool definitions used by
the Claude Agent SDK client. Tool lists are organized by category:

- Base tools: Core file operations (Read, Write, Edit, etc.)
- Web tools: Documentation and research (WebFetch, WebSearch)
- MCP tools: External integrations (Context7, Graphiti, etc.)
- Magestic AI tools: Custom build management tools
"""

import os

# =============================================================================
# Base Tools (Built-in Claude Code tools)
# =============================================================================

# Core file operation tools
BASE_READ_TOOLS = ["Read", "Glob", "Grep"]
BASE_WRITE_TOOLS = ["Write", "Edit", "Bash"]

# Web tools for documentation lookup and research
# Always available to all agents for accessing external information
WEB_TOOLS = ["WebFetch", "WebSearch"]

# =============================================================================
# Magestic AI MCP Tools (Custom build management)
# =============================================================================

# Magestic AI MCP tool names (prefixed with mcp__magestic-ai__)
TOOL_UPDATE_SUBTASK_STATUS = "mcp__magestic-ai__update_subtask_status"
TOOL_GET_BUILD_PROGRESS = "mcp__magestic-ai__get_build_progress"
TOOL_RECORD_DISCOVERY = "mcp__magestic-ai__record_discovery"
TOOL_RECORD_GOTCHA = "mcp__magestic-ai__record_gotcha"
TOOL_GET_SESSION_CONTEXT = "mcp__magestic-ai__get_session_context"
TOOL_UPDATE_QA_STATUS = "mcp__magestic-ai__update_qa_status"
TOOL_TEST_MEMORY_INTEGRATION = "mcp__magestic-ai__test_memory_integration"

# All magestic-ai MCP tools (for permissions)
MAGESTIC_AI_TOOLS = [
    TOOL_UPDATE_SUBTASK_STATUS,
    TOOL_GET_BUILD_PROGRESS,
    TOOL_RECORD_DISCOVERY,
    TOOL_RECORD_GOTCHA,
    TOOL_GET_SESSION_CONTEXT,
    TOOL_UPDATE_QA_STATUS,
    TOOL_TEST_MEMORY_INTEGRATION,
]

# =============================================================================
# External MCP Tools
# =============================================================================

# Context7 MCP tools for documentation lookup (always enabled)
CONTEXT7_TOOLS = [
    "mcp__context7__resolve-library-id",
    "mcp__context7__get-library-docs",
]

# Graphiti MCP tools for knowledge graph memory (when GRAPHITI_MCP_URL is set)
# See: https://github.com/getzep/graphiti
GRAPHITI_MCP_TOOLS = [
    "mcp__graphiti-memory__search_nodes",  # Search entity summaries
    "mcp__graphiti-memory__search_facts",  # Search relationships between entities
    "mcp__graphiti-memory__add_episode",  # Add data to knowledge graph
    "mcp__graphiti-memory__get_episodes",  # Retrieve recent episodes
    "mcp__graphiti-memory__get_entity_edge",  # Get specific entity/relationship
]

# Graphify MCP tools for per-project knowledge graph (when the project has a
# graphify-out/graph.json — populated by docs_generator_service.refresh_graph).
# Distinct from Graphiti above: Graphiti is session memory across runs;
# graphify is the structural knowledge of the project itself (code + docs +
# transcripts + uploads).
GRAPHIFY_TOOLS = [
    "mcp__graphify__query_graph",
    "mcp__graphify__get_node",
    "mcp__graphify__get_neighbors",
    "mcp__graphify__shortest_path",
]

# CodeGraphContext (CGC) MCP tools for precise code-structure investigation
# (when the project has been indexed — i.e. a `.codegraphcontext/` folder
# exists, populated by `codegraphcontext index <path>` /
# docs_generator_service.refresh_codegraph). CGC parses source with
# tree-sitter into an embedded graph DB and answers caller/callee, call-chain,
# dead-code and complexity questions.
#
# Distinct from graphify above: graphify is the docs/transcripts/cross-cutting
# layer; CGC is the exact code-symbol layer. Both run side by side so agents
# can pick the right tool. This is a READ-ONLY set on purpose — indexing is a
# manual trigger, so the index/watch/delete tools are intentionally omitted.
# Server is registered under the key "codegraph", hence the mcp__codegraph__
# prefix.
CODEGRAPH_TOOLS = [
    "mcp__codegraph__find_code",
    "mcp__codegraph__analyze_code_relationships",
    "mcp__codegraph__find_dead_code",
    "mcp__codegraph__calculate_cyclomatic_complexity",
    "mcp__codegraph__find_most_complex_functions",
    "mcp__codegraph__execute_cypher_query",
    "mcp__codegraph__list_indexed_repositories",
    "mcp__codegraph__get_repository_stats",
]

# =============================================================================
# Browser Automation MCP Tools (QA agents only)
# =============================================================================

# Playwright MCP tools for web browser automation
# Used for web frontend validation (non-Electron web apps)
# Uses @playwright/mcp with headless Chromium for reliable Linux support.
# NOTE: Screenshots must be compressed (1280x720, quality 60, JPEG) to stay under
# Claude SDK's 1MB JSON message buffer limit. See GitHub issue #74.
PLAYWRIGHT_TOOLS = [
    "mcp__playwright__browser_navigate",
    "mcp__playwright__browser_take_screenshot",
    "mcp__playwright__browser_click",
    "mcp__playwright__browser_fill_form",
    "mcp__playwright__browser_select_option",
    "mcp__playwright__browser_hover",
    "mcp__playwright__browser_evaluate",
    "mcp__playwright__browser_snapshot",
    "mcp__playwright__browser_console_messages",
    "mcp__playwright__browser_press_key",
    "mcp__playwright__browser_wait_for",
    "mcp__playwright__browser_navigate_back",
    "mcp__playwright__browser_close",
]

# =============================================================================
# Agent Configuration Registry
# =============================================================================
# Single source of truth for phase → tools → MCP servers mapping.
# This enables phase-aware tool control and context window optimization.

AGENT_CONFIGS = {
    # ═══════════════════════════════════════════════════════════════════════
    # SPEC CREATION PHASES (Minimal tools, fast startup)
    # ═══════════════════════════════════════════════════════════════════════
    "spec_gatherer": {
        "tools": BASE_READ_TOOLS + WEB_TOOLS,
        "mcp_servers": [],  # No MCP needed - just reads project
        "magestic_ai_tools": [],
        "thinking_default": "medium",
    },
    "spec_researcher": {
        "tools": BASE_READ_TOOLS + WEB_TOOLS,
        "mcp_servers": ["context7"],  # Needs docs lookup
        "magestic_ai_tools": [],
        "thinking_default": "medium",
    },
    "spec_writer": {
        "tools": BASE_READ_TOOLS + BASE_WRITE_TOOLS,
        "mcp_servers": [],  # Just writes spec.md
        "magestic_ai_tools": [],
        "thinking_default": "high",
    },
    "spec_critic": {
        "tools": BASE_READ_TOOLS,
        "mcp_servers": [],  # Self-critique, no external tools
        "magestic_ai_tools": [],
        "thinking_default": "high",
    },
    "spec_discovery": {
        "tools": BASE_READ_TOOLS + WEB_TOOLS,
        "mcp_servers": [],
        "magestic_ai_tools": [],
        "thinking_default": "medium",
    },
    "spec_context": {
        "tools": BASE_READ_TOOLS,
        "mcp_servers": [],
        "magestic_ai_tools": [],
        "thinking_default": "medium",
    },
    "spec_validation": {
        "tools": BASE_READ_TOOLS,
        "mcp_servers": [],
        "magestic_ai_tools": [],
        "thinking_default": "high",
    },
    "spec_compaction": {
        "tools": BASE_READ_TOOLS + BASE_WRITE_TOOLS,
        "mcp_servers": [],
        "magestic_ai_tools": [],
        "thinking_default": "medium",
    },
    # ═══════════════════════════════════════════════════════════════════════
    # BUILD PHASES (Full tools + Graphiti memory)
    # ═══════════════════════════════════════════════════════════════════════
    "planner": {
        "tools": BASE_READ_TOOLS + BASE_WRITE_TOOLS + WEB_TOOLS,
        "mcp_servers": ["context7", "graphiti", "graphify", "codegraph", "magestic-ai"],
        "magestic_ai_tools": [
            TOOL_GET_BUILD_PROGRESS,
            TOOL_GET_SESSION_CONTEXT,
            TOOL_RECORD_DISCOVERY,
        ],
        "thinking_default": "high",
    },
    "coder": {
        "tools": BASE_READ_TOOLS + BASE_WRITE_TOOLS + WEB_TOOLS,
        "mcp_servers": ["context7", "graphiti", "graphify", "codegraph", "magestic-ai"],
        "magestic_ai_tools": [
            TOOL_UPDATE_SUBTASK_STATUS,
            TOOL_GET_BUILD_PROGRESS,
            TOOL_RECORD_DISCOVERY,
            TOOL_RECORD_GOTCHA,
            TOOL_GET_SESSION_CONTEXT,
            TOOL_TEST_MEMORY_INTEGRATION,
        ],
        "thinking_default": "none",  # Coding doesn't use extended thinking
    },
    # ═══════════════════════════════════════════════════════════════════════
    # QA PHASES (Read + test + browser + Graphiti memory)
    # ═══════════════════════════════════════════════════════════════════════
    "qa_reviewer": {
        # Read + Write/Edit (for QA reports and plan updates) + Bash (for tests)
        # Note: Reviewer writes to spec directory only (qa_report.md, implementation_plan.json)
        "tools": BASE_READ_TOOLS + BASE_WRITE_TOOLS + WEB_TOOLS,
        "mcp_servers": ["context7", "graphiti", "graphify", "codegraph", "magestic-ai", "browser"],
        "magestic_ai_tools": [
            TOOL_GET_BUILD_PROGRESS,
            TOOL_UPDATE_QA_STATUS,
            TOOL_GET_SESSION_CONTEXT,
            TOOL_TEST_MEMORY_INTEGRATION,
        ],
        "thinking_default": "high",
    },
    "qa_fixer": {
        "tools": BASE_READ_TOOLS + BASE_WRITE_TOOLS + WEB_TOOLS,
        "mcp_servers": ["context7", "graphiti", "graphify", "codegraph", "magestic-ai", "browser"],
        "magestic_ai_tools": [
            TOOL_UPDATE_SUBTASK_STATUS,
            TOOL_GET_BUILD_PROGRESS,
            TOOL_UPDATE_QA_STATUS,
            TOOL_RECORD_GOTCHA,
            TOOL_TEST_MEMORY_INTEGRATION,
        ],
        "thinking_default": "medium",
    },
    # ═══════════════════════════════════════════════════════════════════════
    # UTILITY PHASES (Minimal, no MCP)
    # ═══════════════════════════════════════════════════════════════════════
    "insights": {
        "tools": BASE_READ_TOOLS + WEB_TOOLS,
        "mcp_servers": [],
        "magestic_ai_tools": [],
        "thinking_default": "medium",
    },
    "merge_resolver": {
        "tools": [],  # Text-only analysis
        "mcp_servers": [],
        "magestic_ai_tools": [],
        "thinking_default": "low",
    },
    "commit_message": {
        "tools": [],
        "mcp_servers": [],
        "magestic_ai_tools": [],
        "thinking_default": "low",
    },
    "pr_reviewer": {
        "tools": BASE_READ_TOOLS + WEB_TOOLS,  # Read-only
        "mcp_servers": ["context7"],
        "magestic_ai_tools": [],
        "thinking_default": "high",
    },
    "pr_orchestrator_parallel": {
        "tools": BASE_READ_TOOLS + WEB_TOOLS,  # Read-only for parallel PR orchestrator
        "mcp_servers": ["context7"],
        "magestic_ai_tools": [],
        "thinking_default": "high",
    },
    "pr_followup_parallel": {
        "tools": BASE_READ_TOOLS
        + WEB_TOOLS,  # Read-only for parallel followup reviewer
        "mcp_servers": ["context7"],
        "magestic_ai_tools": [],
        "thinking_default": "high",
    },
    # ═══════════════════════════════════════════════════════════════════════
    # ANALYSIS PHASES
    # ═══════════════════════════════════════════════════════════════════════
    "analysis": {
        "tools": BASE_READ_TOOLS + WEB_TOOLS,
        "mcp_servers": ["context7"],
        "magestic_ai_tools": [],
        "thinking_default": "medium",
    },
    "batch_analysis": {
        "tools": BASE_READ_TOOLS + WEB_TOOLS,
        "mcp_servers": [],
        "magestic_ai_tools": [],
        "thinking_default": "low",
    },
    "batch_validation": {
        "tools": BASE_READ_TOOLS,
        "mcp_servers": [],
        "magestic_ai_tools": [],
        "thinking_default": "low",
    },
    # ═══════════════════════════════════════════════════════════════════════
    # ROADMAP & IDEATION
    # ═══════════════════════════════════════════════════════════════════════
    "roadmap_discovery": {
        "tools": BASE_READ_TOOLS + WEB_TOOLS,
        "mcp_servers": ["context7"],
        "magestic_ai_tools": [],
        "thinking_default": "high",
    },
    "competitor_analysis": {
        "tools": BASE_READ_TOOLS + WEB_TOOLS,
        "mcp_servers": ["context7"],  # WebSearch for competitor research
        "magestic_ai_tools": [],
        "thinking_default": "high",
    },
    "ideation": {
        "tools": BASE_READ_TOOLS + WEB_TOOLS,
        "mcp_servers": [],
        "magestic_ai_tools": [],
        "thinking_default": "high",
    },
}


# =============================================================================
# Agent Config Helper Functions
# =============================================================================


def get_agent_config(agent_type: str) -> dict:
    """
    Get full configuration for an agent type.

    Args:
        agent_type: The agent type identifier (e.g., 'coder', 'planner', 'qa_reviewer')

    Returns:
        Configuration dict containing tools, mcp_servers, magestic_ai_tools, thinking_default

    Raises:
        ValueError: If agent_type is not found in AGENT_CONFIGS (strict mode)
    """
    if agent_type not in AGENT_CONFIGS:
        raise ValueError(
            f"Unknown agent type: '{agent_type}'. "
            f"Valid types: {sorted(AGENT_CONFIGS.keys())}"
        )
    return AGENT_CONFIGS[agent_type]


def _map_mcp_server_name(
    name: str, custom_server_ids: list[str] | None = None
) -> str | None:
    """
    Map user-friendly MCP server names to internal identifiers.
    Also accepts custom server IDs directly.

    Args:
        name: User-provided MCP server name
        custom_server_ids: List of custom server IDs to accept as-is

    Returns:
        Internal server identifier or None if not recognized
    """
    if not name:
        return None
    mappings = {
        "context7": "context7",
        "graphiti-memory": "graphiti",
        "graphiti": "graphiti",
        "graphify": "graphify",
        "codegraph": "codegraph",
        "codegraphcontext": "codegraph",
        "cgc": "codegraph",
        "playwright": "playwright",
        "puppeteer": "playwright",  # backward compat: puppeteer maps to playwright
        "magestic-ai": "magestic-ai",
    }
    # Check if it's a known mapping
    mapped = mappings.get(name.lower().strip())
    if mapped:
        return mapped
    # Check if it's a custom server ID (accept as-is)
    if custom_server_ids and name in custom_server_ids:
        return name
    return None


def get_required_mcp_servers(
    agent_type: str,
    project_capabilities: dict | None = None,
    mcp_config: dict | None = None,
    project_dir: object | None = None,
) -> list[str]:
    """
    Get MCP servers required for this agent type.

    Handles dynamic server selection:
    - "browser" → playwright (if is_web_frontend)
    - "graphiti" → only if GRAPHITI_MCP_URL is set
    - Respects per-project MCP config overrides from .magestic-ai/.env
    - Applies per-agent ADD/REMOVE overrides from AGENT_MCP_<agent>_ADD/REMOVE

    Args:
        agent_type: The agent type identifier
        project_capabilities: Dict from detect_project_capabilities() or None
        mcp_config: Per-project MCP server toggles from .magestic-ai/.env
                   Keys: CONTEXT7_ENABLED,
                         PLAYWRIGHT_MCP_ENABLED, AGENT_MCP_<agent>_ADD/REMOVE

    Returns:
        List of MCP server names to start
    """
    config = get_agent_config(agent_type)
    servers = list(config.get("mcp_servers", []))

    # Load per-project config (or use defaults)
    if mcp_config is None:
        mcp_config = {}

    # Filter context7 if explicitly disabled by project config
    if "context7" in servers:
        context7_enabled = mcp_config.get("CONTEXT7_ENABLED", "true")
        if str(context7_enabled).lower() == "false":
            servers = [s for s in servers if s != "context7"]

    # Handle dynamic "browser" → playwright based on project type and config
    if "browser" in servers:
        servers = [s for s in servers if s != "browser"]
        if project_capabilities:
            is_web_frontend = project_capabilities.get("is_web_frontend", False)

            # Check per-project override (default false)
            # Accept both PLAYWRIGHT_MCP_ENABLED and legacy PUPPETEER_MCP_ENABLED
            playwright_enabled = mcp_config.get(
                "PLAYWRIGHT_MCP_ENABLED",
                mcp_config.get("PUPPETEER_MCP_ENABLED", "false"),
            )

            # Playwright: enabled by project config for web frontends
            if is_web_frontend and str(playwright_enabled).lower() == "true":
                servers.append("playwright")

    # Filter graphiti if not enabled
    if "graphiti" in servers:
        if not os.environ.get("GRAPHITI_MCP_URL"):
            servers = [s for s in servers if s != "graphiti"]

    # ===== Code-graph provider selection (exclusive: codegraph OR graphify) =====
    # Both codegraph (CodeGraphContext) and graphify are code-knowledge graph
    # layers. We run exactly ONE of them per project so agents have a single,
    # unambiguous graph to query. The active provider is chosen per-project via
    # CODE_GRAPH_PROVIDER in .magestic-ai/.env; CodeGraphContext is the default.
    #   CODE_GRAPH_PROVIDER=codegraph  -> use CGC, drop graphify   (default)
    #   CODE_GRAPH_PROVIDER=graphify   -> use graphify, drop CGC
    # The non-selected provider is always removed. The selected provider is then
    # gated as before on (a) its kill-switch env not being truthy and (b) when
    # project_dir was passed, its index existing on disk. When project_dir is
    # missing (e.g. permission introspection) we keep the selected provider so
    # those callers don't lose tools; client.py does a final existence check
    # before spawning the server.
    code_graph_provider = str(
        mcp_config.get("CODE_GRAPH_PROVIDER", "codegraph")
    ).strip().lower()
    if code_graph_provider not in ("codegraph", "graphify"):
        code_graph_provider = "codegraph"

    if "graphify" in servers:
        if code_graph_provider != "graphify":
            # Not the selected provider — exclusive selection drops it.
            servers = [s for s in servers if s != "graphify"]
        elif str(os.environ.get("GRAPHIFY_DISABLED", "")).lower() == "true":
            servers = [s for s in servers if s != "graphify"]
        elif project_dir is not None:
            from pathlib import Path as _Path
            graph_file = _Path(str(project_dir)) / "graphify-out" / "graph.json"
            if not graph_file.is_file():
                servers = [s for s in servers if s != "graphify"]

    if "codegraph" in servers:
        if code_graph_provider != "codegraph":
            # Not the selected provider — exclusive selection drops it.
            servers = [s for s in servers if s != "codegraph"]
        elif str(os.environ.get("CODEGRAPH_DISABLED", "")).lower() == "true":
            servers = [s for s in servers if s != "codegraph"]
        elif project_dir is not None:
            from pathlib import Path as _Path
            cgc_dir = _Path(str(project_dir)) / ".codegraphcontext"
            if not cgc_dir.is_dir():
                servers = [s for s in servers if s != "codegraph"]

    # ========== Apply per-agent MCP overrides ==========
    # Format: AGENT_MCP_<agent_type>_ADD=server1,server2
    #         AGENT_MCP_<agent_type>_REMOVE=server1,server2
    add_key = f"AGENT_MCP_{agent_type}_ADD"
    remove_key = f"AGENT_MCP_{agent_type}_REMOVE"

    # Extract custom server IDs for mapping (allows custom servers to be recognized)
    custom_servers = mcp_config.get("CUSTOM_MCP_SERVERS", [])
    custom_server_ids = [s.get("id") for s in custom_servers if s.get("id")]

    # Process additions
    if add_key in mcp_config:
        additions = [
            s.strip() for s in str(mcp_config[add_key]).split(",") if s.strip()
        ]
        for server in additions:
            mapped = _map_mcp_server_name(server, custom_server_ids)
            if mapped and mapped not in servers:
                servers.append(mapped)

    # Process removals (but never remove magestic-ai)
    if remove_key in mcp_config:
        removals = [
            s.strip() for s in str(mcp_config[remove_key]).split(",") if s.strip()
        ]
        for server in removals:
            mapped = _map_mcp_server_name(server, custom_server_ids)
            if mapped and mapped != "magestic-ai":  # magestic-ai cannot be removed
                servers = [s for s in servers if s != mapped]

    return servers


def get_default_thinking_level(agent_type: str) -> str:
    """
    Get default thinking level string for agent type.

    This returns the thinking level name (e.g., 'medium', 'high'), not the token budget.
    To convert to tokens, use phase_config.get_thinking_budget(level).

    Args:
        agent_type: The agent type identifier

    Returns:
        Thinking level string (none, low, medium, high, max)
    """
    config = get_agent_config(agent_type)
    return config.get("thinking_default", "medium")
