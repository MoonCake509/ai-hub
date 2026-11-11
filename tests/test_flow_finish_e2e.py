"""Exercise the isolated hub, workspace worker and actual Docker checks.

The five model responses are deterministic fixtures. The coder fixture performs
only its prescribed edits inside a fresh checkout. No provider calls are made.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

if os.environ.get('CORE03_HUB_URL') != 'http://127.0.0.1:8102':
    raise SystemExit('This fixture requires the isolated FLOW stand on port 8102')

source = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(source/'tools'))
import core03_stage_worker as worker

base = Path(os.environ['CORE03_FIXTURE_ROOT']).resolve(strict=True)
workspace_dir = Path(os.environ['AIHUB_WORKSPACE_DIR']).resolve(strict=True)
assert base != workspace_dir
fixture_run_id = None


def api(method, path, payload=None, expected=200):
    status, body = worker.http(method, path, payload)
    assert status in (expected if isinstance(expected, tuple) else (expected,)), (path,status,body)
    return body


def main():
    global fixture_run_id
    with tempfile.TemporaryDirectory(prefix='flow-', dir=base) as directory:
        fixture=Path(directory)
        for name in ('src','docs','tests'):
            (fixture/name).mkdir()
        (fixture/'src/a.py').write_text('VALUE = 0  # SOURCE-MARKER-913\n')
        (fixture/'docs/contract.md').write_text('CONTRACT-MARKER-513: VALUE must be 2.\n')
        (fixture/'tests/check.py').write_text('from pathlib import Path\nns={}\nexec(Path("src/a.py").read_text(),ns)\nassert ns["VALUE"]==2, "unexpected value"\nprint("ONE-ASSERTION-PASSED")\n')
        (fixture/'auth.json').write_text('SYNTHETIC-DENIED-MARKER-732')
        admin=workspace_dir/'flow-e2e-admin.json'
        image=subprocess.check_output(['/usr/bin/docker','image','inspect','--format','{{.Id}}','python:3.12-slim'],text=True).strip()
        assert re.fullmatch('sha256:[a-f0-9]{64}',image)
        admin.write_text(json.dumps({'allowed_roots':[str(workspace_dir)],'image':image,'docker':'/usr/bin/docker'}))
        runner_argv=json.dumps([sys.executable,str(source/'tools/docker_check_runner.py'),'--config',str(admin)])
        os.environ.pop('AIHUB_ISOLATED_RUNNER_ARGV', None)
        assert worker.beat()[0]==200
        from_ui='--from-ui' in sys.argv
        name='FLOW Browser Acceptance' if from_ui else 'FLOW finish fixture'
        project=api('POST','/projects',{'name':name,'host_id':'laptop','workdir':str(fixture),
            'settings':{'checks':{'fixture':{'cmd':['python','tests/check.py'],'timeout_s':20}}}},expected=(200,201))
        request={'project_id':project['id'],'template_id':'five-roles','goal':'Set the fixture value to 2',
            'entry':'src/a.py','contracts':['docs/contract.md'],'criteria':['Actual assertion passes'],
            'request_key':uuid.uuid4().hex,'policy':{'max_cycles':1,'max_dispatches':10,'max_seconds':180}}
        unavailable=api('POST','/runs',request,expected=409)
        assert unavailable['detail']['code']=='runner_unavailable', unavailable
        assert not api('GET','/runs?project_id='+project['id'])['items']
        os.environ['AIHUB_ISOLATED_RUNNER_ARGV']=runner_argv
        assert worker.beat()[0]==200
        if from_ui:
            run_id=None
            deadline=time.monotonic()+180
            while time.monotonic()<deadline:
                items=api('GET','/runs?project_id='+project['id'])['items']
                if items:
                    run_id=items[0]['id']
                    assert items[0]['goal']==request['goal']
                    break
                worker.beat()
                time.sleep(1)
            assert run_id, 'Browser did not create a run for the fixture'
        else:
            run_id=api('POST','/runs',request,expected=202)['run_id']
            assert api('POST','/runs',request)['run_id']==run_id
        fixture_run_id=run_id
        model_steps=[]
        deadline=time.monotonic()+150
        while time.monotonic()<deadline:
            run=api('GET','/runs/'+run_id)
            if run['state'] in ('done','failed','cancelled'):
                break
            workspace=worker.http('GET','/bridge/jobs?worker=core03-stage&agents=workspace&protocol=2&kinds=workspace')
            if workspace[0]==200 and workspace[1].get('id'):
                worker.execute(workspace[1])
                worker.beat()
                continue
            status, job=worker.http('GET','/bridge/jobs?worker=core03-stage&agents=codex&protocol=2&kinds=agent')
            if status!=200 or not job.get('id'):
                time.sleep(0.3)
                continue
            run=api('GET','/runs/'+run_id)
            task=next(t for t in run['tasks'] if t.get('job_id')==job['id'])
            step=task['key']
            model_steps.append(step)
            prompt=job['prompt']
            assert request['goal'] in prompt
            assert 'SYNTHETIC-DENIED-MARKER-732' not in prompt
            if step=='plan':
                assert 'CONTRACT-MARKER-513' in prompt
                output={'tasks':[{'key':'edit','title':'Set the value','files':['src/a.py'],'acceptance':'Actual assertion passes'}],'risks':[]}
            elif step=='code':
                assert 'SOURCE-MARKER-913' in prompt
                value=model_steps.count('code')
                if value==2:
                    assert 'VALUE = 1' in prompt
                    assert 'unexpected value' in prompt, 'Retry omitted the real check failure'
                (fixture/'src/a.py').write_text(f'VALUE = {value}  # SOURCE-MARKER-913\n')
                output={'summary':'Edited fixture value','files':['src/a.py'],'notes':None}
            elif step=='test':
                assert 'ONE-ASSERTION-PASSED' in prompt
                output={'ok':True,'candidate_digest':run['candidate_digest'],'checks':[{'name':'fixture','cmd':['python','tests/check.py'],'ok':True}]}
            elif step=='review':
                output={'verdict':'approve','candidate_digest':run['candidate_digest'],'findings':[]}
            else:
                assert step=='archive'
                (fixture/'docs/result.md').write_text('Value 2 passed the real fixture assertion.\n')
                output={'summary':'Recorded the checked result','artifacts':[{'path':'docs/result.md','kind':'doc'}],'decisions':[]}
            api('POST','/bridge/result',{'job_id':job['id'],'attempt_id':job['attempt_id'],'ok':True,
                'output':json.dumps(output),'usage':{}})
        run=api('GET','/runs/'+run_id)
        assert run['state']=='done', (run['state'],[(t['key'],t['state'],t.get('error')) for t in run['tasks']],run['events'][-6:])
        assert model_steps==['plan','code','code','test','review','archive'],model_steps
        assert run['cycles']==1
        assert run['check_receipt']['checks'][0]['exit_code']==0
        assert run['check_receipt']['snapshot_id']==run['candidate_snapshot_id']
        assert run['check_receipt']['isolation']['network']=='none'
        assert (fixture/'src/a.py').read_text().startswith('VALUE = 2')
        print(json.dumps({'run_id':run_id,'state':'done','steps':model_steps,'cycles':run['cycles'],
            'real_runner':True,'fixture_model_responses':True,'started_in_browser':from_ui}))


if __name__=='__main__':
    try:
        main()
    except Exception:
        if fixture_run_id:
            try:
                worker.http('POST','/runs/'+fixture_run_id+'/cancel',{})
            except OSError:
                pass
        raise
