"""
simulator/event_stream.py

EventStream — append-only event log, source of truth for the simulation.

Architecture:
    - EventStream is decoupled from storage via StorageBackend interface
    - Phase-1 backend: JSONLBackend (local file)
    - Future backends: SupabaseBackend, KafkaBackend (swap with no changes)
    - In-memory deque maintains live tail for UI (no second file)

Industry standard:
    - Append-only event log (event sourcing pattern)
    - JSONL for local (one JSON object per line)
    - Postgres/Supabase for cloud (each event = one row)
"""

import json
import asyncio
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import asdict
from decimal import Decimal
from enum import Enum
from pathlib import Path

from events import BaseEvent


# ---------------------------------------------------------------------------
# Serialization helper
# ---------------------------------------------------------------------------

def _serialize(event: BaseEvent) -> dict:
    """
    Converts a frozen dataclass event to a JSON-serializable dict.
    Handles Decimal and Enum types which json.dumps cannot handle natively.
    """
    def convert(obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, Enum):
            return obj.value
        return obj

    raw = {}
    for f in event.__dataclass_fields__:
        raw[f] = convert(getattr(event, f))
    return raw


# ---------------------------------------------------------------------------
# StorageBackend interface
# ---------------------------------------------------------------------------

class StorageBackend(ABC):

    @abstractmethod
    async def write(self, records: list[dict]) -> None:
        """Append a batch of serialized event dicts to storage."""
        ...

    @abstractmethod
    async def flush(self) -> None:
        """Flush any buffered writes to storage."""
        ...


# ---------------------------------------------------------------------------
# JSONLBackend — Phase-1 local file backend
# ---------------------------------------------------------------------------

class JSONLBackend(StorageBackend):

    def __init__(self, path: str = "events.jsonl"):
        self._path = Path(path)
        self._handle = None

    async def write(self, records: list[dict]) -> None:
        if self._handle is None:
            self._handle = open(self._path, "a", encoding="utf-8")
        for record in records:
            self._handle.write(json.dumps(record) + "\n")

    async def flush(self) -> None:
        if self._handle:
            self._handle.flush()
            self._handle.close()
            self._handle = None


# ---------------------------------------------------------------------------
# EventStream
# ---------------------------------------------------------------------------

class EventStream:

    def __init__(
        self,
        backend     : StorageBackend,
        tail_size   : int = 100,        # live tail window size
    ):
        self._backend   = backend
        self._tail      : deque[dict] = deque(maxlen=tail_size)
        self._lock      = asyncio.Lock()
        self._total     : int = 0

    async def append(self, events: list[BaseEvent]) -> None:
        """
        Serialize and write events to storage backend.
        Also updates in-memory tail for live UI viewing.
        """
        if not events:
            return

        records = [_serialize(e) for e in events]

        async with self._lock:
            await self._backend.write(records)
            for r in records:
                self._tail.append(r)
            self._total += len(records)

    async def flush(self) -> None:
        """Called on shutdown to ensure all writes are committed."""
        async with self._lock:
            await self._backend.flush()

    def get_tail(self, n: int = 100) -> list[dict]:
        """
        Returns last n events from in-memory tail.
        Used by UI or terminal viewer — no disk read required.
        """
        tail = list(self._tail)
        return tail[-n:]

    @property
    def total_events(self) -> int:
        return self._total