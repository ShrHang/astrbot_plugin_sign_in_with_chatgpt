"""AstrBot 4.29.1 boundary: reuse context assembly, replace all network/retry paths."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import httpx

from astrbot import logger
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.provider.provider import Provider
from astrbot.core.provider.sources.openai_responses_source import ProviderOpenAIResponses

from . import RESOURCE
from .errors import SIWCError, http_error
from .responses import CompletedOutput, ResponseDiagnostics, adapt_payload, completed_response, parse_result, sse_events
from .service import active

PLACEHOLDER = "siwc-managed-no-api-key"


class SIWCProvider(ProviderOpenAIResponses):
    def __init__(self, provider_config: dict, provider_settings: dict):
        # Do not construct the core OpenAI SDK client: its retry and key-rotation
        # behavior is unsuitable for rotating subscription OAuth credentials.
        Provider.__init__(self, copy.deepcopy(provider_config), provider_settings)
        self.set_model(provider_config.get("model", ""))
        self.client = SimpleNamespace(base_url=httpx.URL(RESOURCE))
        self.stopped = False
        self._tasks = set()

    def get_current_key(self):
        return PLACEHOLDER

    def get_keys(self):
        return [PLACEHOLDER]

    def set_key(self, key):
        # AstrBot may set its UI placeholder. Credentials never enter this interface.
        return None

    async def get_models(self):
        try:
            return await active().models()
        except httpx.HTTPError:
            raise SIWCError("network", "模型目录网络请求失败，请检查代理和连接") from None

    async def _resolve_image_part(self, image_url, *, image_detail=None):
        try:
            value = await self._image_ref_to_data_url(image_url, mode="strict")
        except Exception:
            raise SIWCError("image", "图片无法读取；请检查图片来源") from None
        if not value:
            raise SIWCError("image", "图片内容为空")
        return {"type": "image_url", "image_url": {"url": value, "detail": image_detail or "auto"}}

    async def _transform_content_part(self, part):
        if part.get("type") == "image_url":
            url, detail = self._extract_image_part_info(part)
            if not url:
                raise SIWCError("image", "图片内容块缺少地址")
            return await self._resolve_image_part(url, image_detail=detail)
        return copy.deepcopy(part)

    async def assemble_context(self, text, image_urls=None, audio_urls=None, extra_user_content_parts=None):
        from astrbot.core.agent.message import ImageURLPart, TextPart
        from pydantic import ValidationError
        parts = []
        try:
            for part in extra_user_content_parts or []:
                if isinstance(part, dict):
                    part = (TextPart if part.get("type") == "text" else ImageURLPart).model_validate(copy.deepcopy(part))
                parts.append(part)
        except ValidationError:
            raise SIWCError("request", "额外内容块格式无效") from None
        return await super().assemble_context(text, image_urls, audio_urls, parts)

    @staticmethod
    def _check_content(contexts, extra_parts, audio_urls):
        if audio_urls:
            raise SIWCError("unsupported", "当前 SIWC 路线不支持音频输入")
        for message in contexts or []:
            content = message.get("content")
            if isinstance(content, list):
                for part in content:
                    if part.get("type") not in {"text", "image_url", "think"}:
                        raise SIWCError("unsupported", "历史包含尚未支持的内容类型")
        for part in extra_parts or []:
            dumped = part.model_dump() if hasattr(part, "model_dump") else part
            if not isinstance(dumped, dict) or dumped.get("type") not in {"text", "image_url"}:
                raise SIWCError("unsupported", "额外内容包含尚未支持的类型")

    async def text_chat(self, *args, **kwargs):
        final = None
        async for response in self.text_chat_stream(*args, **kwargs):
            if not response.is_chunk:
                final = response
        if final is None:
            raise SIWCError("incomplete", "未收到完整回复")
        return final

    async def text_chat_stream(
        self, *args, **kwargs,
    ):
        if self.stopped:
            raise SIWCError("closed", "此 Provider 实例已停止")
        service = active()
        service.admission()
        generation = service.generation
        queue = asyncio.Queue(maxsize=16)
        kwargs["_native_web_search"] = service.enable_native_web_search
        producer = asyncio.create_task(self._produce(service, queue, *args, **kwargs), name="siwc:response-producer")
        self._tasks.add(producer)
        service.track_request(producer)
        def release(task):
            self._tasks.discard(task)
            service.untrack_request(task)
        producer.add_done_callback(release)
        getter = None
        try:
            while True:
                if producer.done():
                    producer.result()  # Fail before draining queued partial output.
                    if queue.empty():
                        return
                    result = queue.get_nowait()
                else:
                    getter = asyncio.create_task(queue.get())
                    await asyncio.wait({getter, producer}, return_when=asyncio.FIRST_COMPLETED)
                    if producer.done():
                        producer.result()
                    if not getter.done():
                        getter.cancel()
                        await asyncio.gather(getter, return_exceptions=True)
                        getter = None
                        continue
                    result = getter.result()
                    getter = None
                # The consumer outlives a completed producer. Both queue paths
                # must validate immediately before handing text/tools to core,
                # with no await between this check and yield.
                if self.stopped or service.closed or service.logging_out or generation != service.generation:
                    raise SIWCError("cancelled", "请求已失效，未交付的响应已丢弃")
                yield result
        finally:
            if getter is not None:
                getter.cancel()
                await asyncio.gather(getter, return_exceptions=True)
            if not producer.done() and not producer.cancelling():
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            while not queue.empty():
                queue.get_nowait()

    async def _produce(
        self, service, queue,
        prompt=None, session_id=None, image_urls=None, audio_urls=None, func_tool=None,
        contexts=None, system_prompt=None, tool_calls_result=None, model=None,
        extra_user_content_parts=None, tool_choice="auto", request_max_retries=None, _native_web_search=False, **kwargs,
    ):
        if self.stopped:
            raise SIWCError("closed", "此 Provider 实例已停止")
        task = asyncio.current_task()
        generation = service.generation
        output = CompletedOutput()
        try:
            # One total deadline includes queueing, refresh, media preparation and HTTP.
            async with asyncio.timeout(service.total_timeout), service.semaphore:
                context_dicts = self._ensure_message_to_dicts(contexts)
                self._check_content(context_dicts, extra_user_content_parts, audio_urls)
                payload, _ = await self._prepare_chat_payload(
                    # Agent runner already assembled extras into its contexts.
                    # Only synthesize a user message for standalone inputs.
                    prompt="" if prompt is None and not context_dicts and (image_urls or extra_user_content_parts) else prompt,
                    image_urls=image_urls, contexts=context_dicts, system_prompt=system_prompt,
                    tool_calls_result=tool_calls_result, model=model, extra_user_content_parts=extra_user_content_parts,
                )
                if not payload["model"]:
                    raise SIWCError("request", "请先从账号模型目录选择真实模型 slug")
                body, names = adapt_payload(payload, func_tool, tool_choice, self.provider_config.get("reasoning_effort", ""),
                                            native_web_search=_native_web_search)
                token = await service.access_token()
                generation = service.generation
                async with service.client.stream("POST", RESOURCE + "/responses", json=body,
                    headers={"Authorization": "Bearer " + token, "Accept": "text/event-stream"}) as response:
                    if response.is_error:
                        await response.aread()
                        error = http_error(response, stage="inference")
                        raise error
                    diagnostics = ResponseDiagnostics()
                    async for event in sse_events(response):
                        if generation != service.generation or service.closed:
                            raise SIWCError("cancelled", "请求期间账号状态已变化")
                        diagnostics.observe(event)
                        output.observe(event)
                        final = completed_response(event)
                        if final is not None:
                            try:
                                resolved = output.complete(final)
                                result = parse_result(resolved, names, native_web_search=_native_web_search)
                            except SIWCError as error:
                                if error.category != "empty":
                                    raise
                                raise SIWCError("empty", "已收到完成终态，但最终结果没有可用文本或工具调用；未自动重放请求",
                                                stage="inference", diagnostics=diagnostics.snapshot(final)) from None
                            service.note_health(None, generation)
                            if _native_web_search:
                                searches = sum(item.get("type") == "web_search_call" for item in resolved.get("output", []))
                                if searches:
                                    logger.info("SIWC 原生网页搜索完成：调用次数=%s", searches)
                            if _native_web_search and result.completion_text:
                                # Annotations can arrive after text deltas. Emit cited
                                # text only once it is final, identically to text_chat.
                                await queue.put(LLMResponse(role="assistant", completion_text=result.completion_text, is_chunk=True))
                            await queue.put(result)
                            return
                        kind = event.get("type")
                        if kind in {"response.output_text.delta", "response.refusal.delta"} and not _native_web_search:
                            await queue.put(LLMResponse(role="assistant", completion_text=event.get("delta", ""), is_chunk=True))
                        elif kind in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
                            await queue.put(LLMResponse(role="assistant", reasoning_content=event.get("delta", ""), is_chunk=True))
                raise SIWCError("incomplete", "连接在 response.completed 前断开；未自动重放请求")
        except SIWCError as error:
            service.note_health(error, generation)
            raise
        except (httpx.HTTPError, TimeoutError) as exc:
            error = SIWCError("timeout" if isinstance(exc, TimeoutError) else "network",
                              "请求连接失败、中断或超过总预算；未自动重放请求")
            service.note_health(error, generation)
            raise error from None
        finally:
            output.clear()
            self._tasks.discard(task)
            service.untrack_request(task)

    async def terminate(self):
        self.stopped = True
        tasks = [task for task in self._tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
