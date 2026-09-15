# ODCR coverage decisions

## Status

Current API and persistence contract.

ODCR coverage decisions are shared manual planning inputs attached to VM resource IDs. They are independent from the automated ODCR eligibility and reservation lifecycle statuses.

## Values

- `required`: ODCR coverage is needed and `priority` must be `low`, `medium`, or `high`.
- `not_required`: ODCR coverage is not needed and `priority` must be null or omitted.
- `null`: clear the current decision. `priority` and `note` must also be null or omitted.

Notes are optional, trimmed, and limited to 500 characters. A VM has no default decision before explicit user input.

## API

`PUT /api/odcr/coverage/decisions` applies one set of values to every resource ID in `resourceIds`. The array accepts 1 to 500 IDs and removes case-insensitive duplicates. The current UI sends one VM at a time; the contract already supports a future mass-edit UI.

Before writing, the API requires every resource ID to:

- be a syntactically valid standalone VM ARM resource ID;
- belong to the configured application subscription scope; and
- be returned by Azure Resource Graph using the signed-in user's ARM token.

If any VM fails validation or visibility checks, no write is attempted. Table writes use last-write-wins semantics. A transaction is atomic within one subscription partition and up to 100 entities; a request spanning subscriptions can partially succeed if a later Table Storage transaction fails and may be retried safely.

## Storage

The table name is configured by `ODCR_COVERAGE_DECISIONS_TABLE_NAME` and defaults to `OdcrCoverageDecisions`.

- `PartitionKey`: normalized subscription ID.
- `RowKey`: SHA-256 of the normalized full VM resource ID.
- Current values: resource ID, decision, priority, and note.
- Latest audit: editor tenant ID, object ID, display name, and UTC update time.

Clearing writes a `cleared` tombstone. The VM is returned as unmarked, while the identity and time of the clearing action remain available. This is a latest-editor audit record, not append-only change history.

`GET /api/odcr/coverage` joins this shared state into each row as `odcrCoverage`. A VM with no stored input has `odcrCoverage: null`; a cleared VM has `odcrCoverage.decision: null` plus its latest audit metadata.