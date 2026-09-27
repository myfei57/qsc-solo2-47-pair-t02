"""Generation ledger: lineage, rollback impact accounting and immutability."""

import json
import os

import pytest

from paperops.clock import LogicalClock
from paperops.errors import (
    DuplicateBatchError,
    LedgerIntegrityError,
    RevisionNotAncestorError,
    RollbackPlanMismatchError,
    UnknownRevisionError,
)
from paperops.event import EventBus
from paperops.lineage import LineageLog, LineageService

from support import call, ok, refused

NAME = "dry.curve"


def make_service(tmp_path):
    log = LineageLog(str(tmp_path / "lineage"))
    return LineageService(log, LogicalClock(), EventBus())


def test_first_revision_has_no_parent_and_becomes_current(tmp_path):
    lineage = make_service(tmp_path)
    first = lineage.revise(NAME, {"temp": 110, "speed": 900}, "投产首版")
    assert first.parent_id is None
    assert first.revision_id == "rev-000001"
    assert lineage.current_revision(NAME).revision_id == "rev-000001"
    second = lineage.revise(NAME, {"temp": 120, "speed": 900}, "温度上调 10 度")
    assert second.parent_id == first.revision_id
    assert lineage.history(NAME)[-1].reason == "温度上调 10 度"


def test_change_requires_a_reason(tmp_path):
    lineage = make_service(tmp_path)
    with pytest.raises(ValueError):
        lineage.revise(NAME, {"temp": 110}, "   ")


def test_branch_from_an_old_revision_switches_the_chain_and_parks_the_old_tip(tmp_path):
    lineage = make_service(tmp_path)
    first = lineage.revise(NAME, {"v": 1}, "v1")
    second = lineage.revise(NAME, {"v": 2}, "v2")
    third = lineage.revise(NAME, {"v": 3}, "v3")
    branch = lineage.revise(NAME, {"v": 20}, "从 v2 分叉试验", parent_id=second.revision_id)
    assert branch.parent_id == second.revision_id
    # The freshly published branch is in force; the former tip is kept as a side branch.
    assert lineage.current_revision(NAME).revision_id == branch.revision_id
    account = lineage.lineage(NAME)
    assert [rev["revision_id"] for rev in account["chain"]] == [
        first.revision_id,
        second.revision_id,
        branch.revision_id,
    ]
    assert [rev["revision_id"] for rev in account["branches"]] == [third.revision_id]
    assert [child.revision_id for child in lineage.children_of(second.revision_id)] == [
        third.revision_id,
        branch.revision_id,
    ]


def test_revision_id_and_parameters_are_immutable_afterwards(tmp_path):
    lineage = make_service(tmp_path)
    first = lineage.revise(NAME, {"v": 1}, "v1")
    with pytest.raises(Exception):
        first.parameters["v"] = 2
    # Rolling back adds a revision; the historical row still carries the old value.
    lineage.revise(NAME, {"v": 2}, "v2")
    plan = lineage.rollback_plan(NAME, first.revision_id, "退回首版")
    lineage.commit_rollback(NAME, first.revision_id, "退回首版", plan["digest"])
    assert lineage.require_revision("rev-000002").parameters == {"v": 2}
    assert lineage.current_revision(NAME).parameters == {"v": 1}
    assert lineage.current_revision(NAME).rollback_of == first.revision_id


def test_rollback_plan_lists_every_live_output_that_will_go_void(tmp_path):
    lineage = make_service(tmp_path)
    first = lineage.revise(NAME, {"v": 1}, "v1")
    second = lineage.revise(NAME, {"v": 2}, "v2")
    third = lineage.revise(NAME, {"v": 3}, "v3")
    out_old = lineage.register_output("OUT-1", first.revision_id, "curve", {"file": "a.csv"})
    out_two = lineage.register_output("OUT-2", second.revision_id, "curve")
    out_three = lineage.register_output("OUT-3", third.revision_id, "work_order")

    plan = lineage.rollback_plan(NAME, first.revision_id, "首版参数更稳")
    assert plan["plan"]["superseded_revisions"] == [second.revision_id, third.revision_id]
    assert plan["plan"]["voided_outputs"] == ["OUT-2", "OUT-3"]
    # Planning changes nothing yet.
    assert lineage.get_output("OUT-2").status == "live"
    assert out_old.status == "live"


