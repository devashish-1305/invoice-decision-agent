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


def test_not_evaluated_warnings_do_not_decide_the_route_when_a_real_cause_exists():
    consequence = Check(code="DUPLICATE_EXACT", result="WARN", action="hold", message="Not evaluated: no invoice number",
                        not_evaluated=True)
    real = chk("REQUIRED_FIELDS", "FAIL")
    assert engine.route([consequence, real]).team == "AP reviewer"                       # not "Vendor / tell them it duplicates"
    assert engine.route([consequence]).team == "Vendor"                                  # but if it is all there is, still routed


def test_a_printed_po_that_does_not_exist_goes_to_procurement_to_confirm_the_po():
    consequences = [Check(code=c, result="WARN", action="hold", message="Not evaluated: no PO", not_evaluated=True)
                    for c in ("LINE_MATCH", "AMOUNT_TOLERANCE", "CUMULATIVE_PO")]
    route = engine.route([chk("PO_FOUND", "FAIL"), *consequences])
    assert route.team == "Procurement (PO owner)" and "Confirm the correct PO number" in route.action


def test_check_helper_marks_unevaluable_checks():
    from conftest import make_ctx
    from app.rules import checks as c
    assert c.check_bank_match(make_ctx("H", vendor=None)).not_evaluated is True
    assert c.check_bank_match(make_ctx("H")).not_evaluated is False
