"""The generation ledger for process parameters.

Every field change is recorded as a *revision* that says which revision it came
from and why. Revisions are never overwritten: editing from an older revision grows
a branch, and "rolling back" does not delete history either -- it publishes a fresh
revision whose parameters equal the old one and which points back at the revision
that was current.

Things produced against a revision (a generated curve, a released work order, a
produced reel...) register as *outputs* pinned to that revision. Before a rollback
is committed, the ledger reports which live outputs were produced on the revisions
that would be superseded, and therefore must be declared void. The report carries a
digest that has to be handed back unchanged, so the caller proves it saw the list.
"""

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from ..clock import IdSequence, LogicalClock, parse_id
from ..errors import (
    DuplicateBatchError,
    LedgerIntegrityError,
    RevisionNotAncestorError,
    RollbackPlanMismatchError,
    UnknownRevisionError,
)
from ..event import EventBus, topics
from .log import LineageLog

REVISION = "revision"
OUTPUT = "output"
OUTPUT_VOID = "output_void"

EDIT = "edit"
ROLLBACK = "rollback"

LIVE = "live"
VOIDED = "voided"


@dataclass(frozen=True)
class Revision:
    """One immutable version of a named parameter set."""

    revision_id: str
    name: str
    parameters: Mapping[str, Any]
    parent_id: str | None
    reason: str
    change_kind: str
    rollback_of: str | None
    tick: int
    actor: str

    def __post_init__(self) -> None:
        # A frozen dataclass still lets callers mutate the nested mapping; seal it.
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    def to_row(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "name": self.name,
            "parameters": dict(self.parameters),
            "parent_id": self.parent_id,
            "reason": self.reason,
            "change_kind": self.change_kind,
            "rollback_of": self.rollback_of,
            "tick": self.tick,
            "actor": self.actor,
        }


@dataclass(frozen=True)
class Output:
    """Something generated from a revision; it lives or is declared void."""

    output_id: str
    name: str
    revision_id: str
    kind: str
    detail: Mapping[str, Any]
    status: str
    tick: int
    actor: str
    voided_tick: int | None = None
    voided_by: str | None = None
    void_reason: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "output_id": self.output_id,
            "name": self.name,
            "revision_id": self.revision_id,
            "kind": self.kind,
            "detail": dict(self.detail),
            "status": self.status,
            "tick": self.tick,
            "actor": self.actor,
            "voided_tick": self.voided_tick,
            "voided_by": self.voided_by,
            "void_reason": self.void_reason,
        }