def test_rollback_commit_requires_the_seen_digest(tmp_path):
    lineage = make_service(tmp_path)
    first = lineage.revise(NAME, {"v": 1}, "v1")
    lineage.revise(NAME, {"v": 2}, "v2")
    with pytest.raises(RollbackPlanMismatchError):
        lineage.commit_rollback(NAME, first.revision_id, "退回", "not-the-digest")


def test_commit_rollback_voids_outputs_and_keeps_older_ones_live(tmp_path):
    lineage = make_service(tmp_path)
    first = lineage.revise(NAME, {"v": 1}, "v1")
    second = lineage.revise(NAME, {"v": 2}, "v2")
    out_old = lineage.register_output("OUT-1", first.revision_id, "curve")
    out_two = lineage.register_output("OUT-2", second.revision_id, "work_order")

    plan = lineage.rollback_plan(NAME, first.revision_id, "首版更稳")
    result = lineage.commit_rollback(NAME, first.revision_id, "首版更稳", plan["digest"])

    assert result["restored_revision_id"] == first.revision_id
    assert [row["output_id"] for row in result["voided_outputs"]] == ["OUT-2"]
    restored = lineage.current_revision(NAME)
    assert restored.parameters == {"v": 1}
    assert restored.parent_id == second.revision_id
    assert restored.change_kind == "rollback"
    assert lineage.get_output("OUT-2").status == "voided"
    assert lineage.get_output("OUT-2").voided_by == restored.revision_id
    assert lineage.get_output("OUT-1").status == "live"
    assert out_old  # original object reference is untouched history

    # The rollback itself becomes another node on the chain; history still shows all.
    ids = [revision.revision_id for revision in lineage.history(NAME)]
    assert ids == ["rev-000001", "rev-000002", restored.revision_id]


def test_plan_digest_changes_when_the_situation_changes(tmp_path):
    lineage = make_service(tmp_path)
    first = lineage.revise(NAME, {"v": 1}, "v1")
    second = lineage.revise(NAME, {"v": 2}, "v2")
    plan = lineage.rollback_plan(NAME, first.revision_id, "退回")
    stale_digest = plan["digest"]
    lineage.revise(NAME, {"v": 3}, "v3")
    fresh = lineage.rollback_plan(NAME, first.revision_id, "退回")
    assert fresh["digest"] != stale_digest
    with pytest.raises(RollbackPlanMismatchError):
        lineage.commit_rollback(NAME, first.revision_id, "退回", stale_digest)


def test_rollback_target_must_be_on_the_ancestry_chain(tmp_path):
    lineage = make_service(tmp_path)
    first = lineage.revise(NAME, {"v": 1}, "v1")
    second = lineage.revise(NAME, {"v": 2}, "v2")
    third = lineage.revise(NAME, {"v": 3}, "v3")
    # Publishing from v2 parks v3 as a side branch and puts rev4 in force.
    branch = lineage.revise(NAME, {"v": 20}, "分叉", parent_id=second.revision_id)
    with pytest.raises(RevisionNotAncestorError):
        lineage.rollback_plan(NAME, third.revision_id, "想去旁支版本")
    with pytest.raises(RevisionNotAncestorError):
        lineage.rollback_plan(NAME, branch.revision_id, "已是当前版")
    with pytest.raises(UnknownRevisionError):
        lineage.rollback_plan(NAME, "rev-999999", "不存在")


def test_output_is_pinned_to_its_revision_and_ids_are_unique(tmp_path):
    lineage = make_service(tmp_path)
    first = lineage.revise(NAME, {"v": 1}, "v1")
    lineage.register_output("OUT-1", first.revision_id, "curve")
    with pytest.raises(DuplicateBatchError):
        lineage.register_output("OUT-1", first.revision_id, "curve")
    with pytest.raises(UnknownRevisionError):
        lineage.register_output("OUT-2", "rev-999999", "curve")


