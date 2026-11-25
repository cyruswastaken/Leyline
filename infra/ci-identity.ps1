<#
.SYNOPSIS
  Creates the identity the "Deploy to Azure" GitHub workflow signs in with, and the GitHub environment
  that guards it. Safe to run again; use -WhatIf first (it changes nothing).

.DESCRIPTION
  The workflow uses OIDC federation, so there is NO stored Azure password or key. This script sets up:

    Entra       an app registration "Leyline CI Deploy" with one federated credential that trusts ONLY
                this repository's GitHub environment "azure" (subject
                repo:<owner>/<repo>:environment:azure, no wildcard)
    Azure       narrow rights, nothing more:
                  subscription      custom role "Leyline Deployment Starter": start and read
                                    deployments and create or update resource groups (it cannot delete)
                  rg-leyline-app    custom role "Leyline App Deployer": everything except deleting the
                                    resource group and except writing role assignments; plus
                                    "Role Based Access Control Administrator" limited by a condition to
                                    the three roles the template assigns (AcrPull, Key Vault Secrets
                                    User, Storage Blob Data Contributor); plus AcrPush to push the image
                  leyline-openai    "Cognitive Services Contributor" on that one account (model
                                    deployments and the key), and "Role Based Access Control
                                    Administrator" limited by a condition to one role, Cognitive
                                    Services OpenAI User (so the app's identity can call the models)
    GitHub      environment "azure" that requires an approval from a named reviewer and accepts only the
                main branch, with the three non-secret ids as environment variables

  It does not touch Microsoft Entra apps other than its own, and it creates no secret. The deploy
  pipeline cannot rebuild the environment from nothing or change Entra: that stays with deploy.ps1.

  After a full rebuild (teardown deletes rg-leyline-app and its role assignments) run this script again.

.EXAMPLE
  ./infra/ci-identity.ps1 -WhatIf
  ./infra/ci-identity.ps1
  ./infra/ci-identity.ps1 -Remove
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Repository = 'cyruswastaken/Leyline',
    [string]$EnvironmentName = 'azure',
    [string]$AppName = 'Leyline CI Deploy',
    [string]$AppResourceGroup = 'rg-leyline-app',
    [string]$OpenAiResourceGroup = 'rg-leyline',
    [string]$OpenAiAccount = 'leyline-openai',
    # GitHub user who must approve deployments (default: the signed-in gh user)
    [string]$Reviewer = '',
    [switch]$SkipGitHub,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

# Built-in role ids (looked up with `az role definition list`).
$RoleRbacAdministrator = 'f58310d9-a9f6-439a-9e8d-f62e7b41a168'
$RoleAcrPush = '8311e382-0749-4cb8-b61a-304f252e45ec'
$RoleCognitiveServicesContributor = '25fbc0a9-bd7c-42a3-aa1a-3b75d497ee68'
# The only roles the template assigns inside the app group (see infra/modules/platform.bicep): AcrPull,
# Key Vault Secrets User and Storage Blob Data Contributor (the last only when the app signs in to storage
# with its managed identity, decision 0014).
$AssignableByCi = @('7f951dda-4ed3-4680-a7ca-43fe172d538d', '4633458b-17de-408a-b874-0445c86b69e6', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
# The only role the template assigns on the Azure OpenAI account (infra/modules/openai-access.bicep):
# Cognitive Services OpenAI User, so the app's identity can call the models instead of holding a key.
$AssignableOnOpenAi = @('5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')

function Invoke-Rest([string]$Method, [string]$Url, $Body = $null, [switch]$Arm) {
    $a = @('rest', '--method', $Method, '--url', $Url, '--headers', 'Content-Type=application/json')
    $tmp = $null
    if ($null -ne $Body) {
        $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("ci-{0}.json" -f [guid]::NewGuid())
        [System.IO.File]::WriteAllText($tmp, ($Body | ConvertTo-Json -Depth 12 -Compress))
        $a += @('--body', "@$tmp")
    }
    $errFile = Join-Path ([System.IO.Path]::GetTempPath()) ("ci-err-{0}.txt" -f [guid]::NewGuid())
    $WhatIfPreference = $false # every change goes through Step; reads must not be blocked by -WhatIf
    try {
        $out = & az @a 2> $errFile
        if ($LASTEXITCODE -ne 0) {
            $detail = if (Test-Path $errFile) { Get-Content $errFile -Raw } else { '' }
            throw "$Method $Url failed: $detail"
        }
        if ($out) { return ($out -join "`n" | ConvertFrom-Json) }
    }
    finally { foreach ($f in @($tmp, $errFile)) { if ($f -and (Test-Path $f)) { Remove-Item $f -Force } } }
}
function Graph([string]$Method, [string]$Path, $Body = $null) { Invoke-Rest $Method "https://graph.microsoft.com/v1.0$Path" $Body }
function Arm([string]$Method, [string]$Path, $Body = $null) { Invoke-Rest $Method "https://management.azure.com$Path" $Body -Arm }

function Step([string]$What, [scriptblock]$Do) { if ($PSCmdlet.ShouldProcess($What)) { & $Do } }

function New-DeterministicGuid([string]$Text) {
    [guid]::new([System.Security.Cryptography.MD5]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Text)))
}

