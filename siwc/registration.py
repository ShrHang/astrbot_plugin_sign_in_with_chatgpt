"""All dependencies on AstrBot's provider registry/lifecycle live here (4.29.1)."""

from __future__ import annotations

import copy

from . import DISPLAY_NAME, PROVIDER_TYPE

TEMPLATE = {"id": "SIWC", "type": PROVIDER_TYPE, "provider": "openai", "enable": False,
            "provider_type": "chat_completion", "model": "", "key": ["siwc-managed-no-api-key"],
            "reasoning_effort": ""}


class Registration:
    def __init__(self):
        self.metadata = None

    def register(self):
        from astrbot.core.provider.register import provider_cls_map, register_provider_adapter
        from .provider import SIWCProvider

        current = provider_cls_map.get(PROVIDER_TYPE)
        if current is self.metadata and current is not None:
            return
        if current is not None:
            raise RuntimeError("SIWC Provider 类型已被另一插件注册")
        register_provider_adapter(PROVIDER_TYPE, DISPLAY_NAME,
            default_config_tmpl=copy.deepcopy(TEMPLATE), provider_display_name=DISPLAY_NAME)(SIWCProvider)
        self.metadata = provider_cls_map[PROVIDER_TYPE]

    async def restore(self, manager):
        # Core startup loads providers after plugins. Hot enabling/reloading needs
        # its own restoration because manager.initialize() is not run again.
        if manager._mcp_init_task is None:
            return  # Cold startup: core will initialize providers once after plugins.
        async with manager.reload_lock:
            for config in manager.providers_config:
                merged = manager.get_merged_provider_config(config)
                if merged.get("type") == PROVIDER_TYPE and merged.get("enable") and config["id"] not in manager.inst_map:
                    await manager.load_provider(config)
                    if config["id"] not in manager.inst_map:
                        raise RuntimeError("SIWC Provider 实例恢复失败")

    async def remove_instances(self, manager):
        from astrbot.core.provider.register import provider_cls_map
        if self.metadata is None or provider_cls_map.get(PROVIDER_TYPE) is not self.metadata:
            return
        async with manager.reload_lock:
            for provider_id, instance in list(manager.inst_map.items()):
                if instance.provider_config.get("type") == PROVIDER_TYPE and isinstance(instance, self.metadata.cls_type):
                    await manager.terminate_provider(provider_id)

    def unregister(self):
        from astrbot.core.provider.register import provider_cls_map, provider_registry
        from astrbot.core.config.default import CONFIG_METADATA_2

        current = provider_cls_map.get(PROVIDER_TYPE)
        # 4.29.1's config service inserts templates into a shared metadata dict.
        # Removing registry metadata alone leaves the adapter selectable in WebUI.
        if current is None or (self.metadata is not None and current is self.metadata):
            templates = CONFIG_METADATA_2["provider_group"]["metadata"]["provider"]["config_template"]
            template = templates.get(PROVIDER_TYPE)
            if isinstance(template, dict) and template.get("type") == PROVIDER_TYPE:
                templates.pop(PROVIDER_TYPE)
        if self.metadata is not None and provider_cls_map.get(PROVIDER_TYPE) is self.metadata:
            provider_cls_map.pop(PROVIDER_TYPE)
            provider_registry[:] = [item for item in provider_registry if item is not self.metadata]
        self.metadata = None
