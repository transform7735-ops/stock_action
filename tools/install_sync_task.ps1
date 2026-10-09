<#
  PC 한 번 설정: 노션 토큰 저장 + 작업 스케줄러 등록 + 첫 동기화.

  실행 (sync_reports.ps1과 같은 폴더에서):
      powershell -ExecutionPolicy Bypass -File install_sync_task.ps1

  - 관리자 권한은 필요 없다.
  - 노션 토큰은 이 PC의 현재 사용자 계정으로만 풀 수 있게 암호화(DPAPI)해서
    %APPDATA%\stock_action\notion_token.dat 에 저장한다.
  - 작업 이름: '주간 수급 분석 동기화' (매일 08:30, 놓친 실행은 PC가 켜지면 바로 실행)
#>
param(
    [string]$Dest = "C:\Users\S.J.KO\Documents\T_room\stock-autotrader\주간 수급 분석",
    [string]$At = "08:30"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$sync = Join-Path $here "sync_reports.ps1"
if (-not (Test-Path -LiteralPath $sync)) { throw "같은 폴더에 sync_reports.ps1이 있어야 합니다: $here" }
Unblock-File -LiteralPath $sync -ErrorAction SilentlyContinue

# 1) 노션 토큰 저장
Write-Host ""
Write-Host "노션 연동 '라르'의 내부 통합 시크릿이 필요합니다."
Write-Host "  찾는 곳: https://www.notion.so/profile/integrations → 라르 → 구성 → 내부 통합 시크릿 '표시' → 복사"
$secure = Read-Host "시크릿을 붙여 넣고 Enter (화면에는 보이지 않습니다)" -AsSecureString
if ($secure.Length -lt 20) { throw "시크릿이 너무 짧습니다. 다시 복사해서 실행하세요." }
$dir = Join-Path $env:APPDATA "stock_action"
New-Item -ItemType Directory -Path $dir -Force | Out-Null
$tokenFile = Join-Path $dir "notion_token.dat"
$secure | ConvertFrom-SecureString | Set-Content -LiteralPath $tokenFile
Write-Host "토큰 저장: $tokenFile (이 PC·이 사용자만 해독 가능)"

# 2) 작업 스케줄러 등록
$taskName = "주간 수급 분석 동기화"
$arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$sync`" -Dest `"$Dest`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "노션 주간 수급 분석 보고서의 엑셀을 '$Dest'로 내려받습니다 (stock_action)." -Force | Out-Null
Write-Host "작업 등록: '$taskName' — 매일 $At (PC가 꺼져 있었으면 켜진 뒤 바로)"

# 3) 첫 동기화
Write-Host ""
Write-Host "첫 동기화를 실행합니다..."
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $sync -Dest $Dest
Write-Host ""
Write-Host "끝. 결과 기록은 '$Dest\_sync.log'에 쌓입니다."
