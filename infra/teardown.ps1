<#
.SYNOPSIS
  Deletes everything Leyline created in Azure that costs money (the rg-leyline-app group).

.DESCRIPTION
  Your Azure OpenAI account lives in a different group (rg-leyline) and is kept: an idle
  account costs nothing. Add -IncludeModels to also remove the two model deployments.

  The Key Vault is purged after deletion, because Azure keeps a deleted vault's name for
  seven days and the next deploy would otherwise fail on it.

.EXAMPLE
  ./infra/teardown.ps1 -WhatIf        # shows what would be deleted
  ./infra/teardown.ps1                # asks you to type the group name
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$AppResourceGroup = 'rg-leyline-app',
    [string]$OpenAiResourceGroup = 'rg-leyline',
    [string]$OpenAiAccount = 'leyline-openai',
    [string[]]$ModelDeployments = @('gpt-5.4-mini', 'text-embedding-3-small'),
    [switch]$IncludeModels,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$exists = (& az group exists --name $AppResourceGroup) -eq 'true'
if (-not $exists) {
    Write-Host "$AppResourceGroup does not exist: nothing to delete."
}
else {
    $resources = & az resource list --resource-group $AppResourceGroup --query "[].{name:name,type:type}" -o json | ConvertFrom-Json
    Write-Host "These resources in $AppResourceGroup will be deleted for good:"
    $resources | ForEach-Object { Write-Host ("  {0}  ({1})" -f $_.name, $_.type) }
    $vault = & az keyvault list --resource-group $AppResourceGroup --query '[0].name' -o tsv

    if ($WhatIfPreference) { Write-Host 'Preview only: nothing was deleted.'; return }

    if (-not $Force) {
        $answer = Read-Host "Type the resource group name ($AppResourceGroup) to confirm"
        if ($answer -ne $AppResourceGroup) { throw 'Not confirmed. Nothing was deleted.' }
    }
    Write-Host 'Deleting (a few minutes)...'
    & az group delete --name $AppResourceGroup --yes
    if ($LASTEXITCODE -ne 0) { throw 'Deleting the resource group failed.' }
    if ($vault) {
        Write-Host "Purging the deleted Key Vault $vault ..."
        & az keyvault purge --name $vault
        if ($LASTEXITCODE -ne 0) { Write-Warning "Could not purge $vault; purge it before the next deploy (az keyvault purge --name $vault)." }
    }
}

if ($IncludeModels) {
    foreach ($name in $ModelDeployments) {
        if ($WhatIfPreference) { Write-Host "Would delete model deployment $name"; continue }
        Write-Host "Deleting model deployment $name ..."
        & az cognitiveservices account deployment delete --resource-group $OpenAiResourceGroup --name $OpenAiAccount --deployment-name $name
    }
}
Write-Host 'Done. Remember the Entra app registration and test users (not removed by this script).'
