from __future__ import annotations

from unittest.mock import MagicMock

from storage.table import LazyTableClient, decode_json_array_properties, encode_json_array_properties


def test_lazy_table_client_creates_once_and_supports_invalidation():
    service = MagicMock()
    first_client = MagicMock()
    second_client = MagicMock()
    service.get_table_client.side_effect = [first_client, second_client]
    handle = LazyTableClient(lambda: "Example", lambda: service)

    assert handle() is first_client
    assert handle() is first_client
    service.create_table_if_not_exists.assert_called_once_with("Example")

    handle.invalidate()

    assert handle() is second_client
    assert service.create_table_if_not_exists.call_count == 2


def test_json_array_properties_keep_small_legacy_shape():
    properties = encode_json_array_properties(
        ["sub-a", "sub-b"],
        legacy_property="Subs",
        chunk_prefix="Subs",
        count_property="SubsChunkCount",
    )

    assert properties["SubsChunkCount"] == 0
    assert decode_json_array_properties(
        properties,
        legacy_property="Subs",
        chunk_prefix="Subs",
        count_property="SubsChunkCount",
    ) == ["sub-a", "sub-b"]


def test_json_array_properties_chunk_large_subscription_scope():
    subscriptions = [f"{index:08d}-0000-0000-0000-000000000000" for index in range(1900)]

    properties = encode_json_array_properties(
        subscriptions,
        legacy_property="Subs",
        chunk_prefix="Subs",
        count_property="SubsChunkCount",
    )

    assert properties["SubsChunkCount"] > 1
    assert "Subs" not in properties
    assert all(len(value) <= 30_000 for key, value in properties.items() if key.startswith("Subs0"))
    assert decode_json_array_properties(
        properties,
        legacy_property="Subs",
        chunk_prefix="Subs",
        count_property="SubsChunkCount",
    ) == subscriptions