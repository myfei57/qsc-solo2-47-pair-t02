"""Things generated from a parameter revision.

A derivation is anything the floor produced *because* a revision was in force:
a released work order, a calculated setpoint curve, a printed recipe sheet.
When its source revision is superseded or reverted away from, the derivation is
voided with a new row -- the row that recorded it is never deleted or edited.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from ..clock import IdSequence, LogicalClock, parse_id
from ..errors import StaleArtifactError, UnknownRevisionError
from ..store import Journal, keys
from .revision import Revision

if TYPE_CHECKING:
    from .service import ParameterLedger


@dataclass(frozen=True)
class Derivation:
    """A generated artifact pinned to the revision it was produced from."""

    derivation_id: str
    name: str
    sequence: int
    kind: str
    detail: str
    tick: int
    voided_sequence: int
    voided_tick: int
    voided_reason: str

    @property
    def voided(self) -> bool:
        return self.voided_sequence > 0

    def to_row(self) -> dict[str, Any]:
        return {
            "derivation_id": self.derivation_id,
            "name": self.name,
            "sequence": self.sequence,
            "kind": self.kind,
            "detail": self.detail,
            "tick": self.tick,
            "voided_sequence": self.voided_sequence,
            "voided_tick": self.voided_tick,
            "voided_reason": self.voided_reason,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Derivation":
        return cls(
            derivation_id=str(row["derivation_id"]),
            name=str(row["name"]),
            sequence=int(row["sequence"]),
            kind=str(row["kind"]),
            detail=str(row.get("detail", "")),
            tick=int(row["tick"]),
            voided_sequence=int(row.get("voided_sequence", 0)),
            voided_tick=int(row.get("voided_tick", 0)),
            voided_reason=str(row.get("voided_reason", "")),
        )


class DerivationRegistry:
    """Records generated artifacts and marks them void as generations move."""

    def __init__(self, journal: Journal, clock: LogicalClock) -> None:
        self._journal = journal
        self._clock = clock
        self._ids = IdSequence("drv")
        self._restore_ids()

    def record(
        self,
        ledger: "ParameterLedger",
        name: str,
        revision: Revision,
        kind: str,
        detail: str = "",
        batch_id: str | None = None,
    ) -> Derivation:
        """Register something generated under a revision, refusing stale bases."""
        ledger.require_current_revision(name, revision.sequence)
        derivation_id = self._ids.next()
        entry = Derivation(
            derivation_id=derivation_id,
            name=name,
            sequence=revision.sequence,
            kind=kind,
            detail=detail,
            tick=self._clock.tick,
            voided_sequence=0,
            voided_tick=0,
            voided_reason="",
        )

        def write(active_batch: str) -> None:
            self._journal.append(
                keys.ledger_derivation_key(name, derivation_id),
                entry.to_row(),
                active_batch,
                generation=revision.sequence,
            )

        if batch_id is not None:
            write(batch_id)
        else:
            with self._journal.transaction("ledger-derive") as active_batch:
                write(active_batch)
        return entry

    def get(self, name: str, derivation_id: str) -> Derivation | None:
        row = self._journal.read(keys.ledger_derivation_key(name, derivation_id))
        return None if row is None else Derivation.from_row(row)

    def require(self, name: str, derivation_id: str) -> Derivation:
        derivation = self.get(name, derivation_id)
        if derivation is None:
            raise UnknownRevisionError(f"no derivation {derivation_id} for {name}")
        return derivation

    def list(self, name: str | None = None) -> tuple[Derivation, ...]:
        """Every derivation, newest registration first; voided ones stay visible."""
        prefix = (
            keys.DERIVATION_PREFIX
            if name is None
            else f"{keys.DERIVATION_PREFIX}{name}/"
        )
        rows = (self._journal.read(key) for key in self._journal.keys(prefix))
        entries = [Derivation.from_row(row) for row in rows if row is not None]
        entries.sort(key=lambda item: (item.tick, item.derivation_id), reverse=True)
        return tuple(entries)

    def void_many(
        self,
        name: str,
        entries: tuple[Derivation, ...],
        new_sequence: int,
        reason: str,
        batch_id: str,
    ) -> tuple[Derivation, ...]:
        """Append a voiding row for each derivation; the original row is untouched."""
        voided: list[Derivation] = []
        for entry in entries:
            updated = Derivation(
                derivation_id=entry.derivation_id,
                name=entry.name,
                sequence=entry.sequence,
                kind=entry.kind,
                detail=entry.detail,
                tick=entry.tick,
                voided_sequence=new_sequence,
                voided_tick=self._clock.tick,
                voided_reason=reason,
            )
            self._journal.append(
                keys.ledger_derivation_key(name, entry.derivation_id),
                updated.to_row(),
                batch_id,
                generation=new_sequence,
            )
            voided.append(updated)
        return tuple(voided)

    def active(self, name: str) -> tuple[Derivation, ...]:
        """Derivations still in force for a parameter."""
        return tuple(entry for entry in self.list(name) if not entry.voided)

    def require_active(self, name: str, derivation_id: str) -> Derivation:
        """Refuse a derivation that was never registered or has been voided."""
        derivation = self.require(name, derivation_id)
        if derivation.voided:
            raise StaleArtifactError(
                f"derivation {derivation_id} for {name} was voided at "
                f"revision {derivation.voided_sequence}: {derivation.voided_reason}"
            )
        return derivation

    def _restore_ids(self) -> None:
        """Keep the identifier source ahead of rows recovered from a replay."""
        for key in self._journal.keys(keys.DERIVATION_PREFIX):
            tail = key.rsplit("/", 1)[-1]
            issued = parse_id(tail)
            if issued is not None:
                self._ids.restore(issued)
