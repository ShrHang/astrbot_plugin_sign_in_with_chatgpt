"""AstrBot plugin entry point. No message handler: the built-in Agent sends replies."""

from astrbot.api.star import Context, Star, StarTools

from .siwc import DISPLAY_NAME, PLUGIN_NAME
from .siwc.config import read_settings
from .siwc.registration import Registration
from .siwc.management import Management
from .siwc.search import NativeSearchTool
from .siwc.service import Service, bind, unbind


class SignInWithChatGPT(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context, config)
        self.config = config
        self.service = None
        self.registration = Registration()
        self.management = None
        self.search_tool = None

    async def initialize(self):
        if self.service is not None:
            return
        settings = read_settings(self.config)
        service = Service(StarTools.get_data_dir(PLUGIN_NAME), settings)
        try:
            await service.start()
            self.service = service
            bind(service)
            self.management = Management(service)
            self.management.register(self.context)
            if settings.get("register_provider", True):
                self.registration.register()
                await self.registration.restore(self.context.provider_manager)
                if settings.get("enable_search_tool") is True:
                    self.search_tool = NativeSearchTool(self.context, service, settings)
                    self.search_tool.register()
            else:
                self.registration.unregister()  # Also clear templates cached by an older load.
            self.logger.info("%s 已加载，授权状态：%s；账号管理请打开插件 account 页面", DISPLAY_NAME, service.state)
        except BaseException:
            await self.terminate()
            await service.close()
            raise

    async def terminate(self):
        try:
            if self.search_tool:
                self.search_tool.unregister()
                self.search_tool = None
            if self.management:
                await self.management.stop()
            await self.registration.remove_instances(self.context.provider_manager)
        finally:
            try:
                if self.service is not None:
                    unbind(self.service)
                    await self.service.close()
                    self.service = None
            finally:
                if self.management:
                    self.management.unregister(self.context)
                    self.management = None
                self.registration.unregister()
