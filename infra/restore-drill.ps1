<#
.SYNOPSIS
  Proves the database backup can be restored: restores Postgres to a temporary server,
  checks the data, and deletes the temporary server.

.DESCRIPTION
  A backup nobody has restored is a hope, not a backup. This script restores the live
  Postgres server to a point in time into a NEW server named leyline-pg-drill-<random>,
  signs in to the copy as you (it makes you an Entra administrator of the copy only), runs
  the read-only checks in scripts/restore_check.py (schema revision, document and chunk
  counts, no document without an access list, the append-only trigger, the pgvector
  extension) and reports how long it took (the recovery time) and how old the newest
  restorable data was (the recovery point). The copy is always deleted at the end, even
  if a check fails.

  It never changes the live server. It does cost money while it runs: a second burstable
  server for about 15 to 30 minutes (well under one US dollar), plus a firewall rule for
  your current public address on the copy only. Use -WhatIf first.

  Needs: az login with rights on the resource group, and the repository's Python
  environment (.venv) for the checks. Your public address is looked up at api.ipify.org
  unless you pass -ClientIp.

.EXAMPLE
  ./infra/restore-drill.ps1 -WhatIf
  ./infra/restore-drill.ps1                       # restore to 10 minutes ago
  ./infra/restore-drill.ps1 -MinutesAgo 60
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$ResourceGroup = 'rg-leyline-app',
    # How far back to restore. The server keeps 7 days; a few minutes back is the useful test.
    [ValidateRange(5, 10000)]
    [int]$MinutesAgo = 10,
    [string]$ClientIp,
    # Keep the copy after the checks instead of deleting it (you then pay until you delete it).
    [switch]$Keep
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

function Invoke-Az {
    & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args[0..2] -join ' ') failed (exit code $LASTEXITCODE)" }
}

if ($ResourceGroup -notmatch '^[A-Za-z0-9._()-]{1,90}$') { throw 'ResourceGroup has unexpected characters.' }
$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'The repository Python environment (.venv) is needed for the checks.' }

$source = (& az postgres flexible-server list --resource-group $ResourceGroup --query "[?starts_with(name, 'leyline-pg-') && !starts_with(name, 'leyline-pg-drill-')] | [0].name" -o tsv)
if ($LASTEXITCODE -ne 0 -or -not $source) { throw "No Leyline Postgres server found in $ResourceGroup." }
$drill = 'leyline-pg-drill-' + (-join ((48..57) + (97..122) | Get-Random -Count 6 | ForEach-Object { [char]$_ }))
if ($drill -notmatch '^leyline-pg-drill-[a-z0-9]{6}$') { throw 'Unexpected drill server name.' }

$restoreTime = (Get-Date).ToUniversalTime().AddMinutes(-$MinutesAgo).ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
Write-Host "Source server : $source (read only)"
Write-Host "Copy          : $drill (temporary, deleted at the end)"
Write-Host "Restore point : $restoreTime ($MinutesAgo minutes ago)"

if (-not $PSCmdlet.ShouldProcess($drill, "restore $source to a temporary server, check it, then delete it")) { return }

$started = Get-Date
$created = $false
try {
    Write-Host 'Restoring (usually 10 to 20 minutes)...'
    Invoke-Az postgres flexible-server restore --resource-group $ResourceGroup --name $drill --source-server $source --restore-time $restoreTime --output none
    $created = $true
    $restored = Get-Date

    # The copy keeps the source's sign-in rules. Make sure Entra sign-in is on, and make
    # the person running the drill an administrator of the COPY (the live server is untouched).
    $me = & az ad signed-in-user show --query '{id:id,upn:userPrincipalName}' -o json | ConvertFrom-Json
    Invoke-Az postgres flexible-server update --resource-group $ResourceGroup --name $drill --active-directory-auth Enabled --output none
    Invoke-Az postgres flexible-server ad-admin create --resource-group $ResourceGroup --server-name $drill --display-name $me.upn --object-id $me.id --type User --output none

    if (-not $ClientIp) { $ClientIp = (Invoke-RestMethod -Uri 'https://api.ipify.org' -TimeoutSec 15).Trim() }
    if ($ClientIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { throw 'Could not work out a valid public IPv4 address; pass -ClientIp.' }
    Invoke-Az postgres flexible-server firewall-rule create --resource-group $ResourceGroup --name $drill --rule-name drill-client --start-ip-address $ClientIp --end-ip-address $ClientIp --output none

    $host_ = (& az postgres flexible-server show --resource-group $ResourceGroup --name $drill --query fullyQualifiedDomainName -o tsv)
    $earliest = (& az postgres flexible-server show --resource-group $ResourceGroup --name $source --query backup.earliestRestoreDate -o tsv)
    $token = (& az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)

    $env:PGHOST = $host_; $env:PGUSER = $me.upn; $env:PGPASSWORD = $token
    try {
        $raw = & $python (Join-Path $repo 'scripts\restore_check.py')
        if ($LASTEXITCODE -ne 0) { throw 'The checks could not run (see the message above).' }
    }
    finally {
        Remove-Item Env:PGHOST, Env:PGUSER, Env:PGPASSWORD -ErrorAction SilentlyContinue
    }
    $found = $raw | ConvertFrom-Json
    $verified = Get-Date

    $problems = @()
    if ($found.documents -lt 1) { $problems += 'no live documents were restored' }
    if ($found.live_documents_without_acl -ne 0) { $problems += 'a live document has no access list' }
    if ($found.append_only_trigger -ne 1) { $problems += 'the append-only trigger is missing' }
    if ($found.vector_extension -ne 1) { $problems += 'the pgvector extension is missing' }
    if (-not $found.schema_revision) { $problems += 'no schema revision' }

    Write-Host ''
    Write-Host 'Restored data:'
    $found | Format-List | Out-String | Write-Host
    Write-Host ("Recovery time (restore until the checks passed): {0:N0} minutes" -f (($verified - $started).TotalMinutes))
    Write-Host ("  of which the restore itself              : {0:N0} minutes" -f (($restored - $started).TotalMinutes))
    Write-Host "Recovery point: the restore used data from $restoreTime; the server could restore as far back as $earliest."
    Write-Host "  The newest security event in the copy is at $($found.newest_security_event) (compare it with the restore point)."
    if ($problems.Count) {
        Write-Host ('FAILED: ' + ($problems -join '; ')) -ForegroundColor Red
        $global:LASTEXITCODE = 1
    }
    else {
        Write-Host 'PASSED: the backup restores, and the restored data is intact.' -ForegroundColor Green
    }
}
finally {
    if ($created -and -not $Keep) {
        Write-Host "Deleting the temporary server $drill ..."
        # Only a server this script created (the name was checked above) is ever deleted.
        & az postgres flexible-server delete --resource-group $ResourceGroup --name $drill --yes --output none
        if ($LASTEXITCODE -ne 0) { Write-Warning "Could not delete $drill. Delete it by hand: az postgres flexible-server delete -g $ResourceGroup -n $drill --yes" }
    }
    elseif ($created) {
        Write-Warning "$drill was kept. It costs money until you run: az postgres flexible-server delete -g $ResourceGroup -n $drill --yes"
    }
}
