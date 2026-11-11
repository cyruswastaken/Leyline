<#
.SYNOPSIS
  Shows recent console or platform logs of the Leyline containers from Log Analytics.

.DESCRIPTION
  Needs no Azure CLI extension: it queries the workspace through the Log Analytics API.
  Logs appear a minute or two after they are written.

.EXAMPLE
  ./infra/logs.ps1                          # last 15 minutes, all containers, newest last
  ./infra/logs.ps1 -App worker -Minutes 60
  ./infra/logs.ps1 -Errors                  # only lines that look like errors
  ./infra/logs.ps1 -System                  # platform events (image pulls, restarts, secrets)
  ./infra/logs.ps1 -App web -NoProbes       # requests without the health probes
#>
param(
    [string]$App = '',
    [int]$Minutes = 15,
    [int]$Tail = 60,
    [switch]$Errors,
    [switch]$System,
    # Hide the platform's health probes (/healthz, /readyz), which otherwise fill the output.
    [switch]$NoProbes,
    [string]$ResourceGroup = 'rg-leyline-app',
    [string]$Workspace = 'log-leyline'
)

$ErrorActionPreference = 'Stop'
if ($App -and $App -notmatch '^[A-Za-z0-9-]+$') { throw 'App must be a container name such as web, worker or leyline-task.' }

$subscription = & az account show --query id -o tsv
if ($LASTEXITCODE -ne 0) { throw 'Not signed in. Run: az login' }
$customerId = & az rest --method get --url "https://management.azure.com/subscriptions/$subscription/resourceGroups/$ResourceGroup/providers/Microsoft.OperationalInsights/workspaces/$($Workspace)?api-version=2023-09-01" --query properties.customerId -o tsv
if ($LASTEXITCODE -ne 0) { throw 'Could not find the Log Analytics workspace.' }

if ($System) {
    $kql = "ContainerAppSystemLogs_CL | where TimeGenerated > ago($($Minutes)m)"
    if ($App) { $kql += " | where Log_s has '$App' or ContainerAppName_s == '$App'" }
    $kql += " | order by TimeGenerated desc | take $Tail | order by TimeGenerated asc | project TimeGenerated, Reason_s, Log_s"
}
else {
    $kql = "ContainerAppConsoleLogs_CL | where TimeGenerated > ago($($Minutes)m)"
    if ($App) { $kql += " | where ContainerName_s == '$App' or ContainerAppName_s == '$App'" }
    if ($NoProbes) { $kql += " | where Log_s !has '/healthz' and Log_s !has '/readyz'" }
    if ($Errors) { $kql += " | where Log_s matches regex '(?i)(error|exception|traceback|failed|refused|denied|timeout)'" }
    $kql += " | order by TimeGenerated desc | take $Tail | order by TimeGenerated asc | project TimeGenerated, ContainerName_s, Log_s"
}

$file = Join-Path ([System.IO.Path]::GetTempPath()) ("logs-{0}.json" -f [guid]::NewGuid())
try {
    [System.IO.File]::WriteAllText($file, (@{ query = $kql } | ConvertTo-Json -Compress))
    $result = & az rest --method post --resource 'https://api.loganalytics.io' --url "https://api.loganalytics.io/v1/workspaces/$customerId/query" --headers 'Content-Type=application/json' --body "@$file" | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'The log query failed.' }
    $rows = $result.tables[0].rows
    if (-not $rows -or $rows.Count -eq 0) { Write-Host 'No log lines in that window (logs can take a minute or two to appear).'; return }
    foreach ($row in $rows) { Write-Host ($row -join ' | ') }
}
finally { Remove-Item $file -Force -ErrorAction SilentlyContinue -WhatIf:$false }
