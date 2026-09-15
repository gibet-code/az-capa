from clients._http import azure_error_detail, retry_after_seconds


def test_retry_after_seconds_prefers_standard_seconds():
    assert retry_after_seconds({"Retry-After": "2.5", "x-ms-retry-after-ms": "9000"}) == 2


def test_retry_after_seconds_supports_milliseconds_and_fallback():
    assert retry_after_seconds({"x-ms-retry-after-ms": "1501"}) == 2
    assert retry_after_seconds({"Retry-After": "invalid"}, fallback=7) == 7


def test_azure_error_detail_extracts_nested_error():
    raw = b'{"error":{"code":"Denied","message":"No access"}}'

    assert azure_error_detail(raw) == "Denied: No access"


def test_azure_error_detail_handles_non_json_body():
    assert azure_error_detail(b"gateway failure") == "Non-JSON HTTP error response"