def diff_parameters(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Shallow field-by-field difference between two parameter sets."""
    changes: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        if key not in before:
            changes.append({"key": key, "change": "added", "from": None, "to": after[key]})
        elif key not in after:
            changes.append({"key": key, "change": "removed", "from": before[key], "to": None})
        elif before[key] != after[key]:
            changes.append({"key": key, "change": "set", "from": before[key], "to": after[key]})
    return changes


class LineageService:
    """Owns the revision tree, pinned outputs and the rollback accounting."""

    def __init__(
        self,
        log: LineageLog,
        clock: LogicalClock,
        bus: EventBus | None = None,
    ) -> None:
        self._log = log
        self._clock = clock
        self._bus = bus or EventBus()
        self._ids = IdSequence("rev")
        self._revisions: dict[str, Revision] = {}
        self._order: dict[str, list[str]] = {}
        self._children: dict[str | None, list[str]] = {}
        self._current: dict[str, str] = {}
        self._outputs: dict[str, Output] = {}
        self._replay()

    # ------------------------------------------------------------- revisions

    def revise(
        self,
        name: str,
        parameters: Mapping[str, Any],
        reason: str,
        actor: str = "operator",
        parent_id: str | None = None,
    ) -> Revision:
        """Publish a new revision of a parameter set.

        With no parent given the revision descends from the current revision (or
        opens the parameter set). A parent may be named to branch from history.
        """
        if not isinstance(parameters, Mapping):
            raise ValueError("parameters have to be a mapping")
        if not reason or not str(reason).strip():
            raise ValueError("a change reason is required")
        if parent_id is None:
            parent_id = self._current.get(name)
        else:
            parent = self.require_revision(parent_id)
            if parent.name != name:
                raise UnknownRevisionError(
                    f"revision {parent_id} belongs to {parent.name}, not {name}"
                )
        revision_id = self._ids.next()
        revision = Revision(
            revision_id=revision_id,
            name=name,
            parameters=dict(parameters),
            parent_id=parent_id,
            reason=str(reason).strip(),
            change_kind=EDIT,
            rollback_of=None,
            tick=self._clock.tick,
            actor=str(actor),
        )
        self._append_revision(revision)
        self._bus.publish(
            topics.TOPIC_LINEAGE_REVISION,
            {"revision_id": revision_id, "name": name, "parent_id": parent_id,
             "reason": revision.reason, "change_kind": EDIT},
        )
        return revision

    def get_revision(self, revision_id: str) -> Revision | None:
        return self._revisions.get(revision_id)

    def require_revision(self, revision_id: str) -> Revision:
        revision = self.get_revision(revision_id)
        if revision is None:
            raise UnknownRevisionError(f"unknown revision {revision_id}")
        return revision

    def current_revision(self, name: str) -> Revision | None:
        current = self._current.get(name)
        return self._revisions.get(current) if current is not None else None

    def parameter_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._current))

    def history(self, name: str) -> tuple[Revision, ...]:
        """Every revision ever published for a name, in ledger order."""
        return tuple(self._revisions[rid] for rid in self._order.get(name, ()))

    def children_of(self, revision_id: str) -> tuple[Revision, ...]:
        """Revisions that claim the given revision as their parent (branches)."""
        self.require_revision(revision_id)
        return tuple(self._revisions[rid] for rid in self._children.get(revision_id, ()))

    def ancestry(self, name: str, revision_id: str | None = None) -> tuple[Revision, ...]:
        """Root-to-tip chain ending at the named revision, or the current one."""
        tip = self._current[name] if revision_id is None else self.require_revision(revision_id).revision_id
        chain: list[str] = []
        cursor: str | None = tip
        seen: set[str] = set()
        while cursor is not None:
            if cursor in seen:
                raise LedgerIntegrityError(f"revision {cursor} is part of a parent cycle")
            seen.add(cursor)
            revision = self.require_revision(cursor)
            if revision.name != name:
                raise UnknownRevisionError(
                    f"revision {cursor} belongs to {revision.name}, not {name}"
                )
            chain.append(cursor)
            cursor = revision.parent_id
        return tuple(self._revisions[rid] for rid in reversed(chain))

    def lineage(self, name: str) -> dict[str, Any]:
        """The generational account for one parameter set."""
        if name not in self._current:
            raise UnknownRevisionError(f"no revisions recorded for {name}")
        current = self.require_revision(self._current[name])
        chain = self.ancestry(name, current.revision_id)
        chain_ids = {revision.revision_id for revision in chain}
        branches = [
            revision.to_row()
            for revision in self.history(name)
            if revision.revision_id not in chain_ids
        ]
        return {
            "name": name,
            "current": current.to_row(),
            "chain": [revision.to_row() for revision in chain],
            "branches": branches,
        }

    # --------------------------------------------------------------- outputs

    def register_output(
        self,
        output_id: str,
        revision_id: str,
        kind: str,
        detail: Mapping[str, Any] | None = None,
        actor: str = "operator",
    ) -> Output:
        """Record something produced under a revision, pinning it to that version."""
        revision = self.require_revision(revision_id)
        if output_id in self._outputs:
            raise DuplicateBatchError(f"output {output_id} was already registered")
        output = Output(
            output_id=str(output_id),
            name=revision.name,
            revision_id=revision_id,
            kind=str(kind),
            detail=dict(detail or {}),
            status=LIVE,
            tick=self._clock.tick,
            actor=str(actor),
        )
        self._log.append(
            OUTPUT,
            {
                "output_id": output.output_id,
                "name": output.name,
                "revision_id": revision_id,
                "kind": output.kind,
                "detail": dict(output.detail),
                "status": LIVE,
            },
            tick=self._clock.tick,
            actor=output.actor,
        )
        self._outputs[output.output_id] = output
        self._bus.publish(
            topics.TOPIC_LINEAGE_OUTPUT,
            {"output_id": output.output_id, "name": output.name,
             "revision_id": revision_id, "kind": output.kind},
        )
        return output

    def get_output(self, output_id: str) -> Output | None:
        return self._outputs.get(output_id)

    def outputs(
        self,
        name: str | None = None,
        revision_id: str | None = None,
        status: str | None = None,
    ) -> tuple[Output, ...]:
        found = self._outputs.values()
        if name is not None:
            found = (item for item in found if item.name == name)
        if revision_id is not None:
            found = (item for item in found if item.revision_id == revision_id)
        if status is not None:
            found = (item for item in found if item.status == status)
        return tuple(found)

    # -------------------------------------------------------------- rollback

    def _interval_to_rollback(self, name: str, target_id: str) -> tuple[list[Revision], Revision]:
        """The live chain revisions that would be superseded to reach a target."""
        if name not in self._current:
            raise UnknownRevisionError(f"no revisions recorded for {name}")
        target = self.require_revision(target_id)
        if target.name != name:
            raise UnknownRevisionError(
                f"revision {target_id} belongs to {target.name}, not {name}"
            )
        chain = self.ancestry(name)
        ids = [revision.revision_id for revision in chain]
        if target_id not in ids:
            raise RevisionNotAncestorError(
                f"revision {target_id} is not on the ancestry chain of the "
                f"current revision {ids[-1]} of {name}"
            )
        index = ids.index(target_id)
        interval = list(chain[index + 1:])
        if not interval:
            raise RevisionNotAncestorError(
                f"revision {target_id} is already the current revision of {name}"
            )
        return interval, target

    def rollback_plan(self, name: str, target_id: str, reason: str) -> dict[str, Any]:
        """Preview a rollback: what comes back and which live outputs go void."""
        if not reason or not str(reason).strip():
            raise ValueError("a rollback reason is required")
        interval, target = self._interval_to_rollback(name, target_id)
        superseded_ids = [revision.revision_id for revision in interval]
        voided = [
            output
            for output in self.outputs(name=name, status=LIVE)
            if output.revision_id in set(superseded_ids)
        ]
        body = {
            "name": name,
            "target_revision_id": target.revision_id,
            "current_revision_id": superseded_ids[-1],
            "superseded_revisions": superseded_ids,
            "voided_outputs": [output.output_id for output in voided],
            "reason": str(reason).strip(),
        }
        return {"plan": body, "digest": self._plan_digest(body)}

    def commit_rollback(
        self,
        name: str,
        target_id: str,
        reason: str,
        digest: str,
        actor: str = "operator",
    ) -> dict[str, Any]:
        """Publish the rollback revision and void the reported outputs.

        The digest returned by :meth:`rollback_plan` must be handed back exactly,
        proving the caller saw the list of artifacts the rollback invalidates.
        """
        preview = self.rollback_plan(name, target_id, reason)
        if digest != preview["digest"]:
            raise RollbackPlanMismatchError(
                "rollback digest does not match the current impact report; "
                "re-read the plan before committing"
            )
        target = self.require_revision(target_id)
        previous_current = self._current[name]
        revision_id = self._ids.next()
        revision = Revision(
            revision_id=revision_id,
            name=name,
            parameters=dict(target.parameters),
            parent_id=previous_current,
            reason=preview["plan"]["reason"],
            change_kind=ROLLBACK,
            rollback_of=target_id,
            tick=self._clock.tick,
            actor=str(actor),
        )
        self._append_revision(revision)

        superseded = set(preview["plan"]["superseded_revisions"])
        voided: list[Output] = []
        for output in self.outputs(name=name, status=LIVE):
            if output.revision_id not in superseded:
                continue
            voided_output = self._void_output(output, revision_id, revision.reason)
            voided.append(voided_output)
        self._bus.publish(
            topics.TOPIC_LINEAGE_REVISION,
            {"revision_id": revision_id, "name": name, "parent_id": previous_current,
             "reason": revision.reason, "change_kind": ROLLBACK,
             "rollback_of": target_id,
             "voided_outputs": [output.output_id for output in voided]},
        )
        return {
            "revision": revision.to_row(),
            "restored_revision_id": target_id,
            "voided_outputs": [output.to_row() for output in voided],
        }

    # ----------------------------------------------------------------- report

    def report(self) -> dict[str, Any]:
        """One screen of accounts: every parameter set and where it currently stands."""
        entries = []
        for name in self.parameter_names():
            current = self.current_revision(name)
            history = self.history(name)
            live = self.outputs(name=name, status=LIVE)
            entries.append(
                {
                    "name": name,
                    "current_revision_id": current.revision_id if current else None,
                    "revisions": len(history),
                    "live_outputs": len(live),
                    "voided_outputs": len(self.outputs(name=name, status=VOIDED)),
                }
            )
        return {
            "parameters": entries,
            "events": self._log.sequence,
            "tail_hash": self._log.tail_hash,
        }

    def verify(self) -> dict[str, Any]:
        """Re-check the hash chain and replay linkage against the derived state."""
        result = self._log.verify()
        revisions = sum(len(ids) for ids in self._order.values())
        if revisions != sum(1 for event in self._log.events() if event.kind == REVISION):
            raise LedgerIntegrityError("replayed revision state disagrees with the ledger")
        return result

    # ---------------------------------------------------------------- internals

    @staticmethod
    def _plan_digest(body: Mapping[str, Any]) -> str:
        encoded = json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("ascii")).hexdigest()

    def _append_revision(self, revision: Revision) -> None:
        self._log.append(
            REVISION,
            {
                "revision_id": revision.revision_id,
                "name": revision.name,
                "parameters": dict(revision.parameters),
                "parent_id": revision.parent_id,
                "reason": revision.reason,
                "change_kind": revision.change_kind,
                "rollback_of": revision.rollback_of,
            },
            tick=revision.tick,
            actor=revision.actor,
        )
        self._revisions[revision.revision_id] = revision
        self._order.setdefault(revision.name, []).append(revision.revision_id)
        self._children.setdefault(revision.parent_id, []).append(revision.revision_id)
        self._current[revision.name] = revision.revision_id

    def _void_output(self, output: Output, voided_by: str, reason: str) -> Output:
        self._log.append(
            OUTPUT_VOID,
            {"output_id": output.output_id, "voided_by": voided_by, "reason": reason},
            tick=self._clock.tick,
            actor="rollback",
        )
        voided = Output(
            output_id=output.output_id,
            name=output.name,
            revision_id=output.revision_id,
            kind=output.kind,
            detail=dict(output.detail),
            status=VOIDED,
            tick=output.tick,
            actor=output.actor,
            voided_tick=self._clock.tick,
            voided_by=voided_by,
            void_reason=reason,
        )
        self._outputs[output.output_id] = voided
        self._bus.publish(
            topics.TOPIC_LINEAGE_OUTPUT_VOID,
            {"output_id": output.output_id, "voided_by": voided_by, "reason": reason},
        )
        return voided

    def _replay(self) -> None:
        """Rebuild every index from the immutable ledger rows."""
        for event in self._log.events():
            number = parse_id(str(event.payload.get("revision_id", "")))
            if number is not None:
                self._ids.restore(number)
            if event.kind == REVISION:
                payload = event.payload
                revision = Revision(
                    revision_id=str(payload["revision_id"]),
                    name=str(payload["name"]),
                    parameters=dict(payload["parameters"]),
                    parent_id=payload["parent_id"],
                    reason=str(payload["reason"]),
                    change_kind=str(payload["change_kind"]),
                    rollback_of=payload.get("rollback_of"),
                    tick=event.tick,
                    actor=event.actor,
                )
                self._revisions[revision.revision_id] = revision
                self._order.setdefault(revision.name, []).append(revision.revision_id)
                self._children.setdefault(revision.parent_id, []).append(revision.revision_id)
                self._current[revision.name] = revision.revision_id
            elif event.kind == OUTPUT:
                payload = event.payload
                self._outputs[str(payload["output_id"])] = Output(
                    output_id=str(payload["output_id"]),
                    name=str(payload["name"]),
                    revision_id=str(payload["revision_id"]),
                    kind=str(payload["kind"]),
                    detail=dict(payload.get("detail", {})),
                    status=str(payload.get("status", LIVE)),
                    tick=event.tick,
                    actor=event.actor,
                )
            elif event.kind == OUTPUT_VOID:
                payload = event.payload
                original = self._outputs.get(str(payload["output_id"]))
                if original is None:
                    raise LedgerIntegrityError(
                        f"ledger row {event.seq} voids unknown output {payload['output_id']}"
                    )
                self._outputs[original.output_id] = Output(
                    output_id=original.output_id,
                    name=original.name,
                    revision_id=original.revision_id,
                    kind=original.kind,
                    detail=dict(original.detail),
                    status=VOIDED,
                    tick=original.tick,
                    actor=original.actor,
                    voided_tick=event.tick,
                    voided_by=str(payload["voided_by"]),
                    void_reason=str(payload["reason"]),
                )
            else:
                raise LedgerIntegrityError(f"ledger row {event.seq} has unknown kind {event.kind}")
