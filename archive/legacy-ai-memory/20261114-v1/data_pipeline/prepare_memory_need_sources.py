"""Select unused train families before any new historical choices are generated."""
import argparse,json,zipfile
from collections import Counter
from pathlib import Path
from data_pipeline.memory_decision_dataset import ROOT,digest,write,write_rows
from data_pipeline.prepare_memory_state_screen import SOURCE,escaped_catalog_text
from training.memory_selector.travel_state_protocol import past_messages
from training.memory_selector.worker_contract_protocol import signature
from training.memory_selector.memory_need_worker_protocol import configuration,validate_inputs

SOURCE_DATA=ROOT/'datasets/travel_state_transfer/20261109-v1'
CONFIG=ROOT/'training/memory_selector/configs/memory-need-v1.json'
MODELS=ROOT/'training/memory_selector/data/travel-state-pilot-v2'


def key(value):return digest(('memory-need-v1:'+str(value)).encode())


def upload_messages(rows,phase,worker,output):
    if output.exists():raise FileExistsError(output)
    output.mkdir(parents=True)
    write_rows(output/'inputs.jsonl',rows)
    write(output/'config.json',configuration(phase,len(rows)))
    write(output/'worker-provenance.json',worker)
    write(output/'input-manifest.json',{'sha256':{n:digest((output/n).read_bytes()) for n in ('inputs.jsonl','config.json','worker-provenance.json')}})
    validate_inputs(output)


def prepare(output,upload):
    if output.exists():raise FileExistsError(output)
    archive=SOURCE_DATA/'source-specifications.zip'
    if digest(archive.read_bytes())!=json.loads((SOURCE_DATA/'archive-sha256.json').read_text())[archive.name]:raise ValueError('Source changed')
    with zipfile.ZipFile(archive) as z:
        allfamilies=json.loads(z.read('families.json')); jobs=json.loads(z.read('source-jobs.json'))
        cases=json.loads(z.read('cases.json')); catalog=json.loads(z.read('catalog.json'))
    config=json.loads(CONFIG.read_text()); chosen=[]
    for days in (3,5,7):
        pool=sorted([f for f in allfamilies if f['split']=='train' and not f['pilot'] and f['days']==days],key=lambda f:key(f['family_id']))
        offset=0
        for split,n in config['splits_per_duration'].items():
            chosen.extend(dict(f,original_split=f['split'],fit_split=split) for f in pool[offset:offset+n]);offset+=n
    if len(chosen)!=24 or len({f['family_id'] for f in chosen})!=24:raise ValueError('Bad selection')
    ids={f['family_id'] for f in chosen}; selected_jobs=[j for j in jobs if j['family_id'] in ids]
    selected_cases=[c for c in cases if c['family_id'] in ids and c['mode']=='reuse']
    used={i for f in chosen for menu in f['menus'].values() for i in menu}; cat={i:catalog[i] for i in sorted(used)}
    with zipfile.ZipFile(SOURCE) as z:provenance=json.loads(z.read('artifacts/weight-provenance.json'))
    model_hashes={n:digest((MODELS/n).read_bytes()) for n in provenance['adapter_and_head_sha256']}
    if model_hashes!=provenance['adapter_and_head_sha256']:raise ValueError('Frozen trained models differ')
    inputs=[]
    for j in selected_jobs:
        ms=past_messages(j,cat); menu=list(dict.fromkeys(i for t in j['tasks'] for i in t['menu_ids']))
        ms[1]['content']=ms[1]['content'].split('Catalog columns:')[0]+'In catalog names, \\| denotes a literal vertical bar.\nCatalog columns: ID | kind | name | price | rating | attributes | available\n'+escaped_catalog_text(cat,menu)
        jid=signature(ms);j['worker_job_id']=jid;inputs.append({'job_id':jid,'messages':ms})
    output.mkdir(parents=True)
    write(output/'families.json',chosen);write(output/'history-jobs.json',selected_jobs);write(output/'parent-cases.json',selected_cases);write(output/'catalog.json',cat)
    write(output/'config.json',config)
    write(output/'protocol.json',{'families':len(chosen),'fit_splits':dict(Counter(f['fit_split'] for f in chosen)),'source_jobs':len(inputs),
       'unused_train_before_selection':sum(f['split']=='train' and not f['pilot'] for f in allfamilies),
       'original_dev_and_test_used':False,'source_archive_sha256':digest(archive.read_bytes()),'selection':'hash order within 3/5/7-day groups; 4 train, 2 dev, 2 confirmation each',
       'scope':'New request families in the same source pool; shared catalogs/cities remain. Historical choices newly generated; current statuses are controlled interventions.',
       'worker':provenance['worker'],'frozen_model_sha256':model_hashes,
       'eligibility':'Keep every selected source prediction. Derive contrasts only where valid distinct dinner/hotel choices exist; disclose attrition without replacement.'})
    write(output/'manifest.json',{'sha256':{p.name:digest(p.read_bytes()) for p in output.iterdir() if p.is_file()},'source_sha256':digest(Path(__file__).read_bytes())})
    upload_messages(inputs,'history',provenance['worker'],upload)
    return {'families':len(chosen),'fit_splits':dict(Counter(f['fit_split'] for f in chosen)),'history_calls':len(inputs),'new_worker_calls':0}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--upload',type=Path,required=True)
    a=p.parse_args();print(json.dumps(prepare(a.output,a.upload),indent=2))
