"""Generational ledger: lineage, void accounting on revert and immutable history."""

import pytest

from paperops.clock import IdSequence, LogicalClock
from paperops.errors import (
    LedgerTamperedError,
    RevisionConflictError,
    RevertAckMismatchError,
    StaleArtifactError,
    UnknownRevisionError,
)
from paperops.event import EventBus
from paperops.ledger import ParameterLedger
from paperops.version import Confirmations, Generations

from support import call, ok, refused


def build_ledger(journal, bus=None, clock=None):
    bus = bus or EventBus()
    clock = clock or LogicalClock()
    generations = Generations(journal)
    confirmations = Confirmations(journal, clock, IdSequence("cfm"), generations)
    return ParameterLedger(journal, bus, clock, generations, confirmations=confirmations), clock


def test_first_revision_is_based_on_zero(journal):
    ledger, _ = build_ledger(journal)
    revision = ledger.publish("press.recipe", {"load": 45}, "initial recipe", "甲")
    assert revision.sequence == 1
    assert revision.based_on == 0
    assert revision.kind == "change"
    assert ledger.current("press.recipe").revision_id == revision.revision_id
    assert ledger.revision("press.recipe", 1).values == {"load": 45}


def test_each_revision_records_where_it_came_from_and_why(journal):
    ledger, _ = build_ledger(journal)
    first = ledger.publish("press.recipe", {"load": 45}, "v1", "甲")
    second = ledger.publish("press.recipe", {"load": 50}, "起泡提压", "乙")
    assert second.based_on == first.sequence
    assert second.reason == "起泡提压"
    assert second.prev_digest == first.digest
    assert second.digest != first.digest
    revisions = ledger.revisions("press.recipe")
    assert [r.sequence for r in revisions] == [1, 2]
    assert [r.based_on for r in revisions] == [0, 1]


def test_change_based_on_a_superseded_revision_is_refused(journal):
    ledger, _ = build_ledger(journal)
    ledger.publish("press.recipe", {"load": 45}, "v1", "甲")
    ledger.publish("press.recipe", {"load": 50}, "v2", "乙")
    with pytest.raises(RevisionConflictError):
        ledger.publish("press.recipe", {"load": 55}, "stale", "丙", based_on=1)


def test_change_requires_a_reason_and_an_operator(journal):
    ledger, _ = build_ledger(journal)
    with pytest.raises(ValueError):
        ledger.publish("press.recipe", {"load": 45}, "  ", "甲")
    with pytest.raises(ValueError):
        ledger.publish("press.recipe", {"load": 45}, "v1", "")


def test_revert_appends_a_new_revision_instead_of_erasing_history(journal):
    ledger, _ = build_ledger(journal)
    first = ledger.publish("press.recipe", {"load": 45}, "v1", "甲")
    ledger.publish("press.recipe", {"load": 50}, "v2", "乙")
    impact = ledger.impact("press.recipe", first.sequence)
    reverted = ledger.revert("press.recipe", first.sequence, "跑不出来退回", "甲", impact.digest)
    assert reverted.sequence == 3
    assert reverted.based_on == 2
    assert reverted.kind == "revert"
    assert reverted.reverted_sequence == 1
    assert reverted.values == {"load": 45}
    # all three generations are still on the books
    assert [r.sequence for r in ledger.revisions("press.recipe")] == [1, 2, 3]
    assert ledger.current("press.recipe").values == {"load": 45}


def test_revert_requires_the_impact_report_acknowledgement(journal):
    ledger, _ = build_ledger(journal)
    first = ledger.publish("press.recipe", {"load": 45}, "v1", "甲")
    ledger.publish("press.recipe", {"load": 50}, "v2", "乙")
    with pytest.raises(RevertAckMismatchError):
        ledger.revert("press.recipe", first.sequence, "reason", "甲", "")
    with pytest.raises(RevertAckMismatchError):
        ledger.revert("press.recipe", first.sequence, "reason", "甲", "deadbeef")


def test_impact_lists_generated_things_that_will_be_voided(journal):
    ledger, _ = build_ledger(journal)
    revision = ledger.publish("press.recipe", {"load": 45}, "v1", "甲")
    order = ledger.derivations.record(ledger, "press.recipe", revision, "work-order", "WO-7")
    impact = ledger.impact("press.recipe", revision.sequence)
    assert impact.derivations == ()  # reverting onto the current revision changes nothing

    second = ledger.publish("press.recipe", {"load": 50}, "v2", "乙")
    # publishing already voided the derivation produced under v1
    assert ledger.derivations.get("press.recipe", order.derivation_id).voided is True
    new_order = ledger.derivations.record(ledger, "press.recipe", second, "work-order", "WO-8")
    impact = ledger.impact("press.recipe", 1)
    voided = {item.derivation_id for item in impact.derivations}
    assert voided == {new_order.derivation_id}
    ledger.revert("press.recipe", 1, "退回", "甲", impact.digest)
    assert ledger.derivations.get("press.recipe", new_order.derivation_id).voided_reason.startswith(
        "voided by revert"
    )
    with pytest.raises(StaleArtifactError):
        ledger.derivations.require_active("press.recipe", new_order.derivation_id)


def test_recording_against_a_superseded_revision_is_refused(journal):
    ledger, _ = build_ledger(journal)
    revision = ledger.publish("press.recipe", {"load": 45}, "v1", "甲")
    ledger.publish("press.recipe", {"load": 50}, "v2", "乙")
    with pytest.raises(RevisionConflictError):
        ledger.derivations.record(ledger, "press.recipe", revision, "work-order", "late")


