"""AstrBot plugin entry point. No message handler: the built-in Agent sends replies."""

from astrbot.api.star import Context, Star, StarTools

from .siwc import PLUGIN_NAME
from .siwc.registration import Registration
from .siwc.management import Management
from .siwc.service import Service, bind, unbind


class SignInWithChatGPT(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context, config)
        self.config = config
        self.service = None
        self.registration = Registration()
        self.management = None

    async def initialize(self):
        if self.service is not None:
            return
        service = Service(StarTools.get_data_dir(PLUGIN_NAME), dict(self.config))
        try:
            await service.start()
            self.service = service
            bind(service)
            self.management = Management(service)
            self.management.register(self.context)
            if self.config.get("register_provider", True):
                self.registration.register()
                await self.registration.restore(self.context.provider_manager)
            else:
                self.registration.unregister()  # Also clear templates cached by an older load.
            self.logger.info("SIWC 已加载，授权状态：%s；账号管理请打开插件 account 页面", service.state)
        except BaseException:
            await self.terminate()
            await service.close()
            raise

    async def terminate(self):
        try:
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
