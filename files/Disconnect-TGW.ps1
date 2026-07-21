# --- 1. CONFIGURATION ---
$nsxManager = "10.1.1.20"
$user       = "admin"
$credsPath  = "/home/holuser/creds.txt"

# --- 2. AUTOMATED CREDENTIAL RETRIEVAL (STRICT SCRUB) ---
if (Test-Path $credsPath) {
    # -Encoding utf8 handles the BOM; -Raw treats the file as one string
    $rawContent = Get-Content -Path $credsPath -Raw -Encoding utf8
    
    # Regex '\S+' matches only the first block of non-whitespace characters
    # This effectively deletes the '10' (Line Feed) and any trailing spaces
    if ($rawContent -match '(\S+)') {
        $passwordPlain = $matches[1]
    } else {
        Write-Host "ERROR: Credential file is empty or contains only whitespace." -ForegroundColor Red
        exit
    }
}
else {
    Write-Host "ERROR: Credential file not found at $credsPath" -ForegroundColor Red
    exit
}

# --- 3. PREPARE AUTH ---
# Unified to UTF8 for consistent Base64 generation
$pair = "${user}:${passwordPlain}"
$bytes = [System.Text.Encoding]::UTF8.GetBytes($pair)
$base64 = [Convert]::ToBase64String($bytes)

$authHeader = @{
    "Authorization" = "Basic $base64"
    "Content-Type"  = "application/json"
}

# --- 4. EXECUTION: DISCONNECT TGW ATTACHMENTS ---
$tgwBaseUrl = "https://$nsxManager/policy/api/v1/orgs/default/projects/default/transit-gateways/default/attachments"

try {
    Write-Host "--- Task 1: Disconnecting TGW Attachments ---" -ForegroundColor Cyan
    $response = Invoke-RestMethod -Uri $tgwBaseUrl -Method Get -Headers $authHeader -SkipCertificateCheck -NoProxy
    
    if ($response.results.Count -eq 0) {
        Write-Host "No active attachments found to disconnect from TGW." -ForegroundColor Yellow
    }
    else {
        foreach ($attachment in $response.results) {
            $id = $attachment.id
            $name = $attachment.display_name
            Write-Host "Disconnecting: $name (ID: $id) from TGW" -ForegroundColor White
            Invoke-RestMethod -Uri "$tgwBaseUrl/$id" -Method Delete -Headers $authHeader -SkipCertificateCheck -NoProxy
            Write-Host "Successfully removed $name." -ForegroundColor Green
        }
    }
}
catch {
    Write-Host "ERROR: TGW Attachment disconnection failed: $($_.Exception.Message)" -ForegroundColor Red
}

# --- 5. ENHANCEMENT: CLEAR VPC CONNECTIVITY PROFILE BLOCKS (WITH SAFETY CHECK) ---
$vpcProfileUrl = "https://$nsxManager/policy/api/v1/orgs/default/projects/default/vpc-connectivity-profiles/default"

try {
    Write-Host "`n--- Task 2: Clearing VPC Connectivity Profile IP Blocks ---" -ForegroundColor Cyan
    
    # SAFETY CHECK: Verify the profile exists before patching
    Write-Host "Checking for VPC connectivity profile existence..." -ForegroundColor Gray
    $checkProfile = Invoke-RestMethod -Uri $vpcProfileUrl -Method Get -Headers $authHeader -SkipCertificateCheck -NoProxy
    
    if ($null -ne $checkProfile) {
        $vpcBody = @{
            "transit_gateway_path"  = "/orgs/default/projects/default/transit-gateways/default"
            "external_ip_blocks"    = @()
            "private_tgw_ip_blocks" = @()
        } | ConvertTo-Json

        Write-Host "Profile found. Patching to clear IP blocks..." -ForegroundColor White
        Invoke-RestMethod -Uri $vpcProfileUrl -Method Patch -Headers $authHeader -Body $vpcBody -SkipCertificateCheck -NoProxy
        Write-Host "Successfully cleared IP blocks." -ForegroundColor Green
    }
}
catch {
    if ($_.Exception.Response.StatusCode.value__ -eq 404) {
        Write-Host "SKIPPING: VPC connectivity profile 'default' was not found." -ForegroundColor Yellow
    } else {
        Write-Host "ERROR: Failed to update VPC connectivity profile: $($_.Exception.Message)" -ForegroundColor Red
    }
}

# --- 6. ENHANCEMENT: REMOVE DISTRIBUTED VLAN CONNECTIONS ---
$vlanConnUrl = "https://$nsxManager/policy/api/v1/infra/distributed-vlan-connections"

try {
    Write-Host "`n--- Task 3: Removing Distributed VLAN Connections ---" -ForegroundColor Cyan
    $vlanResponse = Invoke-RestMethod -Uri $vlanConnUrl -Method Get -Headers $authHeader -SkipCertificateCheck -NoProxy

    if ($vlanResponse.results.Count -eq 0) {
        Write-Host "No Distributed VLAN connections found." -ForegroundColor Yellow
    }
    else {
        foreach ($vlanConn in $vlanResponse.results) {
            $vlanId = $vlanConn.id
            Write-Host "Deleting VLAN Connection: $vlanId..." -ForegroundColor White
            Invoke-RestMethod -Uri "$vlanConnUrl/$vlanId" -Method Delete -Headers $authHeader -SkipCertificateCheck -NoProxy
            Write-Host "Successfully deleted." -ForegroundColor Green
        }
    }
}
catch {
    Write-Host "ERROR: Failed to remove Distributed VLAN connections: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host "`nAll Tasks Complete." -ForegroundColor White
Read-Host -Prompt "Press Enter to close"
