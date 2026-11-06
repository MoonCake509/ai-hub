#!/bin/bash
docker logs --since 90m aihub-hub 2>&1 | grep -E '401|app\.js|GET /ui/|limit=1|GET /agents|GET /health' | tail -40
echo "--- 401 count ---"
docker logs --since 90m aihub-hub 2>&1 | grep -c ' 401 '