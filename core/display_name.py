"""Normalize plugin display names that are missing in plugin metadata."""

from __future__ import annotations

import re
from typing import Any

from astrbot.api import logger

PLUGIN_PREFIXES: tuple[str, ...] = ("astrbot_plugin_", "astrbot_")
"""Plugin name prefixes stripped before generating a display name."""

_ASTRBOT_RESERVED_NAMES = {"astrbot", "astrbot_builtin", "builtin"}
"""Names that must never be rewritten even when reserved filtering is disabled."""


def _titleize(token: str, uppercase_words: set[str]) -> str:
    """Convert a single token into its display form.

    Args:
        token: Raw token split from the plugin name.
        uppercase_words: Lower-cased words that should always be upper-cased.

    Returns:
        The display form of the token, e.g. ``cve`` -> ``CVE``, ``warning`` -> ``Warning``.
    """
    if token.isupper() and len(token) > 1:
        # Already an acronym such as CVE or API.
        return token

    if token.lower() in uppercase_words:
        return token.upper()

    if any(char.isupper() for char in token[1:]):
        # Keep author-provided camel case such as MsgProcessor.
        return token

    return token[:1].upper() + token[1:].lower()


def normalize_plugin_name(
    name: str,
    uppercase_words: set[str] | None = None,
) -> str:
    """Generate a readable display name from a plugin name.

    Args:
        name: Plugin name from plugin metadata, e.g. ``astrbot_plugin_cve_warning``.
        uppercase_words: Lower-cased words that should stay upper-cased.

    Returns:
        The normalized display name, e.g. ``CVE Warning``. The original name is
        returned when nothing usable is left after stripping the prefix.

    Examples:
        >>> normalize_plugin_name("astrbot_plugin_pixiv_reborn")
        'Pixiv Reborn'
    """
    raw = (name or "").strip()
    if not raw:
        return ""

    lowered = raw.lower()
    stripped = raw
    for prefix in PLUGIN_PREFIXES:
        if lowered.startswith(prefix) and len(raw) > len(prefix):
            stripped = raw[len(prefix) :]
            break

    tokens = [token for token in re.split(r"[_\-\s.]+", stripped) if token]
    if not tokens:
        return raw

    words = uppercase_words or set()
    return " ".join(_titleize(token, words) for token in tokens)


def _as_string_set(raw: Any) -> set[str]:
    """Normalize a config value into a set of non-empty strings.

    Args:
        raw: Config value, expected to be a list of strings.

    Returns:
        A set of stripped strings; empty when the input is unusable.
    """
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(item).strip() for item in raw if str(item).strip()}


def _as_override_map(raw: Any) -> dict[str, str]:
    """Normalize the display name override mapping.

    Args:
        raw: Config value, expected to be a dict of ``name -> display name``.

    Returns:
        A dict containing only non-empty string pairs.
    """
    if not isinstance(raw, dict):
        return {}
    overrides: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()
        display = str(value).strip() if value is not None else ""
        if name and display:
            overrides[name] = display
    return overrides


def apply_display_names(
    *,
    overrides: Any = None,
    skip: Any = None,
    uppercase_words: Any = None,
    include_reserved: bool = False,
) -> list[tuple[str, str]]:
    """Fill in missing plugin display names in the runtime star registry.

    Only plugins whose metadata has no ``display_name`` are touched, unless the
    plugin name appears in ``overrides``. Changes live in memory only: plugin
    files on disk stay untouched and the normalization runs again on next start.

    Args:
        overrides: Mapping of plugin name to forced display name.
        skip: Plugin names that must never be renamed.
        uppercase_words: Lower-cased words that should stay upper-cased.
        include_reserved: Whether AstrBot built-in plugins may be renamed too.

    Returns:
        A list of ``(plugin_name, display_name)`` pairs that were updated.
    """
    from astrbot.core.star.star import star_map

    override_map = _as_override_map(overrides)
    skip_names = _as_string_set(skip)
    uppercase_set = {word.lower() for word in _as_string_set(uppercase_words)}

    changed: list[tuple[str, str]] = []
    for metadata in list(star_map.values()):
        name = (metadata.name or "").strip()
        if not name or name in skip_names:
            continue
        if name.lower() in _ASTRBOT_RESERVED_NAMES:
            continue
        if metadata.reserved and not include_reserved:
            continue

        current = (metadata.display_name or "").strip()
        override = override_map.get(name)
        if override is None and current:
            # The plugin already provides a display name; leave it alone.
            continue

        target = override or normalize_plugin_name(name, uppercase_set)
        if not target or target == current:
            continue

        metadata.display_name = target
        changed.append((name, target))

    if changed:
        logger.debug(f"Normalized display names: {changed}")
    return changed
