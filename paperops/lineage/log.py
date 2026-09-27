"""Append-only, tamper-evident file backing the generation ledger.

Rows are written one JSON object per line and never rewritten. On load every row
is re-checked: its sequence must follow in order, its ``prev_hash`` must equal the
previous row hash, and its own hash must recompute. Any change to history is
therefore detected rather than silently replayed.
"""

import json
import os
from typing import Iterator

from ..errors import LedgerIntegrityError
from .records import GENESIS_HASH, LedgerEvent


class LineageLog:
    """The on-disk hash chain; no mutation of a written row is ever offered."""

    LOG_NAME = "lineage.log"

    def __init__(self, directory: str | None = None) -> None:
        self._path = os.path.join(directory, self.LOG_NAME) if directory else None
        self._events: list[LedgerEvent] = []
        if directory is not None:
            os.makedirs(directory, exist_ok=True)
            self._load()

    # ------------------------------------------------------------------ writes

    def append(self, kind: str, payload: dict, tick: int, actor: str) -> LedgerEvent:
        """Seal a new row onto the tail of the chain."""
        prev_hash = self.tail_hash
        event = LedgerEvent.create(
            seq=len(self._events) + 1,
            kind=kind,
            payload=payload,
            tick=tick,
            actor=actor,
            prev_hash=prev_hash,
        )
        self._events.append(event)
        self._write(event)
        return event

    # ------------------------------------------------------------------- reads

    @property
    def tail_hash(self) -> str:
        return self._events[-1].hash if self._events else GENESIS_HASH

    @property
    def sequence(self) -> int:
        return len(self._events)

    def events(self) -> tuple[LedgerEvent, ...]:
        return tuple(self._events)

    def verify(self) -> dict:
        """Walk the chain; raise :class:`LedgerIntegrityError` at the first break."""
        prev_hash = GENESIS_HASH
        for index, event in enumerate(self._events, start=1):
            if event.seq != index:
                raise LedgerIntegrityError(
                    f"ledger row {index} carries out-of-order sequence {event.seq}"
                )
            if event.prev_hash != prev_hash:
                raise LedgerIntegrityError(
                    f"ledger row {index} is not linked to its predecessor"
                )
            if not event.valid():
                raise LedgerIntegrityError(
                    f"ledger row {index} fails its hash check; history was altered"
                )
            prev_hash = event.hash
        return {"events": len(self._events), "tail_hash": self.tail_hash}

    def __iter__(self) -> Iterator[LedgerEvent]:
        return iter(self._events)

    # ---------------------------------------------------------------- internals

    def _write(self, event: LedgerEvent) -> None:
        if self._path is None:
            return
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_row(), sort_keys=True, ensure_ascii=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _load(self) -> None:
        if self._path is None or not os.path.exists(self._path):
            return
        with open(self._path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    self._events.append(LedgerEvent.from_row(json.loads(line)))
                except (KeyError, TypeError, ValueError) as error:
                    raise LedgerIntegrityError(
                        f"ledger line {line_number} is unreadable: {error}"
                    ) from error
        self.verify()