# --- who and where --------------------------------------------------------------------------------
$sub = (& az account show --query id -o tsv)
$tenant = (& az account show --query tenantId -o tsv)
if (-not $sub) { throw 'Not signed in. Run: az login' }
if ($Repository -notmatch '^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$') { throw 'Repository must look like owner/name.' }
$subScope = "/subscriptions/$sub"
$appScope = "$subScope/resourceGroups/$AppResourceGroup"
$openAiScope = "$subScope/resourceGroups/$OpenAiResourceGroup/providers/Microsoft.CognitiveServices/accounts/$OpenAiAccount"
# The subject GitHub puts in the token is the source of truth. Repositories that use immutable
# subjects name the owner and repository by id (repo:owner@123/name@456), which survives a rename
# and cannot be inherited by a different repository with the same name; GitHub tells us the prefix.
$subjectPrefix = "repo:$Repository"
try {
    $customisation = (& gh api "repos/$Repository/actions/oidc/customization/sub") | ConvertFrom-Json
    if ($customisation.sub_claim_prefix) { $subjectPrefix = [string]$customisation.sub_claim_prefix }
}
catch { throw "Could not read the repository's OIDC subject settings with gh ($($_.Exception.Message)). Run: gh auth login" }
if ($subjectPrefix -notmatch '^repo:[A-Za-z0-9._@/-]+$') { throw "Unexpected OIDC subject prefix: $subjectPrefix" }
$subject = "$($subjectPrefix):environment:$EnvironmentName"
Write-Host "Subscription: $sub  Repository: $Repository  Environment: $EnvironmentName"

$starterName = 'Leyline Deployment Starter'
$deployerName = 'Leyline App Deployer'
$starterId = (New-DeterministicGuid "leyline-role:${sub}:${starterName}").Guid
$deployerId = (New-DeterministicGuid "leyline-role:${sub}:${deployerName}").Guid

function Find-App { (Graph GET "/applications?`$filter=displayName eq '$AppName'").value | Select-Object -First 1 }

# --- remove ------------------------------------------------------------------------------------------
if ($Remove) {
    $app = Find-App
    if ($app) {
        $sp = (Graph GET "/servicePrincipals?`$filter=appId eq '$($app.appId)'").value | Select-Object -First 1
        if ($sp) {
            $assignments = (Arm GET "$subScope/providers/Microsoft.Authorization/roleAssignments?api-version=2022-04-01&`$filter=principalId eq '$($sp.id)'").value
            foreach ($a in $assignments) { Step "delete role assignment $($a.name)" { Arm DELETE "$($a.id)?api-version=2022-04-01" | Out-Null } }
        }
        Step "delete app registration $AppName" { Graph DELETE "/applications/$($app.id)" | Out-Null }
    }
    foreach ($id in $starterId, $deployerId) {
        Step "delete custom role $id" { try { Arm DELETE "$subScope/providers/Microsoft.Authorization/roleDefinitions/$($id)?api-version=2022-04-01" | Out-Null } catch { Write-Verbose "role $id not found" } }
    }
    if (-not $SkipGitHub) {
        Step "delete GitHub environment $EnvironmentName" { & gh api -X DELETE "repos/$Repository/environments/$EnvironmentName" 2>$null | Out-Null }
    }
    Write-Host 'Removed.'
    return
}

