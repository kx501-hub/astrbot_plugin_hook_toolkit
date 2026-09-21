"""Expose AstrBot builtin tools to subagents (handoff agents)."""

from __future__ import annotations

import re
from typing import Any

from astrbot.api import logger
from astrbot.core.agent.handoff import HandoffTool

_DISABLED_TOKENS = {"-", "disable", "disabled", "no", "none", "off"}
"""Values that explicitly mean "inject nothing" for a subagent."""


def _item_name(item: Any) -> str:
    """Return the tool name of a ``str`` or ``FunctionTool`` entry.

    Args:
        item: Entry of ``Agent.tools`` (tool name or tool object).

    Returns:
        The tool name, or an empty string when it cannot be determined.
    """
    if isinstance(item, str):
        return item
    return str(getattr(item, "name", "") or "")


def _resolve_builtin_tool(tool_mgr: Any, name: str) -> Any | None:
    """Resolve a builtin tool instance by name.

    Args:
        tool_mgr: LLM tool manager exposing ``get_builtin_tool``.
        name: Builtin tool name.

    Returns:
        The tool instance, or ``None`` when it is unknown or inactive.
    """
    try:
        tool = tool_mgr.get_builtin_tool(name)
    except KeyError:
        logger.warning(f"Unknown builtin tool '{name}', skipped.")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to resolve builtin tool '{name}': {exc!s}")
        return None

    if not getattr(tool, "active", True):
        logger.info(f"Builtin tool '{name}' is inactive, skipped for subagents.")
        return None
    return tool


def parse_tool_names(raw: Any) -> list[str]:
    """Parse a config value into a list of tool names.

    Args:
        raw: A list of names, or a comma/semicolon/space separated string.

    Returns:
        Non-empty tool names. Opt-out values such as ``none`` or ``off`` yield an
        empty list, which means "inject nothing".
    """
    if isinstance(raw, str):
        items: list[str] = [raw]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(item) for item in raw]
    else:
        return []

    names: list[str] = []
    for item in items:
        for token in re.split(r"[,;\s]+", item.strip()):
            if token and token.lower() not in _DISABLED_TOKENS:
                names.append(token)
    return names


def parse_tool_map(raw: Any) -> dict[str, list[str]]:
    """Parse the per-subagent builtin tool mapping.

    Args:
        raw: Mapping of subagent name to tool names (list or string form).

    Returns:
        Mapping with normalized name lists. A subagent mapped to an empty list
        receives no builtin tool at all.
    """
    if not isinstance(raw, dict):
        return {}

    mapping: dict[str, list[str]] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if name:
            mapping[name] = parse_tool_names(value)
    return mapping


def inject_subagent_tools(
    *,
    tool_mgr: Any,
    toolset: Any,
    default_tool_names: Any = None,
    tool_map: Any = None,
    extra_tools: list[Any] | None = None,
) -> dict[str, list[str]]:
    """Add builtin tools to subagent handoff tools before each LLM request.

    Subagent toolsets are rebuilt from ``HandoffTool.agent.tools`` on every
    delegation, so patching that field here affects the next delegation and is
    idempotent across requests. Each subagent can get its own tool list: entries
    present in ``tool_map`` use that list only, every other subagent falls back
    to ``default_tool_names``.

    Args:
        tool_mgr: LLM tool manager used to resolve builtin tools and the general toolset.
        toolset: ToolSet of the main agent request (``req.func_tool``).
        default_tool_names: Builtin tool names for subagents without an entry in
            ``tool_map``, e.g. ``["send_message_to_user"]``.
        tool_map: Mapping of subagent name to its own builtin tool names.
        extra_tools: Tools added alongside the general toolset when a subagent
            previously inherited "all tools" (``agent.tools is None``), e.g. the
            runtime computer-use tools.

    Returns:
        Mapping of subagent name to the builtin tool names actually added.
    """
    defaults = parse_tool_names(default_tool_names)
    overrides = parse_tool_map(tool_map)
    if not defaults and not overrides:
        return {}

    resolved_cache: dict[str, Any] = {}

    def _resolve(names: list[str]) -> list[Any]:
        tools: list[Any] = []
        for name in names:
            if name not in resolved_cache:
                resolved_cache[name] = _resolve_builtin_tool(tool_mgr, name)
            tool = resolved_cache[name]
            if tool is not None:
                tools.append(tool)
        return tools

    added: dict[str, list[str]] = {}
    for tool in list(getattr(toolset, "tools", None) or []):
        if not isinstance(tool, HandoffTool):
            continue

        agent = tool.agent
        names = overrides.get(agent.name, defaults)
        if not names:
            continue

        builtin_tools = _resolve(names)
        if not builtin_tools:
            continue

        current = agent.tools
        if current is None:
            # ``None`` means "all general tools plus runtime computer tools".
            # Injecting a builtin tool requires turning that into an explicit list.
            try:
                general_tools = list(tool_mgr.get_full_tool_set().tools)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"Failed to expand tools of subagent '{agent.name}': {exc!s}"
                )
                continue
            agent.tools = [*general_tools, *(extra_tools or []), *builtin_tools]
            added[agent.name] = [tool_item.name for tool_item in builtin_tools]
            continue

        existing = {_item_name(item) for item in current}
        missing = [
            tool_item for tool_item in builtin_tools if tool_item.name not in existing
        ]
        if missing:
            current.extend(missing)
            added[agent.name] = [tool_item.name for tool_item in missing]

    return added
