"""Execute startup code in fresh pinned, offline, read-only Docker containers.

This checks a fixed process probe, not patch correctness or full SWE success.
Generated code never executes in the host Python interpreter.
"""
from __future__ import annotations
import argparse
import ast
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import uuid

from data_pipeline.memory_unit_pilot import PYTHON, sha, write

VERSION='memory-unit-startup-grader-v1'
RULE="""import os, sys, django
from django.conf import settings
from django.apps import apps
if not settings.configured:
    sys.path.insert(0, '/testbed/tests')
    os.environ['DJANGO_SETTINGS_MODULE'] = 'test_sqlite'
if not apps.ready:
    django.setup()
"""


def code_from_text(text):
    fences=re.findall(r'```(?:python|py)?\s*\n(.*?)```',text,re.S)
    code=(fences[0] if fences else text).strip()
    if len(code)>12000:raise ValueError('startup code length limit')
    ast.parse(code)
    return code


def driver(state,code):
    # Probe/control definitions are frozen before worker output is collected.
    header='PRELUDE='+repr(state['prelude'])+'\nSTARTUP='+repr(code)+'\nPRESERVE='+repr(state['preserve_configured_settings'])+'\n'
    return header+"""import json as _json, time as _time, traceback as _traceback
_namespace={'__name__':'__startup__'}
_result={'passed':False, 'phase':'prelude'}
_start=_time.perf_counter()
try:
    exec(PRELUDE, _namespace)
    from django.conf import settings as _settings
    from django.apps import apps as _apps
    _before={'configured':_settings.configured,'apps_ready':_apps.ready}
    if _settings.configured:
        _before.update(DEBUG=_settings.DEBUG, marker=getattr(_settings,'MEMORY_PILOT_MARKER',None))
    _result['before']=_before
    _result['phase']='startup'
    exec(compile(STARTUP, '<generated-startup>', 'exec'), _namespace)
    _result['phase']='probe'
    _configured=_settings.configured
    _ready=_apps.ready
    _models=list(_apps.get_models())
    from django import forms as _forms
    class _ProbeForm(_forms.Form):
        probe_field=_forms.CharField()
    _rendered=str(_ProbeForm()['probe_field'])
    _after={'configured':_settings.configured,'apps_ready':_apps.ready,
        'DEBUG':_settings.DEBUG,'marker':getattr(_settings,'MEMORY_PILOT_MARKER',None),
        'settings_module':getattr(_settings,'SETTINGS_MODULE',None)}
    _preserved=(not PRESERVE or (_before['DEBUG']==_after['DEBUG'] and _before['marker']==_after['marker']))
    _module_ok=PRESERVE or _after['settings_module']=='test_sqlite'
    _render_ok='name="probe_field"' in _rendered
    _result.update(after=_after, configuration_preserved=_preserved,
        settings_module_correct=_module_ok, field_rendered=_render_ok,
        passed=bool(_configured and _ready and _preserved and _module_ok and _render_ok),
        phase='completed')
except BaseException as _error:
    _result.update(exception_type=type(_error).__name__,exception=str(_error),traceback=_traceback.format_exc())
_result['startup_probe_seconds']=_time.perf_counter()-_start
print('MEMORY_UNIT_RESULT='+_json.dumps(_result,sort_keys=True))
"""


def run_one(state,text,timeout=30):
    try:code=code_from_text(text)
    except (SyntaxError,ValueError) as error:
        return {'passed':False,'category':'syntax','exception':str(error),'docker_seconds':0.0}
    name='ai-memory-unit-'+uuid.uuid4().hex[:16]
    command=['docker','run','--name',name,'--rm','-i','--network','none','--cpus','1','--memory','1g',
        '--pids-limit','64','--read-only','--tmpfs','/tmp:rw,size=64m','--cap-drop','ALL',
        '--security-opt','no-new-privileges','--workdir','/testbed','--entrypoint',PYTHON,
        state['image_id'],'-B','-']
    tick=time.perf_counter()
    try:
        response=subprocess.run(command,input=driver(state,code),text=True,capture_output=True,timeout=timeout)
    except subprocess.TimeoutExpired as error:
        return {'passed':False,'category':'timeout','docker_seconds':time.perf_counter()-tick,
            'stdout':(error.stdout or b'').decode(errors='replace') if isinstance(error.stdout,bytes) else error.stdout,
            'stderr':(error.stderr or b'').decode(errors='replace') if isinstance(error.stderr,bytes) else error.stderr}
    finally:
        # Only the unique container owned by this invocation can be removed.
        subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=15)
    matches=[line[len('MEMORY_UNIT_RESULT='):] for line in response.stdout.splitlines() if line.startswith('MEMORY_UNIT_RESULT=')]
    result={'passed':False,'category':'infrastructure'}
    if response.returncode==0 and len(matches)==1:
        try:result=json.loads(matches[0])
        except ValueError:pass
        if result.get('passed'):result['category']='passed'
        elif result.get('phase')=='startup':result['category']='startup_exception'
        elif result.get('phase')=='probe':result['category']='probe_failure'
        elif result.get('phase')=='completed':result['category']='existing_settings_changed' if not result.get('configuration_preserved') else 'probe_failure'
    result.update(returncode=response.returncode,stdout=response.stdout,stderr=response.stderr,
        docker_seconds=time.perf_counter()-tick,code_sha256=hashlib.sha256(code.encode()).hexdigest())
    return result


