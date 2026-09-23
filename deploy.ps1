<#
  Tests, builds dms and deploys sms-gateway to the router with the SSH key from dms.yml.
  dms keeps backups of replaced files and restarts the previous release if /api/health fails.
#>
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

py -3 -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }

$dms = Resolve-Path (Join-Path $PSScriptRoot '..\dms')
Push-Location $dms
try {
    go build -o bin\dms-client.exe .\cmd\dms-client
    if ($LASTEXITCODE -ne 0) { throw 'dms-client build failed' }
    $env:GOOS = 'linux'; $env:GOARCH = 'arm64'; $env:CGO_ENABLED = '0'
    go build -o bin\dms-service-linux-arm64 .\cmd\dms-service
    if ($LASTEXITCODE -ne 0) { throw 'dms-service build failed' }
} finally {
    Remove-Item Env:GOOS, Env:GOARCH, Env:CGO_ENABLED -ErrorAction SilentlyContinue
    Pop-Location
}

& "$dms\bin\dms-client.exe" apply --config dms.yml --service-bin "$dms\bin\dms-service-linux-arm64"
if ($LASTEXITCODE -ne 0) { throw 'Deploy failed' }
