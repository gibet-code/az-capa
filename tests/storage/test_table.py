from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from azure.core.exceptions import ServiceRequestError, ServiceResponseError

from storage.table import (
    LazyTableClient,
    decode_json_array_properties,
    encode_json_array_properties,
    submit_table_transaction,
)


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


def test_local_transaction_serializes_and_retries_transport_resets():
    table = MagicMock()
    table.submit_transaction.side_effect = [
        ServiceResponseError("reset"),
        ServiceRequestError("closed"),
        None,
    ]
    gate = MagicMock()
    operations = [("upsert", {"PartitionKey": "p", "RowKey": "r"})]

    with (
        patch("storage.table.credentials.is_running_locally", return_value=True),
        patch("storage.table._local_bulk_write_gate", gate),
        patch("storage.table.time.sleep") as sleep,
    ):
        submit_table_transaction(table, operations)

    gate.__enter__.assert_called_once_with()
    gate.__exit__.assert_called_once()
    assert table.submit_transaction.call_count == 3
    assert [entry.args[0] for entry in sleep.call_args_list] == [3, 10]


def test_azure_transaction_does_not_use_local_gate_or_outer_retry():
    table = MagicMock()
    table.submit_transaction.side_effect = ServiceResponseError("reset")
    gate = MagicMock()
    operations = [("upsert", {"PartitionKey": "p", "RowKey": "r"})]

    with (
        patch("storage.table.credentials.is_running_locally", return_value=False),
        patch("storage.table._local_bulk_write_gate", gate),
        patch("storage.table.time.sleep") as sleep,
    ):
        try:
            submit_table_transaction(table, operations)
        except ServiceResponseError:
            pass
        else:
            raise AssertionError("Azure transaction error was unexpectedly swallowed")

    gate.__enter__.assert_not_called()
    table.submit_transaction.assert_called_once_with(operations)
    sleep.assert_not_called()


def test_local_transaction_raises_after_retry_exhaustion():
    table = MagicMock()
    table.submit_transaction.side_effect = ServiceResponseError("reset")
    operations = [("delete", {"PartitionKey": "p", "RowKey": "r"})]

    with (
        patch("storage.table.credentials.is_running_locally", return_value=True),
        patch("storage.table.time.sleep") as sleep,
        pytest.raises(ServiceResponseError),
    ):
        submit_table_transaction(table, operations)

    assert table.submit_transaction.call_count == 3
    assert [entry.args[0] for entry in sleep.call_args_list] == [3, 10]