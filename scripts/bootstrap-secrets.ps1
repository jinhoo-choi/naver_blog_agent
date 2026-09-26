param(
  [string]$Repo = "jinhoo-choi/naver_blog_agent",
  [switch]$IncludeLogin,
  [switch]$IncludeTelegram
)
$ErrorActionPreference = "Stop"
$names = @("OPENAI_API_KEY", "NAVER_BLOG_ID")
if ($IncludeLogin) { $names += @("NAVER_ID", "NAVER_PASSWORD") }
if ($IncludeTelegram) { $names += @("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") }
foreach ($name in $names) {
  Write-Host "GitHub Secret: $name"
  gh secret set $name --repo $Repo
  if ($LASTEXITCODE -ne 0) { throw "Failed to set $name" }
}
$variables = @{
  BLOG_DATA_DIR = "C:\blogbot\data"
  NAVER_PROFILE_DIR = "C:\blogbot\chrome-profile"
  OPENAI_MODEL = "gpt-5"
  BLOG_DAILY_COUNT = "3"
}
foreach ($name in $variables.Keys) {
  gh variable set $name --body $variables[$name] --repo $Repo
  if ($LASTEXITCODE -ne 0) { throw "Failed to set $name" }
}
Write-Host "Secrets saved. Enable scheduling and notifications separately when configured."
