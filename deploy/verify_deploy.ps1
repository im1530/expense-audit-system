# 部署后自检脚本（Windows PowerShell 5.1 / PowerShell 7 均可）
#
# 用途：发版后 30 秒内确认「静态托管 + API 反代 + gzip + SPA 回退」四项都正常。
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\verify_deploy.ps1 -BaseUrl "https://expense.example.com"
#   pwsh -File deploy\verify_deploy.ps1 -BaseUrl "http://127.0.0.1:8080" -SkipGzip
#
# 退出码：0 全部通过；1 有检查项失败（可直接接发布流水线）。
# 说明：本文件保存为 UTF-8 with BOM —— Windows PowerShell 5.1 按 ANSI 读取无 BOM 的 .ps1，
#       中文会变乱码并导致解析失败，务必保留 BOM。

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BaseUrl,

    # 跳过 gzip 检查（本地 nginx 未启用压缩时使用）
    [switch]$SkipGzip
)

$ErrorActionPreference = 'Stop'
$base = $BaseUrl.TrimEnd('/')
$script:failures = @()

function Invoke-Check {
    <#
    .SYNOPSIS
        发一个 GET 请求并返回状态码/响应头/响应体（4xx/5xx 也返回，不抛异常）
    .PARAMETER Url
        请求地址
    .PARAMETER Headers
        额外请求头（可选）
    #>
    param(
        [string]$Url,
        [hashtable]$Headers
    )

    $request = [System.Net.HttpWebRequest]::Create($Url)
    $request.Method = 'GET'
    $request.AllowAutoRedirect = $false
    $request.Timeout = 15000

    if ($Headers) {
        foreach ($key in $Headers.Keys) { $request.Headers[$key] = $Headers[$key] }
    }

    try {
        $response = $request.GetResponse()
    } catch [System.Net.WebException] {
        # 4xx/5xx：.NET 抛 WebException，但响应体仍在 Response 里
        $response = $_.Exception.Response
        if (-not $response) { throw }
    }

    $status = [int]$response.StatusCode
    $contentType = [string]$response.ContentType
    $contentEncoding = [string]$response.Headers['Content-Encoding']

    $stream = $response.GetResponseStream()
    $body = ''
    if ($stream) {
        $reader = New-Object System.IO.StreamReader($stream)
        $body = $reader.ReadToEnd()
        $reader.Dispose()
    }
    $response.Close()

    return [pscustomobject]@{
        Status          = $status
        Body            = $body
        ContentType     = $contentType
        ContentEncoding = $contentEncoding
    }
}

function Test-Case {
    <#
    .SYNOPSIS
        执行一项检查并输出 PASS/FAIL
    .PARAMETER Name
        检查项名称
    .PARAMETER Url
        请求地址
    .PARAMETER ExpectStatus
        期望状态码
    .PARAMETER ExpectContains
        响应体应包含的文本（可选）
    .PARAMETER ExpectContentType
        响应头 Content-Type 应包含的文本（可选）
    #>
    param(
        [string]$Name,
        [string]$Url,
        [int]$ExpectStatus,
        [string]$ExpectContains,
        [string]$ExpectContentType
    )

    try {
        $result = Invoke-Check -Url $Url
        $ok = $result.Status -eq $ExpectStatus
        $detail = "HTTP $($result.Status)"

        if ($ok -and $ExpectContains) {
            $ok = $result.Body -like "*$ExpectContains*"
            if (-not $ok) { $detail += "，响应体缺少「$ExpectContains」" }
        }
        if ($ok -and $ExpectContentType) {
            $ok = $result.ContentType -like "*$ExpectContentType*"
            if (-not $ok) { $detail += "，Content-Type=$($result.ContentType)" }
        }

        if ($ok) {
            Write-Host ("[PASS] {0,-16} {1}" -f $Name, $detail) -ForegroundColor Green
        } else {
            Write-Host ("[FAIL] {0,-16} {1}" -f $Name, $detail) -ForegroundColor Red
            $script:failures += $Name
        }
    } catch {
        Write-Host ("[FAIL] {0,-16} 请求异常：{1}" -f $Name, $_.Exception.Message) -ForegroundColor Red
        $script:failures += $Name
    }
}

Write-Host "自检目标：$base" -ForegroundColor Cyan
Write-Host ("-" * 62)

# 1.首页：静态托管是否生效
Test-Case -Name '首页' -Url "$base/" -ExpectStatus 200 -ExpectContentType 'text/html'

# 2.探活：后端是否在线
Test-Case -Name '健康检查' -Url "$base/health" -ExpectStatus 200 -ExpectContains 'healthy'

# 3.未登录访问业务接口：应 401 且是统一错误结构（证明反代到了后端而不是静态 404）
Test-Case -Name '接口反代' -Url "$base/api/v1/expense/category" -ExpectStatus 401 -ExpectContains 'UNAUTHORIZED'

# 4.SPA 回退：前端深链接应返回 index.html 而不是 404
Test-Case -Name 'SPA 路由回退' -Url "$base/expenses" -ExpectStatus 200 -ExpectContentType 'text/html'

# 5.gzip：取首页引用的第一个 /assets/*.js 判断是否压缩
if (-not $SkipGzip) {
    try {
        $index = Invoke-Check -Url "$base/"
        $match = [regex]::Match($index.Body, '/assets/[^"'']+\.js')
        if ($match.Success) {
            $asset = Invoke-Check -Url ("$base" + $match.Value) -Headers @{ 'Accept-Encoding' = 'gzip' }
            if ($asset.ContentEncoding -like '*gzip*') {
                Write-Host ("[PASS] {0,-16} Content-Encoding={1}" -f 'gzip 压缩', $asset.ContentEncoding) -ForegroundColor Green
            } else {
                Write-Host ("[FAIL] {0,-16} 未启用 gzip（Content-Encoding 为空）" -f 'gzip 压缩') -ForegroundColor Red
                $script:failures += 'gzip 压缩'
            }
        } else {
            Write-Host "[SKIP] gzip 压缩        首页未引用 /assets/*.js" -ForegroundColor Yellow
        }
    } catch {
        Write-Host ("[FAIL] {0,-16} {1}" -f 'gzip 压缩', $_.Exception.Message) -ForegroundColor Red
        $script:failures += 'gzip 压缩'
    }
}

Write-Host ("-" * 62)
if ($script:failures.Count -eq 0) {
    Write-Host "全部通过" -ForegroundColor Green
    exit 0
}

Write-Host ("失败 {0} 项：{1}" -f $script:failures.Count, ($script:failures -join '、')) -ForegroundColor Red
exit 1
