param([switch]$Start)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$dataPath = Join-Path $project 'data'
$config = Join-Path $dataPath 'openbot.env'
if (-not (Test-Path -LiteralPath $config)) {
    New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
    $template = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'env.example') -Raw
    $encryption = [Convert]::ToBase64String([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
    $token = [Convert]::ToHexString([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
    $template.Replace('KEY_ENCRYPTION_KEY=', "KEY_ENCRYPTION_KEY=$encryption").Replace(
        'UPI_OPENBOT_TOKEN=', "UPI_OPENBOT_TOKEN=$token") | Set-Content -LiteralPath $config -Encoding utf8
}
if (-not $Start) {
    Write-Output 'Private OpenBot configuration is ready in data/openbot.env.'
    exit 0
}

$settings = @{}
foreach ($line in Get-Content -LiteralPath $config) {
    $parts = $line -split '=', 2
    if ($parts.Count -eq 2) { $settings[$parts[0].Trim()] = $parts[1].Trim().Trim('"', "'") }
}
$required = @('UPI_LLM_API_KEY','UPI_LLM_MODEL','CPK_INTELLIGENCE_API_KEY','KEY_ENCRYPTION_KEY')
$missing = @($required | Where-Object { -not $settings[$_] })
if ($missing.Count) { throw "Fill these values in data/openbot.env before starting OpenBot: $($missing -join ', ')." }
Push-Location $project
try {
    if (-not (Test-Path -LiteralPath 'node_modules/esbuild')) {
        npm ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    npm run build
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $python = "$env:USERPROFILE/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe"
    if (-not (Test-Path -LiteralPath $python)) { $python = 'python' }
    & $python -c "from demo_auth import load_demo_passwords; load_demo_passwords('data/demo.signins.json')"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    docker compose --env-file $config -f compose.openbot.yml --profile openbot up --build -d
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $address = $settings['UPI_BIND_IP'] ?? '127.0.0.1'
    $port = $settings['UPI_MONITOR_PORT'] ?? '8000'
    Write-Output "Open the workspace at http://${address}:${port}/bank, then choose OpenBot."
} finally {
    Pop-Location
}
