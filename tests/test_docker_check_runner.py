"""Real Docker negative/positive integration; explicit operator opt-in only."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('docker_runner', ROOT/'tools/docker_check_runner.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def main():
    image = os.environ.get('AIHUB_RUNNER_TEST_IMAGE')
    if not image:
        raise SystemExit('Set AIHUB_RUNNER_TEST_IMAGE to an existing immutable image id; no auto-pull')
    with tempfile.TemporaryDirectory(prefix='aihub-runner-test-') as folder:
        base = Path(folder)
        snapshot = base/'snapshots'/'case1'
        snapshot.mkdir(parents=True)
        sentinel = base/'host-marker.txt'
        sentinel.write_text('SYNTHETIC-HOST-ONLY')
        (snapshot/'hello.py').write_text("print('ISOLATED-OK')\n")
        host = runner.load_host()
        config = {'allowed_roots':[str(snapshot.parent)], 'image':image,'docker':'/usr/bin/docker'}
        config_path = base/'admin.json'
        config_path.write_text(json.dumps(config))
        limits = {'network':'none','rootfs_read_only':True,'cap_drop':['ALL'],
                  'no_new_privileges':True,'memory_mb':512,'cpus':1,'pids':64,
                  'tmpfs_mb':64,'stdout_bytes':65536,'stderr_bytes':65536}
        def invoke(command, timeout=10, mutate=None, digest_override=None):
            request = {'protocol':1,'request_id':'fixture-request',
                       'snapshot':{'path':str(snapshot),'read_only':True,
                                   'tree_digest':digest_override or host.tree_digest(str(snapshot))},
                       'command':command,'timeout_s':timeout,'limits':limits}
            if mutate:
                mutate(request)
            process = subprocess.run([sys.executable,str(ROOT/'tools/docker_check_runner.py'),
                                      '--config',str(config_path)],
                                     input=json.dumps(request),text=True,capture_output=True,timeout=45)
            result = json.loads(process.stdout)
            return process.returncode,result
        results = {}
        code, result = invoke(['python','hello.py'])
        assert code == 0 and result['exit_code'] == 0 and result['stdout'].strip() == 'ISOLATED-OK', result
        results['real_check_executed'] = True
        code, result = invoke(['python','-c','raise SystemExit(7)'])
        assert code == 0 and result['exit_code'] == 7, result
        results['real_failure_exit_code_preserved'] = True
        code, result = invoke(['python','-c',
            "from pathlib import Path;Path('hello.py').write_text('ephemeral change');print('COPY-WRITE-OK')"])
        assert code == 0 and result['exit_code'] == 0 and 'COPY-WRITE-OK' in result['stdout'], result
        assert (snapshot/'hello.py').read_text() == "print('ISOLATED-OK')\n"
        results['writable_copy_does_not_change_snapshot'] = True
        script = """import os,socket,json
from pathlib import Path
def writable(path):
 try: Path(path).write_text('probe');return True
 except OSError:return False
try:
 socket.create_connection(('1.1.1.1',443),timeout=.3);network=True
except OSError:network=False
print(json.dumps(dict(host_file=Path(%r).exists(),secret=os.environ.get('AIHUB_RUNNER_SYNTHETIC_SECRET'),
 uid=os.getuid(),input_write=writable('/input/new.txt'),root_write=writable('/etc/aihub-probe'),network=network,
 nnp=next(s for s in Path('/proc/self/status').read_text().splitlines() if s.startswith('NoNewPrivs:')))))
""" % str(sentinel)
        os.environ['AIHUB_RUNNER_SYNTHETIC_SECRET'] = 'SYNTHETIC-DO-NOT-PASS'
        code, result = invoke(['python','-c',script])
        assert code == 0 and result['exit_code'] == 0, result
        evidence = json.loads(result['stdout'])
        assert evidence['host_file'] is False and evidence['secret'] is None and evidence['uid'] == 65534, evidence
        assert evidence['input_write'] is False and evidence['root_write'] is False and evidence['network'] is False, evidence
        assert evidence['nnp'].split(':')[1].strip() == '1', evidence
        results['host_env_fs_network_and_privileges_isolated'] = evidence
        code, result = invoke(['python','-c','import time;time.sleep(20)'],timeout=1)
        assert code == 0 and result['exit_code'] == 124 and result['timed_out'] and result['termination_confirmed'], result
        results['timeout_terminated_own_container'] = True
        code, result = invoke(['python','-c','print("X"*200000)'])
        assert code == 0 and result['exit_code'] == 0 and len(result['stdout'].encode()) <= 65536, result
        results['output_bounded'] = True
        code, result = invoke(['python','hello.py'],mutate=lambda r:r['snapshot'].update(tree_digest='0'*64))
        assert code != 0 and result.get('error'), result
        results['stale_input_rejected'] = True
        code, result = invoke(['python','hello.py'],mutate=lambda r:r['snapshot'].update(path=str(base)))
        assert code != 0 and result.get('error'), result
        results['outside_managed_root_rejected'] = True
        # An explicit operator mapping of the same Windows drive into WSL, not a
        # fallback to another host. This fixture lives on the actual R: mount.
        mapped_parent = ROOT/'tmp'
        mapped_parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='runner-map-',dir=mapped_parent) as mapped_folder:
            mapped = Path(mapped_folder)
            (mapped/'mapped.py').write_text("print('MAPPED-OK')\n")
            windows_root = str(mapped_parent).replace('/mnt/r/', 'R:\\').replace('/', '\\')
            windows_path = str(mapped).replace('/mnt/r/', 'R:\\').replace('/', '\\')
            mapped_config = dict(config,allowed_roots=[str(mapped_parent)],
                                 windows_roots=[{'source':windows_root,'target':str(mapped_parent)}])
            config_path.write_text(json.dumps(mapped_config))
            def mapped_request(r):
                r['snapshot'].update(path=windows_path,tree_digest=host.tree_digest(str(mapped)))
            code,result = invoke(['python','mapped.py'],mutate=mapped_request)
            assert code == 0 and result['exit_code'] == 0 and result['stdout'].strip() == 'MAPPED-OK', result
            results['explicit_same_device_windows_mapping'] = True
            config_path.write_text(json.dumps(config))
        (snapshot/'escape').symlink_to(sentinel)
        code, result = invoke(['python','hello.py'],digest_override='0'*64)
        assert code != 0 and result.get('error'), result
        results['symlink_rejected'] = True
        containers = subprocess.run(['/usr/bin/docker','ps','-a','--filter','name=aihub-check-',
                                     '--format','{{.Names}}'],capture_output=True,text=True,check=True)
        assert not containers.stdout.strip(), containers.stdout
        results['no_owned_container_left'] = True
        print(json.dumps(results,indent=2))


if __name__ == '__main__':
    main()
