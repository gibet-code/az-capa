from clients.resource_graph_kql import in_filter, normalize_values


def test_normalize_values_strips_blanks_and_deduplicates_stably():
    assert normalize_values([" eastus ", "", "eastus", "westus"]) == ["eastus", "westus"]
    assert normalize_values(None) is None


def test_in_filter_escapes_kql_literals():
    assert in_filter("resourceGroup", ["team's-rg"]) == (
        "| where resourceGroup in~ ('team''s-rg')"
    )


def test_in_filter_omits_empty_values():
    assert in_filter("location", []) is None
