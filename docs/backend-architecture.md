# Backend architecture

The Python backend uses four application layers plus the Azure Functions adapter.

```text
function_app.py
functions/     Azure Functions routes, triggers, and Durable control flow
core/          Configuration, credentials, identity, and application scope
clients/       External Azure API adapters
services/      Application behavior and report construction
storage/       Azure Table and Blob persistence
tests/         Tests grouped by owning layer
```

## Dependency direction

```text
functions -> services -> clients
                      -> storage
                      -> core

clients  -> core
storage  -> core
```

`functions` must not import Azure Storage implementations directly. `storage`
must not import `services`. Durable orchestrators must remain deterministic:
network calls, credentials, current-time reads, and persistence belong in
activities or services called by activities.

## Layers

### Functions

Blueprints parse HTTP inputs, construct HTTP responses, register triggers, and
control Durable retries, timers, fan-out, and progress. Shared Durable mechanics
live in `functions/_durable.py`. Internal adapter helpers also live here with an
underscore prefix: `functions/_authorization.py` owns Easy Auth principal and
role enforcement, while `functions/_app_scope_http.py` maps App Scope failures
to HTTP responses. They are not blueprints and do not register functions.

### Core

`core/settings.py` reads typed application settings. `core/credentials.py` and
`core/identity.py` select backend or user identity. `core/app_scope.py` resolves
the configured application boundary. Core modules do not construct Azure
Functions requests or responses.

### Clients

Clients communicate with external Azure APIs and normalize source-specific
responses. They do not persist data. Current adapters cover Resource Graph,
Cost Management, Activity Log, Azure locations, and Compute SKUs.

### Services

Services own refresh planning, scope reconciliation, feature rules, report
projection, and coordination between clients and stores. They do not import
Azure Functions request or response types.

### Storage

Stores own entity keys, serialization, batching, checkpoints, snapshots, and
storage lifecycle. `storage/table.py` owns the Table service client and common
Table limits/encoding. `storage/blob.py` owns the Blob service client and
deterministic snapshot encoding.

Usage and Activity Log checkpoints support both the legacy `Subs` property and
numbered chunks for subscription scopes that exceed Azure Table string-property
limits.

## Tests

Tests mirror the production owner:

```text
tests/core/         configuration, identity, and scope policies
tests/clients/      Azure request, paging, and response normalization
tests/services/     planning, feature rules, and report construction
tests/storage/      keys, serialization, batching, and snapshots
tests/functions/    route and trigger adapter behavior
tests/integration/  function discovery and cross-layer behavior
```

When behavior crosses layers, test the rule in its owning layer and use mocks at
the next boundary. `tests/integration/test_function_discovery.py` protects
blueprint registration during module moves and renames.