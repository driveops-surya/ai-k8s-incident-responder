$ErrorActionPreference = 'Stop'

$templatePath = Join-Path $PSScriptRoot 'cluster.example.yaml'
$outputPath = Join-Path $PSScriptRoot 'cluster.local.yaml'
if (Test-Path -LiteralPath $outputPath) {
    throw "Local cluster configuration already exists; review it before replacing it: $outputPath"
}

$addressText = (Invoke-RestMethod -Uri 'https://checkip.amazonaws.com' -TimeoutSec 15).Trim()
$address = $null
if (-not [System.Net.IPAddress]::TryParse($addressText, [ref]$address) -or
    $address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
    throw 'The AWS IP endpoint did not return a valid IPv4 address.'
}

$template = [System.IO.File]::ReadAllText($templatePath)
$placeholder = '192.0.2.1/32'
if (($template.Split(@($placeholder), [System.StringSplitOptions]::None).Length - 1) -ne 1) {
    throw 'Expected exactly one public API IP placeholder in the cluster template.'
}

$config = $template.Replace($placeholder, "$addressText/32")
$config = $config.Replace(
    '# Documentation-only placeholder: replace with your trusted public IP /32.',
    '# Trusted public IPv4 observed when this local configuration was generated.'
)
[System.IO.File]::WriteAllText($outputPath, $config, (New-Object System.Text.UTF8Encoding($false)))
Write-Output "Prepared local configuration: $outputPath"
Write-Output 'Recheck the allowlist if your internet connection or VPN changes.'