def test_state_is_replayed_from_disk_across_restarts(tmp_path):
    directory = str(tmp_path / "lineage")
    bus = EventBus()
    first_run = LineageService(LineageLog(directory), LogicalClock(), bus)
    first = first_run.revise(NAME, {"v": 1}, "v1")
    second = first_run.revise(NAME, {"v": 2}, "v2")
    first_run.register_output("OUT-1", second.revision_id, "curve")
    plan = first_run.rollback_plan(NAME, first.revision_id, "退回")
    first_run.commit_rollback(NAME, first.revision_id, "退回", plan["digest"])

    replayed = LineageService(LineageLog(directory), LogicalClock())
    assert replayed.current_revision(NAME).parameters == {"v": 1}
    assert [rev.revision_id for rev in replayed.history(NAME)] == [
        "rev-000001",
        "rev-000002",
        "rev-000003",
    ]
    assert replayed.get_output("OUT-1").status == "voided"
    # The id counter keeps ahead of the replayed rows.
    assert replayed.revise(NAME, {"v": 9}, "继续") .revision_id == "rev-000004"


def test_tampering_with_a_history_line_is_detected_on_load(tmp_path):
    directory = str(tmp_path / "lineage")
    lineage = LineageService(LineageLog(directory), LogicalClock())
    lineage.revise(NAME, {"v": 1}, "v1")
    lineage.revise(NAME, {"v": 2}, "v2")
    log_path = os.path.join(directory, LineageLog.LOG_NAME)
    with open(log_path, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    row = json.loads(lines[0])
    row["payload"]["parameters"] = {"v": 999}
    lines[0] = json.dumps(row, sort_keys=True, ensure_ascii=True)
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    with pytest.raises(LedgerIntegrityError):
        LineageLog(directory)


def test_dropping_a_history_line_is_detected_on_load(tmp_path):
    directory = str(tmp_path / "lineage")
    lineage = LineageService(LineageLog(directory), LogicalClock())
    lineage.revise(NAME, {"v": 1}, "v1")
    lineage.revise(NAME, {"v": 2}, "v2")
    log_path = os.path.join(directory, LineageLog.LOG_NAME)
    with open(log_path, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write(lines[1] + "\n")
    with pytest.raises(LedgerIntegrityError):
        LineageLog(directory)


# --------------------------------------------------------------- through the console


def test_full_rollback_flow_through_the_console(console):
    first = ok(console, "POST", "/api/lineage/revise", {
        "name": NAME, "parameters": {"temp": 110}, "reason": "首版",
    })
    ok(console, "POST", "/api/lineage/revise", {
        "name": NAME, "parameters": {"temp": 120}, "reason": "升温",
    })
    ok(console, "POST", "/api/lineage/output", {
        "output_id": "OUT-1", "revision_id": first["revision_id"], "kind": "curve",
    })
    ok(console, "POST", "/api/lineage/output", {
        "output_id": "OUT-2", "revision_id": "rev-000002", "kind": "work_order",
    })
    plan = ok(console, "POST", "/api/lineage/rollback/plan", {
        "name": NAME, "target_revision_id": "rev-000001", "reason": "旧版更稳",
    })
    assert plan["plan"]["voided_outputs"] == ["OUT-2"]
    committed = ok(console, "POST", "/api/lineage/rollback/commit", {
        "name": NAME,
        "target_revision_id": "rev-000001",
        "reason": "旧版更稳",
        "digest": plan["digest"],
    })
    assert committed["revision"]["parameters"] == {"temp": 110}
    outputs = ok(console, "GET", "/api/lineage/outputs?name=" + NAME)["outputs"]
    status = {row["output_id"]: row["status"] for row in outputs}
    assert status == {"OUT-1": "live", "OUT-2": "voided"}
    verify = ok(console, "GET", "/api/lineage/verify")
    assert verify["events"] >= 5


def test_console_refuses_stale_digest_and_unknown_revision(console):
    ok(console, "POST", "/api/lineage/revise", {"name": NAME, "parameters": {"v": 1}, "reason": "v1"})
    mismatch = refused(console, "POST", "/api/lineage/rollback/commit", {
        "name": NAME, "target_revision_id": "rev-000001",
        "reason": "x", "digest": "deadbeef",
    })
    assert mismatch.status == 409
    missing = refused(console, "POST", "/api/lineage/rollback/plan", {
        "name": NAME, "target_revision_id": "rev-000404", "reason": "x",
    })
    assert missing.status == 404


def test_lineage_page_is_served(console):
    page = call(console, "GET", "/lineage.html")
    assert page.status == 200
    assert "代际账" in page.to_bytes().decode("utf-8")
