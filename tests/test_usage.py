"""Offline dashboard persistence, account lifecycle and inference accounting checks."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import test_search as host_fixture
from siwc.service import Service
from siwc.usage import MAX_MODELS, MAX_RECENT, UsageStore, reported_usage


class UsagePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = UsageStore(Path(self.directory.name))
        self.store.open()

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def test_roundtrip_totals_and_metadata_allowlist(self):
        self.store.record(model="model-a", kind="chat", outcome="succeeded", duration_ms=125,
                          usage={"input_tokens": 20, "cached_tokens": 10, "output_tokens": 5, "prompt": "secret"})
        self.store.record(model="model-b", kind="search", outcome="failed", duration_ms=30)
        self.store.record(model="model-b", kind="chat", outcome="cancelled", duration_ms=5)
        await self.store.close()
        restored = UsageStore(Path(self.directory.name))
        restored.open()
        snapshot = restored.snapshot()
        self.assertEqual(snapshot["totals"], dict(requests=3, succeeded=1, failed=1, cancelled=1,
            input_tokens=20, cached_tokens=10, output_tokens=5, usage_reported_requests=1,
            input_reported_requests=1, cached_reported_requests=1, output_reported_requests=1, total_duration_ms=160))
        self.assertEqual(len(snapshot["daily"]), 1)
        self.assertEqual(len(snapshot["models"]), 2)
        self.assertEqual(snapshot["recent"][1]["kind"], "search")
        self.assertIsNone(snapshot["recent"][0]["input_tokens"])
        persisted = self.store.path.read_text(encoding="utf-8")
        self.assertNotIn("secret", persisted)
        self.assertNotIn("prompt", persisted)
        snapshot["recent"][0]["model"] = "changed"
        self.assertNotEqual(restored.snapshot()["recent"][0]["model"], "changed")

    async def test_retention_and_bounds(self):
        now = time.time()
        with patch("siwc.usage.time.time", return_value=now - 31 * 86400):
            self.store.record(model="expired", kind="chat", outcome="succeeded", duration_ms=1)
        for i in range(MAX_RECENT + MAX_MODELS):
            self.store.record(model=f"model-{i}", kind="chat", outcome="succeeded", duration_ms=1)
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["totals"]["requests"], MAX_RECENT + MAX_MODELS)
        self.assertEqual(len(snapshot["recent"]), MAX_RECENT)
        self.assertEqual(len(snapshot["models"]), MAX_MODELS + 1)
        self.assertNotIn("expired", str(snapshot))

    async def test_failure_is_visible_and_later_write_recovers(self):
        with patch.object(self.store, "_save", side_effect=OSError("disk unavailable")):
            self.store.record(model="model", kind="chat", outcome="failed", duration_ms=1)
            await self.store._writer
        self.assertTrue(self.store.snapshot()["storage_error"])
        self.store.record(model="model", kind="chat", outcome="succeeded", duration_ms=1)
        await self.store._writer
        self.assertFalse(self.store.snapshot()["storage_error"])
        self.assertEqual(self.store.snapshot()["totals"]["requests"], 2)

    async def test_malformed_file_is_unavailable_and_close_rejects_new_records(self):
        self.store.path.write_text("{broken", encoding="utf-8")
        self.store.open()
        self.assertTrue(self.store.snapshot()["storage_error"])
        await self.store.close()
        self.store.record(model="model", kind="chat", outcome="succeeded", duration_ms=1)
        self.assertEqual(self.store.snapshot()["totals"]["requests"], 0)

    async def test_offloop_writes_are_serialized_and_close_flushes_latest_record(self):
        entered, release = threading.Event(), threading.Event()
        save = self.store._save
        calls = []
        loop_thread = threading.get_ident()

        def blocked_save(payload):
            calls.append(threading.get_ident())
            if len(calls) == 1:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test did not release writer")
            save(payload)

        with patch.object(self.store, "_save", side_effect=blocked_save):
            self.store.record(model="model", kind="chat", outcome="succeeded", duration_ms=1)
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.store.record(model="model", kind="search", outcome="cancelled", duration_ms=2)
            closing = asyncio.create_task(self.store.close())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            self.store.record(model="ignored-after-close", kind="chat", outcome="failed", duration_ms=1)
            release.set()
            await asyncio.wait_for(closing, 2)
        self.assertEqual(len(calls), 2)
        self.assertNotIn(loop_thread, calls)
        restored = UsageStore(Path(self.directory.name))
        restored.open()
        self.assertEqual(restored.snapshot()["totals"]["requests"], 2)

    def test_usage_absence_invalid_and_zero_are_distinct(self):
        for value in (None, {}, {"input_tokens": -1}, {"output_tokens": True}, {"input_tokens_details": "bad"}):
            self.assertIsNone(reported_usage(value))
        self.assertEqual(reported_usage({"input_tokens": 0}), {"input_tokens": 0})

    async def test_partial_usage_preserves_per_field_coverage_after_reload(self):
        self.store.record(model="model", kind="chat", outcome="succeeded", duration_ms=1,
                          usage=reported_usage({"output_tokens": 0}))
        self.store.record(model="model", kind="search", outcome="failed", duration_ms=1,
                          usage=reported_usage({"input_tokens": 10, "input_tokens_details": {"cached_tokens": 0}}))
        await self.store.close()
        restored = UsageStore(Path(self.directory.name))
        restored.open()
        snapshot = restored.snapshot()
        for counts in (snapshot["totals"], snapshot["daily"][0], snapshot["models"][0]):
            self.assertEqual(counts["usage_reported_requests"], 2)
            self.assertEqual(counts["input_reported_requests"], 1)
            self.assertEqual(counts["output_reported_requests"], 1)
            self.assertEqual(counts["cached_reported_requests"], 1)
        self.assertIsNone(snapshot["recent"][0]["output_tokens"])
        self.assertIsNone(snapshot["recent"][1]["input_tokens"])
        self.assertEqual(snapshot["recent"][1]["output_tokens"], 0)


class ServiceDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.calls = []

        def handler(request):
            self.calls.append(request)
            return httpx.Response(200, json={"models": [
                {"slug": "alpha", "display_name": "Alpha Friendly", "visibility": "list"},
                {"slug": "beta", "visibility": "list"}, {"slug": "hidden", "visibility": "hidden"}]})

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.service = Service(Path(self.directory.name), client=self.client)
        await self.service.start()

    async def asyncTearDown(self):
        await self.service.close()
        self.directory.cleanup()

    async def login(self):
        self.service.oauth.sign_in = AsyncMock(return_value={"client_id": "client", "sub": "subject",
            "email": "person@example.com", "access_token": "fake-token", "expires_at": time.time() + 3600})
        await self.service.login(lambda url: None)

    async def test_new_login_welcome_acknowledgement_survives_logout(self):
        self.assertFalse(self.service.status()["welcome_pending"])
        await self.login()
        self.assertTrue(self.service.status()["welcome_pending"])
        self.assertEqual(self.service.status()["email"], "person@example.com")
        await self.service.acknowledge_welcome()
        self.assertFalse(self.service.status()["welcome_pending"])
        await self.service.logout()
        await self.login()
        self.assertTrue(self.service.record["plan_notice_acknowledged"])
        self.assertFalse(self.service.status()["welcome_pending"])

    async def test_existing_valid_account_does_not_gain_welcome(self):
        await self.service._save({**self.service.record, "registration": {"access_token": "existing-token"}})
        await self.service.close()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        self.service = Service(Path(self.directory.name), client=self.client)
        await self.service.start()
        self.assertTrue(self.service.status()["connected"])
        self.assertFalse(self.service.status()["welcome_pending"])

    async def test_model_catalog_preserves_slug_contract_and_cache(self):
        await self.login()
        self.assertEqual(await self.service.models(), ["alpha", "beta"])
        catalog = await self.service.model_catalog()
        self.assertEqual(catalog, [{"slug": "alpha", "display_name": "Alpha Friendly"}, {"slug": "beta", "display_name": "beta"}])
        catalog[0]["slug"] = "mutated"
        self.assertEqual(await self.service.models(), ["alpha", "beta"])
        self.assertEqual(len(self.calls), 1)
        await self.service.model_catalog(force=True)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.service.usage_snapshot()["totals"]["requests"], 0)

    async def test_historical_generation_retained_but_closed_store_rejects_records(self):
        request = dict(model="model", kind="chat", outcome="succeeded", duration_ms=1)
        self.service.record_usage(self.service.generation - 1, **request)
        self.service.record_usage(self.service.generation, **request)
        await self.service.close()
        self.service.record_usage(self.service.generation, **request)
        self.assertEqual(self.service.usage_snapshot()["totals"]["requests"], 2)
        restored = UsageStore(Path(self.directory.name))
        restored.open()
        self.assertEqual(restored.snapshot()["totals"]["requests"], 2)

    async def test_logout_and_shutdown_count_inflight_cancellation(self):
        for action in (self.service.logout, self.service.close):
            generation = self.service.generation
            dispatched = asyncio.Event()

            async def producer():
                task = asyncio.current_task()
                try:
                    # Simulate an admitted producer after its network dispatch.
                    dispatched.set()
                    await asyncio.Event().wait()
                finally:
                    self.service.record_usage(generation, model="model", kind="chat",
                        outcome="cancelled", duration_ms=1)
                    self.service.untrack_request(task)

            task = asyncio.create_task(producer())
            self.service.track_request(task)
            await asyncio.wait_for(dispatched.wait(), 1)
            await action()
            self.assertTrue(task.cancelled())
        snapshot = self.service.usage_snapshot()
        self.assertEqual(snapshot["totals"]["requests"], 2)
        self.assertEqual(snapshot["totals"]["cancelled"], 2)
        self.assertTrue(self.service.usage.closed)
        restored = UsageStore(Path(self.directory.name))
        restored.open()
        self.assertEqual(restored.snapshot()["totals"]["cancelled"], 2)


class InferenceAccountingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = host_fixture.SearchIntegrationTests.asyncSetUp
    asyncTearDown = host_fixture.SearchIntegrationTests.asyncTearDown

    async def test_chat_and_search_record_actual_reported_usage(self):
        records = []
        self.service.record_usage = lambda generation, **record: records.append(record)
        response = host_fixture.search_response()
        response["usage"] = {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 40}, "output_tokens": 20}
        self.events = [{"type": "response.completed", "response": response}]
        await self.provider.native_search("private search query")
        response["output"].pop(0)
        await self.provider.text_chat(prompt="private prompt")
        self.assertEqual([record["kind"] for record in records], ["search", "chat"])
        self.assertTrue(all(record["outcome"] == "succeeded" for record in records))
        self.assertEqual(records[0]["usage"], {"input_tokens": 100, "cached_tokens": 40, "output_tokens": 20})
        self.assertNotIn("private", str(records))

    async def test_completed_empty_records_usage_but_validation_does_not_count(self):
        records = []
        self.service.record_usage = lambda generation, **record: records.append(record)
        self.events = [{"type": "response.completed", "response": {"status": "completed", "output": [],
            "usage": {"input_tokens": 10, "output_tokens": 0}}}]
        with self.assertRaises(host_fixture.SIWCError):
            await self.provider.text_chat(prompt="prompt")
        self.assertEqual(records[0]["outcome"], "failed")
        self.assertEqual(records[0]["usage"]["input_tokens"], 10)
        with self.assertRaises(host_fixture.SIWCError):
            await self.provider.text_chat(prompt="prompt", audio_urls=["unsupported"])
        self.assertEqual(len(records), 1)

    async def test_network_cancellation_and_http_failure(self):
        records = []
        self.service.record_usage = lambda generation, **record: records.append(record)
        entered = asyncio.Event()

        async def slow(request):
            entered.set()
            await asyncio.Event().wait()

        await self.client.aclose()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
        self.service.client = self.client
        task = asyncio.create_task(self.provider.native_search("query"))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(records[0]["outcome"], "cancelled")
        self.assertIsNone(records[0]["usage"])
        await self.client.aclose()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        self.service.client = self.client
        with self.assertRaises(host_fixture.SIWCError):
            await self.provider.native_search("query")
        self.assertEqual(records[1]["outcome"], "failed")


if __name__ == "__main__":
    unittest.main()
