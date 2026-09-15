from __future__ import annotations

import function_app
from azure.functions.decorators.http import HttpTrigger

# get_functions() mutates app.functions_bindings and rejects a second call, so
# resolve the function list exactly once and share it across tests.
_FUNCTIONS = function_app.app.get_functions()


def test_all_blueprint_families_are_discoverable():
    names = [function.get_function_name() for function in _FUNCTIONS]

    assert len(names) == len(set(names))
    assert {
        "data_collection_dispatch",
        "data_collection_list",
        "data_collection_status",
        "data_collection_run_history",
        "data_collection_action",
        "data_collection_config",
        "record_run_outcome",
        "context_fields",
        "odcr_usage_list",
        "manual_template",
        "preview_values",
        "resolve_values",
        "subscription_inventory",
        "tag_source_options",
        "update_odcr_coverage_decisions",
        "vm_list",
        "zzz_context_field",
        "zzz_spa_fallback",
    }.issubset(names)


def test_http_routes_are_api_prefixed():
    # host.json sets routePrefix="", so every HTTP route must carry api/ itself.
    offenders = []
    for function in _FUNCTIONS:
        trigger = function.get_trigger()
        if not isinstance(trigger, HttpTrigger):
            continue
        route = trigger.route or ""
        if route == "{*path}":  # SPA catch-all fallback serves the shell at "/"
            continue
        if not route.startswith("api/"):
            offenders.append((function.get_function_name(), route))

    assert offenders == [], f"HTTP routes must be api/-prefixed: {offenders}"