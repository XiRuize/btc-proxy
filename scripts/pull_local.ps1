# 本机拉取器（走 SSH，不依赖 raw.githubusercontent.com）
#
# 为什么不用 HTTPS：这台机器的 shell 里 TLS 拿不到凭据（schannel SEC_E_NO_CREDENTIALS），
# 但 SSH 是通的（已用 id_ed25519_zwp 认证成 XiRuize），所以直接用 git fetch + git show 取数据。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\pull_local.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\pull_local.ps1 -Out "D:\data\crypto"

param(
    [string]$Out = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$cfg = Get-Content (Join-Path $repoRoot "config.json") -Raw -Encoding UTF8 | ConvertFrom-Json

if (-not $Out) {
    $Out = Join-Path $repoRoot ($cfg.local_output -replace '^\.\./', '..\')
}
$Out = [IO.Path]::GetFullPath($Out)
New-Item -ItemType Directory -Path $Out -Force | Out-Null

$env:GIT_SSH = "C:\Windows\System32\OpenSSH\ssh.exe"
$env:GIT_SSH_VARIANT = "ssh"

Push-Location $repoRoot
try {
    Write-Host "仓库：$($cfg.repo.owner)/$($cfg.repo.name)@$($cfg.repo.branch)"
    Write-Host "输出：$Out`n"

    git fetch --quiet origin $cfg.repo.branch
    if ($LASTEXITCODE -ne 0) { throw "git fetch 失败（检查 SSH 与远程仓库）" }

    $files = git ls-tree -r --name-only FETCH_HEAD -- data | Where-Object { $_ -match '\.(csv|json)$' }
    if (-not $files) { throw "data 分支里没有数据文件，等采集器跑一轮再来" }

    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $now = [DateTime]::UtcNow

    foreach ($f in $files) {
        $name = Split-Path $f -Leaf
        $lines = git show "FETCH_HEAD:$f"
        $text = ($lines -join "`n") + "`n"
        [IO.File]::WriteAllText((Join-Path $Out $name), $text, $utf8)

        if ($name -like "*.csv") {
            $rows = $lines.Count - 1
            $last = $lines[-1]
            $ts = ($last -split ",")[0]
            $age = ""
            try {
                $t = [DateTime]::ParseExact($ts, "yyyy-MM-ddTHH:mm:ssZ", $null, [Globalization.DateTimeStyles]::AdjustToUniversal)
                $age = "，滞后 {0:N0} 分钟" -f ($now - $t).TotalMinutes
            } catch { }
            Write-Host ("  {0,-28} {1,7} 行，最后 {2}{3}" -f $name, $rows, $ts, $age)
        } else {
            Write-Host ("  {0,-28} (json)" -f $name)
        }
    }

    $latestPath = Join-Path $Out "latest.json"
    if (Test-Path $latestPath) {
        $latest = Get-Content $latestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        Write-Host "`n快照生成时间：$($latest.generated_at)"
        foreach ($p in $latest.symbols.PSObject.Properties) {
            $s = $p.Value
            Write-Host ("  {0}: 收盘 {1}  标记价 {2}  资金费率 {3}  持仓量 {4}" -f `
                $p.Name, $s.last_close, $s.mark_price, $s.funding_rate, $s.open_interest)
        }
    }
}
finally {
    Pop-Location
}
