"""Забирает свежий бэкап хаба с сервера на ноутбук и проверяет sha256."""
import base64, hashlib, os, subprocess, sys

SSH = ["ssh", "-o", "BatchMode=yes",
       "-o", "UserKnownHostsFile=C:/Users/lavy/.ssh/known_hosts_lan",
       "-o", "ConnectTimeout=25", "-i", os.path.expanduser("~/.ssh/cubeworld_server"),
       "User@192.168.31.95"]

DEST = r"C:\Users\lavy\AI-Hub-backups"
os.makedirs(DEST, exist_ok=True)

remote = (
    'python -c "import base64,glob,os;'
    'f=max(glob.glob(r\'R:/AI-Hub/backups/aihub-*.dump\'), key=os.path.getmtime);'
    'print(os.path.basename(f));'
    'print(open(f+chr(46)+\'sha256\').read().strip());'
    'print(base64.b64encode(open(f,\'rb\').read()).decode())"'
)
r = subprocess.run(SSH + [remote], capture_output=True, timeout=180)
if r.returncode != 0:
    print("ssh failed:", r.stderr.decode("utf-8", "replace")[:300])
    sys.exit(1)
lines = r.stdout.decode("ascii", "replace").replace("\r", "").split("\n")
name, expected, b64 = lines[0], lines[1], "".join(lines[2:])
data = base64.b64decode(b64)
digest = hashlib.sha256(data).hexdigest()
path = os.path.join(DEST, name)
tmp = path + ".part"

# Публикация только после сверки: неполная копия никогда не лежит под финальным именем.
if digest != expected.strip():
    print("FAIL: sha256 не совпал, копия не опубликована")
    print("  ожидалось:", expected.strip())
    print("  получено: ", digest)
    sys.exit(2)

with open(tmp, "wb") as fh:
    fh.write(data)
os.replace(tmp, path)
with open(path + ".sha256", "w", encoding="ascii") as fh:
    fh.write(f"{digest}  {name}\n")
print("saved:", path, len(data), "байт")
print("sha256 совпал: True")
