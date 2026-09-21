"""Hook Toolkit plugin entry point."""

from __future__ import annotations

from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.handoff import HandoffTool
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

from .core.display_name import apply_display_names
from .core.handoff_tools import inject_subagent_tools

DEFAULT_SUBAGENT_TOOLS = ["send_message_to_user"]


class HookToolkitPlugin(Star):
    """Patch AstrBot through hooks: subagent tools and plugin display names."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config: AstrBotConfig = config

    async def initialize(self) -> None:
        """Normalize plugin display names as soon as this plugin is loaded."""
        self._fix_display_names("initialize")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """Normalize again after every plugin finished loading."""
        self._fix_display_names("on_astrbot_loaded")

    @filter.on_llm_request()
    async def inject_subagent_builtin_tools(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """Let subagents inherit selected builtin tools before each LLM request.

        Args:
            event: Current message event, used to resolve session config.
            req: Provider request whose toolset holds the handoff tools.
        """
        if not self._feature_enabled("enable_subagent_tools"):
            return

        toolset = req.func_tool
        if not toolset:
            return

        tool_names = self._string_list("subagent_tool_names") or DEFAULT_SUBAGENT_TOOLS
        only_subagents = self._string_list("subagent_tool_only")

        handoffs = [
            tool
            for tool in (getattr(toolset, "tools", None) or [])
            if isinstance(tool, HandoffTool)
        ]
        if not handoffs:
            # SubAgent orchestration is not in use for this request.
            return

        # Only needed when a subagent used to inherit "all tools"; expanding that
        # into an explicit list would otherwise drop runtime computer tools.
        extra_tools = (
            self._runtime_computer_tools(event)
            if any(tool.agent.tools is None for tool in handoffs)
            else []
        )

        injected = inject_subagent_tools(
            tool_mgr=self.context.get_llm_tool_manager(),
            toolset=toolset,
            tool_names=tool_names,
            only_subagents=only_subagents,
            extra_tools=extra_tools,
        )
        if injected:
            logger.debug(
                f"Injected builtin tools {tool_names} into subagents {injected}."
            )

    def _runtime_computer_tools(self, event: AstrMessageEvent) -> list[Any]:
        """Resolve runtime computer-use tools that a subagent would inherit.

        Args:
            event: Current message event, used to read the session config.

        Returns:
            Runtime computer-use tool instances; empty when the runtime is disabled
            or when resolution fails.
        """
        try:
            config = self.context.get_config(umo=event.unified_msg_origin) or {}
            provider_settings = config.get("provider_settings") or {}
            runtime = str(provider_settings.get("computer_use_runtime", "none"))
            if runtime in {"", "none"}:
                return []
            booter = (provider_settings.get("sandbox") or {}).get("booter")
            tools = FunctionToolExecutor._get_runtime_computer_tools(
                runtime,
                self.context.get_llm_tool_manager(),
                booter,
            )
            return list(tools.values())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to resolve runtime computer tools: {exc!s}")
            return []

    def _fix_display_names(self, source: str) -> None:
        """Normalize display names of plugins that do not provide one.

        Args:
            source: Hook that triggered the run, used for logging only.
        """
        if not self._feature_enabled("enable_display_name_fix"):
            return

        changed = apply_display_names(
            overrides=self.config.get("display_name_overrides"),
            skip=self._string_list("display_name_skip"),
            uppercase_words=self._string_list("display_name_uppercase_words"),
            include_reserved=bool(
                self.config.get("display_name_include_reserved", False)
            ),
        )
        if changed:
            summary = ", ".join(f"{name} -> {display}" for name, display in changed)
            logger.info(
                f"[{source}] normalized {len(changed)} plugin display name(s): {summary}"
            )

    def _feature_enabled(self, key: str) -> bool:
        """Return whether a feature switch is on.

        Disabling the plugin itself is the global switch, so only the feature
        switch is checked here.

        Args:
            key: Feature switch name in the plugin config.

        Returns:
            ``True`` when the feature switch is truthy (missing means enabled).
        """
        return bool(self.config.get(key, True))

    def _string_list(self, key: str) -> list[str]:
        """Read a plugin config value as a list of non-empty strings.

        Args:
            key: Config item name.

        Returns:
            Stripped string items; empty when the value is not a list.
        """
        raw = self.config.get(key)
        if not isinstance(raw, (list, tuple)):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]
