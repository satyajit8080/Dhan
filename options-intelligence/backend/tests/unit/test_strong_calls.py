"""Strong calls are committee-verified paper buys only. Weaker meetings stay off the desk."""

from doi.ai.committee_view import strong_call
from doi.ai.repository import list_strong_calls
from tests.unit.test_meetings import PROPOSAL, ctx, engine, run


def _call(**over):
    summary = {
        "label": "STRONG BULLISH",
        "final_action": "RECOMMEND_PAPER",
        "cio_confidence": 4,
        "direction": "UP",
        "vetoes": {"risk_officer": False, "contrarian": False, "compliance_failed": False},
        "size": {"risk_gate_approved": True},
    }
    summary.update(over.pop("summary", {}))
    rec = {
        "id": "REC-1",
        "meeting_id": "MTG-1",
        "underlying_key": "NIFTY",
        "option_type": "CE",
        "status": "PENDING_HUMAN",
        "strike": 24000,
        "expiry": "2026-11-10",
    }
    rec.update(over.pop("rec", {}))
    detail = {"summary": summary, "recommendation": rec, "meeting": {"id": "MTG-1"}}
    detail.update(over)
    return strong_call(detail)


def test_strong_call_is_a_matching_paper_buy():
    call = _call()
    assert call["signal"] == "BUY CE" and call["side"] == "BUY" and call["option_type"] == "CE"
    assert call["underlying"] == "NIFTY" and call["confidence"] == 4 and call["status"] == "PENDING_HUMAN"
    assert call["meeting_id"] == "MTG-1"
    put = _call(summary={"label": "STRONG BEARISH", "direction": "DOWN", "cio_confidence": 5}, rec={"option_type": "PE"})
    assert put["signal"] == "BUY PE" and put["side"] == "BUY" and put["confidence"] == 5


def test_strong_call_drops_anything_the_committee_did_not_clear():
    assert _call(summary={"cio_confidence": 3, "label": "BULLISH"}) is None
    assert _call(summary={"final_action": "WATCH", "label": "STRONG BULLISH"}) is None
    assert _call(summary={"label": "WAIT", "final_action": "NO_ACTION", "direction": None}) is None
    assert _call(summary={"vetoes": {"risk_officer": True, "contrarian": False, "compliance_failed": False}}) is None
    assert _call(summary={"vetoes": {"risk_officer": False, "contrarian": True, "compliance_failed": False}}) is None
    assert _call(summary={"vetoes": {"risk_officer": False, "contrarian": False, "compliance_failed": True}}) is None
    assert _call(summary={"size": {"risk_gate_approved": False}}) is None
    assert _call(rec={"option_type": "PE"}) is None  # bullish committee, put contract
    assert _call(recommendation=None) is None


def test_recorded_meeting_is_listed_only_when_the_cio_is_strong():
    weak = engine()
    run(weak)
    assert list_strong_calls(weak.store) == []

    strong = engine({"cio": {"confidence": 5}})
    run(strong)
    calls = list_strong_calls(strong.store)
    assert len(calls) == 1 and calls[0]["signal"] == "BUY CE" and calls[0]["status"] == "PENDING_HUMAN"

    put = engine({"cio": {"confidence": 5, "prediction": {"target": "INDEX", "direction": "DOWN", "horizon_min": 30}}})
    run(put, ctx(proposal={**PROPOSAL, "option_type": "PE"}))
    got = list_strong_calls(put.store)
    assert len(got) == 1 and got[0]["signal"] == "BUY PE" and got[0]["underlying"] == "NIFTY"

    mismatch = engine({"cio": {"confidence": 5}})
    run(mismatch, ctx(proposal={**PROPOSAL, "option_type": "PE"}))
    assert list_strong_calls(mismatch.store) == []
