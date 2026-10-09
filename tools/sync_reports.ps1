<#
  노션 '📊 주간 수급 분석 보고서' 아래 주차별 보고서에 첨부된 분석 엑셀을 PC 폴더로 내려받는다.

  - 작업 스케줄러가 매일 08:30에 실행한다. PC가 꺼져 있었으면 켜진 뒤 바로 실행된다.
  - 새 보고서(또는 다시 만든 보고서)가 있을 때만 내려받고, 없으면 바로 끝난다.
  - 노션 토큰은 install_sync_task.ps1이 이 PC 사용자 계정으로 암호화해 저장한 것을 쓴다.

  수동 실행:  powershell -ExecutionPolicy Bypass -File sync_reports.ps1
#>
param(
    [string]$Dest = "C:\Users\S.J.KO\Documents\T_room\stock-autotrader\주간 수급 분석",
    [string]$ParentPageId = "3f4ce99325d481b986a5c39dd6c641ae",
    [string]$TokenFile = (Join-Path $env:APPDATA "stock_action\notion_token.dat"),
    [string]$Token = "",                                  # 시험용. 비우면 TokenFile을 쓴다
    [string]$ApiBase = "https://api.notion.com/v1"       # 시험용
)

$ErrorActionPreference = "Stop"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

if (-not (Test-Path -LiteralPath $Dest)) { New-Item -ItemType Directory -Path $Dest -Force | Out-Null }
$LogFile = Join-Path $Dest "_sync.log"

function Write-Log([string]$msg) {
    $line = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $msg
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

if (-not $Token) {
    if (-not (Test-Path -LiteralPath $TokenFile)) {
        Write-Log "노션 토큰 파일이 없습니다. install_sync_task.ps1을 먼저 실행하세요."
        exit 1
    }
    $secure = Get-Content -LiteralPath $TokenFile | ConvertTo-SecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $Token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}
$headers = @{ "Authorization" = "Bearer $Token"; "Notion-Version" = "2022-06-28" }

function Invoke-Notion([string]$path) {
    for ($i = 1; $i -le 5; $i++) {
        try {
            $resp = Invoke-WebRequest -Uri "$ApiBase$path" -Headers $headers -UseBasicParsing
            # 한글이 깨지지 않도록 바이트를 직접 UTF-8로 읽는다 (Windows PowerShell 5.1 대응)
            $text = [Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray())
            return ($text | ConvertFrom-Json)
        } catch {
            $code = 0
            if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
            if (($code -eq 429 -or $code -ge 500) -and $i -lt 5) { Start-Sleep -Seconds (2 * $i); continue }
            if ($code -eq 401) { throw "노션 토큰이 거부됐습니다(401). install_sync_task.ps1로 토큰을 다시 저장하세요." }
            if ($code -eq 404) { throw "보고서 페이지를 찾을 수 없습니다(404). 노션에서 연동 '라르'가 연결돼 있는지 확인하세요." }
            throw
        }
    }
}

function Get-Children([string]$id) {
    $all = @()
    $cursor = $null
    do {
        $q = "/blocks/$id/children?page_size=100"
        if ($cursor) { $q += "&start_cursor=$cursor" }
        $r = Invoke-Notion $q
        $all += @($r.results)
        $cursor = $r.next_cursor
    } while ($r.has_more)
    return $all
}

function To-LocalTime($v) {
    # PowerShell 7은 날짜 문자열을 DateTime으로 바꿔 주고, 5.1은 문자열 그대로 둔다
    if ($v -is [DateTime]) { return $v.ToLocalTime() }
    return [DateTimeOffset]::Parse([string]$v, [Globalization.CultureInfo]::InvariantCulture).LocalDateTime
}

try {
    $pages = @(Get-Children $ParentPageId | Where-Object { $_.type -eq "child_page" })
} catch {
    Write-Log "실패: $($_.Exception.Message)"
    exit 1
}

$got = 0
$failed = 0
foreach ($page in $pages) {
    foreach ($b in @(Get-Children $page.id | Where-Object { $_.type -eq "file" })) {
        # 첨부 블록 자체의 시각으로 비교한다 (보고서 페이지에 메모만 달아도 다시 받지 않게)
        $stamp = if ($b.last_edited_time) { $b.last_edited_time } else { $page.last_edited_time }
        $edited = To-LocalTime $stamp
        $f = $b.file
        $url = if ($f.type -eq "external") { $f.external.url } else { $f.file.url }
        $name = $f.name
        if (-not $name) { $name = [Uri]::UnescapeDataString(([Uri]$url).Segments[-1]) }
        if ($name -notlike "*.xlsx") { continue }

        $target = Join-Path $Dest $name
        if ((Test-Path -LiteralPath $target) -and ((Get-Item -LiteralPath $target).LastWriteTime -ge $edited)) { continue }

        $part = "$target.part"
        try {
            Invoke-WebRequest -Uri $url -OutFile $part -UseBasicParsing
            Move-Item -LiteralPath $part -Destination $target -Force
            Write-Log "내려받음: $name  ← $($page.child_page.title)"
            $got++
        } catch {
            if (Test-Path -LiteralPath $part) { Remove-Item -LiteralPath $part -Force }
            Write-Log "실패: $name ($($_.Exception.Message)) — 엑셀에서 열려 있으면 닫고 다시 실행하세요."
            $failed++
        }
    }
}

Write-Log ("동기화 완료: 보고서 {0}개 확인, 새로 받은 파일 {1}개, 실패 {2}개" -f $pages.Count, $got, $failed)
if ($failed) { exit 1 }
