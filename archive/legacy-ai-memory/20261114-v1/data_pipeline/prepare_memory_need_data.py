"""Source-grounded need labels are separate from later measured worker utility."""
import argparse,json,shutil,zipfile
from collections import Counter
from itertools import permutations
from pathlib import Path
from tokenizers import Tokenizer
from data_pipeline.memory_decision_dataset import ROOT,digest,readl,write,write_rows
from data_pipeline.prepare_memory_need_sources import key
from data_pipeline.prepare_memory_state_screen import memory_text,escaped_catalog_text,public_rule
from training.memory_selector.travel_state_protocol import make_record,parse_past,references


def build_cases(parent,bank,catalog,fit_split):
    slots=('dinner','accommodation');by={}
    for m in bank:
        if m['slot'] in slots and m['choice'] in catalog:by.setdefault((m['person'],m['day']),{})[m['slot']]=m
    candidates=[(a,b) for a,b in permutations(by,2) if set(by[a])==set(slots) and set(by[b])==set(slots) and a[0]!=b[0] and all(by[a][s]['choice']!=by[b][s]['choice'] for s in slots)]
    if not candidates:return [],[],[]
    a,b=min(candidates,key=lambda pair:key((parent['family_id'],pair)))
    primary=[by[a][s] for s in slots];alternate=[by[b][s] for s in slots]
    ref=lambda ms:f"{ms[0]['person']}'s dinner on day {ms[0]['day']} and {ms[1]['person']}'s accommodation on day {ms[1]['day']}"
    reuse=(f"For my dinner, choose the same restaurant as {primary[0]['person']}'s dinner on day {primary[0]['day']}. "
           f"For my accommodation, choose the same hotel as {primary[1]['person']}'s accommodation on day {primary[1]['day']}. Each chosen place must currently be available.")
    retry=f"For dinner and accommodation, start by reusing {ref(primary)}. If the current tool report says FIRST_ATTEMPT_FAILED, instead reuse {ref(alternate)}. Each chosen place must currently be available."
    menu=parent['menus']['restaurant']+parent['menus']['hotel'];current={i:dict(catalog[i],available=True) for i in menu}
    cases=[];labels=[];relations=[]
    for mode,goal in [('observed_component',reuse),('retry_branch',retry)]:
        for state in range(3):
            if mode=='observed_component':
                active=primary;observed=[primary[state]] if state<2 else primary
                status='Earlier lookup completed; its verified result is listed below.'
                k,budget=1,128;counter=[primary[state]] if state<2 else [alternate[0]]
            else:
                active=primary if state==0 else alternate;observed=[] if state<2 else alternate
                status=('FIRST_ATTEMPT_PENDING: no checkout attempt has been made.' if state==0 else 'FIRST_ATTEMPT_FAILED: checkout for the primary plan failed; use the alternative plan.')
                k,budget=2,256;counter=alternate if state==0 else primary
            known={m['memory_id'] for m in observed};needed=[m['memory_id'] for m in active if m['memory_id'] not in known]
            sid=key((parent['family_id'],mode,state))[:24]
            context=status+'\n'+memory_text(observed)
            public=f"Current city: {parent['city']}.\n{goal}\nCurrent tool observations:\n{context}\nIn catalog names, \\| denotes a literal vertical bar.\nCurrent catalog columns: ID | kind | name | price | rating | attributes | available\n"+escaped_catalog_text(current,menu)
            case={'state_id':sid,'family_id':parent['family_id'],'fit_split':fit_split,'parent_case_id':parent['case_id'],
                  'mode':mode,'state':state,'goal':goal,'status_text':status,'selection_context':context,'public_text':public,
                  'observed':observed,'bank':bank,'max_records':k,'memory_token_budget':budget,'current_state_origin':'controlled intervention'}
            if public_rule(case)!=needed:raise ValueError('Public rule mismatch')
            labels.append({'state_id':sid,'family_id':parent['family_id'],'required_memory_ids':needed,'acceptable':{s:[active[i]['choice']] for i,s in enumerate(slots)},'counter_memory_ids':[m['memory_id'] for m in counter],'actual_gain_or_harm':None})
            for m in bank:
                linked=any(all(m[k]==v for k,v in r.items()) for r in references(goal))
                active_match=m['memory_id'] in {x['memory_id'] for x in active}
                relations.append({'state_id':sid,'memory_id':m['memory_id'],'goal_link':linked,'active_goal_link':active_match,
                    'currently_observed':m['memory_id'] in known,'valid_source_choice':m['choice'] is not None,'needed_now':m['memory_id'] in needed,
                    'evidence_location':{'source_memory_id':m['memory_id'],'source_text_field':'evidence','current_observation_ids':sorted(known)},'actual_utility':None})
            cases.append(case)
    return cases,labels,relations


