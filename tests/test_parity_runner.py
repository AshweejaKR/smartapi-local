import json

import smartapi_parity as parity


def test_redaction_normalisation_and_structure():
    real = {"status": True, "data": {"jwtToken": "secret", "ltp": 101.2, "rows": [{"x": 1}]}}
    local = {"status": True, "data": {"jwtToken": "other", "ltp": 99.1, "rows": [{"x": 1}]}}
    assert parity.clean(real)["data"]["jwtToken"] == "<redacted>"
    assert parity.norm(parity.clean(real)) == parity.norm(parity.clean(local))
    assert parity.shape(real) == parity.shape(local)
    assert parity.shape(real, True) == parity.shape(local, True)


def test_list_shape_ignores_history_length():
    real = {"status": True, "data": [{"orderid": "1", "qty": 1}, {"orderid": "2", "qty": 2}]}
    local = {"status": True, "data": [{"orderid": "9", "qty": 1}]}
    assert parity.shape(real) == parity.shape(local)
    assert parity.shape(real, True) == parity.shape(local, True)


def test_differences_report_missing_keys_and_types():
    changes = parity.diff({"data": {"value": 1}}, {"data": {"value": "1", "extra": None}})
    assert any("data.value" in item for item in changes)
    assert any("data.extra" in item for item in changes)


def test_order_uses_carryforward_for_derivatives():
    mcx = {"exchange": "MCX", "tradingsymbol": "GOLDPETAL30SEP26FUT", "symboltoken": "1"}
    nse = {"exchange": "NSE", "tradingsymbol": "NIFTYBEES-EQ", "symboltoken": "2"}
    assert parity.order(mcx, "BUY", "MARKET", 1)["producttype"] == "CARRYFORWARD"
    assert parity.order(nse, "BUY", "MARKET", 1)["producttype"] == "DELIVERY"


def test_reports_write_json_and_markdown(tmp_path, monkeypatch):
    monkeypatch.setenv("PARITY_REPORT_DIR", str(tmp_path))
    monkeypatch.setattr(parity.time, "sleep", lambda _: None)
    runner = parity.Parity()
    runner.add("T1", "offline compatibility", {}, {},
               lambda: {"status": True, "data": []}, lambda: {"status": True, "data": []})
    runner.local_only("T2", "local only", {}, lambda: {"status": True, "data": []})
    runner.skip("T3", "skipped", "disabled")
    json_path, markdown_path = runner.reports()
    report = json.loads(json_path.read_text())
    assert report["counts"] == {"PASS": 1, "FAIL": 0, "SKIPPED": 1, "LOCAL_ONLY": 1}
    markdown = markdown_path.read_text()
    assert "T1" in markdown and "T2" in markdown and "T3" in markdown
