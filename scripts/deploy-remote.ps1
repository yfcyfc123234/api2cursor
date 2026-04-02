param(
  [Parameter(Mandatory = $false)]
  [string]$Server = "104.225.148.168",

  [Parameter(Mandatory = $false)]
  [string]$User = "root",

  [Parameter(Mandatory = $false)]
  [int]$Port = 22,

  # 你仓库的 git 地址（建议用只读 https，或配好 ssh key 的 git@ 方式）
  [Parameter(Mandatory = $true)]
  [string]$RepoUrl,

  [Parameter(Mandatory = $false)]
  [string]$Branch = "main",

  [Parameter(Mandatory = $false)]
  [string]$AppDir = "/www/wwwroot/api2cursor"
)

$ErrorActionPreference = "Stop"

$localScript = Join-Path $PSScriptRoot "deploy-centos7.sh"
if (!(Test-Path $localScript)) {
  throw "Missing local script: $localScript"
}

Write-Host "Ensuring remote dir exists: $AppDir"
ssh -p $Port "$User@$Server" "mkdir -p `"$AppDir`""

$remoteScript = "$AppDir/deploy.sh"
Write-Host "Uploading script to: $remoteScript"
scp -P $Port "$localScript" "$User@$Server:`"$remoteScript`""

Write-Host "Running remote deployment..."
ssh -p $Port "$User@$Server" `
  "export APP_DIR=`"$AppDir`"; export REPO_URL=`"$RepoUrl`"; export BRANCH=`"$Branch`"; bash `"$remoteScript`""

Write-Host "Done."

