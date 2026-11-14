"""Collect exact public base-tree files in isolated containers, without model calls."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'experiments/swe_memory/scripts'))
from records import now, write_json
from datasets import Dataset

HERE = Path(__file__).resolve().parent
FILES = {
    'pvlib/pvlib-python': ['pvlib/irradiance.py', 'pvlib/iam.py', 'pvlib/tools.py'],
    'sphinx-doc/sphinx': ['sphinx/ext/autodoc/__init__.py', 'sphinx/util/inspect.py',
        'sphinx/util/typing.py', 'sphinx/ext/autodoc/typehints.py', 'sphinx/ext/viewcode.py'],
    'django/django': ['django/db/models/sql/query.py', 'django/forms/widgets.py',
        'django/forms/boundfield.py', 'django/forms/formsets.py', 'tests/test_sqlite.py'],
    'pytest-dev/pytest': ['src/_pytest/mark/evaluate.py', 'src/_pytest/skipping.py',
        'src/_pytest/outcomes.py', 'src/_pytest/python.py'],
    'scikit-learn/scikit-learn': ['sklearn/preprocessing/label.py', 'sklearn/preprocessing/_label.py',
        'sklearn/preprocessing/_encoders.py', 'sklearn/mixture/base.py', 'sklearn/mixture/_base.py',
        'sklearn/cluster/affinity_propagation_.py', 'sklearn/utils/validation.py'],
    'sympy/sympy': ['sympy/matrices/expressions/blockmatrix.py', 'sympy/physics/units/unitsystem.py',
        'sympy/physics/units/dimensions.py', 'sympy/physics/units/prefixes.py'],
}
PUBLIC_FIELDS = ['instance_id', 'repo', 'base_commit', 'created_at', 'version', 'problem_statement']
EXTRA_CONTEXTS = ['sympy__sympy-19007', 'sympy__sympy-22840',
                  'sphinx-doc__sphinx-8595', 'sphinx-doc__sphinx-8721']
NAMESPACE = 'ai-memory-process07'


def run_container(image, program, timeout=90):
    name = 'ai-memory-relcase-' + uuid.uuid4().hex[:12]
    arguments = ['docker', 'run', '--rm', '--name', name, '--network=none', '--read-only',
        '--cpus=1', '--memory=1g', '--tmpfs', '/tmp:rw,size=64m', '--entrypoint', 'python', image, '-c', program]
    start = time.monotonic()
    try:
        result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        return dict(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
                    seconds=time.monotonic() - start, image_id=image,
                    resources=dict(cpus=1, memory='1g', network='none', root_filesystem='read-only'))
    finally:
        # Remove only this collector's uniquely named container if a timeout left it alive.
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def collect_one(job):
    folder = HERE / 'evidence/public-code'
    path = folder / (job['task']['instance_id'] + '.json')
    if path.exists():
        stored = json.loads(path.read_text())
        assert stored['public_task'] == job['task'] and stored['image_id'] == job['image_id']
        return dict(instance_id=job['task']['instance_id'], reused=True, files=len(stored['files']))
    # Compatible with Python3.6 in the oldest image. Only the supplied base is read.
    program = """import hashlib,json,os,subprocess,sys
base=BASE
paths=PATHS
rows=[]
for path in paths:
    result=subprocess.run(['git','-C','/testbed','show',base+':'+path],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    current='/testbed/'+path
    original=open(current,'rb').read() if os.path.isfile(current) else None
    rows.append(dict(path=path,exists_at_base=result.returncode==0,
        text=result.stdout.decode('utf-8') if result.returncode==0 else None,
        base_sha256=hashlib.sha256(result.stdout).hexdigest() if result.returncode==0 else None,
        filesystem_matches_base=(original==result.stdout) if result.returncode==0 else original is None))
print(json.dumps(dict(files=rows,python=sys.version)))
""".replace('BASE', repr(job['task']['base_commit'])).replace('PATHS', repr(job['paths']))
    result = run_container(job['image_id'], program)
    if result['returncode'] != 0:
        raise RuntimeError('Public code collector failed for ' + job['task']['instance_id'] + ': ' + result['stderr'][-1000:])
    payload = json.loads(result.pop('stdout'))
    write_json(path, dict(at=now(), model_api_calls=0, public_task=job['task'],
        image_id=job['image_id'], prior_actor_target=job['prior_actor_target'],
        provenance=job['provenance'], files=payload['files'], image_python=payload['python'], execution=result,
        policy='git show exact public base commit only; no target patch,test_patch,grader answer or future Git history was read.'))
    return dict(instance_id=job['task']['instance_id'], reused=False, files=len(payload['files']),
                filesystem_mismatches=[r['path'] for r in payload['files'] if not r['filesystem_matches_base']])


def collect():
    plan_path = HERE / 'collection-plan.json'
    if plan_path.exists():
        plan = json.loads(plan_path.read_text())
    else:
        records_path = ROOT / 'docs/reports/data/runs.json'
        records = json.loads(records_path.read_text()); current = {}
        for row in records:
            if row.get('record_kind') != 'target_trial':
                continue
            directory = ROOT / 'runs' / row['record_id']
            if not (directory / 'task.json').exists():
                continue
            public = json.loads((directory / 'task.json').read_text())
            manifest = json.loads((directory / 'manifest.json').read_text())
            # Later records favor the repaired exact-tree images; no outcome filter.
            current[row['instance_id']] = dict(task={k: public[k] for k in PUBLIC_FIELDS},
                image_id=manifest['image_id'], paths=FILES[public['repo']], prior_actor_target=True,
                provenance=dict(manifest=str((directory / 'manifest.json').relative_to(ROOT))))
        arrow = ROOT / '.cache/huggingface/datasets/princeton-nlp___swe-bench_lite/default/0.0.0/6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2/swe-bench_lite-test.arrow'
        selected = {r['instance_id']: {k: r[k] for k in PUBLIC_FIELDS}
                    for r in Dataset.from_file(str(arrow)) if r['instance_id'] in EXTRA_CONTEXTS}
        for iid, public in selected.items():
            name = NAMESPACE + '/sweb.eval.x86_64.' + iid.replace('__', '_1776_').replace('/', '_') + ':latest'
            image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', name], text=True))[0]
            current[iid] = dict(task=public, image_id=image['Id'], paths=FILES[public['repo']],
                prior_actor_target=False, provenance=dict(public_dataset_revision='6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2',
                    source_tree_audit='docs/reports/data/process-restoration-20261107/source-tree-audit.json'))
        plan = dict(at=now(), stage='public_context_collection_before_new_annotations_or_runtime_probes',
            model_api_calls=0, neural_training_runs=0, context_jobs=[current[k] for k in sorted(current)],
            selection='All17 existing target tasks plus4 previously prepared public contexts, without outcome-based filtering. '
                'Existing outcomes are known discovery material;4 new contexts are not newly validated SWE agent trials.',
            input_records_sha256=hashlib.sha256(records_path.read_bytes()).hexdigest())
        write_json(plan_path, plan)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(collect_one, plan['context_jobs']))
    write_json(HERE / 'public-collection.json', dict(at=now(), model_api_calls=0,
        context_tasks=len(results), prior_actor_tasks=sum(j['prior_actor_target'] for j in plan['context_jobs']),
        new_context_only_tasks=sum(not j['prior_actor_target'] for j in plan['context_jobs']),
        rows=results))
    print(json.dumps(dict(status='collected', public_contexts=len(results),
        original_actor_tasks=17, new_public_contexts=4, model_api_calls=0), ensure_ascii=False))


if __name__ == '__main__':
    collect()
