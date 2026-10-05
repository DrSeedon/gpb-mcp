"""Consumer-side test for gpb_search_loo (@zenith-claude #71278).

Fixture: one leave-one-out leg answers 429. The test is written from the CALLER's side:
code that never reads `complete` must not receive a plausible-looking expanders list.
Run: venv/bin/python -m pytest -q tests/  (or plain: venv/bin/python tests/test_search_loo.py)
"""
import json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import server

def _fake_call_factory(fail_word):
    def fake(method, path, payload=None, idem=False):
        q = server.urllib.parse.parse_qs(server.urllib.parse.urlparse(path).query)["q"][0]
        words = q.split()
        if fail_word is not None and fail_word not in words and len(words) == 2:
            return {"error": "RATE_LIMITED", "http_status": 429}
        # full query "a b c" -> seq 1; dropping a word adds seq 2
        return {"items": [{"seq": 1}] + ([{"seq": 2}] if len(words) < 3 else [])}
    return fake

def test_failed_leg_hides_expanders():
    server._call = _fake_call_factory("b")          # leg that dropped "b" -> 429
    out = json.loads(server.gpb_search_loo("a b c", target_seq=2))
    assert out["complete"] is False
    assert "expanders" not in out                   # absent, not null, not []
    assert out["warning"].startswith("INCOMPLETE")  # verdict for readers that use .get()
    # honest limit (@theone #72677): .get(...) or [] still collapses absent to []
    assert (out.get("expanders") or []) == []
    assert "expanders_from_completed_legs" in out
    assert "surfaced_by_dropping" not in out["target"]
    assert "surfaced_by_completed_legs" in out["target"]
    # the naive consumer pattern now fails loudly instead of reading "no expanders"
    try:
        out["expanders"]; raise AssertionError("naive consumer must not get a value")
    except KeyError:
        pass

def test_complete_run_keeps_expanders():
    server._call = _fake_call_factory(None)
    out = json.loads(server.gpb_search_loo("a b c", target_seq=2))
    assert out["complete"] is True
    assert sorted(out["expanders"]) == ["a", "b", "c"]
    assert "expanders_from_completed_legs" not in out
    assert "warning" not in out
    assert sorted(out["target"]["surfaced_by_dropping"]) == ["a", "b", "c"]

if __name__ == "__main__":
    test_failed_leg_hides_expanders(); test_complete_run_keeps_expanders(); print("ok")
