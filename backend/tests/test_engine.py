"""The decision function (PRD 5.2) and routing (5.3). Pure: checks in, decision out."""
from app.rules import engine
from app.schemas import Check


def chk(code, result, action=None, risk="low"):
    return Check(code=code, result=result, message=f"{code} {result}", action=action or ("hold" if result != "PASS" else None), risk=risk)


def test_all_pass_is_approve():
    d = engine.decide([chk("A", "PASS"), chk("B", "PASS")])
    assert (d.decision, d.risk_level, d.triggered_codes, d.top_reason) == ("Approve", "none", [], None)


def test_any_warn_is_hold():
    assert engine.decide([chk("A", "PASS"), chk("B", "WARN")]).decision == "Hold"


def test_any_fail_is_hold():
    assert engine.decide([chk("A", "FAIL")]).decision == "Hold"


def test_reject_beats_hold():
    d = engine.decide([chk("A", "FAIL"), chk("B", "FAIL", action="reject")])
    assert d.decision == "Reject" and d.top_reason == "B FAIL"


def test_a_warn_can_never_become_a_reject():
    assert engine.decide([chk("A", "WARN", action="reject")]).decision == "Hold"  # only FAIL + reject rejects


def test_risk_is_the_highest_among_triggered_checks():
    d = engine.decide([chk("A", "FAIL", risk="medium"), chk("B", "WARN", risk="high"), chk("C", "PASS", risk="high")])
    assert d.risk_level == "high"


def test_passing_checks_do_not_raise_risk():
    assert engine.decide([chk("A", "PASS", risk="high")]).risk_level == "none"


def test_failed_stage_means_only_stage_failed_is_evaluated_and_it_holds():
    from conftest import make_ctx
    from app.schemas import StageFailure
    ctx = make_ctx("H", stage_failures=[StageFailure(stage="match", error="boom")])
    found = engine.run_rule_checks(ctx)
    assert [k.code for k in found] == ["STAGE_FAILED"]
    assert engine.decide(found).decision == "Hold"


def test_routing_table():
    assert engine.route([chk("BANK_MATCH", "FAIL")]).team == "Vendor master / treasury team"
    assert engine.route([chk("CUMULATIVE_PO", "FAIL")]).team == "Procurement (PO owner)"
    assert engine.route([chk("PO_INFERRED", "WARN")]).team == "AP reviewer"
    assert engine.route([chk("DUPLICATE_NEAR", "FAIL")]).team == "AP reviewer"
    assert engine.route([chk("DUPLICATE_EXACT", "FAIL", action="reject")]).team == "Vendor"
    assert engine.route([chk("A", "PASS")]) is None


def test_bank_mismatch_outranks_other_routes():
    assert engine.route([chk("PO_INFERRED", "WARN"), chk("BANK_MATCH", "FAIL")]).team == "Vendor master / treasury team"
