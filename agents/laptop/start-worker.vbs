Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\lavy\aihub-bridge"
sh.Run """C:\Users\lavy\AppData\Local\hermes\tools\python-3.14.7+20260901-win32-x64\pythonw.exe"" ""C:\Users\lavy\aihub-bridge\worker_v2.py""", 0, False
