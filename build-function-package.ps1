[CmdletBinding()]
param(
    [string] $OutputDirectory = (Join-Path $PSScriptRoot 'artifacts'),
    [ValidatePattern('^https://')]
    [string] $PackageIndexUrl,
    [switch] $NoCache
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.IO.Compression.FileSystem

if (-not $PackageIndexUrl) {
    $PackageIndexUrl = if ($env:PIP_INDEX_URL) {
        $env:PIP_INDEX_URL
    }
    else {
        'https://pypi.org/simple'
    }
}
if ($PackageIndexUrl -notmatch '^https://') {
    throw 'The Python package index URL must use HTTPS.'
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory)]
        [string] $FilePath,

        [Parameter(Mandatory)]
        [string[]] $ArgumentList
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($ArgumentList -join ' ')"
    }
}

$dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
if ($dockerCommand) {
    $docker = $dockerCommand.Source
}
else {
    $docker = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe'
}
if (-not (Test-Path -LiteralPath $docker -PathType Leaf)) {
    throw 'Docker was not found. Install Docker Desktop, enable Linux containers, and restart this shell.'
}

$dockerOs = (& $docker version --format '{{.Server.Os}}' 2>$null).Trim()
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Desktop is installed, but its engine is unavailable.'
}
if ($dockerOs -ne 'linux') {
    throw "Docker must use Linux containers; the current engine reports '$dockerOs'."
}

Invoke-NativeCommand $docker @('buildx', 'version')

$resolvedOutput = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDirectory)
$temporaryOutput = Join-Path ([System.IO.Path]::GetTempPath()) ("az-capacity-package-{0}" -f [guid]::NewGuid())
$dockerfile = Join-Path $PSScriptRoot 'tools\function-package\Dockerfile'

try {
    New-Item -ItemType Directory -Path $temporaryOutput -Force | Out-Null
    New-Item -ItemType Directory -Path $resolvedOutput -Force | Out-Null

    $buildArguments = @(
        'buildx', 'build',
        '--file', $dockerfile,
        '--platform', 'linux/amd64',
        '--target', 'export',
        '--build-arg', "PIP_INDEX_URL=$PackageIndexUrl",
        '--output', "type=local,dest=$temporaryOutput"
    )
    if ($NoCache) {
        $buildArguments += '--no-cache'
    }
    $buildArguments += $PSScriptRoot

    Invoke-NativeCommand $docker $buildArguments

    $temporaryPackage = Join-Path $temporaryOutput 'az-capacity-function.zip'
    if (-not (Test-Path -LiteralPath $temporaryPackage -PathType Leaf)) {
        throw "Container build did not produce $temporaryPackage"
    }

    $commit = (& git -C $PSScriptRoot rev-parse --short=12 HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $commit) {
        $commit = Get-Date -Format 'yyyyMMddHHmmss'
    }

    $packagePath = Join-Path $resolvedOutput "az-capacity-$commit.zip"
    $checksumPath = "$packagePath.sha256"
    Move-Item -LiteralPath $temporaryPackage -Destination $packagePath -Force

    $entries = [System.IO.Compression.ZipFile]::OpenRead($packagePath)
    try {
        $entryNames = @($entries.Entries.FullName)
    }
    finally {
        $entries.Dispose()
    }

    $requiredEntries = @('host.json', 'function_app.py', 'requirements.txt')
    foreach ($requiredEntry in $requiredEntries) {
        if ($requiredEntry -notin $entryNames) {
            throw "Deployment package is missing required root entry '$requiredEntry'."
        }
    }
    if (-not ($entryNames | Where-Object { $_ -like '.python_packages/lib/site-packages/*' })) {
        throw 'Deployment package does not contain Python runtime dependencies.'
    }

    $forbiddenPatterns = @('tests/*', 'local.settings*.json', '.git/*', 'tools/*', 'infra/*')
    foreach ($pattern in $forbiddenPatterns) {
        if ($entryNames | Where-Object { $_ -like $pattern }) {
            throw "Deployment package contains forbidden content matching '$pattern'."
        }
    }

    $package = Get-Item -LiteralPath $packagePath
    if ($package.Length -ge 1GB) {
        throw 'Deployment package exceeds the Azure Functions 1 GB package limit.'
    }

    $hash = (Get-FileHash -LiteralPath $packagePath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $($package.Name)" | Set-Content -LiteralPath $checksumPath -Encoding ascii

    Write-Host "Package:  $packagePath"
    Write-Host ("Size:     {0:N2} MB" -f ($package.Length / 1MB))
    Write-Host "SHA-256:  $hash"
}
finally {
    Remove-Item -LiteralPath $temporaryOutput -Recurse -Force -ErrorAction SilentlyContinue
}