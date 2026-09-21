[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$bicepPath = Join-Path $PSScriptRoot 'infra\main.bicep'
$templatePath = Join-Path $PSScriptRoot 'infra\mainTemplate.json'
$uiPath = Join-Path $PSScriptRoot 'infra\createUiDefinition.json'

az bicep build --file $bicepPath --outfile $templatePath
if ($LASTEXITCODE -ne 0) {
    throw "Bicep compilation failed with exit code $LASTEXITCODE."
}

$template = Get-Content -LiteralPath $templatePath -Raw | ConvertFrom-Json
$ui = Get-Content -LiteralPath $uiPath -Raw | ConvertFrom-Json
$templateParameters = @($template.parameters.PSObject.Properties.Name)
$uiOutputs = @($ui.parameters.outputs.PSObject.Properties.Name)
$missingOutputs = @($templateParameters | Where-Object { $_ -notin $uiOutputs })
$unknownOutputs = @($uiOutputs | Where-Object { $_ -notin $templateParameters })

if ($missingOutputs.Count -gt 0) {
    throw "UI definition is missing template parameters: $($missingOutputs -join ', ')"
}
if ($unknownOutputs.Count -gt 0) {
    throw "UI definition has unknown template parameters: $($unknownOutputs -join ', ')"
}

Write-Host "Template: $templatePath"
Write-Host "UI:       $uiPath"
Write-Host "Validated $($templateParameters.Count) deployment parameters."
