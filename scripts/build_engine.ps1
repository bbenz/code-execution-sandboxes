#!/usr/bin/env pwsh
# Build the referee engine image in Azure Container Registry (server-side; no local Docker needed).
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$acr = (azd env get-value acrName 2>$null)
$rg = (azd env get-value AZURE_RESOURCE_GROUP 2>$null)

if ([string]::IsNullOrWhiteSpace($acr)) {
    Write-Error "acrName not found in the azd environment. Run 'azd provision' first."
    exit 1
}

Write-Host "Building referee-engine:latest in $acr…"
$acrArgs = @("acr", "build", "--registry", $acr, "--image", "referee-engine:latest",
             "--file", "$root/engine/Dockerfile", "$root/engine")
if (-not [string]::IsNullOrWhiteSpace($rg)) { $acrArgs += @("--resource-group", $rg) }
az @acrArgs
exit $LASTEXITCODE
