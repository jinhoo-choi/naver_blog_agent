param(
  [string]$Repo = "jinhoo-choi/naver_blog_agent",
  [string]$RunnerDir = "C:\blogbot\runner"
)
$ErrorActionPreference = "Stop"
Get-Command gh -ErrorAction Stop | Out-Null
gh auth status
if ($LASTEXITCODE -ne 0) { throw "Run gh auth login first" }
if (Test-Path (Join-Path $RunnerDir ".runner")) {
  throw "Runner already configured. Run $RunnerDir\run.cmd in the desktop session."
}
New-Item -ItemType Directory -Force -Path $RunnerDir | Out-Null
$releaseJson = gh api repos/actions/runner/releases/latest
if ($LASTEXITCODE -ne 0) { throw "Cannot read official runner release" }
$release = $releaseJson | ConvertFrom-Json
$asset = $release.assets | Where-Object { $_.name -match '^actions-runner-win-x64-.*\.zip$' } | Select-Object -First 1
if (-not $asset) { throw "Windows x64 runner asset not found" }
$archive = Join-Path $RunnerDir $asset.name
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $archive
if ($asset.digest -and $asset.digest.StartsWith("sha256:")) {
  $actual = (Get-FileHash $archive -Algorithm SHA256).Hash.ToLower()
  if ($actual -ne $asset.digest.Substring(7)) { throw "Runner checksum mismatch" }
}
Expand-Archive -Path $archive -DestinationPath $RunnerDir -Force
$registrationJson = gh api --method POST "repos/$Repo/actions/runners/registration-token"
if ($LASTEXITCODE -ne 0) { throw "Cannot register runner; check repository admin access" }
$registration = $registrationJson | ConvertFrom-Json
Push-Location $RunnerDir
try {
  & .\config.cmd --unattended --url "https://github.com/$Repo" --token $registration.token --labels "naver-blog" --name "$env:COMPUTERNAME-naver-blog" --work "_work"
  if ($LASTEXITCODE -ne 0) { throw "Runner configuration failed" }
} finally {
  $registration = $null
  $registrationJson = $null
  Pop-Location
}
Write-Host "Run $RunnerDir\run.cmd in the logged-in Windows desktop. Keep it open."
