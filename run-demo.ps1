param(
    [int]$Port = 8000,
    [int]$AgentPort = 8765,
    [string]$PublicUrl,
    [string]$Database = (Join-Path $PSScriptRoot 'data/demo.sqlite3'),
    [string]$Python = "$env:USERPROFILE/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe"
)

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (-not (Test-Path -LiteralPath 'node_modules/esbuild')) {
        npm ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    npm run build
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
if (-not (Test-Path -LiteralPath $Python)) { $Python = 'python' }
$command = @((Join-Path $PSScriptRoot 'app.py'), '--port', $Port, '--db', $Database)
if ($PublicUrl) { $command += @('--public-url', $PublicUrl) }
$config = Join-Path $PSScriptRoot 'data/openbot.env'
if (Test-Path -LiteralPath $config) {
    $command += @('--config', $config, '--ag-ui-port', $AgentPort)
}
& $Python -X utf8 @command
exit $LASTEXITCODE
