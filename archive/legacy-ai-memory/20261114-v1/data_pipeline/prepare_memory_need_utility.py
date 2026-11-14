"""Freeze all held-family policies before new fixed-worker calls."""
import argparse
import json
from pathlib import Path
import zipfile
from tokenizers import Tokenizer
from data_pipeline.memory_decision_dataset import digest,readl,write,write_rows
from data_pipeline.prepare_memory_need_sources import upload_messages
from data_pipeline.prepare_memory_state_screen import public_rule,memory_text
from training.memory_selector.travel_state_protocol import current_messages
from training.memory_selector.worker_contract_protocol import signature
from evaluation.memory_need import manifest_check


def prepare(data,run,evaluation,output,upload):
    if output.exists():raise FileExistsError(output)
    manifest_check(data)
    summary=json.loads((evaluation/'summary.json').read_text())
    if summary['status']!='complete' or summary['run_status_sha256']!=digest((run/'run-status.json').read_bytes()):raise ValueError('Unaudited selections')
    with zipfile.ZipFile(data/'source-results.zip') as z:
        tokenizer=Tokenizer.from_str(z.read('worker-tokenizer/tokenizer.json').decode())
        worker=json.loads(z.read('worker-provenance.json'))
    choices=json.loads((evaluation/'selections.json').read_text())
    status=json.loads((run/'run-status.json').read_text());cases=[c for c in readl(data/'cases.jsonl') if c['fit_split']=='confirmation']
    labels={r['state_id']:r for r in readl(data/'labels-confirmation.jsonl')}
    policies=['none','frozen_goal',*status['models'],'required_evidence','counter_memory']
    if len(policies)!=8:raise ValueError('Wrong policy set')
    jobs={};decisions=[]
    timing={}
    for name in ['frozen_goal']+status['models']:
        rows=readl(run/name/('predictions.jsonl' if name=='frozen_goal' else 'confirmation-predictions.jsonl'))
        for r in rows:timing[name,r['state_id']]=timing.get((name,r['state_id']),0.)+r['forward_seconds']
    for c in cases:
        sid=c['state_id'];bank={m['memory_id']:m for m in c['bank']}
        for policy in policies:
            if policy=='none':ids=[]
            elif policy=='required_evidence':
                ids=public_rule(c)
                if ids!=labels[sid]['required_memory_ids']:raise ValueError('Rule and need annotation disagree')
            elif policy=='counter_memory':ids=labels[sid]['counter_memory_ids']
            else:ids=choices[policy][sid]
            selected=[bank[i] for i in ids]
            tokens=len(tokenizer.encode(memory_text(selected),add_special_tokens=False).ids) if selected else 0
            if len(ids)>c['max_records'] or tokens>c['memory_token_budget']:raise ValueError('Policy exceeded shared budget')
            messages=current_messages(c['public_text'],selected);jid=signature(messages)
            jobs[jid]={'job_id':jid,'messages':messages}
            decisions.append({'state_id':sid,'family_id':c['family_id'],'policy':policy,'selected_memory_ids':ids,
                              'memory_tokens':tokens,'selector_reader_seconds':timing.get((policy,sid),0.),'job_id':jid})
    output.mkdir(parents=True);write_rows(output/'decisions.jsonl',decisions)
    write(output/'protocol.json',{'families':len({c['family_id'] for c in cases}),'states':len(cases),'policies':policies,'decisions':len(decisions),'distinct_jobs':len(jobs),
        'selection_summary_sha256':digest((evaluation/'summary.json').read_bytes()),'run_status_sha256':digest((run/'run-status.json').read_bytes()),
        'data_manifest_sha256':digest((data/'manifest.json').read_bytes()),'label_use':'Required/counter diagnostic policies only; learned policies frozen before confirmation labels/worker outputs',
        'cost':'Reader forward time measured locally; common candidate retrieval and cached bank construction are separate costs.'})
    write(output/'manifest.json',{'sha256':{p.name:digest(p.read_bytes()) for p in output.iterdir() if p.is_file()},'source_sha256':digest(Path(__file__).read_bytes())})
    upload_messages(list(jobs.values()),'utility',worker,upload)
    return json.loads((output/'protocol.json').read_text())

if __name__=='__main__':
    p=argparse.ArgumentParser()
    for n in ('data','run','evaluation','output','upload'):p.add_argument('--'+n,type=Path,required=True)
    a=p.parse_args();print(json.dumps(prepare(a.data,a.run,a.evaluation,a.output,a.upload),indent=2))
