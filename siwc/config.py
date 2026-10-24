"""Read grouped plugin settings for transport and CLI consumers."""

from __future__ import annotations


CONFIG_GROUPS = {
    "provider": ("register_provider",),
    "search": ("enable_search_tool", "search_provider_id", "search_timeout", "search_max_chars"),
    "network": ("proxy", "connect_timeout", "read_timeout"),
    "requests": ("total_timeout", "max_concurrency"),
}


def read_settings(config: dict) -> dict:
    """Keep transport and CLI consumers independent of the dashboard grouping."""
    settings = {}
    for group, keys in CONFIG_GROUPS.items():
        values = config.get(group)
        values = values if isinstance(values, dict) else {}
        for key in keys:
            if key in values:
                settings[key] = values[key]
    return settings
