<#
.SYNOPSIS
  Registers Leyline in Microsoft Entra ID: the app, its four app roles, four security groups
  for document access, a client secret, and (optionally) demo users.

.DESCRIPTION
  Safe to run again: everything is looked up by name first. Use -WhatIf to see what it would
  change without changing anything.

  What it creates in your directory:
    * app registration "Leyline Helpdesk" (single tenant) and its service principal, with sign-in
      limited to assigned users and groups
    * app roles user, kb_admin, support_agent, security_auditor (the token's "roles" claim)
    * security groups "Leyline staff", "Leyline security", "Leyline hr", "Leyline finance"
      (the token's "groups" claim carries their object ids; deploy maps them to the names
      documents use)
    * a client secret, kept encrypted for your Windows account in infra/.entra.local.json
      (never printed, never in git; deploy.ps1 reads it)
    * with -DemoUsers: seven demo users like the local Keycloak ones; their passwords go to
      infra/.entra-demo-users.local.txt (git-ignored), never to the screen

  -Remove deletes all of it again (including demo users).

.EXAMPLE
  ./infra/entra.ps1 -WhatIf
  ./infra/entra.ps1 -RedirectUri https://leyline-web.<domain>/auth/callback
  ./infra/entra.ps1 -DemoUsers
  ./infra/entra.ps1 -DemoUsers -ResetPasswords   # also new passwords for demo users that exist
  ./infra/entra.ps1 -Remove
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # Pass 1 of deploy.ps1 prints it. Keep the local address so local sign-in tests still work.
    [string]$RedirectUri = '',
    [switch]$DemoUsers,
    # With -DemoUsers: also give demo users that already exist a new password (use it when the
    # password file is lost). Passwords go to the git-ignored file, never to the screen.
    [switch]$ResetPasswords,
    [switch]$AssignMe,
    [switch]$NewSecret,
    [switch]$Remove,
    # Not 'Leyline': an older registration of that name from the first lab exists and is left alone.
    [string]$AppName = 'Leyline Helpdesk',
    [string]$GroupPrefix = 'Leyline '
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$stateFile = Join-Path $here '.entra.local.json'
$usersFile = Join-Path $here '.entra-demo-users.local.txt'

# Fixed ids: an app role keeps its id forever, or existing assignments would break.
$roles = @(
    @{ id = '9db7f144-7111-4564-8504-988a3d5e5bb8'; value = 'user';             name = 'User';             description = 'Ask questions and raise tickets' },
    @{ id = '397a6752-c49c-41ed-a243-cc102dda4ee1'; value = 'kb_admin';         name = 'Knowledge admin';  description = 'Manage knowledge-base documents' },
    @{ id = '0dfefcc7-d513-4262-99bc-186580885f96'; value = 'support_agent';    name = 'Support agent';    description = 'Work escalated helpdesk tickets' },
    @{ id = '8836b8cf-f0c7-4b72-ae2c-0e96c95cdd60'; value = 'security_auditor'; name = 'Security auditor'; description = 'View security monitoring and audit data' }
)
# document ACL group name -> directory group
$groupNames = @('staff', 'security', 'hr', 'finance')

# demo user -> display name, document groups, app roles (the same personas as local Keycloak)
$personas = @(
    @{ user = 'alice.employee'; display = 'Alice Employee'; groups = @('staff');             roles = @('user') },
    @{ user = 'sam.security';   display = 'Sam Security';   groups = @('staff', 'security'); roles = @('user') },
    @{ user = 'ada.auditor';    display = 'Ada Auditor';    groups = @('staff');             roles = @('user', 'security_auditor') },
    @{ user = 'kim.admin';      display = 'Kim Admin';      groups = @('staff');             roles = @('user', 'kb_admin') },
    @{ user = 'sue.agent';      display = 'Sue Agent';      groups = @('staff');             roles = @('user', 'support_agent') },
    @{ user = 'hank.hr';        display = 'Hank HR';        groups = @('staff', 'hr');       roles = @('user') },
    @{ user = 'fiona.finance';  display = 'Fiona Finance';  groups = @('staff', 'finance');  roles = @('user') }
)

function Invoke-Az {
    $output = & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args[0..2] -join ' ') failed (exit code $LASTEXITCODE)" }
    $output
}