def prepare(source,history,output):
    if output.exists():raise FileExistsError(output)
    manifest=json.loads((source/'manifest.json').read_text())
    for n,h in manifest['sha256'].items():
        if digest((source/n).read_bytes())!=h:raise ValueError('Changed selected sources')
    status=json.loads((history/'run-status.json').read_text())
    if status['status']!='complete' or not status['stopped']:raise ValueError('History not recovered and stopped')
    artifacts=history/'artifacts';pred={p['job_id']:p for p in readl(artifacts/'predictions.jsonl')}
    jobs=json.loads((source/'history-jobs.json').read_text());cat=json.loads((source/'catalog.json').read_text())
    fam={f['family_id']:f for f in json.loads((source/'families.json').read_text())};parents=json.loads((source/'parent-cases.json').read_text())
    if set(pred)!={j['worker_job_id'] for j in jobs}:raise ValueError('Incomplete historical choices')
    records={};record_audit=[]
    for j in jobs:
        p=pred[j['worker_job_id']];choices=parse_past(p['text'],len(j['tasks']))
        for t,choice in zip(j['tasks'],choices):
            m=make_record(t,choice,cat,t['menu_ids']);records[m['memory_id']]=m;g=t['goal']
            valid=[i for i in t['menu_ids'] if g['attribute'] in cat[i]['attributes'] and cat[i]['rating']>=g['min_rating'] and cat[i]['price']<=g['max_price']]
            best=min(valid,key=lambda i:(cat[i]['price'],i))
            record_audit.append({'memory_id':m['memory_id'],'job_id':j['worker_job_id'],'choice':m['choice'],'valid_catalog_choice':m['choice'] is not None,'original_request_satisfied':m['choice'] in valid,'original_cheapest_tiebreak_satisfied':m['choice']==best})
    cases=[];labels=[];relations=[];excluded=[]
    for p in parents:
        bank=sorted([records[m['memory_id']] for m in p['memories']],key=lambda m:key(m['memory_id']))
        cs,ys,rs=build_cases(p,bank,cat,fam[p['family_id']]['fit_split'])
        if not cs:excluded.append({'family_id':p['family_id'],'fit_split':fam[p['family_id']]['fit_split'],'reason':'No valid distinct pair of actual dinner/hotel histories'})
        cases+=cs;labels+=ys;relations+=rs
    counts=Counter(c['fit_split'] for c in cases)
    for split,minimum in [('train',8),('dev',3),('confirmation',3)]:
        if counts[split]<minimum*6:raise ValueError('Insufficient source yield; preserve source outputs, no training')
    tok=Tokenizer.from_file(str(artifacts/'worker-tokenizer/tokenizer.json'));by={c['state_id']:c for c in cases}
    for y in labels:
        c=by[y['state_id']];bank={m['memory_id']:m for m in c['bank']}
        for ids in (y['required_memory_ids'],y['counter_memory_ids']):
            tokens=len(tok.encode(memory_text([bank[i] for i in ids]),add_special_tokens=False).ids) if ids else 0
            if len(ids)>c['max_records'] or tokens>c['memory_token_budget']:raise ValueError('Budget violation')
    output.mkdir(parents=True);write_rows(output/'cases.jsonl',cases);write_rows(output/'record-relations.jsonl',relations)
    for split in ('train','dev','confirmation'):write_rows(output/('labels-'+split+'.jsonl'),[y for y in labels if by[y['state_id']]['fit_split']==split])
    write_rows(output/'source-record-audit.jsonl',record_audit);write(output/'source-records.json',records)
    shutil.copy2(history/'results.zip',output/'source-results.zip')
    write(output/'protocol.json',{'experiment':'memory-need-20261111-v1','states':len(cases),'families':len(cases)//6,'states_by_split':dict(counts),'excluded':excluded,
        'actual_new_history_calls':len(pred),'valid_catalog_records':sum(r['valid_catalog_choice'] for r in record_audit),'records':len(records),
        'request_satisfied_records':sum(r['original_request_satisfied'] for r in record_audit),'cheapest_request_records':sum(r['original_cheapest_tiebreak_satisfied'] for r in record_audit),
        'relation_rows':len(relations),'current_state_origin':'controlled, not naturally observed failures','worker_outcome_labels':0,
        'selector_state_input':'goal plus public status and observed record facts; catalog retained for fixed worker, not needed-now reader',
        'source_manifest_sha256':digest((source/'manifest.json').read_bytes()),'actual_history_archive_sha256':digest((history/'results.zip').read_bytes())})
    write(output/'manifest.json',{'sha256':{p.name:digest(p.read_bytes()) for p in output.iterdir() if p.is_file()},'generator_sha256':digest(Path(__file__).read_bytes())})
    return json.loads((output/'protocol.json').read_text())

if __name__=='__main__':
    p=argparse.ArgumentParser()
    for n in ('source','history','output'):p.add_argument('--'+n,type=Path,required=True)
    a=p.parse_args();print(json.dumps(prepare(a.source,a.history,a.output),indent=2))
