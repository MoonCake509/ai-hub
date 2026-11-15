# Держит правило portproxy для хаба актуальным.
# Docker Desktop публикует 8090 внутри WSL-машины docker-desktop; после её
# перезапуска IP меняется и правило 8000 -> WSL:8090 перестаёт работать.
# Скрипт запускается при входе и каждые 5 минут: если IP другой — переписывает.
$ErrorActionPreference = 'SilentlyContinue'

$ip = (wsl -d docker-desktop sh -lc "ip -4 -o addr show eth0 | awk '{print \`$4}' | cut -d/ -f1" 2>$null | Out-String).Trim()
if (-not $ip -or $ip -notmatch '^\d+\.\d+\.\d+\.\d+$') { exit 0 }

$line = (netsh interface portproxy show v4tov4 | Out-String) -split "`r?`n" |
    Where-Object { $_ -match '^\s*0\.0\.0\.0\s+8000\s+' } | Select-Object -First 1
if ($line -and $line -match [regex]::Escape($ip) -and $line -match '\s8090\s*$') { exit 0 }

netsh interface portproxy delete v4tov4 listenport=8000 listenaddress=0.0.0.0 | Out-Null
netsh interface portproxy add v4tov4 listenport=8000 listenaddress=0.0.0.0 connectport=8090 connectaddress=$ip | Out-Null
Write-Output "portproxy 8000 -> ${ip}:8090"
