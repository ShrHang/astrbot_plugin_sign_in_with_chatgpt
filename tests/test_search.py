"""Offline search integration tests; stub only the unavailable AstrBot boundary.

Run with the plugin dependencies installed: python -m unittest discover -s tests -v
"""

import asyncio
import copy
import json
import logging
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx


class HostResponse:
    def __init__(self, **kwargs):
        self.is_chunk = False
        self.completion_text = ""
        self.tools_call_ids, self.tools_call_name, self.tools_call_args = [], [], []
        self.__dict__.update(kwargs)


class HostUsage:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class HostProvider:
    def __init__(self, config, settings):
        self.provider_config = config


class HostResponses(HostProvider):
    def set_model(self, model):
        self.model = model

    def _ensure_message_to_dicts(self, contexts):
        return contexts or []

    async def _prepare_chat_payload(self, *, prompt, contexts, system_prompt, model, **kwargs):
        messages = copy.deepcopy(contexts)
        if system_prompt:
            messages.insert(0, {"type": "message", "role": "system", "content": system_prompt})
        if prompt is not None:
            messages.append({"type": "message", "role": "user", "content": prompt})
        return {"model": model or self.model, "input": messages}, None


class HostFunctionTool:
    def __init__(self, **kwargs):
        self.active = True
        self.__dict__.update(kwargs)


def host_module(name, **values):
    module = types.ModuleType(name)
    module.__dict__.update(values)
    return module


HOST_MODULES = {
    "astrbot": host_module("astrbot", logger=logging.getLogger("test.siwc")),
    "astrbot.core.provider.entities": host_module("entities", LLMResponse=HostResponse, TokenUsage=HostUsage),
    "astrbot.core.provider.provider": host_module("provider", Provider=HostProvider),
    "astrbot.core.provider.sources.openai_responses_source": host_module("responses", ProviderOpenAIResponses=HostResponses),
    "astrbot.core.agent.tool": host_module("tool", FunctionTool=HostFunctionTool),
}

with patch.dict(sys.modules, HOST_MODULES):
    from siwc.errors import SIWCError
    from siwc.provider import SIWCProvider
    from siwc.responses import CompletedOutput, adapt_payload, parse_search_result
    from siwc.search import NativeSearchTool
    from siwc.service import bind, unbind


def search_response():
    return {"id": "response1", "status": "completed", "output": [
        {"id": "search1", "type": "web_search_call", "status": "completed", "action": {
            "type": "search", "sources": [{"url": "https://example.com/consulted", "title": "Other source"}]}},
        {"id": "message1", "type": "message", "role": "assistant", "status": "completed", "content": [
            {"type": "output_text", "text": "Research summary.\ue200cite\ue202turn0search0\ue201", "annotations": [
                {"type": "url_citation", "url": "https://example.com/cited", "title": "Cited source"}]}]},
    ]}


def sse_response(events):
    data = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    return httpx.Response(200, content=data.encode(), headers={"content-type": "text/event-stream"})


