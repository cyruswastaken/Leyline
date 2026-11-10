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
    # Stop after pass 1 even when saved Entra values exist (the Azure redirect URI is only known then).
    [switch]$InfraOnly,
    # After the migrations, load the sample corpus (the worker then indexes it).
    [switch]$Seed,
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

# Values saved by infra/entra.ps1 (the secret is encrypted for this Windows account).
$oidcGroupNames = '{}'
$entraState = Join-Path $PSScriptRoot '.entra.local.json'
if (-not ($OidcClientId -or $OidcTenantId -or $OidcClientSecret) -and (Test-Path $entraState)) {
    $saved = Get-Content $entraState -Raw | ConvertFrom-Json
    if ($saved.clientId -and $saved.tenantId -and $saved.clientSecret) {
        $OidcClientId = $saved.clientId
        $OidcTenantId = $saved.tenantId
        $OidcClientSecret = ConvertTo-SecureString $saved.clientSecret
        if ($saved.groupNames) { $oidcGroupNames = $saved.groupNames }
        Write-Host 'Using the Entra registration saved by infra/entra.ps1.'
    }
}
$deployApps = [bool]($OidcClientId -and $OidcTenantId -and $OidcClientSecret) -and -not $InfraOnly
if (-not $deployApps) {
    Write-Host 'Deploying infrastructure and the image only (pass 1).' -ForegroundColor Yellow
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
    # Azure sometimes answers a RequestConflict while a model deployment is still settling;
    # the deployment is repeatable, so one retry after a pause is safe.
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        $out = & az deployment sub create --location $Location --template-file $template --parameters "@$paramFile" --name leyline-infra --query properties.outputs -o json
        if ($LASTEXITCODE -eq 0) { break }
        if ($attempt -eq 2) { throw 'Infrastructure deployment failed.' }
        Write-Warning 'The deployment failed; retrying once in 60 seconds (this is safe: it only fills in what is missing).'
        Start-Sleep -Seconds 60
    }
    $outputs = $out | ConvertFrom-Json
    $registry = $outputs.registryServer.value
    Write-Host "Registry: $registry"

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
        Write-Host 'Run ./infra/entra.ps1 -AssignMe, then run this script again (it reads the saved Entra values) to deploy the apps.'
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
    $second.oidcGroupNames = $oidcGroupNames
    Remove-Item $paramFile -Force -WhatIf:$false
    $paramFile = Write-ParameterFile $second
    Write-Host 'Pass 2: web app, worker and task job...'
    $out = & az deployment sub create --location $Location --template-file $template --parameters "@$paramFile" --name leyline-apps --query properties.outputs -o json
    if ($LASTEXITCODE -ne 0) { throw 'App deployment failed.' }
    $outputs = $out | ConvertFrom-Json

    # The sign-in address depends on the environment's generated domain, so it can only be
    # registered now. (Entra only needs it before the first sign-in.)
    $redirect = "https://leyline-web.$($outputs.environmentDefaultDomain.value)/auth/callback"
    Write-Host "Registering the sign-in address in Entra: $redirect"
    & (Join-Path $PSScriptRoot 'entra.ps1') -RedirectUri $redirect | Out-Null

    # --- migrations -------------------------------------------------------------------------
    Write-Host 'Running database migrations...'
    # The job's own command is the migration (see modules/apps.bicep), so nothing is overridden:
    # az would pass '-m leyline.db upgrade' to Python as one argument and fail.
    Invoke-Az containerapp job start --name leyline-task --resource-group $AppResourceGroup --output none
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 10
        $status = & az containerapp job execution list --name leyline-task --resource-group $AppResourceGroup --query '[0].properties.status' -o tsv
        if ($status -in 'Succeeded', 'Failed', 'Stopped') { break }
    }
    if ($status -ne 'Succeeded') { throw "Migration job ended with status '$status'. See: ./infra/logs.ps1 -App leyline-task -Errors" }

    if ($Seed) {
        Write-Host 'Loading the sample corpus...'
        Invoke-Az containerapp job start --name leyline-seed --resource-group $AppResourceGroup --output none
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Seconds 10
            $seedStatus = & az containerapp job execution list --name leyline-seed --resource-group $AppResourceGroup --query '[0].properties.status' -o tsv
            if ($seedStatus -in 'Succeeded', 'Failed', 'Stopped') { break }
        }
        if ($seedStatus -ne 'Succeeded') { throw "Seed job ended with status '$seedStatus'. See: ./infra/logs.ps1 -App leyline-seed -Errors" }
        Write-Host 'Corpus queued; the worker indexes it in the background (watch: ./infra/logs.ps1 -App worker).'
    }

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
