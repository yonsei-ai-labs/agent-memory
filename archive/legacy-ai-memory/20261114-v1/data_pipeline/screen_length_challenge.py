"""Targeted source screening for an evaluation-only length-confusable scope set."""
from __future__ import annotations
from collections import defaultdict
import hashlib
import itertools
import json
from pathlib import Path
import pyarrow.parquet as pq
from tokenizers import Tokenizer
from data_pipeline.scope_expansion import selected_scopes,render,sha


def main():
    root=Path(__file__).resolve().parents[1]
    plan_path=root/'docs/design-data/scope-length-challenge-20261108/selection-plan.json'
    plan=json.loads(plan_path.read_text())
    source=root/'training/memory_selector/outputs/overnight-20261108/source-cache/swe-smith-tool-00000.parquet'
    if sha(source)!=plan['candidate_parquet_sha256']:raise ValueError('Parquet changed')
    tokenizer_path=root/'training/memory_selector/outputs/20261107T144957Z/artifacts/tokenizer/tokenizer.json'
    tokenizer=Tokenizer.from_file(str(tokenizer_path))
    def tokens(t):return len(tokenizer.encode(t,add_special_tokens=False).ids)
    low,high=plan['candidate_row_range_inclusive'];index=0;candidates=[];groups=defaultdict(lambda:defaultdict(list));all_evidence={}
    for batch in pq.ParquetFile(source).iter_batches(batch_size=128,columns=plan['loaded_columns']):
        for record in batch.to_pylist():
            row=index;index+=1
            if row<low:continue
            if row>high:break
            part,revision,_=record['instance_id'].split('.',2);repo=part.replace('__','/',1)
            if repo in plan['excluded_repositories']:
                candidates.append({'row':row,'instance_id':record['instance_id'],'repository':repo,'status':'excluded_existing_repository'});continue
            messages=json.loads(record['messages']);created=set();actions=[]
            for mi,message in enumerate(messages):
                for call in message.get('tool_calls') or []:
                    args=json.loads(call['function']['arguments']);fn=call['function']['name']
                    if fn=='str_replace_editor' and args.get('command')=='create':created.add(args.get('path'))
                    if fn!='bash':continue
                    command=args.get('command','');scopes=selected_scopes(command)
                    if not scopes:continue
                    matches=[(oi,o) for oi,o in enumerate(messages[mi+1:mi+3],mi+1) if o['role']=='tool' and call['id'] in (o.get('tool_call_ids') or [])]
                    if len(matches)!=1:continue
                    actions.append((mi,call,command,scopes,matches[0]))
            eligible=[]
            for mi,call,command,scopes,(oi,observation) in actions:
                scopes=[s for s in scopes if '/testbed/'+s not in created and s not in created]
                if not scopes:continue
                oid=f'swesmith-tool0-row{row}-message{mi}'
                all_evidence[oid]={'parquet_row_index':row,'instance_id':record['instance_id'],'traj_id':record['traj_id'],
                    'repository':repo,'upstream_revision':revision,'raw_messages_sha256':hashlib.sha256(record['messages'].encode()).hexdigest(),
                    'message_index':mi,'tool_call':call,'command':command,'observation_index':oi,'observation':observation,
                    'created_paths_in_trace':sorted(created),'selected_scopes':scopes}
                for s in scopes:groups[repo,revision][s].append(oid)
                eligible.extend(scopes)
            candidates.append({'row':row,'instance_id':record['instance_id'],'repository':repo,'upstream_revision':revision,'status':'supported_scopes' if eligible else 'excluded_no_supported_noncreated_scope','scopes':sorted(set(eligible))})
        if index>high:break
    pools=[]
    for (repo,revision),scopes in groups.items():
        pairs=[]
        for a,b in itertools.combinations(sorted(scopes),2):
            if len(a)!=len(b) or tokens(a)!=tokens(b):continue
            # Full rendered inputs, not just isolated filename tokens.
            lengths={}
            valid=True
            for template in plan['templates']:
                cells=[render(m,q,c,template) for m,q,c in itertools.product([a,b],repeat=3)]
                sl={tokens(v['state_text']) for v in cells};ml={tokens(v['candidate_text']) for v in cells}
                if len(sl)!=1 or len(ml)!=1:valid=False
                lengths[template]={'state_tokens':sorted(sl),'memory_tokens':sorted(ml)}
            if valid:pairs.append({'scopes':[a,b],'characters':len(a),'scope_tokens':tokens(a),'rendered_known_lengths':lengths})
        if pairs:
            first=min(all_evidence[oid]['parquet_row_index'] for values in scopes.values() for oid in values)
            pools.append({'repository':repo,'upstream_revision':revision,'first_row':first,'eligible_pairs':pairs,
                          'scope_evidence':dict(scopes)})
    pools.sort(key=lambda p:(p['first_row'],p['repository'],p['upstream_revision']))
    selected=[];seen=set()
    for pool in pools:
        if pool['repository'] in seen:continue
        selected.append(pool);seen.add(pool['repository'])
        if len(selected)==plan['target_new_repository_revision_pools']:break
    chosen_ids={oid for p in selected for s in p['eligible_pairs'][0]['scopes'] for oid in p['scope_evidence'][s]}
    result={'plan_sha256':sha(plan_path),'tokenizer_sha256':sha(tokenizer_path),'candidate_rows':len(candidates),
            'candidates':candidates,'eligible_pools':pools,'selected_pools_pending_manual_and_upstream_review':selected,
            'evidence_for_review':{oid:all_evidence[oid] for oid in sorted(chosen_ids)},
            'source_outcomes_or_patches_loaded':False,'model_predictions_used':False}
    target=root/'training/memory_selector/outputs/overnight-20261108/source-cache/length-challenge-screen.json'
    if target.exists():raise FileExistsError(target)
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'candidate_rows':len(candidates),'eligible_pools':len(pools),'selected':[{k:p[k] for k in ['repository','upstream_revision','first_row','eligible_pairs']} for p in selected],'commands_for_review':len(chosen_ids)},indent=2))


if __name__=='__main__':main()
