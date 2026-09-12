import json

import smartapi_parity as parity


def test_redaction_normalisation_and_structure():
    real = {"status": True, "data": {"jwtToken": "secret", "ltp": 101.2, "rows": [{"x": 1}]}}
    local = {"status": True, "data": {"jwtToken": "other", "ltp": 99.1, "rows": [{"x": 1}]}}
    assert parity.clean(real)["data"]["jwtToken"] == "<redacted>"
    assert parity.norm(parity.clean(real)) == parity.norm(parity.clean(local))
    assert parity.shape(real) == parity.shape(local)
    assert parity.shape(real, True) == parity.shape(local, True)


def test_differences_report_missing_keys_and_types():
    changes = parity.diff({"data": {"value": 1}}, {"data": {"value": "1", "extra": None}})
    assert any("data.value" in item for item in changes)
    assert any("data.extra" in item for item in changes)


def test_reports_write_json_and_markdown(tmp_path, monkeypatch):
    monkeypatch.setenv("PARITY_REPORT_DIR", str(tmp_path))
    runner = parity.Parity()
    runner.add("T1", "offline compatibility", {}, {},
               lambda: {"status": True, "data": []}, lambda: {"status": True, "data": []})
    json_path, markdown_path = runner.reports()
    report = json.loads(json_path.read_text())
    assert report["counts"] == {"PASS": 1, "FAIL": 0, "SKIPPED": 0}
    assert "T1" in markdown_path.read_text()