function Invoke-Graph([string]$Method, [string]$Path, $Body = $null) {
    $WhatIfPreference = $false  # every change is guarded by Step, so -WhatIf must not hit the stderr redirect below
    $a = @('rest', '--method', $Method, '--url', "https://graph.microsoft.com/v1.0$Path", '--headers', 'Content-Type=application/json')
    $tmp = $null
    if ($null -ne $Body) {
        $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("entra-{0}.json" -f [guid]::NewGuid())
        [System.IO.File]::WriteAllText($tmp, ($Body | ConvertTo-Json -Depth 10 -Compress))
        $a += @('--body', "@$tmp")
    }
    $errFile = Join-Path ([System.IO.Path]::GetTempPath()) ("entra-err-{0}.txt" -f [guid]::NewGuid())
    try {
        $out = & az @a 2> $errFile
        if ($LASTEXITCODE -ne 0) {
            $detail = if (Test-Path $errFile) { (Get-Content $errFile -Raw) } else { '' }
            throw "Graph $Method $Path failed: $detail"
        }
        if ($out) { return ($out -join "`n" | ConvertFrom-Json) }
    }
    finally {
        foreach ($f in @($tmp, $errFile)) { if ($f -and (Test-Path $f)) { Remove-Item $f -Force -WhatIf:$false } }
    }
}

$script:failures = @()

function Invoke-GraphRetry([string]$Description, [string]$Method, [string]$Path, $Body = $null) {
    # A user created a moment ago is not yet known to every Entra replica, so adding it to a
    # group or giving it a role can fail at first. Retry; treat "already there" as success;
    # report anything else (it used to be swallowed, leaving users with no role or group).
    for ($attempt = 1; $attempt -le 8; $attempt++) {
        try { Invoke-Graph $Method $Path $Body | Out-Null; return $true }
        catch {
            $message = $_.Exception.Message
            if ($message -match 'already exist|already been assigned|references already exist') { return $true }
            if ($attempt -eq 8) {
                Write-Warning "$Description failed: $message"
                $script:failures += $Description
                return $false
            }
            Start-Sleep -Seconds 5
        }
    }
}

function New-DemoPassword {
    $chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'.ToCharArray()
    $bytes = New-Object byte[] 20
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    (-join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })) + 'a1!'
}

function Step([string]$What, [scriptblock]$Do) {
    # Every change goes through here so -WhatIf shows it and skips it.
    if ($PSCmdlet.ShouldProcess($What)) { & $Do }
}

function Find-App { Invoke-Graph GET "/applications?`$filter=displayName eq '$AppName'&`$select=id,appId,displayName" | Select-Object -ExpandProperty value | Select-Object -First 1 }
function Find-Group([string]$display) { Invoke-Graph GET "/groups?`$filter=displayName eq '$display'&`$select=id,displayName" | Select-Object -ExpandProperty value | Select-Object -First 1 }

# --- who and where ------------------------------------------------------------------------
$account = (Invoke-Az account show --query '{tenant:tenantId,user:user.name}' -o json) -join "`n" | ConvertFrom-Json
$me = Invoke-Graph GET '/me?$select=id,userPrincipalName'
$domain = (Invoke-Graph GET '/domains' | Select-Object -ExpandProperty value | Where-Object { $_.isDefault }).id
Write-Host "Tenant: $($account.tenant)  Signed in as: $($me.userPrincipalName)  Default domain: $domain"

# --- remove -----------------------------------------------------------------------------------
if ($Remove) {
    foreach ($p in $personas) {
        $upn = "$($p.user)@$domain"
        $u = Invoke-Graph GET "/users?`$filter=userPrincipalName eq '$upn'&`$select=id" | Select-Object -ExpandProperty value | Select-Object -First 1
        if ($u) { Step "delete demo user $upn" { Invoke-Graph DELETE "/users/$($u.id)" | Out-Null } }
    }
    foreach ($g in $groupNames) {
        $grp = Find-Group "$GroupPrefix$g"
        if ($grp) { Step "delete group $GroupPrefix$g" { Invoke-Graph DELETE "/groups/$($grp.id)" | Out-Null } }
    }
    $app = Find-App
    if ($app) { Step "delete app registration $AppName" { Invoke-Graph DELETE "/applications/$($app.id)" | Out-Null } }
    foreach ($f in $stateFile, $usersFile) { if (Test-Path $f) { Step "delete $f" { Remove-Item $f -Force } } }
    Write-Host 'Removed.'
    return
}

# --- app registration ---------------------------------------------------------------------------
$redirects = @('http://localhost:8000/auth/callback', 'http://127.0.0.1:8000/auth/callback')
if ($RedirectUri) {
    if ($RedirectUri -notmatch '^https://[a-z0-9.-]+/auth/callback$') { throw 'RedirectUri must look like https://<host>/auth/callback' }
    $redirects += $RedirectUri
}

