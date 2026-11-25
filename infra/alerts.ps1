<#
.SYNOPSIS
  Creates (or updates) the action group that Leyline's alerts email, in the app resource group.

.DESCRIPTION
  The alert rules themselves are part of the template (infra/modules/monitoring.bicep) and
  exist whether or not anyone is notified. This script only creates the "who to tell" part,
  an action group named ag-leyline with one email receiver. deploy.ps1 and the CI deploy
  look the group up and attach it to every rule.

  The email address is a parameter on purpose: it is not stored in this repository.
  Run it after the first deployment pass has created the resource group. It is safe to run
  again (it replaces the receiver). Use -WhatIf to preview.

.EXAMPLE
  ./infra/alerts.ps1 -Email you@example.com -WhatIf
  ./infra/alerts.ps1 -Email you@example.com
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Email,
    [string]$ResourceGroup = 'rg-leyline-app',
    [string]$Name = 'ag-leyline'
)

$ErrorActionPreference = 'Stop'

function Invoke-Az {
    & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args[0..2] -join ' ') failed (exit code $LASTEXITCODE)" }
}

if ($Name -notmatch '^[A-Za-z0-9-]{1,60}$') { throw 'Name must be letters, digits and hyphens.' }
$sub = (& az account show --query id -o tsv)
if ($LASTEXITCODE -ne 0) { throw 'Not signed in. Run: az login' }

if ($Email -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') { throw 'Give a valid -Email address for the alerts.' }

Write-Host "Action group $Name in $ResourceGroup, one email receiver (address not shown)."
if ($PSCmdlet.ShouldProcess("$ResourceGroup/$Name", 'create or update the action group')) {
    Invoke-Az monitor action-group create --resource-group $ResourceGroup --name $Name --short-name leyline --action email owner $Email usecommonalertschema --output none
    Write-Host 'Done. Run ./infra/deploy.ps1 (or the Deploy workflow) so every alert rule notifies it.' -ForegroundColor Green
}
