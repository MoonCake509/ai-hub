@echo off
setlocal
cd /d D:\llm\engine || exit /b 1
"D:\llm\engine\llama-server.exe" --model "D:\llm\models\xiaomi-ocr\Xiaomi-OCR-0-Q8_0.gguf" --mmproj "D:\llm\models\xiaomi-ocr\mmproj-Xiaomi-OCR-0-F16.gguf" --host 127.0.0.1 --port 11438 --alias xiaomi-ocr-q8 --gpu-layers 0 --no-mmproj-offload --no-op-offload --threads 4 --threads-batch 4 --ctx-size 8192 --parallel 1 >> "D:\llm\logs\aihub-xiaomi-ocr.stdout.log" 2>> "D:\llm\logs\aihub-xiaomi-ocr.stderr.log"
exit /b %ERRORLEVEL%
