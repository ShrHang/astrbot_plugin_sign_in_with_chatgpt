"""Bounded local inference counters; never persist request or response content."""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time


COUNTERS = ("requests", "succeeded", "failed", "cancelled", "input_tokens", "cached_tokens",
            "output_tokens", "usage_reported_requests", "input_reported_requests", "cached_reported_requests",
            "output_reported_requests", "total_duration_ms")
MAX_MODELS, MAX_RECENT = 64, 100


def empty_counters():
    return dict.fromkeys(COUNTERS, 0)


def number(value):
    return value if type(value) is int and 0 <= value <= 10**15 else 0


def reported_usage(value):
    """Keep absent usage distinct from a server reporting zero tokens."""
    if not isinstance(value, dict):
        return None
    details = value.get("input_tokens_details")
    fields = {"input_tokens": value.get("input_tokens"), "output_tokens": value.get("output_tokens"),
              "cached_tokens": details.get("cached_tokens") if isinstance(details, dict) else None}
    return {key: item for key, item in fields.items() if type(item) is int and 0 <= item <= 10**15} or None


def model_label(model):
    return model if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,96}", model) else "(other)"


class UsageStore:
    def __init__(self, directory: Path):
        self.path = directory / "usage.json"
        self.daily = {}
        self.recent = []
        self.storage_error = False
        self.closed = False
        self._revision = 0
        self._writer = None

    @staticmethod
    def _window(now=None):
        today = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).date()
        start = today - timedelta(days=29)
        return start.isoformat(), today.isoformat(), int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp())

    def _prune(self):
        first, last, since = self._window()
        self.daily = {day: values for day, values in self.daily.items() if first <= day <= last}
        self.recent = [item for item in self.recent if since <= item["at"] <= time.time() + 60][-MAX_RECENT:]

    def open(self):
        """Called off-loop only after the credential store owns/protects the directory."""
        try:
            if not self.path.exists():
                return
            if self.path.stat().st_size > 2_000_000:
                raise ValueError("oversized statistics")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("daily"), list):
                raise ValueError("invalid statistics")
            # Whitelist and bound every persisted field, including externally edited files.
            known = set()
            first, last, since = self._window()
            for row in raw["daily"][-30:]:
                day = row.get("date") if isinstance(row, dict) else None
                if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or not first <= day <= last:
                    continue
                datetime.strptime(day, "%Y-%m-%d")
                models = row.get("models")
                cleaned = {}
                if isinstance(models, dict):
                    for label, counts in list(models.items())[:MAX_MODELS + 1]:
                        label = model_label(label)
                        if label != "(other)" and label not in known:
                            if len(known) >= MAX_MODELS:
                                label = "(other)"
                            else:
                                known.add(label)
                        if isinstance(counts, dict):
                            target = cleaned.setdefault(label, empty_counters())
                            for key in COUNTERS:
                                target[key] += number(counts.get(key))
                self.daily[day] = cleaned
            rows = raw.get("recent", [])
            for row in rows[-MAX_RECENT:] if isinstance(rows, list) else []:
                if not isinstance(row, dict) or type(row.get("at")) not in (int, float) or not math.isfinite(row["at"]) or not since <= row["at"] <= time.time() + 60:
                    continue
                if row.get("kind") not in ("chat", "search") or row.get("outcome") not in ("succeeded", "failed", "cancelled"):
                    continue
                label = model_label(row.get("model"))
                reported = row.get("usage_reported") is True
                self.recent.append({"at": row["at"], "model": label if label in known else "(other)",
                    "kind": row["kind"], "outcome": row["outcome"], "duration_ms": number(row.get("duration_ms")),
                    "usage_reported": reported,
                    **{key: number(row.get(key)) if reported and row.get(key) is not None else None
                       for key in ("input_tokens", "cached_tokens", "output_tokens")}})
        except Exception:
            self.daily, self.recent = {}, []
            self.storage_error = True

    def record(self, *, model, kind, outcome, duration_ms, usage=None):
        if self.closed:
            return
        self._prune()
        known = {label for models in self.daily.values() for label in models if label != "(other)"}
        label = model_label(model)
        if label not in known and len(known) >= MAX_MODELS:
            label = "(other)"
        now = time.time()
        date = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        counts = self.daily.setdefault(date, {}).setdefault(label, empty_counters())
        counts["requests"] += 1
        counts[outcome] += 1
        counts["total_duration_ms"] += number(duration_ms)
        if usage is not None:
            counts["usage_reported_requests"] += 1
            for key in ("input_tokens", "cached_tokens", "output_tokens"):
                counts[key] += number(usage.get(key))
                if key in usage:
                    counts[key.removesuffix("_tokens") + "_reported_requests"] += 1
        self.recent.append({"at": now, "model": label, "kind": kind, "outcome": outcome,
            "duration_ms": number(duration_ms), "usage_reported": usage is not None,
            **{key: usage.get(key) if usage else None for key in ("input_tokens", "cached_tokens", "output_tokens")}})
        self.recent = self.recent[-MAX_RECENT:]
        self._revision += 1
        if self._writer is None or self._writer.done():
            self._writer = asyncio.create_task(self._write_pending(), name="siwc:usage-save")

    def snapshot(self):
        self._prune()
        totals, daily, models = empty_counters(), [], {}
        for day, values in sorted(self.daily.items()):
            row = {"date": day, **empty_counters()}
            for model, counts in values.items():
                model_counts = models.setdefault(model, empty_counters())
                for key in COUNTERS:
                    row[key] += counts[key]
                    totals[key] += counts[key]
                    model_counts[key] += counts[key]
            daily.append(row)
        return {"days": 30, "timezone": "UTC", "since": self._window()[2], "totals": totals,
            "daily": daily, "models": [{"model": model, **counts} for model, counts in sorted(models.items())],
            "recent": copy.deepcopy(list(reversed(self.recent))), "storage_error": self.storage_error}

    def _save(self, payload):
        fd, name = tempfile.mkstemp(prefix=".usage-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    async def _write_pending(self):
        while True:
            revision = self._revision
            payload = {"version": 1, "daily": [{"date": day, "models": copy.deepcopy(models)} for day, models in sorted(self.daily.items())],
                       "recent": copy.deepcopy(self.recent)}
            try:
                await asyncio.to_thread(self._save, payload)
                self.storage_error = False
            except Exception:
                self.storage_error = True
            if revision == self._revision:
                return

    async def close(self):
        self.closed = True
        if self._writer is not None:
            await self._writer
