import time


def test_sdk_rest_fault_recovers(sdk_server):
    s = sdk_server
    refresh = s.sdk.refresh_token
    assert s.http.post("/admin/faults", data={"mode": "unavailable", "duration": ".35"}).status_code == 303
    assert s.http.get("/admin").status_code == 200
    assert s.http.get("/health").status_code == 200
    assert s.sdk.getProfile(refresh)["status"] is False
    time.sleep(.4)
    assert s.sdk.getProfile(refresh)["status"] is True
