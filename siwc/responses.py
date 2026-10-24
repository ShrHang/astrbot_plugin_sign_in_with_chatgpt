"""SIWC payload restrictions, namespace round trips and strict SSE termination."""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from urllib.parse import urlsplit

from .errors import SIWCError, sse_error

NAMESPACE = "astrbot"
REASONING_STATE = "openai_responses_reasoning"


class CitationRenderer:
    """Render only response annotations; no source lookup or invented URLs."""

    MARKER = re.compile(r"\ue200cite\ue202[^\ue201]*\ue201")

    def __init__(self):
        self.sources = {}

    @staticmethod
    def valid_url(value):
        if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 or c in '<>\\' for c in value):
            return False
        try:
            url = urlsplit(value)
            return url.scheme in {"https", "http"} and bool(url.hostname) and not url.username and not url.password
        except ValueError:
            return False

    def render(self, text, annotations):
        insertions = {}
        for annotation in annotations if isinstance(annotations, list) else []:
            if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
                continue
            url = annotation.get("url")
            if not self.valid_url(url):
                continue
            if url not in self.sources:
                title = annotation.get("title")
                title = " ".join(title.split()) if isinstance(title, str) else ""
                self.sources[url] = (len(self.sources) + 1, title)
            number = self.sources[url][0]
            start, end = annotation.get("start_index"), annotation.get("end_index")
            if type(start) is int and type(end) is int and 0 <= start <= end <= len(text):
                if url not in text[start:end]:
                    insertions.setdefault(end, set()).add(number)
        for end in sorted(insertions, reverse=True):
            labels = "".join(f"[{n}]" for n in sorted(insertions[end]))
            text = text[:end] + labels + text[end:]
        return self.MARKER.sub("", text)

    def finish(self, text):
        existing = {u.rstrip('.,;!?，。；！？') for u in re.findall(r'https?://[^\s<>()\[\]{}]+', text)}
        sources = [f"[{number}] {title or '来源'}\n{url}" for url, (number, title) in self.sources.items() if url not in existing]
        return text + ("\n\n来源：\n" + "\n".join(sources) if sources else "")


class CompletedOutput:
    """Hold finalized items for streams whose successful terminal omits output."""

    MAX_ITEMS = 1024
    MAX_BYTES = 16 * 1024 * 1024

    def __init__(self):
        self.added, self.items = {}, {}
        self.size = 0
        self.invalid = False

    def observe(self, event):
        kind = event.get("type")
        if self.invalid or kind not in ("response.output_item.added", "response.output_item.done"):
            return
        index, item = event.get("output_index"), event.get("item")
        if (type(index) is not int or not 0 <= index < self.MAX_ITEMS or not isinstance(item, dict)
                or not isinstance(item.get("id"), str) or not item["id"]):
            self.invalid = True
            return
        identity = (item["id"], item.get("type"))
        if kind == "response.output_item.added":
            self.size += len(json.dumps(identity, ensure_ascii=False).encode("utf-8"))
            if self.size > self.MAX_BYTES:
                self.invalid = True
                return
            if index in self.added or index in self.items:
                self.invalid = True
            else:
                self.added[index] = identity
            return
        if (index in self.items or (index in self.added and self.added[index] != identity)
                or item.get("status", "completed") != "completed"
                or (item.get("type") == "message" and
                    (item.get("status") != "completed" or item.get("role") != "assistant"))):
            self.invalid = True
            return
        self.size += len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        if self.size > self.MAX_BYTES:
            self.invalid = True
            return
        self.items[index] = copy.deepcopy(item)

    def complete(self, response):
        # Never concatenate deltas, overwrite a populated terminal, or publish
        # a done item before the response itself has completed successfully.
        if response.get("status") != "completed":
            raise SIWCError("incomplete", "流式输出尚未完整完成")
        if response.get("output") != []:
            return response
        indices = sorted(self.items)
        if (self.invalid or (self.added and self.added.keys() != self.items.keys())
                or indices != list(range(len(indices)))
                or len({item["id"] for item in self.items.values()}) != len(indices)):
            raise SIWCError("protocol", "流式输出项不完整、不一致或超过大小限制，未交付最终结果", stage="inference")
        if not self.items:
            return response
        return {**response, "output": [self.items[index] for index in indices]}

    def clear(self):
        self.added.clear()
        self.items.clear()


