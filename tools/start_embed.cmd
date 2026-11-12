@echo off
rem Локальные модели эмбеддингов для семантического поиска AI Hub (P-09).
rem E5 — документация и (в пилоте) код. Порт 11442, только loopback.
rem code_retriever_ru_en конвертирован (D:\llm\models\embed\code-v2.gguf), но
rem сборка llama.cpp 12058 не грузит modernbert: key not found
rem modern-bert.attention.layer_norm_epsilon. Когда движок обновится — запустить
rem второй сервер на 11441 и задать AIHUB_EMBED_CODE_URL.
set ENGINE=D:\llm\engine\llama-server.exe
set MODEL=D:\llm\models\embed\multilingual-e5-base-q8_0.gguf

netstat -ano | findstr "127.0.0.1:11442" >nul && (
  echo embed e5 already listening on 11442
  exit /b 0
)

start "aihub-embed-e5" /min "%ENGINE%" -m "%MODEL%" --embeddings --pooling mean ^
  --host 127.0.0.1 --port 11442 -c 512 -b 512 -ub 512 -t 4 -ngl 0
echo started embed e5 on 127.0.0.1:11442
