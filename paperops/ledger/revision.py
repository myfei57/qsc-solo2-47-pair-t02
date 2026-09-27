"""One immutable revision of a process parameter.

A revision records not only the values in force but also the generation it came
from (``based_on``) and why the change was made. Every revision carries a digest
chained to its predecessor, so a row altered after the fact can be detected.
"""

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from ..store.snapshot import canonical


@dataclass(frozen=True)
class Revision:
    """A single entry in a parameter's generational ledger."""

    revision_id: str
    name: str
    sequence: int
    based_on: int
    values: dict[str, Any]
    reason: str
    operator: str
    kind: str
    reverted_sequence: int
    tick: int
    prev_digest: str
    digest: str

    @property
    def is_revert(self) -> bool:
        return self.kind == "revert"

    def digest_input(self) -> str:
        """Text folded into this revision's digest, predecessor excluded."""
        return canonical(
            {
                "revision_id": self.revision_id,
                "name": self.name,
                "sequence": self.sequence,
                "based_on": self.based_on,
                "values": self.values,
                "reason": self.reason,
                "operator": self.operator,
                "kind": self.kind,
                "reverted_sequence": self.reverted_sequence,
                "tick": self.tick,
                "prev_digest": self.prev_digest,
            }
        )

    def compute_digest(self) -> str:
        return hashlib.sha256(self.digest_input().encode("utf-8")).hexdigest()

    def to_row(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "name": self.name,
            "sequence": self.sequence,
            "based_on": self.based_on,
            "values": self.values,
            "reason": self.reason,
            "operator": self.operator,
            "kind": self.kind,
            "reverted_sequence": self.reverted_sequence,
            "tick": self.tick,
            "prev_digest": self.prev_digest,
            "digest": self.digest,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Revision":
        return cls(
            revision_id=str(row["revision_id"]),
            name=str(row["name"]),
            sequence=int(row["sequence"]),
            based_on=int(row["based_on"]),
            values=dict(row["values"]),
            reason=str(row["reason"]),
            operator=str(row["operator"]),
            kind=str(row.get("kind", "change")),
            reverted_sequence=int(row.get("reverted_sequence", 0)),
            tick=int(row["tick"]),
            prev_digest=str(row.get("prev_digest", "")),
            digest=str(row["digest"]),
        )

    @classmethod
    def new(
        cls,
        revision_id: str,
        name: str,
        sequence: int,
        based_on: int,
        values: Mapping[str, Any],
        reason: str,
        operator: str,
        tick: int,
        prev_digest: str,
        kind: str = "change",
        reverted_sequence: int = 0,
    ) -> "Revision":
        """Build a revision and seal it with its chained digest."""
        revision = cls(
            revision_id=revision_id,
            name=name,
            sequence=sequence,
            based_on=based_on,
            values=dict(values),
            reason=reason,
            operator=operator,
            kind=kind,
            reverted_sequence=reverted_sequence,
            tick=tick,
            prev_digest=prev_digest,
            digest="",
        )
        return cls(**{**revision.to_row(), "digest": revision.compute_digest()})