def states_at(data):
    for name,digest in json.loads((data/'artifact-index.json').read_text())['sha256'].items():
        if sha(data/name)!=digest:raise ValueError('Frozen pilot data changed')
    return {r['state_id']:r for r in map(json.loads,(data/'states.jsonl').read_text().splitlines())}


def controls(data,output):
    if output.exists():raise FileExistsError(output)
    rows=[]
    for state in states_at(data).values():
        for name,text in [('noop','pass'),('state_aware_rule',RULE),('premature_exit','raise SystemExit(0)')]:
            expected=name=='state_aware_rule' or name=='noop' and state['mode']=='ready'
            result=run_one(state,text)
            rows.append({'state_id':state['state_id'],'control':name,'expected_pass':expected,'result':result})
    report={'status':'passed' if all(r['result']['passed']==r['expected_pass'] for r in rows) else 'failed',
        'grader_version':VERSION,'grader_sha256':sha(Path(__file__)),'states':len(states_at(data)),
        'controls':rows,'generated_model_code':False,'model_calls':0,'notes':'Controls test probe readiness and setting preservation; no memory-effect label.'}
    write(output,report)
    return {'status':report['status'],'controls':len(rows)}


def evaluate(data,bundle,predictions,output):
    if output.exists():raise FileExistsError(output)
    states=states_at(data)
    jobs={r['job_id']:r for r in map(json.loads,(bundle/'jobs.jsonl').read_text().splitlines())}
    generations=list(map(json.loads,predictions.read_text().splitlines()))
    if len(generations)!=len(jobs) or {r['job_id'] for r in generations}!=set(jobs):raise ValueError('Incomplete worker results')
    output.mkdir(parents=True)
    rows=[]
    with (output/'judgments.jsonl').open('w') as stream:
        for row in generations:
            job=jobs[row['job_id']]
            if any(row[k]!=job[k] for k in ['state_id','action','split','messages_sha256','repeat_of']):raise ValueError('Unaligned generation')
            result=run_one(states[job['state_id']],row['text'])
            judged={k:job[k] for k in ['job_id','state_id','action','split','repeat_of']}
            judged.update(mode=states[job['state_id']]['mode'],result=result,
                input_tokens=row['input_tokens'],output_tokens=row['output_tokens'],generation_seconds=row['seconds'],
                hit_token_limit=row['hit_token_limit'])
            stream.write(json.dumps(judged,ensure_ascii=False)+'\n');stream.flush();rows.append(judged)
    base=[r for r in rows if r['repeat_of'] is None]
    grid=defaultdict(dict)
    for row in base:grid[row['state_id']][row['action']]=row
    summary={}
    for action in sorted({r['action'] for r in base}):
        rs=[r for r in base if r['action']==action]
        summary[action]={'states':len(rs),'successes':sum(r['result']['passed'] for r in rs),
            'improvements_over_none':sum(r['result']['passed'] and not grid[r['state_id']]['none']['result']['passed'] for r in rs),
            'harms_vs_none':sum(not r['result']['passed'] and grid[r['state_id']]['none']['result']['passed'] for r in rs),
            'input_tokens':sum(r['input_tokens'] for r in rs),'output_tokens':sum(r['output_tokens'] for r in rs),
            'generation_seconds':sum(r['generation_seconds'] for r in rs),
            'failures':{kind:sum(r['result']['category']==kind for r in rs) for kind in sorted({r['result']['category'] for r in rs if not r['result']['passed']})}}
    fixed=max(v['successes'] for v in summary.values());oracle=sum(any(r['result']['passed'] for r in arms.values()) for arms in grid.values())
    report={'status':'complete','created_at':datetime.now(timezone.utc).isoformat(),'grader_version':VERSION,'grader_sha256':sha(Path(__file__)),
        'source_families':1,'states':len(states),'worker_generations':len(generations),'primary_contrast_success_count':summary['both']['successes']-summary['procedure']['successes'],
        'arms':summary,'best_fixed_successes':fixed,'observed_action_oracle':oracle,'selection_opportunity_over_best_fixed':oracle-fixed,
        'informative_states':sum(len({r['result']['passed'] for r in arms.values()})>1 for arms in grid.values()),
        'worker_prediction_sha256':sha(predictions),'input_index_sha256':sha(data/'artifact-index.json'),
        'input_manifest_sha256':sha(bundle/'manifest.json'),'judgments_sha256':sha(output/'judgments.jsonl'),
        'interpretation':'Diagnostic startup-probe success, not SWE patch success or independent-source generalization. All arms retained.'}
    write(output/'summary.json',report)
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--data',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--controls',action='store_true');p.add_argument('--bundle',type=Path);p.add_argument('--predictions',type=Path);a=p.parse_args()
    value=controls(a.data,a.output) if a.controls else evaluate(a.data,a.bundle,a.predictions,a.output)
    print(json.dumps(value,ensure_ascii=False))
