from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

from storage.reference_data_store import LOCATIONS, SUBSCRIPTIONS, ReferenceDataStore


class ReferenceDataStoreTests(unittest.TestCase):
    def _store(self):
        store = ReferenceDataStore(lambda: "reference-data", lambda: 60)
        container = MagicMock()
        store._container_cache = container
        return store, container

    def test_publish_writes_generation_before_manifest_and_seeds_cache(self):
        store, container = self._store()
        events = []
        snapshot_blob = MagicMock()
        snapshot_blob.upload_blob.side_effect = lambda *_args, **_kwargs: events.append("snapshot")
        manifest_blob = MagicMock()
        manifest_blob.upload_blob.side_effect = lambda *_args, **_kwargs: (
            events.append("manifest") or {"etag": '"etag"'}
        )
        container.get_blob_client.side_effect = lambda name: (
            manifest_blob if name == "locations/current.json" else snapshot_blob
        )

        generation = store.publish(
            LOCATIONS,
            {"westeurope": {"displayName": "West Europe"}},
            {"subscriptionId": "sub-a"},
        )

        self.assertEqual(["snapshot", "manifest"], events)
        self.assertEqual(generation, store.open_snapshot(LOCATIONS)["generation"])
        manifest = json.loads(manifest_blob.upload_blob.call_args.args[0])
        self.assertEqual(generation, manifest["generation"])

    def test_catalogue_caches_are_independent(self):
        store, _container = self._store()
        store._set_cache(SUBSCRIPTIONS, {"kind": SUBSCRIPTIONS}, '"sub-etag"')
        store._set_cache(LOCATIONS, {"kind": LOCATIONS}, '"loc-etag"')

        self.assertEqual(SUBSCRIPTIONS, store.open_snapshot(SUBSCRIPTIONS)["kind"])
        self.assertEqual(LOCATIONS, store.open_snapshot(LOCATIONS)["kind"])

    def test_rejects_unknown_catalogue_kind(self):
        store, _container = self._store()

        with self.assertRaisesRegex(ValueError, "Unsupported reference-data kind"):
            store.open_snapshot("unknown")


if __name__ == "__main__":
    unittest.main()