<#
.SYNOPSIS
  Deploys Leyline to Azure in two passes (see docs/DEPLOYMENT.md).

.DESCRIPTION
  Pass 1 creates the infrastructure and the model deployments. The script then builds
  the image, pushes it to the new registry and, when Entra sign-in values are given,
  runs pass 2 (web app, worker, task job) and the database migrations.

  Use -WhatIf first: it only previews the changes and creates nothing.

  Secrets never touch a file in the repository or a command line: the Postgres password
  is generated here, written to a temporary parameter file that only you can read, and
  that file is deleted when the script ends.

.EXAMPLE
  ./infra/deploy.ps1 -WhatIf
  ./infra/deploy.ps1                                  # infrastructure + image only
  ./infra/deploy.ps1 -OidcClientId <id> -OidcTenantId <tenant> -OidcClientSecret (Read-Host -AsSecureString)
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Location = 'koreacentral',
    [string]$AppResourceGroup = 'rg-leyline-app',
    [string]$OidcClientId = '',
    [string]$OidcTenantId = '',
    [securestring]$OidcClientSecret,
    [switch]$SkipBuild,
    [switch]$ScaleToZero
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$template = Join-Path $PSScriptRoot 'main.bicep'

function Invoke-Az {
    # Runs az and stops on a non-zero exit code (native commands do not throw by themselves).
    & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args[0..2] -join ' ') failed (exit code $LASTEXITCODE)" }
}

function New-Password {
    # 32 random characters from an unambiguous set (upper, lower, digits), cryptographically random.
    $chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'.ToCharArray()
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}

function Write-ParameterFile([hashtable]$Values) {
    $file = Join-Path ([System.IO.Path]::GetTempPath()) ("leyline-params-{0}.json" -f [guid]::NewGuid())
    $doc = @{
        '$schema'      = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'
        contentVersion = '1.0.0.0'
        parameters     = @{}
    }
    foreach ($key in $Values.Keys) { $doc.parameters[$key] = @{ value = $Values[$key] } }
    [System.IO.File]::WriteAllText($file, ($doc | ConvertTo-Json -Depth 6))
    # Only the current user may read it (removes inherited permissions).
    & icacls $file /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
    return $file
}

# --- preflight --------------------------------------------------------------------------
$account = (& az account show --query '{name:name,id:id,state:state}' -o json | ConvertFrom-Json)
if (-not $account) { throw 'Not signed in. Run: az login' }
if ($account.state -ne 'Enabled') { throw "Subscription '$($account.name)' is $($account.state)." }
Write-Host "Subscription: $($account.name)  Region: $Location  Resource group: $AppResourceGroup"

$deployApps = [bool]($OidcClientId -and $OidcTenantId -and $OidcClientSecret)
if (-not $deployApps) {
    Write-Host 'No Entra sign-in values given: deploying infrastructure and the image only.' -ForegroundColor Yellow
}

$paramFile = $null
try {
    # Reading an existing password back from the vault would need data-plane rights, so a
    # fresh one is generated per run; the apps are restarted at the end to pick it up.
    $password = New-Password
    $base = @{
        location              = $Location
        appResourceGroupName  = $AppResourceGroup
        postgresAdminPassword = $password
        webAlwaysOn           = (-not $ScaleToZero.IsPresent)
    }

    if ($WhatIfPreference) {
        $paramFile = Write-ParameterFile $base
        Invoke-Az deployment sub what-if --location $Location --template-file $template --parameters "@$paramFile" --name leyline-whatif
        Write-Host 'Preview only: nothing was created.' -ForegroundColor Green
        return
    }

    # --- pass 1: infrastructure (apps off) ---------------------------------------------
    $paramFile = Write-ParameterFile $base
    Write-Host 'Pass 1: infrastructure (takes about 10 minutes: Postgres and Redis are slow)...'
    $out = & az deployment sub create --location $Location --template-file $template --parameters "@$paramFile" --name leyline-infra --query properties.outputs -o json
    if ($LASTEXITCODE -ne 0) { throw 'Infrastructure deployment failed.' }
    $outputs = $out | ConvertFrom-Json
    $registry = $outputs.registryServer.value
    $domain = $outputs.environmentDefaultDomain.value
    Write-Host "Registry: $registry"
    Write-Host "Redirect URI to register in Entra: https://leyline-web.$domain/auth/callback"

    # --- image ---------------------------------------------------------------------------
    $tag = Get-Date -Format 'yyyyMMdd-HHmmss'
    $image = "$registry/leyline:$tag"
    if (-not $SkipBuild) {
        Write-Host "Building $image ..."
        Invoke-Az acr login --name ($registry -split '\.')[0]
        & docker build --tag $image $repo
        if ($LASTEXITCODE -ne 0) { throw 'docker build failed.' }
        & docker push $image
        if ($LASTEXITCODE -ne 0) { throw 'docker push failed.' }
    }

    if (-not $deployApps) {
        Write-Host ''
        Write-Host "Infrastructure and image are ready ($image)." -ForegroundColor Green
        Write-Host 'Register the app in Entra, then run this script again with -OidcClientId, -OidcTenantId and -OidcClientSecret.'
        return
    }

    # --- pass 2: web app, worker, task job -------------------------------------------------
    $secretPlain = [System.Net.NetworkCredential]::new('', $OidcClientSecret).Password
    $second = $base.Clone()
    $second.deployApps = $true
    $second.appImage = $image
    $second.oidcClientId = $OidcClientId
    $second.oidcTenantId = $OidcTenantId
    $second.oidcClientSecret = $secretPlain
    Remove-Item $paramFile -Force -WhatIf:$false
    $paramFile = Write-ParameterFile $second
    Write-Host 'Pass 2: web app, worker and task job...'
    $out = & az deployment sub create --location $Location --template-file $template --parameters "@$paramFile" --name leyline-apps --query properties.outputs -o json
    if ($LASTEXITCODE -ne 0) { throw 'App deployment failed.' }
    $outputs = $out | ConvertFrom-Json

    # --- migrations -------------------------------------------------------------------------
    Write-Host 'Running database migrations...'
    Invoke-Az containerapp job start --name leyline-task --resource-group $AppResourceGroup --image $image `
        --command python --args '-m leyline.db upgrade' --output none
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 10
        $status = & az containerapp job execution list --name leyline-task --resource-group $AppResourceGroup --query '[0].properties.status' -o tsv
        if ($status -in 'Succeeded', 'Failed', 'Stopped') { break }
    }
    if ($status -ne 'Succeeded') { throw "Migration job ended with status '$status'. See: az containerapp job logs show --name leyline-task --resource-group $AppResourceGroup" }

    # The password changed this run, so make the running replicas pick up the new secret.
    foreach ($app in 'leyline-web', 'leyline-worker') {
        $rev = & az containerapp revision list --name $app --resource-group $AppResourceGroup --query '[0].name' -o tsv
        Invoke-Az containerapp revision restart --name $app --resource-group $AppResourceGroup --revision $rev --output none
    }

    Write-Host ''
    Write-Host "Leyline is up: $($outputs.webUrl.value)" -ForegroundColor Green
    Write-Host 'Next: load the corpus (see docs/DEPLOYMENT.md, "Seed the corpus").'
}
finally {
    # -WhatIf:$false, or a preview run would leave the file behind.
    if ($paramFile -and (Test-Path $paramFile)) { Remove-Item $paramFile -Force -WhatIf:$false }
}