# --- Entra app, service principal, federated credential ---------------------------------------------
$app = Find-App
if (-not $app) {
    Step "create app registration '$AppName' (single tenant)" {
        $script:app = Graph POST '/applications' @{ displayName = $AppName; signInAudience = 'AzureADMyOrg' }
    }
}
else { Write-Host "App registration exists: $($app.appId)" }
if (-not $app) {
    # Preview only: the rest needs the new app's id, so list what the real run would do.
    Write-Host 'Then, once the app exists, the real run would also:'
    Write-Host "  - create the service principal and ONE federated credential trusting only: $subject"
    Write-Host "  - create custom role '$starterName' on the subscription (start/read deployments, create/update resource groups, no delete)"
    Write-Host "  - create custom role '$deployerName' (everything except deleting the resource group and writing role assignments)"
    Write-Host "  - grant '$starterName' on the subscription"
    Write-Host "  - grant '$deployerName', AcrPush and a CONDITIONED role-assignment right (only AcrPull, Key Vault Secrets User and Storage Blob Data Contributor) on $AppResourceGroup"
    Write-Host "  - grant Cognitive Services Contributor on the single account $OpenAiAccount, and a CONDITIONED role-assignment right on it (only Cognitive Services OpenAI User)"
    if (-not $SkipGitHub) {
        Write-Host "  - GitHub: environment '$EnvironmentName' on $Repository requiring approval by a named reviewer, main branch only,"
        Write-Host '    plus the variables AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID (ids, not secrets)'
    }
    Write-Host 'No password, key or secret is created anywhere.'
    return
}

$sp = (Graph GET "/servicePrincipals?`$filter=appId eq '$($app.appId)'").value | Select-Object -First 1
if (-not $sp) { Step 'create the service principal' { $script:sp = Graph POST '/servicePrincipals' @{ appId = $app.appId } } }

$credentials = (Graph GET "/applications/$($app.id)/federatedIdentityCredentials").value
$wanted = @{ name = 'github-environment'; issuer = 'https://token.actions.githubusercontent.com'; subject = $subject; audiences = @('api://AzureADTokenExchange'); description = "Deploy workflow, environment $EnvironmentName only" }
$existing = $credentials | Where-Object { $_.name -eq 'github-environment' }
if (-not $existing) { Step "trust GitHub ($subject)" { Graph POST "/applications/$($app.id)/federatedIdentityCredentials" $wanted | Out-Null } }
elseif ($existing.subject -ne $subject) { Step "correct the trusted subject to $subject" { Graph PATCH "/applications/$($app.id)/federatedIdentityCredentials/$($existing.id)" $wanted | Out-Null } }
else { Write-Host "Federated credential is in place ($subject)." }

# --- custom roles ----------------------------------------------------------------------------------------
Step "create or update custom role '$starterName' (subscription)" {
    Arm PUT "$subScope/providers/Microsoft.Authorization/roleDefinitions/$($starterId)?api-version=2022-04-01" @{ properties = @{
            roleName = $starterName; type = 'CustomRole'
            description = 'Start and read deployments and create or update resource groups. It cannot delete anything. Used by the Leyline deploy pipeline.'
            permissions = @(@{ actions = @('Microsoft.Resources/deployments/*', 'Microsoft.Resources/subscriptions/resourceGroups/read', 'Microsoft.Resources/subscriptions/resourceGroups/write'); notActions = @() })
            assignableScopes = @($subScope) } } | Out-Null
}
Step "create or update custom role '$deployerName' (resource group)" {
    Arm PUT "$subScope/providers/Microsoft.Authorization/roleDefinitions/$($deployerId)?api-version=2022-04-01" @{ properties = @{
            roleName = $deployerName; type = 'CustomRole'
            description = 'Contributor on the Leyline app resource group, but it cannot delete the resource group itself. Used by the Leyline deploy pipeline.'
            permissions = @(@{ actions = @('*'); notActions = @(
                    'Microsoft.Authorization/*/Delete', 'Microsoft.Authorization/*/Write', 'Microsoft.Authorization/elevateAccess/Action',
                    'Microsoft.Resources/subscriptions/resourceGroups/delete') })
            assignableScopes = @($subScope) } } | Out-Null
}

