"""Dashboard API contracts; isolated from the AstrBot runtime and real accounts."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from siwc.errors import SIWCError

# Starlette is provided by AstrBot, outside the plugin dependency set.
try:
    from siwc.management import Management, ROUTES
except ModuleNotFoundError as error:
    if error.name not in {"starlette", "starlette.responses"}:
        raise
    from types import ModuleType

    class HostJSONResponse:
        def __init__(self, content, *, status_code, headers):
            self.content, self.status_code = content, status_code
            self.headers = {key.lower(): value for key, value in headers.items()}

    host_responses = ModuleType("starlette.responses")
    host_responses.JSONResponse = HostJSONResponse
    with patch.dict("sys.modules", {"starlette.responses": host_responses}):
        from siwc.management import Management, ROUTES


class DashboardManagementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.account = {"connected": True, "welcome_pending": True, "email": "example@example.com"}
        self.usage = {"totals": {"requests": 3}, "recent": []}
        self.service = SimpleNamespace(
            closed=False,
            status=lambda: copy.deepcopy(self.account),
            usage_snapshot=lambda: copy.deepcopy(self.usage),
            acknowledge_welcome=AsyncMock(),
            model_catalog=AsyncMock(return_value=[{"slug": "test-model", "display_name": "Test model"}]),
        )
        self.management = Management(self.service)

    async def test_snapshot_includes_usage_without_mutable_aliases(self):
        result = await self.management.dispatch("status", {})
        self.assertEqual(result["usage"]["totals"]["requests"], 3)
        result["usage"]["totals"]["requests"] = 99
        self.assertEqual(self.usage["totals"]["requests"], 3)

    async def test_acknowledgement_uses_service_persistence(self):
        result = await self.management.dispatch("plan/acknowledge", {})
        self.service.acknowledge_welcome.assert_awaited_once_with()
        self.assertIn("account", result)
        self.assertEqual(ROUTES["plan/acknowledge"], "POST")

    async def test_acknowledgement_rejects_extra_parameters(self):
        for payload in ({"email": "other@example.com"}, [], None):
            with self.subTest(payload=payload), self.assertRaises(SIWCError):
                await self.management.dispatch("plan/acknowledge", payload)
        self.service.acknowledge_welcome.assert_not_awaited()

    async def test_catalog_preserves_legacy_slug_list(self):
        for action, force in (("models", False), ("models/refresh", True)):
            result = await self.management.dispatch(action, {})
            self.assertEqual(result["models"], ["test-model"])
            self.assertEqual(result["catalog"][0]["display_name"], "Test model")
            self.service.model_catalog.assert_awaited_with(force=force)

    async def test_stopped_service_does_not_acknowledge(self):
        self.service.closed = True
        with self.assertRaises(SIWCError):
            await self.management.dispatch("plan/acknowledge", {})
        self.service.acknowledge_welcome.assert_not_awaited()

    async def test_acknowledgement_passes_existing_auth_boundary(self):
        context = SimpleNamespace(registered_web_apis=[])
        context.register_web_api = lambda path, handler, methods, description: context.registered_web_apis.append((path, handler, methods))
        from types import ModuleType
        host_web = ModuleType("astrbot.api.web")
        host_web.request = SimpleNamespace()
        with patch.dict("sys.modules", {"astrbot.api.web": host_web}):
            self.management.register(context)
        handler = next(handler for path, handler, _ in context.registered_web_apis if path.endswith("/plan/acknowledge"))
        with patch.object(self.management, "authorize_request", side_effect=SIWCError("unauthorized", "认证失效")) as authorize:
            response = await handler()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["cache-control"], "no-store")
        authorize.assert_called_once_with(host_web.request)
        self.service.acknowledge_welcome.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