$appRoles = $roles | ForEach-Object {
    @{ id = $_.id; value = $_.value; displayName = $_.name; description = $_.description
       allowedMemberTypes = @('User'); isEnabled = $true; origin = 'Application' }
}
$settings = @{
    displayName            = $AppName
    signInAudience         = 'AzureADMyOrg'
    groupMembershipClaims  = 'SecurityGroup'
    appRoles               = $appRoles
    web                    = @{ redirectUris = $redirects; implicitGrantSettings = @{ enableIdTokenIssuance = $false; enableAccessTokenIssuance = $false } }
    optionalClaims         = @{ idToken = @(@{ name = 'email'; essential = $false }) }
}

$app = Find-App
if ($app) {
    Write-Host "App registration exists: $($app.appId)"
    # Keep the addresses that are already registered (for example the Azure one added by
    # deploy.ps1): replacing the list silently broke the Azure sign-in once.
    $registered = (Invoke-Graph GET "/applications/$($app.id)").web.redirectUris
    $settings.web.redirectUris = @($registered) + @($redirects) | Where-Object { $_ } | Select-Object -Unique
    Step "update app registration $AppName (roles, redirect URIs, claims)" {
        # An existing role may only be re-sent unchanged; ours keep fixed ids.
        Invoke-Graph PATCH "/applications/$($app.id)" $settings | Out-Null
    }
}
else {
    Step "create app registration $AppName" {
        $script:app = Invoke-Graph POST '/applications' $settings
        Write-Host "Created app registration: $($app.appId)"
    }
}
if (-not $app) { Write-Host 'Preview ends here: the rest depends on the app existing.'; return }

$sp = Invoke-Graph GET "/servicePrincipals?`$filter=appId eq '$($app.appId)'&`$select=id" | Select-Object -ExpandProperty value | Select-Object -First 1
if (-not $sp) {
    Step 'create the service principal' { $script:sp = Invoke-Graph POST '/servicePrincipals' @{ appId = $app.appId } }
}
if ($sp) {
    # Only users assigned to the app can sign in: a stranger in the directory is refused by Entra.
    Step 'require assignment to sign in' { Invoke-Graph PATCH "/servicePrincipals/$($sp.id)" @{ appRoleAssignmentRequired = $true } | Out-Null }
}

# --- groups ------------------------------------------------------------------------------------------
$groupIds = [ordered]@{}
foreach ($g in $groupNames) {
    $display = "$GroupPrefix$g"
    $grp = Find-Group $display
    if (-not $grp) {
        Step "create security group '$display'" {
            $script:grp = Invoke-Graph POST '/groups' @{ displayName = $display; mailNickname = "leyline-$g"; mailEnabled = $false; securityEnabled = $true; description = "Leyline document access: $g" }
        }
    }
    if ($grp) { $groupIds[$grp.id] = $g }
}

# --- client secret (encrypted to your Windows account, never printed) ---------------------------------------
$state = @{}
if (Test-Path $stateFile) {
    $saved = Get-Content $stateFile -Raw | ConvertFrom-Json
    foreach ($property in $saved.PSObject.Properties) { $state[$property.Name] = $property.Value }
}
$needSecret = $NewSecret -or -not $state.ContainsKey('clientSecret')
if ($needSecret) {
    Step 'create a client secret (valid 180 days)' {
        $end = (Get-Date).AddDays(180).ToString('yyyy-MM-ddTHH:mm:ssZ')
        $cred = Invoke-Graph POST "/applications/$($app.id)/addPassword" @{ passwordCredential = @{ displayName = "leyline-$(Get-Date -Format yyyyMMdd)"; endDateTime = $end } }
        # DPAPI: only this Windows account on this machine can decrypt the stored value.
        $state['clientSecret'] = ConvertTo-SecureString $cred.secretText -AsPlainText -Force | ConvertFrom-SecureString
        $state['secretExpires'] = $end
    }
}
$state['tenantId'] = $account.tenant
$state['clientId'] = $app.appId
$state['groupNames'] = ($groupIds | ConvertTo-Json -Compress)
Step "write $stateFile" {
    [System.IO.File]::WriteAllText($stateFile, ($state | ConvertTo-Json))
    & icacls $stateFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
}

# --- you, and optionally demo users ------------------------------------------------------------------------
function Add-Assignments($userId, [string[]]$roleValues) {
    foreach ($rv in $roleValues) {
        $r = $roles | Where-Object { $_.value -eq $rv }
        Invoke-GraphRetry "role $rv for user $userId" POST "/users/$userId/appRoleAssignments" @{ principalId = $userId; resourceId = $sp.id; appRoleId = $r.id } | Out-Null
    }
}
if ($AssignMe -and $sp) {
    Step "give $($me.userPrincipalName) every app role" { Add-Assignments $me.id ($roles | ForEach-Object { $_.value }) }
}