def test_impact_includes_outstanding_confirmations(journal):
    ledger, clock = build_ledger(journal)
    revision = ledger.publish("press.recipe", {"load": 45}, "v1", "甲")
    confirmations = ledger._confirmations
    issued = confirmations.issue("press.recipe", "press", 10)
    # impact only matters when a revert would actually move the generation
    ledger.publish("press.recipe", {"load": 50}, "v2", "乙")
    impact = ledger.impact("press.recipe", revision.sequence)
    subjects = [item.confirmation_id for item in impact.confirmations]
    assert issued.confirmation_id in subjects
    clock.advance(11)
    assert ledger.impact("press.recipe", revision.sequence).confirmations == ()


def test_unknown_and_unchanged_revert_targets(journal):
    ledger, _ = build_ledger(journal)
    ledger.publish("press.recipe", {"load": 45}, "v1", "甲")
    with pytest.raises(UnknownRevisionError):
        ledger.impact("press.recipe", 9)
    impact = ledger.impact("press.recipe", 1)
    with pytest.raises(RevisionConflictError):
        ledger.revert("press.recipe", 1, "noop", "甲", impact.digest)


def test_history_carries_digests_and_verifies(journal):
    ledger, _ = build_ledger(journal)
    ledger.publish("press.recipe", {"load": 45}, "v1", "甲")
    ledger.publish("press.recipe", {"load": 50}, "v2", "乙")
    report = ledger.verify()
    assert report["ok"] is True
    assert report["revisions"] == 2


def test_tampering_with_a_historical_row_is_detected(journal):
    ledger, _ = build_ledger(journal)
    ledger.publish("press.recipe", {"load": 45}, "v1", "甲")
    ledger.publish("press.recipe", {"load": 50}, "v2", "乙")
    # forge an extra row behind the watermark, as someone editing history would
    key = "ledger/revision/press.recipe"
    with journal.transaction("forge") as batch:
        journal.append(key, {**ledger.revision("press.recipe", 1).to_row(), "reason": "篡改"}, batch, generation=1)
    report = ledger.verify()
    assert report["ok"] is False
    assert any("digest" in error for error in report["errors"])
    with pytest.raises(LedgerTamperedError):
        ledger.verify_or_raise()


def test_revisions_survive_restart(tmp_path):
    from paperops.store import Journal

    dir_path = str(tmp_path / "stream")
    first_journal = Journal(dir_path)
    first, _ = build_ledger(first_journal)
    first.publish("press.recipe", {"load": 45}, "v1", "甲")
    first.publish("press.recipe", {"load": 50}, "v2", "乙")

    second, _ = build_ledger(Journal(dir_path))
    assert second.current("press.recipe").values == {"load": 50}
    assert second.verify()["ok"] is True
    # identifier source keeps ahead of replayed revisions
    next_revision = second.publish("press.recipe", {"load": 55}, "v3", "丙")
    assert next_revision.revision_id == "rev-000003"


def test_console_publish_revert_and_impact_flow(console):
    published = ok(console, "POST", "/api/ledger/publish", {
        "name": "press.recipe", "values": {"load": 45}, "reason": "v1", "operator": "甲",
    })
    assert published["sequence"] == 1
    ok(console, "POST", "/api/ledger/derive", {
        "name": "press.recipe", "kind": "work-order", "detail": "WO-7",
    })
    ok(console, "POST", "/api/ledger/publish", {
        "name": "press.recipe", "values": {"load": 50}, "reason": "v2", "operator": "乙",
    })
    ok(console, "POST", "/api/ledger/derive", {
        "name": "press.recipe", "kind": "work-order", "detail": "WO-8",
    })
    impact = ok(console, "GET", "/api/ledger/impact?name=press.recipe&sequence=1")
    assert impact["voided_count"] == 1
    assert impact["derivations"][0]["kind"] == "work-order"
    assert impact["derivations"][0]["detail"] == "WO-8"

    # reverting without the report digest is a precondition failure
    bad = refused(console, "POST", "/api/ledger/revert", {
        "name": "press.recipe", "sequence": 1, "reason": "退回", "operator": "甲", "ack": "",
    })
    assert bad.status == 412

    reverted = ok(console, "POST", "/api/ledger/revert", {
        "name": "press.recipe", "sequence": 1, "reason": "退回", "operator": "甲", "ack": impact["digest"],
    })
    assert reverted["kind"] == "revert"
    rows = ok(console, "GET", "/api/ledger/revisions?name=press.recipe")
    assert rows["count"] == 3
    assert ok(console, "GET", "/api/ledger/verify")["ok"] is True


def test_console_stale_base_is_conflict(console):
    ok(console, "POST", "/api/ledger/publish", {
        "name": "press.recipe", "values": {"load": 45}, "reason": "v1", "operator": "甲",
    })
    ok(console, "POST", "/api/ledger/publish", {
        "name": "press.recipe", "values": {"load": 50}, "reason": "v2", "operator": "乙",
    })
    response = refused(console, "POST", "/api/ledger/publish", {
        "name": "press.recipe", "values": {"load": 55}, "reason": "v3", "operator": "丙", "based_on": 1,
    })
    assert response.status == 409


def test_ledger_page_is_served(console):
    response = call(console, "GET", "/ledger.html")
    assert response.status == 200
    assert "代际账" in response.to_bytes().decode("utf-8")
