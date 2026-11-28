# Reproduce the v0.0.2 "CERTIFICATE_VERIFY_FAILED" error on a PC that doesn't have it.
#
# aion2.exchange's chain is:  aion2.exchange -> YE1 -> Root YE -> ISRG Root X2 -> ISRG Root X1
# v0.0.2 only trusts certificates already in Windows' stores, so it works if ANY of those
# four is present. This script temporarily removes all four (after backing them up), so
# you can see v0.0.2 fail and v0.0.3 recover (Windows re-downloads what it needs).
#
#   Run in an ADMIN PowerShell:
#     .\repro-cert-error.ps1 -Remove     back up, then remove the four certificates
#     .\repro-cert-error.ps1 -Restore    put back anything that's missing (safe to run twice)
#     .\repro-cert-error.ps1             show where they currently are
#
# Removing from the CurrentUser Root store makes Windows show a confirmation dialog per
# certificate: click Yes. Restoring into it shows a security warning: click Yes.
param([switch]$Remove, [switch]$Restore)

$Backup = Join-Path $env:USERPROFILE "Desktop\isrg-cert-backup"
$Names = '^CN=(ISRG Root X1|ISRG Root X2|Root YE|YE1),'
$Stores = @("LocalMachine\Root", "LocalMachine\AuthRoot", "LocalMachine\CA",
            "CurrentUser\Root", "CurrentUser\AuthRoot", "CurrentUser\CA")

function Find-Certs {
    foreach ($s in $Stores) {
        Get-ChildItem "Cert:\$s" -ErrorAction SilentlyContinue | Where-Object { $_.Subject -match $Names } |
            ForEach-Object { [pscustomobject]@{ Store = $s; Name = ($_.Subject -split ',')[0].Substring(3); Cert = $_ } }
    }
}

function Open-Store($path, $mode) {
    $loc, $name = $path -split '\\'
    $st = New-Object Security.Cryptography.X509Certificates.X509Store($name, $loc)
    $st.Open($mode)
    return $st
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (($Remove -or $Restore) -and -not $isAdmin) { throw "Run this in an administrator PowerShell." }

if ($Remove) {
    $found = @(Find-Certs)
    if (-not $found) { Write-Host "None of the four certificates are present; nothing to remove."; return }
    New-Item -ItemType Directory -Force $Backup | Out-Null
    foreach ($f in $found) {
        $file = Join-Path $Backup ("{0}__{1}__{2}.cer" -f ($f.Store -replace '\\', '_'), $f.Name, $f.Cert.Thumbprint)
        [IO.File]::WriteAllBytes($file, $f.Cert.RawData)
    }
    Write-Host "Backed up $($found.Count) certificate(s) to $Backup"
    foreach ($f in $found) {
        $st = Open-Store $f.Store "ReadWrite"
        $st.Remove($f.Cert); $st.Close()
        Write-Host "  removed $($f.Name) from $($f.Store)"
    }
    $left = @(Find-Certs)
    Write-Host ""
    if ($left) { Write-Host "Still present (a dialog may have been declined):"; $left | ForEach-Object { "  $($_.Name) in $($_.Store)" } }
    else { Write-Host "Done. Now run v0.0.2: prices should fail with CERTIFICATE_VERIFY_FAILED. Then try v0.0.3." }
    Write-Host "Restore afterwards with:  .\repro-cert-error.ps1 -Restore"
    return
}

if ($Restore) {
    if (-not (Test-Path $Backup)) { throw "No backup folder at $Backup" }
    # machine-wide stores first: the CurrentUser view also shows machine certificates,
    # so restoring those first avoids adding duplicate per-user copies
    $files = Get-ChildItem $Backup -Filter *.cer | Sort-Object { if ($_.Name -like 'LocalMachine*') { 0 } else { 1 } }, Name
    foreach ($file in $files) {
        $storePart, $name, $thumb = $file.BaseName -split '__'
        $store = $storePart -replace '_', '\'
        $cert = New-Object Security.Cryptography.X509Certificates.X509Certificate2($file.FullName)
        $st = Open-Store $store "ReadWrite"
        if ($st.Certificates.Find("FindByThumbprint", $cert.Thumbprint, $false).Count) {
            Write-Host "  $name already in $store"
        } else {
            $st.Add($cert); Write-Host "  restored $name to $store"
        }
        $st.Close()
    }
    Write-Host "Restore finished. The backup stays in $Backup (delete it whenever you like)."
    return
}

$now = @(Find-Certs)
if ($now) { $now | ForEach-Object { "{0,-14} {1}" -f $_.Name, $_.Store } } else { "None of the four certificates are present." }
