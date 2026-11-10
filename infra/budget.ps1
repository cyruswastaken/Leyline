<#
.SYNOPSIS
  Creates (or updates) a monthly cost budget on the subscription with email alerts.

.DESCRIPTION
  Alerts at 50%, 80% and 100% of the amount as it is actually spent, and when Azure
  forecasts that you will reach 100%. The amount is in your billing currency (INR for
  your subscription). A budget only alerts: it does not stop anything from running, so
  tear the environment down (infra/teardown.ps1) when you are not using it.

  The email address is a parameter on purpose: it is not stored in this repository.

.EXAMPLE
  ./infra/budget.ps1 -Email you@example.com -WhatIf
  ./infra/budget.ps1 -Email you@example.com -Amount 4000
  ./infra/budget.ps1 -Remove
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Email,
    [ValidateRange(1, 10000000)][int]$Amount = 4000,
    [string]$Name = 'leyline-monthly',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$subscription = (& az account show --query id -o tsv)
if ($LASTEXITCODE -ne 0 -or -not $subscription) { throw 'Not signed in. Run: az login' }
$url = "https://management.azure.com/subscriptions/$subscription/providers/Microsoft.Consumption/budgets/$($Name)?api-version=2023-05-01"

if ($Remove) {
    if ($PSCmdlet.ShouldProcess("budget $Name", 'delete')) {
        & az rest --method delete --url $url
        if ($LASTEXITCODE -ne 0) { throw 'Could not delete the budget.' }
        Write-Host "Deleted budget $Name."
    }
    return
}

if ($Email -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') { throw 'Give a valid -Email address for the alerts.' }

$start = (Get-Date -Day 1).ToString('yyyy-MM-01T00:00:00Z')
$end = (Get-Date -Day 1).AddYears(2).ToString('yyyy-MM-01T00:00:00Z')

function New-Alert([int]$Threshold, [string]$Type) {
    @{ enabled = $true; operator = 'GreaterThan'; threshold = $Threshold; thresholdType = $Type
       contactEmails = @($Email); locale = 'en-us' }
}
$body = @{
    properties = @{
        category      = 'Cost'
        amount        = $Amount
        timeGrain     = 'Monthly'
        timePeriod    = @{ startDate = $start; endDate = $end }
        notifications = @{
            Actual_50  = (New-Alert 50 'Actual')
            Actual_80  = (New-Alert 80 'Actual')
            Actual_100 = (New-Alert 100 'Actual')
            Forecast_100 = (New-Alert 100 'Forecasted')
        }
    }
}

Write-Host "Budget '$Name': $Amount per month (billing currency), alerts at 50%, 80%, 100% and on a 100% forecast."
if ($PSCmdlet.ShouldProcess("budget $Name on subscription $subscription", 'create or update')) {
    $file = Join-Path ([System.IO.Path]::GetTempPath()) ("budget-{0}.json" -f [guid]::NewGuid())
    try {
        [System.IO.File]::WriteAllText($file, ($body | ConvertTo-Json -Depth 8))
        & az rest --method put --url $url --headers 'Content-Type=application/json' --body "@$file" --query '{name:name,amount:properties.amount,grain:properties.timeGrain}' -o table
        if ($LASTEXITCODE -ne 0) { throw 'Azure refused the budget (see the message above).' }
    }
    finally { Remove-Item $file -Force -ErrorAction SilentlyContinue -WhatIf:$false }
}