class ResponseDiagnostics:
    """Constant-space structural counters; never retain event bodies or IDs."""

    EVENT_TYPES = frozenset({
        "response.created", "response.in_progress", "response.completed", "response.failed", "response.incomplete", "error",
        "response.output_item.added", "response.output_item.done", "response.content_part.added", "response.content_part.done",
        "response.output_text.delta", "response.output_text.done", "response.refusal.delta", "response.refusal.done",
        "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
        "response.reasoning_text.delta", "response.reasoning_text.done",
        "response.function_call_arguments.delta", "response.function_call_arguments.done",
    })

    def __init__(self):
        self.events = Counter()
        self.delta_text_chars = self.done_text_chars = 0
        self.delta_reasoning_chars = 0
        self.done_output = self.output_summary([])

    @staticmethod
    def text_length(value):
        return len(value) if isinstance(value, str) else 0

    @classmethod
    def output_summary(cls, output):
        summary = {"shape": "list" if isinstance(output, list) else "missing" if output is None else "invalid",
                   "items": 0, "types": {}, "text_chars": 0, "refusal_chars": 0, "reasoning_chars": 0}
        for item in output if isinstance(output, list) else []:
            summary["items"] += 1
            item = item if isinstance(item, dict) else {}
            kind = item.get("type")
            kind = kind if isinstance(kind, str) and kind in {"message", "reasoning", "function_call", "web_search_call"} else "other"
            summary["types"][kind] = summary["types"].get(kind, 0) + 1
            parts = item.get("summary" if kind == "reasoning" else "content", [])
            for part in parts if isinstance(parts, list) else []:
                if not isinstance(part, dict):
                    continue
                if kind == "reasoning":
                    summary["reasoning_chars"] += cls.text_length(part.get("text"))
                elif part.get("type") == "output_text":
                    summary["text_chars"] += cls.text_length(part.get("text"))
                elif part.get("type") == "refusal":
                    summary["refusal_chars"] += cls.text_length(part.get("refusal"))
        return summary

    def observe(self, event):
        kind = event.get("type")
        kind = kind if isinstance(kind, str) and kind in self.EVENT_TYPES else "other"
        self.events[kind] += 1
        if kind in {"response.output_text.delta", "response.refusal.delta"}:
            self.delta_text_chars += self.text_length(event.get("delta"))
        elif kind in {"response.output_text.done", "response.refusal.done"}:
            self.done_text_chars += self.text_length(event.get("text" if kind == "response.output_text.done" else "refusal"))
        elif kind in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
            self.delta_reasoning_chars += self.text_length(event.get("delta"))
        elif kind == "response.output_item.done":
            summary = self.output_summary([event.get("item")])
            for key in ("items", "text_chars", "refusal_chars", "reasoning_chars"):
                self.done_output[key] += summary[key]
            for key, count in summary["types"].items():
                self.done_output["types"][key] = self.done_output["types"].get(key, 0) + count

    def snapshot(self, final):
        return {"events": dict(self.events), "delta_text_chars": self.delta_text_chars,
                "done_text_chars": self.done_text_chars, "delta_reasoning_chars": self.delta_reasoning_chars,
                "done_output": copy.deepcopy(self.done_output), "final_output": self.output_summary(final.get("output"))}


def adapt_payload(payload: dict, tools, tool_choice="auto", reasoning_effort="", *, native_web_search=False) -> tuple[dict, set[str]]:
    body = copy.deepcopy(payload)
    body["store"] = False
    body["stream"] = True
    body["include"] = ["reasoning.encrypted_content"]
    names = set()
    if tools:
        functions = []
        for tool in tools.openai_schema():
            function = copy.deepcopy(tool.get("function", {}))
            name = function.get("name")
            if not isinstance(name, str) or not name or name in names:
                raise SIWCError("request", "工具名称为空或重复")
            names.add(name)
            functions.append({"type": "function", **{k: v for k, v in function.items()
                                                     if k in {"name", "description", "parameters", "strict"}}})
        if functions:
            body["tools"] = [{"type": "namespace", "name": NAMESPACE,
                              "description": "Functions executed by the AstrBot host", "tools": functions}]
            if tool_choice not in {"auto", "required", "none"}:
                raise SIWCError("request", "仅支持 auto、required、none 工具选择")
            body["tool_choice"] = tool_choice
    if native_web_search:
        if tool_choice not in {"auto", "required", "none"}:
            raise SIWCError("request", "仅支持 auto、required、none 工具选择")
        body["tools"] = [tool for tool in body.get("tools", []) if tool.get("type") != "web_search"] + [{"type": "web_search"}]
        body.setdefault("tool_choice", tool_choice)
    if reasoning_effort:
        if reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
            raise SIWCError("request", "未知 reasoning_effort")
        body["reasoning"] = {"effort": reasoning_effort}
    for item in body["input"]:
        if item.get("type") == "message" and item.get("role") == "system":
            item["role"] = "developer"
        if item.get("type") == "function_call":
            item["namespace"] = NAMESPACE
    return body, names


