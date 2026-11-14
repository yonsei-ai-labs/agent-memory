"""Build reviewed source-scale data; applicability labels are not utility labels.

Raw native answers, latent persona profiles and source IDs are never model features.
Candidate donors stay within the split AND within each nested training source set.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re

from tokenizers import Tokenizer
from data_pipeline.source_scale_data import source_rows, WORK, ROOT, INTAKE, PILOT, literal
from data_pipeline.collect_relation_source_pilot import digest, readl, writel, write, file_sha, PM_REVISION, MA_REVISION
from evaluation.travel_reference_data import bm25

DEST = ROOT / 'datasets/memory_source_scale/20261111-v1'
CONFIG = ROOT / 'training/memory_selector/configs/source-scale-v1.json'


def matches(text, aliases):
    return any(re.search(r'(?<!\w)' + re.escape(a).replace(r'\ ', r'\s+') + r'(?!\w)', text, re.I) for a in aliases)


def spans(text):
    """Keep actual user text, including exact offsets, without quoted assistant text."""
    wrapped = list(re.finditer(r'(?m)^Inquirer: ?(.*?)(?=\nResponse assistant:|\nInquirer:|\n\n Please continue|\Z)', text, re.S))
    if wrapped:
        return [(m.start(1), m.end(1)) for m in wrapped]
    return [(0, len(text))]


def read_reviews(reviews=None):
    root=Path(reviews) if reviews else WORK
    train = json.loads((root/('train-reviews.json' if reviews else 'train-review-spec.json')).read_text())
    heldout = json.loads((root/('heldout-reviews.json' if reviews else 'heldout-review-spec.json')).read_text())
    return train, heldout


def build(dest=DEST,reviews=None):
    if dest.exists(): raise FileExistsError(dest)
    persona, questions, sessions, groups, index, reserved = source_rows()
    train, heldout = read_reviews(reviews)
    ma_review = {**train['ma'], **heldout['ma']}
    pm_review = {**train['pm'], **heldout['pm']}
    qmap = {q['qa_id']:q for q in questions}
    pmap = {str(r['_csv_row']):r for r in persona}
    # Fixed nested groups: 54 PersonaMem and 66 Mem2Act. Two additional reviewed
    # train components are retained in the attrition ledger, not fitted.
    ma_groups = sorted({index[qmap[q]['source_conversation_ids'][0]] for q in train['ma']}, key=lambda g:digest('scale-order:'+g))[:66]
    pm_groups = sorted({'persona:'+pmap[r]['persona_id'] for r in train['pm']}, key=lambda g:digest('scale-order:'+g))
    if len(pm_groups) != 54 or len(ma_groups) != 66: raise ValueError('Unexpected approved training group count')
    # Round-robin gives nested 15+15, 30+30, 66+54 domain counts.
    order = [g for i in range(max(len(ma_groups),len(pm_groups))) for seq in (ma_groups,pm_groups) if i<len(seq) for g in [seq[i]]]
    assignment = dict(reserved)
    for g in order:
        if assignment.get(g) not in (None,'train'): raise ValueError('Attempted heldout migration')
        assignment[g]='train'
    tok_path = Path.home()/'.cache/huggingface/hub/models--Qwen--Qwen3.5-4B/snapshots/851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a/tokenizer.json'
    if file_sha(tok_path)!='5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42':raise ValueError('Changed worker tokenizer')
    tokenizer=Tokenizer.from_file(str(tok_path))
    nt=lambda text:len(tokenizer.encode(text,add_special_tokens=False).ids)
    records={}; origin={}; diagnostics=[]
    def add(text,group,source,turn,start,end,full):
        if not text.strip():return None
        mid='m-'+digest(group+'|'+source+'|'+str(turn)+'|'+str(start)+'|'+text)[:18]
        if nt(text)>480:
            diagnostics.append({'source_group':group,'reason':'source_turn_over_480_worker_tokens','source_id':source,'turn':turn})
            return None
        records[mid]={'memory_id':mid,'text':text,'source_group':group,'tokens':nt(text)}
        origin[mid]={'memory_id':mid,'source_group':group,'source_id':source,'turn_index':turn,'char_start':start,'char_end':end,
            'source_text':full,'source_text_sha256':digest(full),'role':'user','synthetic_public_source':True}
        assert full[start:end]==text
        return mid
    relevant_groups=set(order)
    for q in heldout['ma']:relevant_groups.add(index[qmap[q]['source_conversation_ids'][0]])
    source_records=defaultdict(list)
    for sess in sessions:
        for i,t in enumerate(sess['turns']):
            source=t.get('source_id');group=index.get(source)
            if group not in relevant_groups or t['role']!='user':continue
            full=t['content']
            for start,end in spans(full):
                mid=add(full[start:end],group,source,i,start,end,full)
                if mid and mid not in source_records[source]:source_records[source].append(mid)
    tasks=[]; ledger=[]
    for q in questions:
        group=index.get(q['source_conversation_ids'][0]) if q['source_conversation_ids'] else None
        split=assignment.get(group)
        if split not in ('train','dev','test'):continue
        if q['qa_id'] not in ma_review:
            ledger.append({'task_id':q['qa_id'],'source_group':group,'split':split,'status':'not_approved','reason':'missing_or_ambiguous_fields_or_not_selected_for_narrow_grounded_subset'})
            continue
        if split=='train' and group not in order:
            ledger.append({'task_id':q['qa_id'],'source_group':group,'split':split,'status':'reviewed_reserve','reason':'fixed_120_source_cap'})
            continue
        spec=ma_review[q['qa_id']];facts=spec['facts']
        mids=list(dict.fromkeys(m for source in q['source_conversation_ids'] for m in source_records[source]))
        coverage={m:[key for key,aliases in facts.items() if matches(records[m]['text'],aliases)] for m in mids}
        provided=set(x for v in coverage.values() for x in v)
        missing=set(facts)-provided
        if missing:
            ledger.append({'task_id':q['qa_id'],'source_group':group,'split':split,'status':'rejected_span_check','missing':sorted(missing)})
            continue
        tasks.append({'task_id':q['qa_id'],'source':'mem2act','source_group':group,'split':split,'goal':q['query'],'facts':facts,
            'coverage':coverage,'positive_ids':[m for m in mids if coverage[m]],'source_record_ids':mids,
            'utility_eligible':spec['utility_eligible'],'native_arguments':q['tool_call']['arguments'],'tool_schema':q['target_tool_schema'],'review_scope':spec['scope']})
        ledger.append({'task_id':q['qa_id'],'source_group':group,'split':split,'status':'accepted','review_scope':spec['scope']})
    history_manifest=json.loads((WORK/'persona-history-manifest.json').read_text())
    for rid,spec in pm_review.items():
        r=pmap[rid];group='persona:'+r['persona_id'];split=assignment[group]
        snippet=literal(r['related_conversation_snippet']);quote=max((t['content'] for t in snippet if t['role']=='user'),key=len)
        path=WORK/'raw'/Path(r['chat_history_32k_link']).name
        if file_sha(path)!=history_manifest[path.name]['sha256']:raise ValueError('Changed persona history')
        hist=json.loads(path.read_text())['chat_history']
        found=[i for i,t in enumerate(hist) if t['role']=='user' and t['content']==quote]
        if not found:raise ValueError('Reviewed user quote absent from source history: '+rid)
        mid=add(quote,group,r['persona_id'],found[0],0,len(quote),quote)
        if mid is None:raise ValueError('Reviewed PM turn too long: '+rid)
        tasks.append({'task_id':'pm-row-'+rid,'source':'personamem_v2','source_group':group,'split':split,'goal':literal(r['user_query'])['content'],
            'positive_ids':[mid],'source_record_ids':[mid],'review_scope':spec['scope'],'utility_eligible':False,'facts':{'reviewed_context':[]},'coverage':{mid:['reviewed_context']}})
        ledger.append({'task_id':'pm-row-'+rid,'source_group':group,'split':split,'status':'accepted_sparse_annotation','review_scope':spec['scope']})
    actual={t['source_group'] for t in tasks if t['split']=='train'}
    if actual!=set(order):raise ValueError('Training groups lost in grounding checks: '+str(set(order)-actual))
    dest.mkdir(parents=True)
    writel(dest/'records.jsonl',list(records.values()));writel(dest/'provenance.jsonl',list(origin.values()))
    writel(dest/'review-ledger.jsonl',ledger);write(dest/'train-reviews.json',train);write(dest/'heldout-reviews.json',heldout)
    write(dest/'source-order.json',order);write(dest/'source-reservation.json',assignment)
    write(dest/'excluded-turns.json',diagnostics)
    write(dest/'worker-tokenizer.json',{'model':'Qwen/Qwen3.5-4B','revision':tok_path.parent.name,'sha256':file_sha(tok_path)})
    # Use source-proven candidate facts only. External records containing any
    # target alias are left unlabelled and excluded, never forced negative.
    def make(subtasks, pool_groups):
        cases=[];labels=[]
        for t in subtasks:
            own=set(t['source_record_ids']);positives=t['positive_ids']
            donors=[m for m,r in records.items() if r['source_group'] in pool_groups and
                ((t['source']=='mem2act')==(r['source_group'].startswith('mem2act:'))) and m not in positives]
            if t['source']=='mem2act':
                donors=[m for m in donors if not any(matches(records[m]['text'],a) for a in t['facts'].values())]
            # PM donors are context controls only, NOT labelled irrelevant.
            scores=bm25(t['goal'],[records[m]['text'] for m in donors])
            neg=[m for _,m in sorted(zip(scores,donors),key=lambda x:(-x[0],x[1]))]
            bankids=list(dict.fromkeys(positives+neg[:max(0,10-len(positives))]))
            bankids=sorted(bankids,key=lambda m:digest(t['task_id']+m))
            if not neg:raise ValueError('No context control available')
            # Greedy factual cover, with existing goal facts taken into account.
            present={k for k,a in t['facts'].items() if a and matches(t['goal'],a)}
            needed=set(t['facts'])-present;covered=set();support=[]
            while needed-covered:
                m=max(positives,key=lambda m:(len(set(t['coverage'][m])&(needed-covered)),-records[m]['tokens'],m))
                new=set(t['coverage'][m])&(needed-covered)
                if not new:raise ValueError('No complete reviewed cover')
                covered.update(new);support.append(m)
            known='\n\n'.join(records[m]['text'] for m in support)
            # Closest length controls reduce, but cannot eliminate, style shortcuts.
            unrelated=min(neg,key=lambda m:(abs(records[m]['tokens']-nt(known)),m))
            for mode,observed in [('missing',''),('known',known),('unrelated',records[unrelated]['text'])]:
                sid=t['task_id']+'--'+mode
                remaining=[] if mode=='known' else sorted(needed)
                bank=[records[m] for m in bankids]
                cases.append({'state_id':sid,'task_id':t['task_id'],'source_group':t['source_group'],'source':t['source'],'split':t['split'],
                    'mode':mode,'goal':t['goal'],'observations':observed,'bank':bank,'controlled_state':True})
                y={m:int(bool(set(t['coverage'].get(m,[]))&set(remaining))) for m in bankids}
                if t['source']=='personamem_v2':y={m:y[m] for m in positives}
                labels.append({'state_id':sid,'task_id':t['task_id'],'source_group':t['source_group'],'source':t['source'],
                    'labels':y,'remaining_facts':remaining,'coverage':{m:t['coverage'].get(m,[]) for m in bankids},
                    'oracle_ids':support if remaining else [],'utility':None,'sparse':t['source']=='personamem_v2'})
        return cases,labels
    counts={}
    for n in (30,60,120):
        selected=set(order[:n]);sub=[t for t in tasks if t['split']=='train' and t['source_group'] in selected]
        cases,labels=make(sub,selected);writel(dest/f'train-{n}-cases.jsonl',cases);writel(dest/f'train-{n}-labels.jsonl',labels)
        counts['train-'+str(n)]={'groups':n,'native_queries':len(sub),'controlled_states':len(cases),'labelled_pairs':sum(len(x['labels']) for x in labels)}
    for split in ('dev','test'):
        sub=[t for t in tasks if t['split']==split];pool={t['source_group'] for t in sub}
        cases,labels=make(sub,pool);writel(dest/(split+'-cases.jsonl'),cases);writel(dest/(split+'-labels.jsonl'),labels)
        counts[split]={'groups':len(pool),'native_queries':len(sub),'controlled_states':len(cases),'labelled_pairs':sum(len(x['labels']) for x in labels)}
    writel(dest/'tasks-supervision-only.jsonl',tasks)
    manifest={'version':'source-scale-v1','counts':counts,'training_order':order,'source_revisions':{'mem2act':MA_REVISION,'personamem_v2':PM_REVISION},
        'review':'One agent reviewed source passages before training; no independent human adjudication.',
        'limits':['Synthetic benchmark histories, not new natural user episodes.','Controlled observation states, not real agent execution or failure history.',
                  'PersonaMem labels sparse: only reviewed contextual passage is supervised.','Mem2Act candidates are a curated cross-source pool, not full benchmark recall.',
                  'Related/missing information labels are not benefit/harm labels.','Nested training banks use only the corresponding source subset.'],
        'config_sha256':file_sha(CONFIG),'sha256':{p.name:file_sha(p) for p in dest.iterdir() if p.is_file()}}
    write(dest/'manifest.json',manifest)
    return manifest['counts']


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=DEST);p.add_argument('--reviews',type=Path);a=p.parse_args();print(json.dumps(build(a.output,a.reviews),indent=2))
