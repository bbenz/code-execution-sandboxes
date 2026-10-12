#!/usr/bin/env pwsh
# Build the referee engine image in Azure Container Registry (server-side; no local Docker needed).
# ACR builds aren't reproducible, so a rebuild of unchanged sources still gets a new digest and new disk
# images. The image is also tagged with a hash of engine/, and the build is skipped when `latest` already
# came from the current sources. Set REFEREE_FORCE_BUILD=true to rebuild anyway.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$engine = Join-Path $root "engine"
$acr = (azd env get-value acrName 2>$null)
$rg = (azd env get-value AZURE_RESOURCE_GROUP 2>$null)

if ([string]::IsNullOrWhiteSpace($acr)) {
    Write-Error "acrName not found in the azd environment. Run 'azd provision' first."
    exit 1
}

# Hash the files the build sends (the ones engine/.dockerignore doesn't exclude).
$entries = foreach ($file in Get-ChildItem -LiteralPath $engine -Recurse -File -Force) {
    $relative = [IO.Path]::GetRelativePath($engine, $file.FullName).Replace('\', '/')
    if ("/$relative" -match '/(target|__pycache__|\.pytest_cache|\.ruff_cache|\.referee)/|\.pyc$') { continue }
    "$relative $((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash)"
}
$listing = [Text.Encoding]::UTF8.GetBytes((($entries | Sort-Object -CaseSensitive) -join "`n"))
$sourceTag = "src-" + [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($listing)).Substring(0, 12).ToLower()

function Get-Digest([string]$tag) {
    $digest = az acr repository show --name $acr --image "referee-engine:$tag" --query digest -o tsv 2>$null
    if ($LASTEXITCODE -eq 0) { return "$digest".Trim() }
    return ""
}

if ($env:REFEREE_FORCE_BUILD -ne "true") {
    $built = Get-Digest $sourceTag
    if ($built -and $built -eq (Get-Digest "latest")) {
        Write-Host "referee-engine:latest is already built from the current engine sources ($sourceTag). Skipping the build."
        exit 0
    }
}

Write-Host "Building referee-engine:latest ($sourceTag) in $acr (server-side)..."
# --no-logs: on Windows the Azure CLI crashes (UnicodeEncodeError) when it streams ACR build logs into a
# pipe, which is how azd runs hooks. Wait for the run without streaming, then check how it finished.
$acrArgs = @("acr", "build", "--registry", $acr, "--image", "referee-engine:latest",
             "--image", "referee-engine:$sourceTag", "--file", "$engine/Dockerfile",
             "--no-logs", "--output", "json", $engine)
if (-not [string]::IsNullOrWhiteSpace($rg)) { $acrArgs += @("--resource-group", $rg) }
$output = az @acrArgs | Out-String
if ($LASTEXITCODE -ne 0) {
    Write-Host "az acr build failed with exit code $LASTEXITCODE."
    exit 1
}
$run = $output | ConvertFrom-Json
if ($run.status -ne "Succeeded") {
    Write-Host ("Engine image build finished as '$($run.status)'. " +
                "See the build log with: az acr task logs --registry $acr --run-id $($run.runId)")
    exit 1
}
Write-Host "Built referee-engine:latest (ACR run $($run.runId))."
exit 0
