"""The generational ledger itself.

Every parameter change is a *revision*: it says which revision it came from
(``based_on``), why it happened, and seals a digest chained to its predecessor.
Revisions are append-only rows in the same record stream as everything else, so
the history can never be edited -- only added to.

A revert is not an erasure: it is a brand new revision whose values are copied
from an older revision, linked back to it (``reverted_sequence``). Before a
revert is accepted the caller must acknowledge an :class:`ImpactReport` listing
everything already generated that will be voided as the generation moves.
"""

import hashlib
from dataclasses import dataclass, field
from typing import Any

from ..clock import IdSequence, LogicalClock
from ..errors import (
    LedgerTamperedError,
    RevisionConflictError,
    RevertAckMismatchError,
    UnknownRevisionError,
)
from ..event import EventBus, topics
from ..store.snapshot import canonical
from ..store import Journal, keys
from ..version import Confirmations, Generations
from .derivation import Derivation, DerivationRegistry
from .revision import Revision

ROOT_DIGEST = ""


@dataclass(frozen=True)
class ImpactReport:
    """What would be voided if a parameter were reverted to an older revision."""

    name: str
    current_sequence: int
    target_sequence: int
    target_values: dict[str, Any]
    derivations: tuple[Derivation, ...]
    confirmations: tuple[Any, ...]
    digest: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "current_sequence": self.current_sequence,
            "target_sequence": self.target_sequence,
            "target_values": self.target_values,
            "derivations": [
                {
                    "derivation_id": item.derivation_id,
                    "kind": item.kind,
                    "detail": item.detail,
                    "sequence": item.sequence,
                }
                for item in self.derivations
            ],
            "confirmations": [
                {
                    "confirmation_id": item.confirmation_id,
                    "subject": item.subject,
                    "generation": item.generation,
                    "expires_tick": item.expires_tick,
                }
                for item in self.confirmations
            ],
        }

    def compute_digest(self) -> str:
        return hashlib.sha256(canonical(self.payload()).encode("utf-8")).hexdigest()

    def sealed(self) -> "ImpactReport":
        return ImpactReport(
            name=self.name,
            current_sequence=self.current_sequence,
            target_sequence=self.target_sequence,
            target_values=self.target_values,
            derivations=self.derivations,
            confirmations=self.confirmations,
            digest=self.compute_digest(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.payload(),
            "voided_count": len(self.derivations),
            "confirmation_count": len(self.confirmations),
            "digest": self.digest,
        }


@dataclass
class _Verification:
    errors: list[str] = field(default_factory=list)
    revisions: int = 0


class ParameterLedger:
    """Append-only revision history with revert impact accounting."""

    def __init__(
        self,
        journal: Journal,
        bus: EventBus,
        clock: LogicalClock,
        generations: Generations,
        confirmations: Confirmations | None = None,
        derivations: DerivationRegistry | None = None,
        ids: IdSequence | None = None,
    ) -> None:
        self._journal = journal
        self._bus = bus
        self._clock = clock
        self._generations = generations
        self._confirmations = confirmations
        self._derivations = derivations or DerivationRegistry(journal, clock)
        self._ids = ids or IdSequence("rev")
        self._restore_ids()

    # ----------------------------------------------------------------- writes

    def publish(
        self,
        name: str,
        values: dict[str, Any],
        reason: str,
        operator: str,
        based_on: int | None = None,
    ) -> Revision:
        """Record a new revision of a parameter, based on the revision in force."""
        reason = self._require_reason(reason)
        operator = self._require_operator(operator)
        if not isinstance(values, dict):
            raise ValueError("revision values have to be a mapping")
        current = self._generations.current(name)
        expected = current if based_on is None else int(based_on)
        if expected != current:
            raise RevisionConflictError(
                f"{name} is at revision {current}; change was based on {expected}"
            )
        sequence = current + 1
        revisions = self._revisions(name)
        prev_digest = revisions[-1].digest if revisions else ROOT_DIGEST
        revision = Revision.new(
            revision_id=self._ids.next(),
            name=name,
            sequence=sequence,
            based_on=current,
            values=values,
            reason=reason,
            operator=operator,
            tick=self._clock.tick,
            prev_digest=prev_digest,
        )
        with self._journal.transaction("ledger-publish") as batch:
            self._store_revision(revision, batch)
            self._derivations.void_many(
                name,
                self._derivations.active(name),
                sequence,
                f"superseded by revision {sequence}",
                batch,
            )
        self._bus.publish(
            topics.TOPIC_LEDGER_PUBLISHED,
            {"name": name, "sequence": sequence, "revision_id": revision.revision_id},
        )
        return revision

    def revert(
        self,
        name: str,
        sequence: int,
        reason: str,
        operator: str,
        ack: str,
    ) -> Revision:
        """Reinstate an older revision's values as a new revision.

        The caller must pass the digest of the impact report it was shown, so a
        revert can never go through blind to what it is about to void.
        """
        reason = self._require_reason(reason)
        operator = self._require_operator(operator)
        target = self.revision(name, sequence)
        current = self._generations.current(name)
        if target.sequence == current:
            raise RevisionConflictError(f"{name} is already at revision {current}")
        report = self.impact(name, target.sequence)
        if not ack or ack != report.digest:
            raise RevertAckMismatchError(
                f"acknowledgement does not match the current impact report "
                f"({report.digest}) for reverting {name} to {target.sequence}"
            )
        new_sequence = current + 1
        revisions = self._revisions(name)
        revision = Revision.new(
            revision_id=self._ids.next(),
            name=name,
            sequence=new_sequence,
            based_on=current,
            values=dict(target.values),
            reason=reason,
            operator=operator,
            tick=self._clock.tick,
            prev_digest=revisions[-1].digest,
            kind="revert",
            reverted_sequence=target.sequence,
        )
        with self._journal.transaction("ledger-revert") as batch:
            self._store_revision(revision, batch)
            self._derivations.void_many(
                name,
                self._derivations.active(name),
                new_sequence,
                f"voided by revert to revision {target.sequence}",
                batch,
            )
        self._bus.publish(
            topics.TOPIC_LEDGER_REVERTED,
            {
                "name": name,
                "sequence": new_sequence,
                "reverted_sequence": target.sequence,
                "revision_id": revision.revision_id,
            },
        )
        return revision

    # ------------------------------------------------------------------ reads

    def names(self) -> tuple[str, ...]:
        prefix = keys.LEDGER_PREFIX
        return tuple(
            sorted(key[len(prefix) :] for key in self._journal.keys(prefix))
        )

    def current(self, name: str) -> Revision | None:
        sequence = self._generations.current(name)
        return None if sequence == 0 else self.revision(name, sequence)

    def revision(self, name: str, sequence: int) -> Revision:
        for revision in self._revisions(name):
            if revision.sequence == int(sequence):
                return revision
        raise UnknownRevisionError(f"no revision {sequence} in the ledger for {name}")

    def revisions(self, name: str) -> tuple[Revision, ...]:
        """Every revision of a parameter, oldest first; nothing is ever removed."""
        return self._revisions(name)

    def require_current_revision(self, name: str, sequence: int) -> Revision:
        current = self.current(name)
        if current is None or current.sequence != int(sequence):
            at = current.sequence if current is not None else 0
            raise RevisionConflictError(
                f"revision {sequence} of {name} is not in force (current is {at})"
            )
        return current

    def impact(self, name: str, sequence: int) -> ImpactReport:
        """List what reverting to a revision would void, without changing anything."""
        target = self.revision(name, sequence)
        current_sequence = self._generations.current(name)
        if target.sequence == current_sequence:
            derivations: tuple[Any, ...] = ()
            confirmations: tuple[Any, ...] = ()
        else:
            derivations = self._derivations.active(name)
            confirmations = ()
            if self._confirmations is not None:
                confirmations = tuple(
                    item
                    for item in self._confirmations.outstanding()
                    if item.name == name
                )
        return ImpactReport(
            name=name,
            current_sequence=current_sequence,
            target_sequence=target.sequence,
            target_values=dict(target.values),
            derivations=derivations,
            confirmations=confirmations,
        ).sealed()

    @property
    def derivations(self) -> DerivationRegistry:
        return self._derivations

    # ------------------------------------------------------------- integrity

    def verify(self) -> dict[str, Any]:
        """Replay every revision row and confirm the history has not been altered."""
        errors: list[str] = []
        checked = 0
        for name in self.names():
            result = self._verify_name(name)
            errors.extend(result.errors)
            checked += result.revisions
        return {
            "ok": not errors,
            "parameters": len(self.names()),
            "revisions": checked,
            "errors": errors,
        }

    def verify_or_raise(self) -> dict[str, Any]:
        report = self.verify()
        if not report["ok"]:
            raise LedgerTamperedError("; ".join(report["errors"]))
        return report

    # -------------------------------------------------------------- internals

    def _store_revision(self, revision: Revision, batch: str) -> None:
        self._journal.append(
            keys.ledger_revision_key(revision.name),
            revision.to_row(),
            batch,
            generation=revision.sequence,
        )
        self._journal.append(
            keys.version_key(revision.name),
            revision.sequence,
            batch,
            generation=revision.sequence,
        )

    def _revisions(self, name: str) -> tuple[Revision, ...]:
        entries = self._journal.history(keys.ledger_revision_key(name))
        return tuple(Revision.from_row(entry.value) for entry in entries)

    def _restore_ids(self) -> None:
        for name in self.names():
            for revision in self._revisions(name):
                number = self._revision_number(revision.revision_id)
                if number is not None:
                    self._ids.restore(number)

    @staticmethod
    def _revision_number(revision_id: str) -> int | None:
        _, _, tail = revision_id.rpartition("-")
        return int(tail) if tail.isdigit() else None

    @staticmethod
    def _require_reason(reason: str) -> str:
        if not str(reason or "").strip():
            raise ValueError("a revision needs a non-empty reason")
        return str(reason).strip()

    @staticmethod
    def _require_operator(operator: str) -> str:
        if not str(operator or "").strip():
            raise ValueError("a revision needs the operator who made it")
        return str(operator).strip()

    def _verify_name(self, name: str) -> _Verification:
        result = _Verification()
        revisions = self._revisions(name)
        previous: Revision | None = None
        for position, revision in enumerate(revisions, start=1):
            result.revisions += 1
            label = f"{name} revision {revision.sequence}"
            if revision.sequence != position:
                result.errors.append(f"{label}: expected sequence {position}")
            if revision.compute_digest() != revision.digest:
                result.errors.append(f"{label}: digest does not match its contents")
            expected_prev = previous.digest if previous is not None else ROOT_DIGEST
            if revision.prev_digest != expected_prev:
                result.errors.append(f"{label}: digest chain is broken")
            expected_based_on = previous.sequence if previous is not None else 0
            if revision.based_on != expected_based_on:
                result.errors.append(
                    f"{label}: based_on {revision.based_on} does not follow {expected_based_on}"
                )
            if revision.is_revert:
                try:
                    target = self.revision(name, revision.reverted_sequence)
                except UnknownRevisionError:
                    result.errors.append(
                        f"{label}: reverts unknown revision {revision.reverted_sequence}"
                    )
                else:
                    if revision.values != target.values:
                        result.errors.append(
                            f"{label}: values do not match revision {target.sequence}"
                        )
            previous = revision
        current = self._generations.current(name)
        if revisions and current != revisions[-1].sequence:
            result.errors.append(
                f"{name}: in-force generation is {current} but ledger head is {revisions[-1].sequence}"
            )
        return result
