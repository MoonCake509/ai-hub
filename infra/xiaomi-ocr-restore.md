# Xiaomi OCR local service restore

## Service command

`start-xiaomi-ocr.cmd` starts the previously inventoried Xiaomi model on `127.0.0.1:11438` with the expected API model id `xiaomi-ocr-q8`. It uses the existing llama.cpp server binary and the existing model and projector files:

- `D:\llm\engine\llama-server.exe`
- `D:\llm\models\xiaomi-ocr\Xiaomi-OCR-0-Q8_0.gguf` (833,592,768 bytes)
- `D:\llm\models\xiaomi-ocr\mmproj-Xiaomi-OCR-0-F16.gguf` (204,987,264 bytes)

The model plus projector total 1,038,580,032 bytes (0.967 GiB). The host had 18.41 GiB free RAM immediately before the CPU-only start, leaving about 17.44 GiB after those files, above the 6 GiB headroom target. The command sets `--gpu-layers 0 --no-mmproj-offload --no-op-offload`, uses four CPU threads, an 8192 token context, and one parallel slot. The server binds only to loopback. Stdout and stderr append to `D:\llm\logs\aihub-xiaomi-ocr.stdout.log` and `D:\llm\logs\aihub-xiaomi-ocr.stderr.log`.

The llama.cpp binary's `--help` confirms support for the model/projector, GPU-layer, projector-offload, op-offload, thread, context, parallel, alias, host, and port flags. No `--device none` flag is exposed by this binary.

## Startup task

`AIHub-XiaomiOCR` is a user-interactive, limited-privilege Scheduled Task for the existing `user` principal. It starts at that user's logon and was started immediately after registration. It has no stored password, runs at the normal integrity level, ignores duplicate starts, and retries three times at one minute intervals if the server exits. Windows Task Scheduler rejected a 30 second interval as out of range, so the supported minimum was used. No global ACLs or execution policy were changed.

## Verification record

The first launch from an SSH child process disappeared when the SSH session closed. A same-session check showed the process and port briefly alive, while a second SSH session found it gone. The scheduled task keeps the project dependency independent of SSH lifetime.

Verified in a separate SSH session on 2026-11-09: task `AIHub-XiaomiOCR` is Running; llama-server PID `16088` is the sole listener on 11438; `/health` returned 200 and `/v1/models` returned `xiaomi-ocr-q8`. The running process command line includes CPU-only flags, four threads, 8192 context, and loopback host. Free RAM after load was 17.05 GiB. GPU usage was 5886 MiB used / 144 MiB free versus 5849 / 181 MiB before start; DeepSeek listener PID `5864` remained unchanged. Task stdout/stderr are `D:\llm\logs\aihub-xiaomi-ocr.stdout.log` and `D:\llm\logs\aihub-xiaomi-ocr.stderr.log`.

No OCR inference was run as part of service restoration; UI acceptance performs the next request.