if ($DemoUsers -and $sp) {
    $lines = @('Leyline demo users for Microsoft Entra. Keep this file private; delete it with: ./infra/entra.ps1 -Remove', '')
    foreach ($p in $personas) {
        $upn = "$($p.user)@$domain"
        $existing = Invoke-Graph GET "/users?`$filter=userPrincipalName eq '$upn'&`$select=id" | Select-Object -ExpandProperty value | Select-Object -First 1
        if (-not $existing) {
            $password = New-DemoPassword
            Step "create demo user $upn" {
                $script:existing = Invoke-Graph POST '/users' @{
                    accountEnabled = $true; displayName = $p.display; mailNickname = $p.user; userPrincipalName = $upn
                    passwordProfile = @{ forceChangePasswordNextSignIn = $false; password = $password }
                }
                $script:lines += "$upn  $password"
            }
        }
        elseif ($ResetPasswords) {
            $password = New-DemoPassword
            Step "set a new password for $upn" {
                Invoke-Graph PATCH "/users/$($existing.id)" @{ passwordProfile = @{ forceChangePasswordNextSignIn = $false; password = $password } } | Out-Null
                $script:lines += "$upn  $password"
            }
        }
        if ($existing) {
            foreach ($g in $p.groups) {
                $gid = ($groupIds.GetEnumerator() | Where-Object { $_.Value -eq $g }).Key
                if ($gid) { Step "add $upn to $GroupPrefix$g" { Invoke-GraphRetry "membership of $upn in $GroupPrefix$g" POST "/groups/$gid/members/`$ref" @{ '@odata.id' = "https://graph.microsoft.com/v1.0/directoryObjects/$($existing.id)" } | Out-Null } }
            }
            Step "assign roles $($p.roles -join ', ') to $upn" { Add-Assignments $existing.id $p.roles }
        }
    }
    if ($lines.Count -gt 2) {
        Step "write demo user passwords to $usersFile" {
            [System.IO.File]::WriteAllLines($usersFile, $lines)
            & icacls $usersFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
        }
        Write-Host "Demo user passwords were written to $usersFile (not shown here)."
    }

    if (-not $WhatIfPreference) {
        Write-Host 'Verifying the demo users (groups and roles as Entra reports them)...'
        $roleName = @{}
        foreach ($r in $roles) { $roleName[$r.id] = $r.value }
        for ($round = 1; $round -le 4; $round++) {
            $assigned = (Invoke-Graph GET "/servicePrincipals/$($sp.id)/appRoleAssignedTo").value
            $members = @{}
            foreach ($entry in $groupIds.GetEnumerator()) {
                $members[$entry.Value] = @((Invoke-Graph GET "/groups/$($entry.Key)/members").value | ForEach-Object { $_.userPrincipalName })
            }
            $problems = @()
            foreach ($p in $personas) {
                $upn = "$($p.user)@$domain"
                $haveGroups = @($groupNames | Where-Object { $members[$_] -contains $upn })
                $haveRoles = @($assigned | Where-Object { $_.principalDisplayName -eq $p.display } | ForEach-Object { $roleName[$_.appRoleId] })
                $missingGroups = @($p.groups | Where-Object { $haveGroups -notcontains $_ })
                $missingRoles = @($p.roles | Where-Object { $haveRoles -notcontains $_ })
                if ($missingGroups -or $missingRoles) { $problems += "$($p.user): missing groups [$($missingGroups -join ',')] roles [$($missingRoles -join ',')]" }
                elseif ($round -eq 1 -or -not $problems) { Write-Host ("  ok  {0,-15} groups=[{1}] roles=[{2}]" -f $p.user, (($haveGroups | Sort-Object) -join ','), (($haveRoles | Sort-Object) -join ',')) }
            }
            if (-not $problems) { break }
            if ($round -lt 4) { Start-Sleep -Seconds 10 }
        }
        if ($problems) { foreach ($line in $problems) { Write-Warning $line }; $script:failures += $problems }
    }
}

if ($script:failures.Count -gt 0) {
    Write-Error "Some assignments are missing (see the warnings above). Run this script again: it only fills in what is missing."
    exit 1
}

Write-Host ''
Write-Host "Client id : $($app.appId)"
Write-Host "Tenant id : $($account.tenant)"
Write-Host "Groups    : $(($groupIds.GetEnumerator() | ForEach-Object { "$($_.Value)=$($_.Key)" }) -join ', ')"
if (-not $RedirectUri) { Write-Host 'No -RedirectUri given: only local redirect URIs are registered. Add the Azure one after pass 1 of deploy.ps1.' -ForegroundColor Yellow }
