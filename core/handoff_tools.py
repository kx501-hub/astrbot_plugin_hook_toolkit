"""Expose AstrBot builtin tools to subagents (handoff agents)."""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.core.agent.handoff import HandoffTool


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


def _resolve_builtin_tools(
    tool_mgr: Any,
    tool_names: list[str],
) -> list[Any]:
    """Resolve builtin tool instances by name.

    Args:
        tool_mgr: LLM tool manager exposing ``get_builtin_tool``.
        tool_names: Builtin tool names to resolve.

    Returns:
        Resolved and still-active builtin tool instances.
    """
    resolved: list[Any] = []
    for name in tool_names:
        try:
            tool = tool_mgr.get_builtin_tool(name)
        except KeyError:
            logger.warning(f"Unknown builtin tool '{name}', skipped.")
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to resolve builtin tool '{name}': {exc!s}")
            continue

        if not getattr(tool, "active", True):
            logger.info(
                f"Builtin tool '{name}' is currently inactive, skipped for subagents."
            )
            continue
        resolved.append(tool)
    return resolved


def inject_subagent_tools(
    *,
    tool_mgr: Any,
    toolset: Any,
    tool_names: list[str],
    only_subagents: list[str] | None = None,
    extra_tools: list[Any] | None = None,
) -> list[str]:
    """Add builtin tools to subagent handoff tools before each LLM request.

    Subagent toolsets are rebuilt from ``HandoffTool.agent.tools`` on every
    delegation, so patching that field here affects the next delegation and is
    idempotent across requests.

    Args:
        tool_mgr: LLM tool manager used to resolve builtin tools and the general toolset.
        toolset: ToolSet of the main agent request (``req.func_tool``).
        tool_names: Builtin tool names to inject, e.g. ``["send_message_to_user"]``.
        only_subagents: Subagent names to patch; empty means every subagent.
        extra_tools: Tools added alongside the general toolset when a subagent
            previously inherited "all tools" (``agent.tools is None``), e.g. the
            runtime computer-use tools.

    Returns:
        Names of the subagents whose toolset was updated.
    """
    wanted = [str(name).strip() for name in (tool_names or []) if str(name).strip()]
    if not wanted:
        return []

    allowed = {
        str(name).strip() for name in (only_subagents or []) if str(name).strip()
    }
    builtin_tools = _resolve_builtin_tools(tool_mgr, wanted)
    if not builtin_tools:
        return []

    touched: list[str] = []
    for tool in list(getattr(toolset, "tools", None) or []):
        if not isinstance(tool, HandoffTool):
            continue

        agent = tool.agent
        if allowed and agent.name not in allowed:
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
            touched.append(agent.name)
            continue

        existing = {_item_name(item) for item in current}
        missing = [
            tool_item for tool_item in builtin_tools if tool_item.name not in existing
        ]
        if missing:
            current.extend(missing)
            touched.append(agent.name)

    return touched