# --- role assignments ----------------------------------------------------------------------------------------
function Grant([string]$Scope, [string]$RoleDefinitionId, [string]$What, [string]$Condition = '') {
    $name = (New-DeterministicGuid "leyline-ci:${Scope}:${RoleDefinitionId}:$($sp.id)").Guid
    $properties = @{ roleDefinitionId = "$subScope/providers/Microsoft.Authorization/roleDefinitions/$RoleDefinitionId"; principalId = $sp.id; principalType = 'ServicePrincipal' }
    if ($Condition) { $properties.condition = $Condition; $properties.conditionVersion = '2.0' }
    Step "grant $What" {
        # A principal created a moment ago may not be visible everywhere yet: retry, then fail loudly.
        for ($attempt = 1; $attempt -le 8; $attempt++) {
            try { Arm PUT "$Scope/providers/Microsoft.Authorization/roleAssignments/$($name)?api-version=2022-04-01" @{ properties = $properties } | Out-Null; return }
            catch {
                $message = $_.Exception.Message
                if ($message -match 'already exists') { return }
                if ($attempt -eq 8) { throw "Could not grant $What : $message" }
                Start-Sleep -Seconds 6
            }
        }
    }
}

# Role assignments inside the app group may only be for the three roles the template uses.
$ids = $AssignableByCi -join ', '
$condition = "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {$ids})) AND ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {$ids}))"
# On the OpenAI account the only assignable role is Cognitive Services OpenAI User.
$openAiIds = $AssignableOnOpenAi -join ', '
$openAiCondition = "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {$openAiIds})) AND ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {$openAiIds}))"

if ($sp) {
    Grant $subScope $starterId "'$starterName' on the subscription"
    $groupExists = (& az group exists --name $AppResourceGroup) -eq 'true'
    if ($groupExists) {
        Grant $appScope $deployerId "'$deployerName' on $AppResourceGroup"
        Grant $appScope $RoleRbacAdministrator "role-assignment rights on $AppResourceGroup, limited to AcrPull, Key Vault Secrets User and Storage Blob Data Contributor" $condition
        Grant $appScope $RoleAcrPush "AcrPush on $AppResourceGroup"
    }
    else { Write-Warning "$AppResourceGroup does not exist now; run this script again after the environment is built." }
    Grant $openAiScope $RoleCognitiveServicesContributor "Cognitive Services Contributor on $OpenAiAccount"
    Grant $openAiScope $RoleRbacAdministrator "role-assignment rights on $OpenAiAccount, limited to Cognitive Services OpenAI User" $openAiCondition
}

# --- GitHub environment ---------------------------------------------------------------------------------------
if (-not $SkipGitHub) {
    if (-not $Reviewer) { $Reviewer = (& gh api user -q .login) }
    $reviewerId = (& gh api "users/$Reviewer" -q .id)
    if (-not $reviewerId) { throw "Could not find the GitHub user '$Reviewer'." }
    Step "create GitHub environment '$EnvironmentName' (approval by $Reviewer, main branch only)" {
        $body = @{ wait_timer = 0; prevent_self_review = $false; reviewers = @(@{ type = 'User'; id = [int]$reviewerId })
            deployment_branch_policy = @{ protected_branches = $false; custom_branch_policies = $true } } | ConvertTo-Json -Depth 6 -Compress
        $file = Join-Path ([System.IO.Path]::GetTempPath()) ("env-{0}.json" -f [guid]::NewGuid())
        try {
            [System.IO.File]::WriteAllText($file, $body)
            & gh api -X PUT "repos/$Repository/environments/$EnvironmentName" --input $file | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Could not create the GitHub environment.' }
            $policies = (& gh api "repos/$Repository/environments/$EnvironmentName/deployment-branch-policies" -q '.branch_policies[].name') -split "`n"
            if ($policies -notcontains 'main') { & gh api -X POST "repos/$Repository/environments/$EnvironmentName/deployment-branch-policies" -f name=main -f type=branch | Out-Null }
        }
        finally { Remove-Item $file -Force -ErrorAction SilentlyContinue -WhatIf:$false }
    }
    foreach ($pair in @(@('AZURE_CLIENT_ID', $app.appId), @('AZURE_TENANT_ID', $tenant), @('AZURE_SUBSCRIPTION_ID', $sub))) {
        Step "set environment variable $($pair[0]) (an id, not a secret)" { & gh variable set $pair[0] --env $EnvironmentName --body $pair[1] --repo $Repository | Out-Null }
    }
}

Write-Host ''
Write-Host "Client id       : $($app.appId)   (not a secret)"
Write-Host "Trusted subject : $subject"
Write-Host 'Next: GitHub > Actions > "Deploy to Azure" > Run workflow (action = preview first).'
