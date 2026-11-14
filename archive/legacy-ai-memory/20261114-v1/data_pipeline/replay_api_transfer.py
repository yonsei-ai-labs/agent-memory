"""Repeat the frozen authored executions under their exact package versions."""
from __future__ import annotations
import argparse
import importlib.metadata
import json
from pathlib import Path
import platform
import time
from data_pipeline.api_transfer import sha
from data_pipeline import api_transfer_runtime as runtime


def replay(data):
    environment=json.loads((data/'evidence/environment.json').read_text())
    if sha(Path(runtime.__file__))!=environment['runtime_code_sha256']:raise ValueError('Use the frozen runtime producer')
    if platform.python_version()!=environment['python']:raise ValueError('Python version differs')
    for name,version in environment['packages'].items():
        if importlib.metadata.version(name)!=version:raise ValueError('Package version differs: '+name)
    fixtures=[{r['case_id']:r for r in json.loads((data/'evidence'/name).read_text())}
              for name in ['catalog.json','confirmation-fixtures.json']]
    records=json.loads((data/'evidence/executions.json').read_text());started=time.monotonic()
    for expected in records:
        spec=fixtures[expected['fixture_id']][expected['case_id']]
        actual=runtime.run_case(spec,expected['recipe'],expected['goal'],expected['fixture_id'])
        for key,value in expected.items():
            if key!='elapsed_seconds' and actual[key]!=value:
                raise ValueError('Fresh execution differs: '+expected['case_id']+' '+key)
    return {'status':'passed','fresh_executions_reproduced':len(records),'seconds':time.monotonic()-started,
            'ignored_comparison_fields':['elapsed_seconds'],'environment':environment,'gpu_used':False,'model_calls':0}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('data',type=Path)
    print(json.dumps(replay(p.parse_args().data),ensure_ascii=False,indent=2))
