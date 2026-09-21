# Function Package Build and Deployment

## Status

Current operational runbook. Last verified on 2026-09-15.

Installing a published release does not require a local build. For a normal
installation, follow [Install Azure Capacity in Azure](installation.md), download
the ZIP from the private GitHub release in a browser, and use the deployment
steps below. The build sections on this page are for release maintainers only.

The local build runs the complete test suite in a Linux container, installs only
runtime dependencies into the Azure Functions package layout, and exports a
ready-to-run ZIP. Tests and development dependencies are not included in the
artifact.

## Prerequisites

- Docker Desktop configured to use Linux containers.
- Docker Buildx (included with current Docker Desktop releases).
- PowerShell 7 or Windows PowerShell 5.1.
- Network access from Docker to the configured container registry, Debian
  package repositories, and the configured Python package feed.

By default, the build uses public PyPI. The package index is selected in this
order:

1. The `-PackageIndexUrl` command parameter.
2. The existing `PIP_INDEX_URL` environment variable.
3. `https://pypi.org/simple`.

Python, Node.js, pytest, Azure CLI, and Azure Functions Core Tools do not need to
be installed on the laptop to build the package.

If an HTTPS-inspecting proxy is used, configure Docker Desktop to use the proxy
and trust its root certificate before building. Never pass package-feed tokens
as Docker build arguments; use BuildKit secrets when private-feed credentials
are introduced.

## Build

Run from any directory:

```powershell
.\build-function-package.ps1
```

Force a build without Docker layer cache or choose another output directory:

```powershell
.\build-function-package.ps1 -NoCache
.\build-function-package.ps1 -OutputDirectory C:\release\az-capacity
```

To use any other PEP 503-compatible package index:

```powershell
.\build-function-package.ps1 `
  -PackageIndexUrl https://approved-feed.example/pypi/simple
```

The build uses Python 3.12 on Linux AMD64 and includes Node.js so the frontend
model tests cannot be skipped merely because Node is absent. Packaging starts
only after `python -m pytest -q` succeeds. A failed test or validation command
stops the Docker build and exports no package.

The generated `artifacts/az-capacity-<commit>.zip` contains only:

```text
function_app.py
host.json
requirements.txt
clients/
core/
functions/
services/
storage/
web/
.python_packages/lib/site-packages/
```

The script validates the ZIP manifest, size, and checksum after export. The
container also compiles the staged Python files and performs Functions discovery
before creating the ZIP.

## Deploy

Use the ZIP produced by the build or manually downloaded from the private GitHub
release. Do not extract or modify it.

### Azure portal (Cloud Shell, public profile only)

1. In the Azure portal, select the **Cloud Shell** button and choose PowerShell.
2. In the Cloud Shell toolbar, select **Manage files** → **Upload**, then upload
  the local `az-capacity-<version-or-commit>.zip` file.
3. Run the command below, using the uploaded file's path:

```powershell
az functionapp deployment source config-zip `
  --resource-group <resource-group> `
  --name <function-app-name> `
  --src $HOME/az-capacity-<version-or-commit>.zip `
  --build-remote false
```

The Azure portal does not currently provide a documented Deployment Center
button for uploading a local ZIP to a Flex Consumption app. Cloud Shell keeps
the public-profile workflow in the portal while using the supported One Deploy
command. Wait for it to report success and do not browse to the app until Easy
Auth has also been configured.

### Azure CLI fallback

```powershell
az functionapp deployment source config-zip `
  --resource-group <resource-group> `
  --name <function-app-name> `
  --src C:\Downloads\az-capacity-<version-or-commit>.zip `
  --build-remote false
```

Wait for the command to succeed. Then restart the Function App from its
**Overview** page and continue with the verification in the installation guide.

For the `Private` connectivity profile, run the command from a workstation or
self-hosted agent with network access to the private endpoint and private DNS
resolution for both the app and SCM hostnames. Azure Cloud Shell does not run in
your VNet and cannot normally reach that private SCM endpoint.

The target Function subnet does not need access to PyPI or Oryx for this
artifact. Application runtime and Functions extension bundle network
requirements still apply.