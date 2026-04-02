param(
  [Parameter(Mandatory = $false)]
  [string]$WorkDir = (Resolve-Path ".\"),

  [Parameter(Mandatory = $false)]
  [int]$ProxyPort = 3029
)

$ErrorActionPreference = "Stop"

Write-Host "Working directory: $WorkDir"
Set-Location $WorkDir

$envFile = Join-Path $WorkDir ".env"
$envExample = Join-Path $WorkDir ".env.example"

if (!(Test-Path $envFile)) {
  if (!(Test-Path $envExample)) {
    throw ".env.example not found."
  }
  Write-Host "Copying .env.example -> .env"
  Copy-Item -Force $envExample $envFile
}

# 仅当 .env 中没有显式设置 PROXY_PORT 时，给一个默认值方便本地访问
$envContent = Get-Content $envFile -Raw
if ($envContent -notmatch "(?m)^\s*PROXY_PORT\s*=") {
  Write-Host "Setting PROXY_PORT=$ProxyPort in .env"
  Add-Content -Path $envFile -Value "`nPROXY_PORT=$ProxyPort"
}

$dockerComposeCmd = @("docker", "compose")
try {
  # 用数组调用，避免 PowerShell 把 "docker compose" 当成单一命令名
  $null = & $dockerComposeCmd version 2>$null
} catch {
  $dockerComposeCmd = @("docker-compose")
  try {
    $null = & $dockerComposeCmd version 2>$null
  } catch {
    throw "Neither 'docker compose' nor 'docker-compose' is available. Please install Docker Desktop (with Compose plugin) or docker-compose, and ensure PATH is set."
  }
}

$dockerComposePreview = ($dockerComposeCmd -join " ")
Write-Host "Running: $dockerComposePreview up -d --build --remove-orphans"
$null = & $dockerComposeCmd up -d --build --remove-orphans
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
  throw "docker compose build/start failed (exit code: $exitCode). Please check Docker Desktop network/proxy/registry access."
}

Write-Host "Done. You can open: http://localhost:$ProxyPort/admin"
Write-Host "Tip: docker compose logs -f api2cursor"

