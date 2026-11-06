$wsl = "C:\Program Files\WSL\wsl.exe"
& $wsl -d AIHub -e bash -lc "cd /mnt/r/AI-Hub/infra && docker compose -f docker-compose.yml -f docker-compose.test.yml --profile test build hub-test 2>&1 | tail -3"
& $wsl -d AIHub -e bash -lc "cd /mnt/r/AI-Hub/infra && docker compose -f docker-compose.yml -f docker-compose.test.yml --profile test up -d --force-recreate hub-test 2>&1 | tail -2"
Start-Sleep -Seconds 10
$tok = ((Get-Content R:\AI-Hub\infra\.env) | Where-Object { $_ -match '^HUB_TOKEN=' }) -replace '^HUB_TOKEN=',''
$r = Invoke-WebRequest -Uri http://127.0.0.1:8100/agent-profiles/codex -Headers @{Authorization="Bearer $tok"} -UseBasicParsing
Write-Output $r.Content
