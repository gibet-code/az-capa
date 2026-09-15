from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from storage.zone_mapping_store import ZoneMappingBlobStore, _decode, _encode


class ZoneMappingBlobStoreTests(unittest.TestCase):
    def test_gzip_round_trip_is_deterministic(self):
        payload = {"schemaVersion": 1, "subscriptions": {"sub": {"eastus": {"1": "az1"}}}}

        first = _encode(payload)
        second = _encode(payload)

        self.assertEqual(first, second)
        self.assertEqual(payload, _decode(first))
        self.assertEqual(payload, _decode(_decode_json_bytes(first)))

    def test_get_mappings_distinguishes_regional_from_missing(self):
        store = ZoneMappingBlobStore(lambda: "zone-mappings")
        store._snapshot = MagicMock(return_value={
            "subscriptions": {
                "sub-a": {
                    "eastus": {"1": "eastus-az2"},
                    "westus": {},
                },
            },
        })

        result = store.get_mappings({
            ("SUB-A", "EastUS"),
            ("sub-a", "westus"),
            ("sub-b", "eastus"),
        })

        self.assertEqual({"1": "eastus-az2"}, result[("sub-a", "eastus")])
        self.assertEqual({}, result[("sub-a", "westus")])
        self.assertNotIn(("sub-b", "eastus"), result)

    def test_snapshot_cache_reuses_same_etag(self):
        store = ZoneMappingBlobStore(lambda: "zone-mappings")
        blob = MagicMock()
        blob.get_blob_properties.return_value = SimpleNamespace(etag='"etag-1"')
        blob.download_blob.return_value.readall.return_value = _encode({
            "schemaVersion": 1,
            "state": {"subs": ["sub-a"]},
            "subscriptions": {},
        })
        container = MagicMock()
        container.get_blob_client.return_value = blob
        store._container_cache = container

        first = store.get_state()
        second = store.get_state()

        self.assertEqual({"subs": ["sub-a"]}, first)
        self.assertEqual(first, second)
        self.assertEqual(1, blob.download_blob.call_count)
        self.assertEqual(2, blob.get_blob_properties.call_count)

    def test_publish_snapshot_combines_subscription_fragments(self):
        store = ZoneMappingBlobStore(lambda: "zone-mappings")
        container = MagicMock()
        container.list_blobs.return_value = [
            SimpleNamespace(name="subscriptions/sub-a.json.gz"),
            SimpleNamespace(name="subscriptions/sub-b.json.gz"),
        ]
        current_blob = MagicMock()
        current_blob.upload_blob.return_value = {"etag": '"current"'}
        container.get_blob_client.return_value = current_blob
        store._container_cache = container
        store._read_fragment = MagicMock(side_effect=[
            ("sub-a", {"eastus": {"1": "az1"}}),
            ("sub-b", {"westus": {}}),
        ])

        result = store.publish_snapshot({"subs": ["sub-a", "sub-b"]})

        self.assertEqual(2, result["subscriptions"])
        self.assertEqual(2, result["regions"])
        uploaded = _decode(current_blob.upload_blob.call_args.args[0])
        self.assertEqual({"sub-a", "sub-b"}, set(uploaded["subscriptions"]))
        self.assertEqual(["sub-a", "sub-b"], uploaded["state"]["subs"])


def _decode_json_bytes(compressed: bytes) -> bytes:
    import gzip

    return gzip.decompress(compressed)


if __name__ == "__main__":
    unittest.main()
