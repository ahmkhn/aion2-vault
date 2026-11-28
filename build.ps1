# Builds release\AION2Vault-v<version>-win64.zip  (run from this folder: .\build.ps1)
#   -SkipItemDb   reuse the existing web\itemdb.json + icons instead of refreshing them
param([switch]$SkipItemDb)
Set-Location $PSScriptRoot

$version = (Select-String -Path prices.py -Pattern '^VERSION = "(.+)"').Matches[0].Groups[1].Value
Write-Host "Building AION 2 Vault $version"

python -m pip install --quiet --disable-pip-version-check -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
python test_protocol.py
if ($LASTEXITCODE -ne 0) { throw "decoder self-test failed" }
python tools\make_icon.py web\icon.ico

# item names + 64 px icons (incremental; the first run takes ~20 min, later runs seconds)
if (-not $SkipItemDb) {
    python -u tools\build_itemdb.py
    if ($LASTEXITCODE -ne 0) { throw "item database build failed" }
}

# build in a short path: deep project folders hit Windows' 260-character path limit
$work = Join-Path $env:TEMP "a2v-build"
python -m PyInstaller --noconfirm --clean --windowed `
    --name AION2Vault `
    --icon "$PSScriptRoot\web\icon.ico" `
    --add-data "$PSScriptRoot\web;web" `
    --exclude-module scapy --exclude-module PIL `
    --workpath "$work\work" --distpath "$work\dist" --specpath "$work" `
    app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# users get the README without the maintainer (build) section
$readme = (Get-Content README.md -Raw -Encoding UTF8) -replace "`r`n", "`n"   # git may check out CRLF
$cut = $readme.IndexOf("`n---`n")
if ($cut -gt 0) { $readme = $readme.Substring(0, $cut).TrimEnd() + "`n" }
$readme = $readme -replace "(?m)^!\[[^\]]*\]\(docs/[^)]*\)\n\n?", ""   # screenshots aren't in the zip
[IO.File]::WriteAllText("$work\dist\AION2Vault\README.md", $readme, (New-Object Text.UTF8Encoding $false))
python tools\collect_licenses.py "$work\dist\AION2Vault\THIRD-PARTY-NOTICES.txt"
if ($LASTEXITCODE -ne 0) { throw "license collection failed" }
if (Get-ChildItem "$work\dist\AION2Vault" -Recurse -Filter "*scapy*") { throw "scapy (GPL) ended up in the bundle" }
New-Item -ItemType Directory -Force release | Out-Null
$zip = "release\AION2Vault-v$version-win64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "$work\dist\AION2Vault" -DestinationPath $zip
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash
"$hash  $(Split-Path $zip -Leaf)" | Out-File -Encoding ascii "$zip.sha256"
Write-Host "Done: $zip"
Write-Host "SHA256: $hash"
