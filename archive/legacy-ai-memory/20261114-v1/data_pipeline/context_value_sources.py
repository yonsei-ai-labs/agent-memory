"""Pinned BEAM source intake for current-context / memory value contrasts."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import time
import urllib.request

from data_pipeline.collect_relation_source_pilot import write, writel, file_sha
from data_pipeline.prepare_external_updates import (
    _beam_messages, _beam_case, assign_splits, answer_key, _stale_value, contains, _numbers,
)

REVISION='b2da22eac88bb0874c64665f13457eb99835774a'
ROOT=Path(__file__).resolve().parents[1]
WORK=ROOT/'runs/memory-context-value-20261111-v1'
CACHE=ROOT/'.claude/worktrees/external-memory-updates/runs/external-sources/beam'
OLD_TREE=Path('/tmp/claude-1000/-home-depa-ai-memory/69e02f0f-45b3-47b9-962e-a1bfd50a9611/scratchpad/fc/beam_tree.json')
BASE='https://raw.githubusercontent.com/mohammadtavakoli78/BEAM/'+REVISION+'/'


def blob_sha(path):
    h=hashlib.sha1(('blob '+str(path.stat().st_size)+'\0').encode())
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def collect():
    WORK.mkdir(parents=True,exist_ok=True)
    tree_path=WORK/'source-tree.json'
    if not tree_path.exists():
        if OLD_TREE.exists():tree_path.write_bytes(OLD_TREE.read_bytes())
        else:
            url='https://api.github.com/repos/mohammadtavakoli78/BEAM/git/trees/'+REVISION+'?recursive=1'
            with urllib.request.urlopen(url,timeout=60) as f:tree_path.write_bytes(f.read())
    tree=json.loads(tree_path.read_text())
    if tree['sha']!=REVISION or tree.get('truncated'):raise ValueError('Incomplete or different source tree')
    selected=[r for r in tree['tree'] if (len(r['path'].split('/'))==4 and r['path'].endswith('/chat.json'))
              or (len(r['path'].split('/'))==5 and r['path'].endswith('/probing_questions/probing_questions.json'))]
    def fetch(item):
        rel=item['path'];cached=CACHE/rel;path=cached if cached.exists() else WORK/'raw'/rel
        if not path.exists():
            path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix('.part')
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(BASE+rel,timeout=90) as r,temp.open('wb') as f:
                        total=0
                        while chunk:=r.read(1024*1024):
                            total+=len(chunk)
                            if total>item['size']:raise ValueError('Oversized source')
                            f.write(chunk)
                    temp.rename(path);break
                except Exception:
                    temp.unlink(missing_ok=True)
                    if attempt==2:raise
                    time.sleep(1)
        if path.stat().st_size!=item['size'] or blob_sha(path)!=item['sha']:raise ValueError('Changed pinned source: '+rel)
        return {'relative_path':rel,'local_path':str(path),'url':BASE+rel,'git_blob_sha1':item['sha'],
                'sha256':file_sha(path),'bytes':path.stat().st_size}
    records=[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        for record in pool.map(fetch,selected):
            records.append(record)
            if len(records)%20==0:print('Pinned source files',len(records),'/',len(selected),flush=True)
    write(WORK/'source-files.json',{'revision':REVISION,'files':records})
    return records


def screen():
    paths={r['relative_path']:Path(r['local_path']) for r in json.loads((WORK/'source-files.json').read_text())['files']}
    cases=[];ledger=[];manual=[]
    folders=sorted({str(Path(r).parent) for r in paths if r.endswith('/chat.json')})
    groups=['beam:'+f.split('/')[1]+'-'+f.split('/')[2] for f in folders]
    splits=assign_splits(groups)
    existing=[json.loads(l) for l in (ROOT/'datasets/external_memory_updates/20261111-v1/cases.jsonl').read_text().splitlines()]
    for row in existing:
        if row['source']=='beam' and splits[row['family_id']]!=row['split']:raise ValueError('Changed legacy source partition')
    write(WORK/'source-partitions.json',splits)
    for folder,group in zip(folders,groups):
        chat=paths[folder+'/chat.json'];questions=json.loads(paths[folder+'/probing_questions/probing_questions.json'].read_text())
        batches=json.loads(chat.read_text())
        if batches and 'turns' not in batches[0]:
            batches=[batch for plan in batches for nested in plan.values() for batch in nested]
        ids=[m['id'] for batch in batches for turn in batch['turns'] for m in turn]
        if len(ids)!=len(set(ids)):
            for index,_ in enumerate(questions.get('knowledge_update',[]),1):
                ledger.append({'case_id':group+':ku'+str(index),'source_group':group,'split':splits[group],
                               'screen_reason':'duplicate_message_ids_in_native_conversation'})
            print('Excluded ambiguous message IDs',group,flush=True);continue
        messages=_beam_messages(batches,group)
        for index,q in enumerate(questions.get('knowledge_update',[]),1):
            raw=_beam_case(q,group+':ku'+str(index),group,messages);raw['split']=splits[group]
            reason=raw['exclusion'];accept=[answer_key(v) for v in raw['accept_values']]
            old=' '.join(m['text'] for m in raw['old']);new=' '.join(m['text'] for m in raw['new'])
            stale,note=_stale_value(raw,accept,old,new) if not reason else (None,None)
            if not reason and not stale and len(accept)==1 and accept[0]['kind']=='number':
                options=[n for n in _numbers(raw['stale_hint'] or '') if n!=accept[0]['value'] and contains(old,{'kind':'number','value':n})]
                if len(options)==1:stale={'kind':'number','value':options[0]}
            if not reason and (len(accept)!=1 or accept[0]['kind']!='number'):reason='not_one_numeric_target'
            if not reason and not contains(new,accept[0]):reason='new_value_not_in_new_evidence'
            if not reason and not stale:reason='old_value_not_grounded_in_source_note_and_old_text'
            if not reason and max(m['order'] for m in raw['old'])>=min(m['order'] for m in raw['new']):reason='nonchronological_evidence'
            ledger.append({'case_id':raw['case_id'],'source_group':group,'split':raw['split'],'screen_reason':reason})
            if reason=='old_value_not_grounded_in_source_note_and_old_text':
                manual.append({k:v for k,v in raw.items() if k!='pool'})
            if reason:continue
            cases.append({k:raw[k] for k in ('case_id','family_id','split','question','old','new','stale_hint')}
                         |{'old_value':stale['value'],'new_value':accept[0]['value'],'chat_sha256':file_sha(chat)})
        print('Screened',group,flush=True)
    writel(WORK/'candidate-cases.jsonl',cases);writel(WORK/'screening-ledger.jsonl',ledger)
    writel(WORK/'manual-screen-candidates.jsonl',manual)
    return {'source_groups':len(groups),'questions':len(ledger),'candidate_questions':len(cases),
            'candidate_groups':len({r['family_id'] for r in cases})}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['collect','screen']);a=p.parse_args()
    result=collect() if a.mode=='collect' else screen()
    print(json.dumps({'files':len(result)} if isinstance(result,list) else result,indent=2))
