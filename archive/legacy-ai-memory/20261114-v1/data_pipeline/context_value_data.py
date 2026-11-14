"""Source passages, reviews and same-bank current-context interventions."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re

from data_pipeline.collect_relation_source_pilot import digest, file_sha, readl, writel, write
from data_pipeline.prepare_external_updates import contains, lexical_order, _beam_messages
from data_pipeline.context_value_sources import ROOT, WORK

DATA=ROOT/'datasets/memory_context_value/20261111-v1'
TOKENIZER=Path.home()/'.cache/huggingface/hub/models--Qwen--Qwen3.5-4B/snapshots/851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a/tokenizer.json'


def tokenizer():
    from tokenizers import Tokenizer
    return Tokenizer.from_file(str(TOKENIZER))


def passages(record,tok):
    """Uniform verbatim prose units; code fences omitted, never generated text."""
    text=record['text'];spans=[];cursor=0
    for match in re.finditer(r'```[\s\S]*?(?:```|\Z)',text):
        spans.append((cursor,match.start()));cursor=match.end()
    spans.append((cursor,len(text)))
    for a,b in spans:
        for match in re.finditer(r'\S[\s\S]*?(?=\n\s*\n|\Z)',text[a:b]):
            start=a+match.start();end=a+match.end()
            while end>start and text[end-1].isspace():end-=1
            quote=text[start:end];n=len(tok.encode(quote,add_special_tokens=False).ids)
            if not quote or n>384:continue
            yield {'record_id':'p-'+digest(record['record_id']+':'+str(start)+':'+str(end))[:20],
                'source_record_id':record['record_id'],'order':record['order'],'time':record['time'],
                'text':quote,'tokens':n,'char_start':start,'char_end':end,'source_text':text,'source_text_sha256':digest(text)}


def review_candidates():
    tok=tokenizer();rows=[]
    candidates=readl(WORK/'candidate-cases.jsonl')
    overrides_path=ROOT/'docs/design-data/memory-context-value-20261111-v1/manual-source-overrides.json'
    if overrides_path.exists():
        raw={r['case_id']:r for r in readl(WORK/'manual-screen-candidates.jsonl')}
        partitions=json.loads((WORK/'source-partitions.json').read_text())
        for override in json.loads(overrides_path.read_text()):
            c=raw[override['case_id']]
            candidates.append({**c,**override,'split':partitions[c['family_id']]})
    for c in candidates:
        row={k:c[k] for k in ['case_id','family_id','split','question','old_value','new_value']}
        for role in ['old','new']:
            units=[p for r in c[role] if r['speaker']=='user' for p in passages(r,tok)]
            selected=[p for p in units if contains(p['text'],{'kind':'number','value':c[role+'_value']})
                      or (c.get(role+'_literal') and c[role+'_literal'] in p['text'])]
            ranked=lexical_order(c['question'],selected);lookup={p['record_id']:p for p in selected}
            row[role+'_passages']=[lookup[i] for i in ranked]
        rows.append(row)
    writel(WORK/'review-candidates.jsonl',rows)
    return {'candidates':len(rows),'both_have_passage':sum(bool(r['old_passages'] and r['new_passages']) for r in rows)}


def bank_draft():
    """Prepare candidates for human/agent source review, never infer negatives."""
    tok=tokenizer();out=[]
    reviews=json.loads((ROOT/'docs/design-data/memory-context-value-20261111-v1/source-review.json').read_text())
    candidates={r['case_id']:r for r in readl(WORK/'review-candidates.jsonl')}
    files=json.loads((WORK/'source-files.json').read_text())['files'];cache={}
    for review in reviews:
        if not review['include']:continue
        c=candidates[review['case_id']];group=c['family_id'];size,number=group.split(':')[1].split('-')
        if group not in cache:
            entry=next(r for r in files if r['relative_path']==f'chats/{size}/{number}/chat.json')
            raw=json.loads(Path(entry['local_path']).read_text())
            if raw and 'turns' not in raw[0]:raw=[batch for plan in raw for nested in plan.values() for batch in nested]
            messages=_beam_messages(raw,group)
            cache[group]=[p for r in messages.values() if r['speaker']=='user' for p in passages(r,tok) if len(p['text'])>=40]
        pool=cache[group];lookup={p['record_id']:p for p in pool}
        old=[lookup[i] for i in review['old_passage_ids']];new=[lookup[i] for i in review['new_passage_ids']]
        for rid in review.get('extra_old_source_ids',[]):
            units=[p for p in pool if p['source_record_id']==rid]
            if len(units)!=1:raise ValueError('Context anchor needs explicit paragraph review: '+rid)
            old+=units
        cutoff=min(p['order'] for p in new);allowed=[p for p in pool if p['order']<cutoff]
        goal=review.get('goal',c['question']);ranked=lexical_order(goal,allowed);byid={p['record_id']:p for p in allowed}
        bank=list(old);seen={' '.join(p['text'].split()).lower() for p in old}
        for rid in ranked:
            p=byid[rid];norm=' '.join(p['text'].split()).lower()
            if norm in seen:continue
            bank.append(p);seen.add(norm)
            if len(bank)>=8:break
        # Low lexical overlap is only a candidate unrelated observation; it must
        # still be read, along with all candidate bank records, before freezing.
        noise=[byid[i] for i in reversed(ranked) if i not in {p['record_id'] for p in bank}]
        noise.sort(key=lambda p:(abs(p['tokens']-sum(x['tokens'] for x in old)),digest(c['case_id']+p['record_id'])))
        out.append({'case_id':c['case_id'],'source_group':group,'split':c['split'],'goal':goal,'original_question':c['question'],
                    'review':review,'old':old,'new':new,'bank':bank,'unrelated_candidates':noise[:3]})
    writel(WORK/'bank-draft.jsonl',out)
    return {'cases':len(out),'groups':len({r['source_group'] for r in out}),'bank_records':sum(len(r['bank']) for r in out)}


def build():
    if DATA.exists():raise FileExistsError(DATA)
    design=ROOT/'docs/design-data/memory-context-value-20261111-v1'
    review={r['case_id']:r for r in json.loads((design/'bank-review.json').read_text())}
    tok=tokenizer();cases=[];labels=[];records={};provenance={};ledger=[]
    def record(p,group):
        mid=p['record_id'];r={'memory_id':mid,'source_group':group,'text':p['text'],'order':p['order'],'time':p['time']}
        records[mid]=r
        provenance[mid]={'memory_id':mid,**{k:p[k] for k in ['source_record_id','char_start','char_end','source_text','source_text_sha256']}}
        return r
    for draft in readl(WORK/'bank-draft.jsonl'):
        rev=review[draft['case_id']];ledger.append(rev)
        if not rev['include']:continue
        group=draft['source_group'];bank=[record(p,group) for p in draft['bank'] if not rev['bank'][p['record_id']]['exclude']]
        # Deterministic label-blind order; neither original evidence order nor
        # target-first draft position is exposed to a selector or tie breaker.
        bank.sort(key=lambda m:digest(draft['case_id']+'|'+m['memory_id']))
        old=[record(p,group) for p in draft['old']];new=[record(p,group) for p in draft['new']]
        noise=record(next(p for p in draft['unrelated_candidates'] if p['record_id']==rev['unrelated_id']),group)
        groups=rev['support_groups'];positive=set(x for g in groups for x in g);unknown={m for m,v in rev['bank'].items() if v['relation']=='unknown'}
        if any(p['order']>=min(n['order'] for n in new) for p in bank):raise ValueError('Future bank record')
        for mode in ['missing','known','updated','unrelated']:
            observed={'missing':[],'known':old,'updated':new,'unrelated':[noise]}[mode]
            sid=draft['case_id']+':'+mode
            obs='\n\n'.join(p['text'] for p in observed) or '(No current observations.)'
            case={'state_id':sid,'case_id':draft['case_id'],'source_group':group,'source':'beam','split':draft['split'],'mode':mode,
                  'goal':rev.get('goal',draft['goal']),'observations':obs,'observation_records':observed,'bank':bank}
            lab={};roles={}
            for m in bank:
                mid=m['memory_id']
                if mode=='known':lab[mid]=0;roles[mid]='already_sufficient' if mid in positive else 'non_support'
                elif mid in unknown:roles[mid]='unknown'
                elif mid in positive:
                    if mode=='updated' and draft['review']['updated_old_label']=='unknown':roles[mid]='unknown_scope_context'
                    else:lab[mid]=int(mode in ('missing','unrelated'));roles[mid]='missing_support' if lab[mid] else 'superseded'
                else:lab[mid]=0;roles[mid]='non_support'
            oracle=[p['memory_id'] for p in old] if mode in ('missing','unrelated') or (mode=='updated' and draft['review']['updated_old_label']=='unknown') else []
            cases.append(case);labels.append({'state_id':sid,'case_id':draft['case_id'],'source_group':group,'split':draft['split'],'mode':mode,
              'labels':lab,'roles':roles,'support_groups':groups,'oracle_ids':oracle,'old_ids':[p['memory_id'] for p in old],
              'expected_value':draft['review']['new_value' if mode=='updated' else 'old_value'],
              'current_numeric_answer_available':mode in ('known','updated'),'scope_context_missing':mode=='updated' and draft['review']['updated_old_label']=='unknown',
              'label_kind':'source_relation_not_worker_utility'})
            if len(tok.encode('\n\n'.join(p['text'] for p in old),add_special_tokens=False).ids)>512:raise ValueError('Reviewed old support exceeds budget')
    DATA.mkdir(parents=True)
    for split in ('train','dev','confirm'):
        writel(DATA/(split+'-cases.jsonl'),[r for r in cases if r['split']==split]);writel(DATA/(split+'-labels.jsonl'),[r for r in labels if r['split']==split])
    writel(DATA/'records.jsonl',list(records.values()));writel(DATA/'provenance.jsonl',list(provenance.values()))
    writel(DATA/'bank-review.jsonl',ledger);writel(DATA/'source-review.jsonl',json.loads((design/'source-review.json').read_text()))
    writel(DATA/'screening-ledger.jsonl',readl(WORK/'screening-ledger.jsonl'))
    sources=json.loads((WORK/'source-files.json').read_text());sources['files']=[{k:v for k,v in r.items() if k!='local_path'} for r in sources['files']]
    write(DATA/'source-files.json',sources);write(DATA/'protocol.json',json.loads((design/'protocol.json').read_text()))
    counts={s:{'groups':len({c['source_group'] for c in cases if c['split']==s}),'questions':len({c['case_id'] for c in cases if c['split']==s}),'states':sum(c['split']==s for c in cases),'labelled_pairs':sum(len(l['labels']) for l in labels if l['split']==s)} for s in ('train','dev','confirm')}
    write(DATA/'manifest.json',{'experiment':'memory-context-value-20261111-v1','counts':counts,'source_groups':len({c['source_group'] for c in cases}),'sha256':{p.name:file_sha(p) for p in sorted(DATA.iterdir())},'design_sha256':{p.name:file_sha(p) for p in sorted(design.glob('*.json'))},'builder_sha256':file_sha(Path(__file__))})
    return counts


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['review-candidates','bank-draft','build']);a=p.parse_args()
    print(json.dumps({'review-candidates':review_candidates,'bank-draft':bank_draft,'build':build}[a.mode](),indent=2))
