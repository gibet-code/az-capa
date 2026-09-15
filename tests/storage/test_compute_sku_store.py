from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from storage.compute_sku_store import ComputeSkuBlobStore, _decode


class ComputeSkuBlobStoreTests(unittest.TestCase):
    def _catalog(self):
        return [
            {"name": "Standard_D2s_v5", "location": "eastus", "capacityReservationSupported": True},
            {"name": "Standard_D2s_v5", "location": "westeurope", "capacityReservationSupported": False},
            {"name": "Standard_E2s_v5", "location": "eastus", "capacityReservationSupported": True},
        ]

    def test_publish_uploads_snapshot_before_manifest_and_seeds_cache(self):
        events = []
        snapshot_blob = MagicMock()
        snapshot_blob.upload_blob.side_effect = lambda *_args, **_kwargs: events.append("snapshot")
        manifest_blob = MagicMock()
        manifest_blob.upload_blob.side_effect = lambda *_args, **_kwargs: (
            events.append("manifest") or {"etag": '"etag"'}
        )
        manifest_blob.get_blob_properties.return_value = SimpleNamespace(etag='"etag"')
        container = MagicMock()
        container.get_blob_client.side_effect = lambda name: (
            manifest_blob if name == "current.json" else snapshot_blob
        )
        store = ComputeSkuBlobStore(lambda: "compute-skus")
        store._container_cache = container

        generation = store.publish_catalog(self._catalog(), "SUB-A", {"subs": ["sub-a"]})

        self.assertEqual(["snapshot", "manifest"], events)
        self.assertEqual(generation, store.get_state()["activeGeneration"])
        self.assertEqual(3, store.get_state()["skuLocationCount"])
        snapshot = _decode(snapshot_blob.upload_blob.call_args.args[0])
        manifest = json.loads(manifest_blob.upload_blob.call_args.args[0])
        self.assertEqual(snapshot["generation"], manifest["generation"])

    def test_find_and_find_pairs_use_memory_indexes(self):
        store = ComputeSkuBlobStore(lambda: "compute-skus")
        snapshot = {
            "schemaVersion": 1,
            "generation": "generation",
            "state": {"activeGeneration": "generation"},
            "catalog": self._catalog(),
        }
        store._set_cache(snapshot, '"etag"')
        store._snapshot = MagicMock(return_value=snapshot)

        self.assertEqual(2, len(store.find("STANDARD_D2S_V5")))
        self.assertEqual(2, len(store.find(location="EASTUS")))
        self.assertTrue(store.find("standard_d2s_v5", "eastus")[0]["capacityReservationSupported"])
        matches, errors = store.find_pairs({
            ("Standard_D2s_v5", "eastus"),
            ("missing", "eastus"),
        })
        self.assertEqual({}, errors)
        self.assertEqual({("standard_d2s_v5", "eastus")}, set(matches))

    def test_manifest_etag_reuses_downloaded_snapshot(self):
        store = ComputeSkuBlobStore(lambda: "compute-skus")
        manifest_blob = MagicMock()
        manifest_blob.get_blob_properties.return_value = SimpleNamespace(etag='"etag"')
        manifest_blob.download_blob.return_value.readall.return_value = json.dumps({
            "schemaVersion": 1,
            "generation": "generation",
            "snapshotBlob": "snapshots/generation.json.gz",
        }).encode()
        snapshot_blob = MagicMock()
        from storage.compute_sku_store import _encode
        snapshot_blob.download_blob.return_value.readall.return_value = _encode({
            "schemaVersion": 1,
            "generation": "generation",
            "state": {"activeGeneration": "generation"},
            "catalog": self._catalog(),
        })
        container = MagicMock()
        container.get_blob_client.side_effect = lambda name: (
            manifest_blob if name == "current.json" else snapshot_blob
        )
        store._container_cache = container

        first = store.get_state()
        second = store.get_state()

        self.assertEqual(first, second)
        self.assertEqual(1, manifest_blob.download_blob.call_count)
        self.assertEqual(1, snapshot_blob.download_blob.call_count)
        self.assertEqual(2, manifest_blob.get_blob_properties.call_count)

    def test_snapshot_failure_does_not_publish_manifest(self):
        store = ComputeSkuBlobStore(lambda: "compute-skus")
        snapshot_blob = MagicMock()
        snapshot_blob.upload_blob.side_effect = RuntimeError("snapshot failed")
        manifest_blob = MagicMock()
        container = MagicMock()
        container.get_blob_client.side_effect = lambda name: (
            manifest_blob if name == "current.json" else snapshot_blob
        )
        store._container_cache = container

        with self.assertRaisesRegex(RuntimeError, "snapshot failed"):
            store.publish_catalog(self._catalog(), "sub-a", {})

        manifest_blob.upload_blob.assert_not_called()


if __name__ == "__main__":
    unittest.main()
