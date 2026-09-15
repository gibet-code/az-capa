[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')]
    [string] $Profile,

    [Parameter(ValueFromRemainingArguments)]
    [string[]] $FunctionsArguments
)

$profilePath = Join-Path $PSScriptRoot "local.settings.$Profile.json"
$activeSettingsPath = Join-Path $PSScriptRoot 'local.settings.json'
$activatePath = Join-Path $PSScriptRoot '.venv\Scripts\Activate.ps1'

if (-not (Test-Path -LiteralPath $profilePath -PathType Leaf)) {
    throw "Local settings profile not found: $profilePath"
}
if (-not (Test-Path -LiteralPath $activatePath -PathType Leaf)) {
    throw "Python virtual environment not found: $activatePath"
}

$null = Get-Content -LiteralPath $profilePath -Raw | ConvertFrom-Json
Copy-Item -LiteralPath $profilePath -Destination $activeSettingsPath -Force
. $activatePath

Write-Host "Using local settings profile '$Profile'."
Push-Location $PSScriptRoot
try {
    & func start @FunctionsArguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