class SearchParsingTests(unittest.TestCase):
    def test_payload_isolates_hosted_search(self):
        payload = {"model": "test-model", "input": [], "tools": [{"type": "function", "name": "stale"}]}
        body, names = adapt_payload(payload, None, native_web_search=True)
        self.assertEqual(body["tools"], [{"type": "web_search"}])
        self.assertEqual(body["tool_choice"], "required")
        self.assertIn("web_search_call.action.sources", body["include"])
        self.assertFalse(names)
        self.assertEqual(payload["tools"][0]["name"], "stale")
        with self.assertRaises(SIWCError):
            adapt_payload(payload, object(), native_web_search=True)

    def test_normal_payload_cannot_inherit_native_search(self):
        body, _ = adapt_payload({"model": "test-model", "input": [], "tools": [{"type": "web_search"}],
                                 "tool_choice": "required"}, None)
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)
        self.assertNotIn("web_search_call.action.sources", body["include"])

    def test_summary_and_source_provenance(self):
        response = search_response()
        response["output"][0]["action"]["sources"].extend([
            {"url": "https://example.com/cited", "title": "Duplicate"},
            {"url": "javascript:alert(1)"}, {"url": "https://user:password@example.com"}, None])
        original = copy.deepcopy(response)
        result = parse_search_result(response)
        self.assertEqual(result["summary"], "Research summary.")
        self.assertEqual(result["search_count"], 1)
        self.assertEqual([source["cited"] for source in result["sources"]], [True, False])
        self.assertEqual(response, original)

    def test_source_limit_and_model_written_links(self):
        response = search_response()
        response["output"][0]["action"]["sources"] = [{"url": f"https://example.com/{i}"} for i in range(60)]
        response["output"][1]["content"][0]["text"] = "See [source](https://example.com/cited)."
        result = parse_search_result(response)
        self.assertEqual(len(result["sources"]), 32)
        self.assertEqual(result["summary"], response["output"][1]["content"][0]["text"])

    def test_no_fake_search_success(self):
        for case in ("no_call", "incomplete_call", "empty", "function_call", "incomplete_response"):
            with self.subTest(case=case):
                response = search_response()
                if case == "no_call":
                    response["output"].pop(0)
                elif case == "incomplete_call":
                    response["output"][0]["status"] = "in_progress"
                elif case == "empty":
                    response["output"][1]["content"] = []
                elif case == "function_call":
                    response["output"].append({"type": "function_call", "name": "siwc_web_search"})
                else:
                    response["status"] = "in_progress"
                with self.assertRaises(SIWCError):
                    parse_search_result(response)

    def test_completed_items_recover_search_metadata(self):
        output = CompletedOutput()
        for index, item in enumerate(search_response()["output"]):
            output.observe({"type": "response.output_item.done", "output_index": index, "item": item})
        result = parse_search_result(output.complete({"status": "completed", "output": []}))
        self.assertTrue(result["searched"])
        self.assertEqual(len(result["sources"]), 2)


class SearchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host_patch = patch.dict(sys.modules, HOST_MODULES)
        self.host_patch.start()
        self.sent = []
        self.events = [{"type": "response.output_text.delta", "delta": "Unfinished search answer"},
                       {"type": "response.reasoning_text.delta", "delta": "Private reasoning"},
                       {"type": "response.completed", "response": search_response()}]

        async def handler(request):
            self.sent.append(json.loads(request.content))
            return sse_response(self.events)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.service = SimpleNamespace(client=self.client, total_timeout=2, semaphore=asyncio.Semaphore(1),
                                       generation=0, closed=False, logging_out=False, tasks=set(), requests={})

        def admission():
            if self.service.closed or self.service.logging_out:
                raise SIWCError("closed", "Service is closed")

        def track(task):
            admission()
            self.service.tasks.add(task)
            self.service.requests[task] = self.service.generation

        def untrack(task):
            self.service.tasks.discard(task)
            self.service.requests.pop(task, None)

        async def token():
            return "offline-test-token"

        self.service.admission = admission
        self.service.track_request = track
        self.service.untrack_request = untrack
        self.service.access_token = token
        self.service.note_health = lambda error, generation: None
        self.service.record_usage = lambda generation, **request: None
        bind(self.service)
        self.provider = SIWCProvider({"model": "research-model", "reasoning_effort": "low"}, {})
        self.tools = []
        self.host = SimpleNamespace(provider_manager=SimpleNamespace(llm_tools=SimpleNamespace(func_list=self.tools)),
                                    get_provider_by_id=lambda provider_id: self.provider if provider_id == "research" else None,
                                    add_llm_tools=lambda *tools: self.tools.extend(tools))
        self.tool = NativeSearchTool(self.host, self.service, {"search_provider_id": "research"})

    async def asyncTearDown(self):
        await self.provider.terminate()
        unbind(self.service)
        await self.client.aclose()
        self.host_patch.stop()

    async def test_tool_runs_isolated_request_and_returns_material(self):
        result = json.loads(await self.tool.call(None, query="Search this question"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["searched"])
        self.assertEqual(result["summary"], "Research summary.")
        self.assertNotIn("Private reasoning", json.dumps(result))
        self.assertEqual(len(self.sent), 1)
        body = self.sent[0]
        self.assertEqual(body["model"], "research-model")
        self.assertEqual(body["tools"], [{"type": "web_search"}])
        self.assertEqual(body["tool_choice"], "required")
        self.assertEqual(len(body["input"]), 2)
        self.assertEqual(body["input"][-1]["content"], "Search this question")
        self.assertFalse(self.service.tasks)
        self.assertFalse(self.provider._tasks)

    async def test_normal_chat_streams_without_native_search(self):
        final = {"id": "normal", "status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "Hello"}]}]}
        self.events = [{"type": "response.output_text.delta", "delta": "Hello"},
                       {"type": "response.completed", "response": final}]
        responses = [response async for response in self.provider.text_chat_stream(prompt="Hi", _native_web_search=True)]
        self.assertEqual(responses[0].completion_text, "Hello")
        self.assertTrue(responses[0].is_chunk)
        self.assertFalse(responses[-1].is_chunk)
        self.assertNotIn("tools", self.sent[0])

    async def test_host_functions_still_work(self):
        functions = SimpleNamespace(openai_schema=lambda: [
            {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}])
        self.events = [{"type": "response.completed", "response": {"status": "completed", "output": [
            {"type": "function_call", "namespace": "astrbot", "name": "lookup", "call_id": "call1", "arguments": "{}"}]}}]
        response = await self.provider.text_chat(prompt="Use a tool", func_tool=functions)
        self.assertEqual(response.tools_call_name, ["lookup"])
        self.assertEqual(self.sent[0]["tools"][0]["type"], "namespace")

    async def test_reject_wrong_missing_or_stopped_provider(self):
        for value in (None, object(), SimpleNamespace(stopped=False)):
            with self.subTest(provider=value):
                self.host.get_provider_by_id = lambda provider_id: value
                result = json.loads(await self.tool.call(None, query="Search"))
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["category"], "config")
        self.assertFalse(self.sent)

    async def test_invalid_queries_and_disabled_tool_do_not_call_network(self):
        for args in ({}, {"query": " "}, {"query": 5}, {"query": "x" * 4001}, {"query": "x", "extra": 1}):
            self.assertFalse(json.loads(await self.tool.call(None, **args))["ok"])
        self.tool.unregister()
        self.assertFalse(json.loads(await self.tool.call(None, query="Search"))["ok"])
        self.assertFalse(self.sent)

    async def test_registration_removes_only_owned_tool(self):
        other = SimpleNamespace(name="other")
        self.tools.append(other)
        self.tool.register()
        with self.assertRaises(SIWCError):
            self.tool.register()
        self.tool.unregister()
        self.assertEqual(self.tools, [other])

    async def test_incomplete_search_is_an_error_not_partial_material(self):
        self.events[-1]["response"]["output"][0]["status"] = "in_progress"
        result = json.loads(await self.tool.call(None, query="Search"))
        self.assertFalse(result["ok"])
        self.assertNotIn("summary", result)
        self.assertEqual(len(self.sent), 1)

    async def test_eof_without_completed_event_fails_without_retry(self):
        self.events.pop()
        result = json.loads(await self.tool.call(None, query="Search"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "incomplete")
        self.assertEqual(len(self.sent), 1)

    async def test_summary_truncation_keeps_sources(self):
        self.tool._max_chars = 8
        result = json.loads(await self.tool.call(None, query="Search"))
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["summary"]), 8)
        self.assertEqual(len(result["sources"]), 2)

    async def test_account_change_discards_final_result(self):
        async def changed(query):
            self.service.generation += 1
            return parse_search_result(search_response())

        self.provider.native_search = changed
        result = json.loads(await self.tool.call(None, query="Search"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "cancelled")

    async def test_cancel_and_timeout_release_shared_semaphore(self):
        started = asyncio.Event()

        async def slow(request):
            started.set()
            await asyncio.Event().wait()

        await self.client.aclose()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
        self.service.client = self.client
        task = asyncio.create_task(self.tool.call(None, query="Search"))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.service.tasks)
        self.assertFalse(self.service.semaphore.locked())
        self.tool._timeout = 0.01
        result = json.loads(await self.tool.call(None, query="Search"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "timeout")
        self.assertFalse(self.service.tasks)
        self.assertFalse(self.service.semaphore.locked())


if __name__ == "__main__":
    unittest.main()
