"""Record generated legacy module wrappers separately from the tracked Git tree."""
import concurrent.futures
import json
from pathlib import Path
import sys

from collect_public_contexts import run_container

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'experiments/swe_memory/scripts'))
from records import now, write_json

HERE = Path(__file__).resolve().parent


def collect_one(iid):
    destination = HERE / 'evidence/compatibility-wrappers' / (iid + '.json')
    if destination.exists():
        return iid
    context = json.loads((HERE / 'evidence/public-code' / (iid + '.json')).read_text())
    program = ("import hashlib,json\np='/testbed/sklearn/preprocessing/label.py'\n"
               "data=open(p,'rb').read()\nprint(json.dumps({'path':p,'text':data.decode('utf-8'),'sha256':hashlib.sha256(data).hexdigest()}))")
    result = run_container(context['image_id'], program, interpreter='/opt/miniconda3/envs/testbed/bin/python')
    assert result['returncode'] == 0, iid
    payload = json.loads(result.pop('stdout'))
    write_json(destination, dict(at=now(), context_task=iid, image_id=context['image_id'],
        wrapper=payload, execution=result, model_api_calls=0,
        note='Public runtime filesystem file; newer sklearn generates this compatibility shim even when old path is absent from the base Git tree.'))
    return iid


if __name__ == '__main__':
    tasks = ['scikit-learn__scikit-learn-12471', 'scikit-learn__scikit-learn-15512', 'scikit-learn__scikit-learn-15535']
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(collect_one, tasks))
    print('Recorded', len(rows), 'runtime module files.')
