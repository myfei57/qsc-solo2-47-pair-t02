"""The immutable rows of the generation ledger.

Every change to a process parameter is one ledger row. Rows never overwrite one
another: a new revision points at the revision it came from, so the rows form a
version tree. Each row also carries the hash of the row before it (a hash chain),
so altering any historical row breaks the chain and is detected on load.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class LedgerEvent:
    """One append-only row of the generation ledger."""

    seq: int
    kind: str
    payload: Mapping[str, Any]
    tick: int
    actor: str
    prev_hash: str
    hash: str

    @staticmethod
    def digest(seq: int, kind: str, payload: Mapping[str, Any], tick: int,
               actor: str, prev_hash: str) -> str:
        """Hash everything the row is made of, except the hash itself."""
        body = {
            "seq": int(seq),
            "kind": str(kind),
            "payload": payload,
            "tick": int(tick),
            "actor": str(actor),
            "prev_hash": prev_hash,
        }
        encoded = json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("ascii")).hexdigest()

    @classmethod
    def create(cls, seq: int, kind: str, payload: Mapping[str, Any], tick: int,
               actor: str, prev_hash: str) -> "LedgerEvent":
        return cls(
            seq=int(seq),
            kind=str(kind),
            payload=dict(payload),
            tick=int(tick),
            actor=str(actor),
            prev_hash=prev_hash,
            hash=cls.digest(seq, kind, payload, tick, actor, prev_hash),
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "payload": dict(self.payload),
            "tick": self.tick,
            "actor": self.actor,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "LedgerEvent":
        return cls(
            seq=int(row["seq"]),
            kind=str(row["kind"]),
            payload=dict(row["payload"]),
            tick=int(row["tick"]),
            actor=str(row["actor"]),
            prev_hash=str(row["prev_hash"]),
            hash=str(row["hash"]),
        )

    def valid(self) -> bool:
        """Recompute the hash and check the linkage against the previous row."""
        expected = self.digest(self.seq, self.kind, self.payload, self.tick,
                               self.actor, self.prev_hash)
        return expected == self.hash
