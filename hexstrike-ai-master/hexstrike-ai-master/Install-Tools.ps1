# HexStrike AI - portable tool installer
# Downloads the security tool binaries into <this folder>\tools\bin,
# a small ffuf wordlist into <this folder>\tools\wordlists,
# and registers tools\bin + hexstrike-env\Scripts on the user PATH.
# Safe to re-run (existing tools are skipped).

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bin  = Join-Path $root "tools\bin"
$wl   = Join-Path $root "tools\wordlists"
New-Item -ItemType Directory -Force -Path $bin, $wl | Out-Null

function Get-ReleaseTool($repo, $assetPattern, $exeName) {
    $dest = Join-Path $bin $exeName
    if (Test-Path $dest) { "skip $exeName (already there)"; return }
    try {
        $rel = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" -Headers @{"User-Agent"="hexstrike-setup"}
        $asset = $rel.assets | Where-Object { $_.name -match $assetPattern } | Select-Object -First 1
        if (-not $asset) { "MISS $repo (no asset match)"; return }
        $tmp = Join-Path $env:TEMP $asset.name
        Invoke-WebRequest $asset.browser_download_url -OutFile $tmp
        $x = Join-Path $env:TEMP ("hsx-" + [guid]::NewGuid().ToString("n"))
        New-Item -ItemType Directory -Force -Path $x | Out-Null
        if ($asset.name -match '\.zip$') { Expand-Archive $tmp -DestinationPath $x -Force }
        elseif ($asset.name -match '\.tar\.gz$') { tar -xzf $tmp -C $x }
        else { Copy-Item $tmp $x -Force }
        $exe = Get-ChildItem -Recurse $x -Filter $exeName -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($exe) { Copy-Item $exe.FullName $dest -Force; "OK   $exeName" } else { "WARN $repo extracted, $exeName not found" }
        Remove-Item -Recurse -Force $x, $tmp -ErrorAction SilentlyContinue
    } catch { "FAIL $repo : $($_.Exception.Message)" }
}

Get-ReleaseTool "ffuf/ffuf" "windows_amd64\.zip$" "ffuf.exe"
Get-ReleaseTool "OJ/gobuster" "Windows_x86_64\.zip$" "gobuster.exe"
Get-ReleaseTool "projectdiscovery/nuclei" "windows_amd64\.zip$" "nuclei.exe"
Get-ReleaseTool "projectdiscovery/katana" "windows_amd64\.zip$" "katana.exe"
Get-ReleaseTool "projectdiscovery/naabu" "windows_amd64\.zip$" "naabu.exe"
Get-ReleaseTool "projectdiscovery/dnsx" "windows_amd64\.zip$" "dnsx.exe"
Get-ReleaseTool "projectdiscovery/httpx" "windows_amd64\.zip$" "httpx.exe"
Get-ReleaseTool "projectdiscovery/subfinder" "windows_amd64\.zip" "subfinder.exe"
Get-ReleaseTool "hahwul/dalfox" "windows-x86_64\.zip$" "dalfox.exe"
Get-ReleaseTool "tomnomnom/waybackurls" "windows-amd64.*\.zip$" "waybackurls.exe"
Get-ReleaseTool "lc/gau" "windows_amd64\.zip$" "gau.exe"
Get-ReleaseTool "tomnomnom/qsreplace" "windows-amd64.*\.zip$" "qsreplace.exe"
Get-ReleaseTool "RustScan/RustScan" "windows|win" "rustscan.exe"
Get-ReleaseTool "aquasecurity/trivy" "Windows-64bit\.zip$" "trivy.exe"
Get-ReleaseTool "tenable/terrascan" "Windows_x86_64\.zip$" "terrascan.exe"

# amass ships tar.gz with the exe inside
Get-ReleaseTool "owasp-amass/amass" "windows_amd64\.tar\.gz$" "amass.exe"

# hashcat (official site, 7z archive)
if (-not (Test-Path (Join-Path $bin "hashcat.exe"))) {
    try {
        $hc = Join-Path $env:TEMP "hashcat.7z"
        Invoke-WebRequest "https://hashcat.net/files/hashcat-6.2.6.7z" -OutFile $hc
        $x = Join-Path $env:TEMP ("hsx-hc-" + [guid]::NewGuid().ToString("n"))
        New-Item -ItemType Directory -Force -Path $x | Out-Null
        tar -xf $hc -C $x
        $exe = Get-ChildItem -Recurse $x -Filter "hashcat.exe" | Select-Object -First 1
        if ($exe) { Copy-Item $exe.FullName (Join-Path $bin "hashcat.exe") -Force; "OK   hashcat.exe" }
        Remove-Item -Recurse -Force $x, $hc -ErrorAction SilentlyContinue
    } catch { "FAIL hashcat : $($_.Exception.Message)" }
} else { "skip hashcat.exe (already there)" }

# john the ripper (openwall john-packages)
Get-ReleaseTool "openwall/john-packages" "winX64.*\.zip$" "john.exe"

# ffuf wordlist (SecLists common.txt)
$common = Join-Path $wl "common.txt"
if (-not (Test-Path $common)) {
    try {
        Invoke-WebRequest "https://raw.githubusercontent.com/danielmiessler/SecLists/master/Discovery/Web-Content/common.txt" -OutFile $common
        "OK   wordlist common.txt"
    } catch { "FAIL wordlist : $($_.Exception.Message)" }
} else { "skip wordlist (already there)" }

# PATH: tools\bin + hexstrike-env\Scripts (user scope, dedup)
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$adds = @($bin, (Join-Path $root "hexstrike-env\Scripts"))
$newPath = $userPath
foreach ($a in $adds) { if ($newPath -notlike "*$a*") { $newPath = "$newPath;$a" } }
[Environment]::SetEnvironmentVariable("Path", $newPath, "User")
"PATH updated (user scope): $bin + hexstrike-env\Scripts"
""
"Done. RESTART any running HexStrike server so it sees the new tools."