async def sse_events(response):
    # A proxy/upstream can omit or mislabel Content-Type. Validate actual SSE
    # framing instead; a 200 status or a JSON response is never a success marker.
    def protocol_error(body_format):
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        declared = {"text/event-stream": "sse", "application/json": "json", "text/html": "html",
                    "text/plain": "text", "application/octet-stream": "octet_stream", "": "missing"}.get(media_type, "other")
        return SIWCError("protocol", "响应内容不是有效的 SSE 事件流", response.status_code, stage="inference",
                         diagnostics={"response_format": body_format, "declared_format": declared})

    if not response.is_success:
        raise protocol_error("unread")
    data = []
    size = 0
    started = False
    first_line = True
    async for line in response.aiter_lines():
        if first_line:
            line = line.removeprefix("\ufeff")  # SSE permits an initial UTF-8 BOM.
            first_line = False
        if len(line) > 16 * 1024 * 1024:
            raise SIWCError("protocol", "SSE 行超过大小限制")
        if line and not started:
            field = line.split(":", 1)[0]
            if not line.startswith(":") and field not in {"data", "event", "id", "retry"}:
                prefix = line.lstrip()
                body_format = "json" if prefix.startswith(("{", "[")) else "html" if prefix.startswith("<") else "other"
                raise protocol_error(body_format)
            started = True
        if not line:
            if data:
                value = "\n".join(data)
                data, size = [], 0
                if value == "[DONE]":
                    # [DONE] alone is never evidence of success.
                    continue
                try:
                    event = json.loads(value)
                    if not isinstance(event, dict):
                        raise ValueError
                except ValueError:
                    raise SIWCError("protocol", "服务端发送了无效 SSE JSON") from None
                yield event
        elif line == "data" or line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            size += len(value)
            if size > 16 * 1024 * 1024:
                raise SIWCError("protocol", "SSE 事件超过大小限制")
            data.append(value)
    if not started:
        raise protocol_error("empty")
    # SSE requires a blank line to dispatch an event; unframed EOF is a broken stream.


def completed_response(event: dict) -> dict | None:
    kind = event.get("type")
    if kind in {"error", "response.failed", "response.incomplete"}:
        raise sse_error(event)
    if kind != "response.completed":
        return None
    response = event.get("response", {})
    if response.get("status") != "completed":
        raise SIWCError("incomplete", "终止事件未包含 completed 状态")
    return response


def parse_result(response: dict, names: set[str], *, native_web_search=False):
    # Import AstrBot only at the adapter boundary so OAuth CLI has no host dependency.
    from astrbot.core.provider.entities import LLMResponse, TokenUsage

    result = LLMResponse(role="assistant", id=response.get("id"))
    text, reasoning, states, seen = [], [], [], set()
    citations = CitationRenderer() if native_web_search else None
    searched = False
    for item in response.get("output", []):
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    value = part.get("text", "")
                    text.append(citations.render(value, part.get("annotations")) if citations else value)
                elif part.get("type") == "refusal":
                    text.append(part.get("refusal", ""))
        elif kind == "reasoning":
            states.append(copy.deepcopy(item))
            reasoning.extend(part["text"] for part in item.get("summary", []) if part.get("text"))
        elif kind == "function_call":
            name, call_id = item.get("name"), item.get("call_id")
            if item.get("namespace") != NAMESPACE or name not in names or not call_id or call_id in seen:
                raise SIWCError("protocol", "工具调用的命名空间、名称或 ID 无效")
            try:
                args = json.loads(item.get("arguments", ""))
                if not isinstance(args, dict):
                    raise ValueError
            except (ValueError, TypeError):
                raise SIWCError("protocol", "工具参数不是有效 JSON 对象，未交给框架执行") from None
            seen.add(call_id)
            result.tools_call_args.append(args)
            result.tools_call_name.append(name)
            result.tools_call_ids.append(call_id)
        elif kind == "web_search_call" and native_web_search:
            if item.get("status") != "completed":
                raise SIWCError("incomplete", "原生网页搜索未完整完成", stage="inference")
            searched = True  # Hosted execution; never hand it to the function executor.
        else:
            raise SIWCError("protocol", "模型返回了本插件尚未支持的输出类型")
    answer = "".join(text)
    result.completion_text = citations.finish(answer) if citations and answer else answer
    result.reasoning_content = "\n".join(reasoning) or None
    if states:
        result.reasoning_signature = json.dumps({"type": REASONING_STATE, "items": states}, ensure_ascii=False)
    if result.tools_call_ids:
        result.role = "tool"
    if not result.completion_text and not result.tools_call_ids and (searched or not result.reasoning_content):
        raise SIWCError("empty", "已完成响应中没有可用文本或工具调用")
    usage = response.get("usage") or {}
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
    result.usage = TokenUsage(input_other=max(0, usage.get("input_tokens", 0) - cached),
                              input_cached=cached, output=usage.get("output_tokens", 0))
    return result
