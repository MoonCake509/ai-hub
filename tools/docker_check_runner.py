#!/usr/bin/env python3
"""Admin-configured Linux Docker executor. No host-command fallback.

Configuration is supplied by the operator, never by a model/job:
  {"allowed_roots":["/managed/snapshots"],"image":"sha256:...",
   "docker":"/usr/bin/docker"}
The input snapshot is mounted read-only. Checks run as an unprivileged user in
a disposable tmpfs copy with no network, host secrets or writable host mounts.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys
import threading
import time
import uuid

MAX_INPUT = 65536
MAX_OUTPUT = 65536
MAX_SNAPSHOT = 48 * 1024 * 1024

COPY_AND_EXEC = r'''
import json,os,shutil,sys
shutil.copytree('/input','/workspace',dirs_exist_ok=True,symlinks=False)
os.chdir('/workspace')
os.environ.clear()
os.environ.update(PATH='/usr/local/bin:/usr/bin:/bin',HOME='/tmp',TMPDIR='/tmp',
                  LANG='C.UTF-8',PYTHONDONTWRITEBYTECODE='1')
args=json.loads(sys.argv[1])
os.execvpe(args[0],args,os.environ)
'''


def require(condition, message):
    if not condition:
        raise ValueError(message)


def bounded_read(stream, limit, tail):
    while True:
        chunk = stream.read(8192)
        if not chunk:
            return
        tail.extend(chunk)
        if len(tail) > limit:
            del tail[:-limit]


def validated(config, request):
    require(os.name == 'posix', 'Linux Docker executor required')
    require(request.get('protocol') == 1, 'unsupported protocol')
    require(isinstance(request.get('request_id'), str) and
            0 < len(request['request_id']) <= 200, 'invalid request id')
    snapshot = request.get('snapshot') or {}
    require(snapshot.get('read_only') is True, 'read-only snapshot required')
    raw = snapshot.get('path')
    require(isinstance(raw, str), 'absolute snapshot required')
    if re.match(r'^[a-zA-Z]:[\\/]', raw):
        original = PureWindowsPath(raw)
        require('..' not in original.parts, 'snapshot traversal refused')
        mapped = None
        for mapping in config.get('windows_roots', []):
            source = PureWindowsPath(mapping['source'])
            target = Path(mapping['target'])
            require(source.is_absolute() and source.drive and not source.drive.startswith('\\\\')
                    and target.is_absolute(), 'invalid admin path mapping')
            if source in original.parents:
                require(mapped is None, 'ambiguous admin path mapping')
                mapped = target.joinpath(*original.relative_to(source).parts)
        require(mapped is not None, 'Windows snapshot has no configured local WSL mapping')
        raw = str(mapped)
    require(Path(raw).is_absolute(), 'absolute snapshot required')
    path = Path(raw)
    require(path.is_dir() and not path.is_symlink(), 'snapshot unavailable')
    require(path.absolute() == path.resolve(), 'symlink snapshot ancestor refused')
    roots = config.get('allowed_roots')
    require(isinstance(roots, list) and roots, 'managed roots not configured')
    allowed = False
    for root in roots:
        require(isinstance(root, str) and Path(root).is_absolute(), 'invalid managed root')
        resolved = Path(root).resolve()
        if resolved != path.resolve() and resolved in path.resolve().parents:
            allowed = True
    require(allowed, 'snapshot outside managed roots')
    require(',' not in str(path) and '\n' not in str(path), 'unsupported bind path')
    total = count = 0
    for base, dirs, names in os.walk(path, followlinks=False):
        for name in dirs + names:
            item = Path(base) / name
            require(not item.is_symlink(), 'snapshot symlink refused')
            if item.is_file():
                count += 1
                total += item.stat().st_size
                require(count <= 20000 and total <= MAX_SNAPSHOT, 'snapshot exceeds runner limit')
            else:
                require(item.is_dir(), 'snapshot special file refused')
    command = request.get('command')
    require(isinstance(command, list) and 0 < len(command) <= 100 and
            all(isinstance(a, str) and '\0' not in a for a in command), 'invalid command')
    require(command[0] and sum(map(len, command)) <= 16000, 'invalid command length')
    timeout = request.get('timeout_s')
    require(type(timeout) in (int, float) and 0 < timeout <= 300, 'invalid timeout')
    limits = request.get('limits') or {}
    require(limits.get('network') == 'none' and limits.get('rootfs_read_only') is True and
            limits.get('cap_drop') == ['ALL'] and limits.get('no_new_privileges') is True,
            'required isolation flags absent')
    for key, expected in [('memory_mb', 512), ('cpus', 1), ('pids', 64),
                          ('tmpfs_mb', 64), ('stdout_bytes', 65536), ('stderr_bytes', 65536)]:
        require(limits.get(key) == expected, 'unsupported limit: ' + key)
    image = config.get('image', '')
    require(isinstance(image, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', image),
            'immutable local image id required')
    docker = config.get('docker', '/usr/bin/docker')
    require(isinstance(docker, str) and Path(docker).is_absolute() and
            os.path.isfile(docker) and os.access(docker, os.X_OK), 'Docker unavailable')
    return path, command, float(timeout), docker, image


def load_host():
    source = Path(__file__).resolve().parents[1] / 'agents/laptop/workspace_host.py'
    spec = importlib.util.spec_from_file_location('aihub_trusted_host_digest', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def execute(config, request):
    path, command, timeout, docker, image = validated(config, request)
    host = load_host()
    before = host.tree_digest(str(path))
    require(before == request['snapshot'].get('tree_digest'), 'snapshot digest conflict')
    name = 'aihub-check-' + uuid.uuid4().hex
    argv = [docker, 'create', '--pull=never', '--name', name, '--network=none',
            '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
            '--user=65534:65534', '--pids-limit=64', '--memory=512m', '--memory-swap=512m',
            '--cpus=1', '--log-driver=none',
            '--mount', 'type=bind,source=' + str(path) + ',target=/input,readonly',
            '--tmpfs', '/workspace:rw,nosuid,nodev,size=64m,uid=65534,gid=65534,mode=0700',
            '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=64m,uid=65534,gid=65534,mode=0700',
            '--workdir=/workspace', '--entrypoint=python', image,
            '-c', COPY_AND_EXEC, json.dumps(command)]
    started = time.monotonic()
    stdout, stderr = bytearray(), bytearray()
    process = None
    timed_out = False
    cleanup_confirmed = False
    try:
        created = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
        require(created.returncode == 0, 'container create failed: ' +
                created.stderr.decode('utf-8', 'replace')[-1000:])
        # Separate creation from start: a timed-out docker run client can leave a
        # new Created container *after* an otherwise successful rm by name.
        process = subprocess.Popen([docker, 'start', '--attach', name],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        readers = [threading.Thread(target=bounded_read, args=(process.stdout, MAX_OUTPUT, stdout), daemon=True),
                   threading.Thread(target=bounded_read, args=(process.stderr, MAX_OUTPUT, stderr), daemon=True)]
        for thread in readers:
            thread.start()
        try:
            code = process.wait(timeout=max(.001, timeout - (time.monotonic() - started)))
        except subprocess.TimeoutExpired:
            timed_out = True
            code = 124
        finally:
            # Kill/remove only our random container; never stop a shared service.
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            cleanup = subprocess.run([docker, 'rm', '-f', name], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
            cleanup_confirmed = cleanup.returncode == 0
            for thread in readers:
                thread.join(timeout=5)
        require(cleanup_confirmed, 'container cleanup unconfirmed')
        after = host.tree_digest(str(path))
        return {'protocol': 1, 'request_id': request['request_id'], 'exit_code': code,
                'stdout': bytes(stdout).decode('utf-8', 'replace'),
                'stderr': bytes(stderr).decode('utf-8', 'replace'),
                'elapsed_s': round(time.monotonic() - started, 3), 'timed_out': timed_out,
                'termination_confirmed': cleanup_confirmed,
                'isolation': {'network': 'none', 'rootfs_read_only': True, 'cap_drop_all': True,
                              'no_new_privileges': True, 'resource_limits': True,
                              'snapshot_read_only': True, 'nonroot': True,
                              'ephemeral_workspace': True, 'image': image},
                'snapshot_tree_digest_before': before, 'snapshot_tree_digest_after': after}
    finally:
        if process is not None and process.poll() is None:
            process.kill()
        if not cleanup_confirmed:
            # Also cover create timeouts / a failed attach-client spawn. A
            # creation timeout never starts the check, but may leave a stopped
            # container if the daemon completed the create after client exit.
            try:
                subprocess.run([docker, 'rm', '-f', name], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
            except (OSError, subprocess.SubprocessError):
                pass  # Caller receives blocked/error, never an attested PASS.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    request = {}
    try:
        require(Path(args.config).is_absolute(), 'absolute admin config required')
        config = json.loads(Path(args.config).read_text(encoding='utf-8'))
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        require(len(raw) <= MAX_INPUT, 'request too large')
        request = json.loads(raw)
        result = execute(config, request)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'protocol': 1, 'request_id': request.get('request_id'),
                          'error': {'code': 'runner_blocked', 'message': str(exc)[:1000]}}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
