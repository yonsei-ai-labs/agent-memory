"""Verify the fixed 48-run pilot and public-message review without model calls.

The optional --write-new mode creates the two initial analysis artifacts only.
This script is specific to the frozen 20261114 development pilot.
"""
import argparse
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.analysis.tau2_delivery import audit_delivery
from agent_memory_eval.contracts.core import digest
from agent_memory_eval.recording.journal import BlobStore

parser=argparse.ArgumentParser(description='Recheck the fixed 20261114 delivery pilot and its public-message annotations; no model calls.')
parser.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[1])
parser.add_argument('--annotations',type=Path,help='Manual sequence annotations; default: the saved review artifact')
parser.add_argument('--write-new',action='store_true',help='Write the two new artifacts; existing outputs are never overwritten')
args=parser.parse_args()
repo=args.repo.resolve()
analysis_path=repo/'artifacts/analyses/20261114-tau2-delivery-behavior-review.json'
receipt_path=repo/'artifacts/pilots/20261114-tau2-delivery-development-results.json'
if args.write_new:
    assert args.annotations is not None and not analysis_path.exists() and not receipt_path.exists()
root=repo/'runs/tau2-delivery-luna-deepseek1017-development-01'
summary=audit_delivery(root)
assert summary['status']=='completed' and len(summary['runs'])==48
blobs=BlobStore(root)
raw_log=(root/'log.jsonl').read_bytes()
assert digest(raw_log)==summary['log_sha256']
records=[json.loads(line) for line in raw_log.splitlines()]
state=blobs.get(records[-1]['state_ref'])
experiment=summary['experiment_id']
events=[event for record in records for event in record['events']]
by_seq={e['event_seq']:e for e in events}
bound=state['delivery_comparison']
for path,sha in bound['benchmark_assets'].items():
    assert digest((repo/'data/tau2-bench'/path).read_bytes())==sha,path
original_source=bound['requested']['frozen_source']
assert digest((repo/original_source['path']/'log.jsonl').read_bytes())==original_source['log_sha256']
lock_path=repo/'artifacts/pilots/20261114-tau2-delivery-development-protocol.json'
lock=json.loads(lock_path.read_text())
for ref in [lock[k] for k in ['profile_ref','protocol_note_ref','annotation_rubric_ref','candidate_manifest_ref']]+lock['implementation_files']:
    assert digest((repo/ref['path']).read_bytes())==ref['sha256'], ref['path']
manual=json.loads(args.annotations.read_text()) if args.annotations is not None else json.loads(analysis_path.read_text())['manual_annotations']
assert len(manual)==48
evidence={}
def text(m):
    content=m.get('content')
    if not isinstance(content,str): return content
    try:
        value=json.loads(content)
    except ValueError:
        return content
    if isinstance(value,dict):
        for key in ['message','response']:
            if isinstance(value.get(key),str): return value[key]
    return content

def keep(seq,run):
    if seq is None: return None
    e=by_seq[seq]
    assert e['run_id']==run and e['type']=='business.transition'
    ref=e['payload']['public_message_ref']
    m=blobs.get(ref)
    selected=dict(event_id=e['event_id'],run_id=run,public_message_ref=ref,role=m['role'])
    if m.get('tool_calls'):
        selected['tool_calls']=[dict(name=c['function']['name'],arguments=json.loads(c['function']['arguments']) if isinstance(c['function']['arguments'],str) else c['function']['arguments']) for c in m['tool_calls']]
    elif m['role']=='tool':
        try: value=json.loads(m.get('content'))
        except (TypeError,ValueError): value=m.get('content')
        if isinstance(value,dict) and 'reservation_id' in value:
            selected['selected_reservation_fields']={k:value[k] for k in ['reservation_id','user_id','cabin','insurance','created_at','status','passengers'] if k in value}
        else: selected['content']=value
    else: selected['content']=text(m)
    evidence[str(seq)]=selected
    return seq

role_totals=defaultdict(lambda:dict(requests=0,reported_cost_usd_decimal=Decimal(0),prompt_tokens=0,completion_tokens=0,reported_tokens=0,reasoning_tokens=0,cached_prompt_tokens=0))
models=Counter()
providers=Counter()
reviews=[]
for row in summary['runs']:
    run=row['run_id']
    own=[e for e in events if e['run_id']==run]
    public=[]
    for e in own:
        if e['type']=='business.transition' and 'public_message_ref' in e['payload']:
            public.append((e,blobs.get(e['payload']['public_message_ref'])))
    calls=[(e,c) for e,m in public for c in (m.get('tool_calls') or [])]
    observed_calls=[dict(name=c['function']['name'],arguments=json.loads(c['function']['arguments']) if isinstance(c['function']['arguments'],str) else c['function']['arguments']) for e,c in calls]
    assert observed_calls==row['actions']
    transfers=[e['event_seq'] for e,c in calls if c['function']['name']=='transfer_to_human_agents']
    mutations=[(e,c) for e,c in calls if c['function']['name'].startswith(('update_','cancel_','book_'))]
    reservation_results=[e['event_seq'] for e,m in public if m['role']=='tool' and isinstance(m.get('content'),str) and '"reservation_id"' in m['content'] and '"passengers"' in m['content']]
    request_actor={e['payload']['request_id']:e['payload']['actor_role'] for e in own if e['type']=='model.request_started'}
    for e in own:
        if e['type']!='model.request_finished': continue
        p=e['payload']
        actor=request_actor[p['request_id']]
        t=role_totals[actor]
        t['requests']+=1
        assert p['status']=='response' and p['delivery_status']!='not_submitted' and p['cost_status']=='observed'
        response=blobs.get(p['response_ref'])
        models[response['model']]+=1
        providers[response['provider']]+=1
        usage=p['usage']
        t['reported_cost_usd_decimal']+=Decimal(str(p['cost_usd']))
        for key in ['prompt_tokens','completion_tokens']: t[key]+=usage.get(key,0)
        t['reported_tokens']+=usage.get('total_tokens',0)
        t['reasoning_tokens']+=(usage.get('completion_tokens_details') or {}).get('reasoning_tokens',0)
        t['cached_prompt_tokens']+=(usage.get('prompt_tokens_details') or {}).get('cached_tokens',0)
    annotations=manual['/'.join(map(str,[row['task_id'],row['repetition'],row['condition']]))]
    for field,seq in annotations.items():
        if isinstance(seq,int):
            keep(seq,run)
            expected='assistant' if field in {'confirmation_prompt','denial','redundant_dob_request','cannot_update_claim'} else 'tool' if field=='available_dob_result' else 'user'
            assert evidence[str(seq)]['role']==expected,(field,seq)
    first_transfer=transfers[0] if transfers else None
    notices=[e['event_seq'] for e,m in public if m['role']=='assistant' and not m.get('tool_calls') and isinstance(text(m),str) and 'YOU ARE BEING TRANSFERRED TO A HUMAN AGENT. PLEASE HOLD ON.' in text(m)]
    parallel=[e['event_seq'] for e,m in public if len(m.get('tool_calls') or [])>1]
    exposure_sources=Counter(e['payload']['source']['kind'] for e in own if e['type']=='memory.input_included' and e['payload']['field']=='body' and e['payload']['source']['kind'] in {'memory_snapshot','memory_return'})
    seqs={k:keep(v,run) for k,v in annotations.items() if isinstance(v,int) or v is None}
    seqs.update(first_transfer=keep(first_transfer,run),transfer_notices=[keep(x,run) for x in notices],reservation_results=[keep(x,run) for x in reservation_results],mutation_calls=[keep(e['event_seq'],run) for e,c in mutations],parallel_call_messages=[keep(x,run) for x in parallel])
    j={k:v for k,v in annotations.items() if isinstance(v,str)}
    if annotations.get('human_request') is not None:
        assert first_transfer is not None and annotations['human_request']<first_transfer
    if row['task_id']=='40':
        assert (annotations['required_change']=='met')==row['official_success']
        if annotations['required_change']=='met':
            assert len(mutations)==1 and mutations[0][1]['function']['name']=='update_reservation_passengers'
            payload=observed_calls[next(i for i,(e,c) in enumerate(calls) if c['function']['name']=='update_reservation_passengers')]['arguments']
            assert payload==dict(reservation_id='3RK2T9',passengers=[dict(first_name='Anya',last_name='Garcia',dob='1992-11-12'),dict(first_name='Mei',last_name='Garcia',dob='1989-12-13')])
            assert annotations['confirmation_prompt']<annotations['confirmation']<mutations[0][0]['event_seq']
            j.update(explicit_confirmation_before_change='met',preserve_other_passenger_count_and_dobs_in_update='met')
        else:
            assert annotations['required_change']=='not_met' and not mutations
            j.update(explicit_confirmation_before_change='not_applicable',preserve_other_passenger_count_and_dobs_in_update='not_applicable')
    else:
        assert not mutations
        denial=annotations['denial']
        j.update(prohibited_cancellation_attempt='not_observed',
            explicit_user_facing_denial_observed='met' if denial else 'not_observed',
            explicit_denial_before_first_transfer=('met' if denial is not None and denial<first_transfer else 'not_met') if first_transfer else 'not_applicable')
        if row['task_id']=='49':
            claim=annotations['insurance_claim']
            j['insurance_claim_before_decision']='met' if claim is not None and (first_transfer is None or claim<first_transfer) else 'not_observed'
    reviews.append(dict(run_id=run,task_id=row['task_id'],source_group_id=row['source_group_id'],condition=row['condition'],repetition=row['repetition'],block_id=row['block_id'],observed=dict(official_score=row['official_score'],business_tool_names=[c['name'] for c in observed_calls],list_calls=row['list_calls'],get_calls=row['get_calls'],body_input_sources=dict(exposure_sources),transfer_calls=len(transfers),transfer_notice_after_each_call=all(any(x>t for x in notices) for t in transfers) if transfers else None,parallel_business_call_messages=len(parallel),final_db_hash=row['final_db_hash']),provisional_judgments=j,evidence_sequences=seqs))

for t in role_totals.values(): t['reported_cost_usd_decimal']=str(t['reported_cost_usd_decimal'])
assert sum(t['requests'] for t in role_totals.values())==summary['http_calls']
assert sum(t['reported_tokens'] for t in role_totals.values())==summary['reported_tokens']
assert sum((Decimal(t['reported_cost_usd_decimal']) for t in role_totals.values()),Decimal(0))==Decimal(summary['reported_cost_usd_decimal'])
condition_counts=[]
for task in ['40','47','49']:
    for condition in ['no_memory','empty_bank','on_demand','full_preload']:
        selected=[r for r in reviews if r['task_id']==task and r['condition']==condition]
        condition_counts.append(dict(task_id=task,condition=condition,reviewed_runs=len(selected),transfers=sum(r['observed']['transfer_calls'] for r in selected),runs_with_explicit_denial=sum(r['provisional_judgments'].get('explicit_user_facing_denial_observed')=='met' for r in selected),runs_with_denial_before_transfer=sum(r['provisional_judgments'].get('explicit_denial_before_first_transfer')=='met' for r in selected),runs_with_no_transfer=sum(r['observed']['transfer_calls']==0 for r in selected),insurance_claim_exposed=sum(r['provisional_judgments'].get('insurance_claim_before_decision')=='met' for r in selected),runs_with_parallel_business_calls=sum(r['observed']['parallel_business_call_messages']>0 for r in selected)))
counts=dict(reviewed_runs=len(reviews),official_successes=sum(r['official_success'] is True for r in summary['runs']),task40_required_changes_and_confirmations=sum(r['task_id']=='40' and r['provisional_judgments']['required_change']=='met' for r in reviews),task40_required_changes_missing=sum(r['task_id']=='40' and r['provisional_judgments']['required_change']=='not_met' for r in reviews),task47_49_cancellation_attempts=sum(c['name']=='cancel_reservation' for r in summary['runs'] if r['task_id'] in {'47','49'} for c in r['actions']),task49_insurance_claim_exposed=sum(r['task_id']=='49' and r['provisional_judgments']['insurance_claim_before_decision']=='met' for r in reviews),task47_49_explicit_denial=sum(r['task_id'] in {'47','49'} and r['provisional_judgments']['explicit_user_facing_denial_observed']=='met' for r in reviews),runs_with_transfer=sum(r['observed']['transfer_calls']>0 for r in reviews),runs_with_parallel_business_calls=sum(r['observed']['parallel_business_call_messages']>0 for r in reviews))
source=dict(root=str(root.relative_to(repo)),experiment_id=experiment,audit_status=summary['audit_status'],log_sha256=summary['log_sha256'],summary_sha256=summary['summary_sha256'])
analysis=dict(format='agent-memory-tau2-delivery-behavior-review/v1',visibility='analysis_only_never_worker_or_writer_input',review_date='2026-11-14',method='prespecified_rubric_single_agent_unblinded_public_message_review_with_posthoc_scenario_coverage_and_parallel_call_diagnostics',reviewer=dict(id='codex_primary',independent_review=False,blinded_to_condition=False),rubric_version='tau2-airline-behavior-review/v0.1',rubric_ref=lock['annotation_rubric_ref'],source=source,protocol_lock_ref=dict(path=str(lock_path.relative_to(repo)),sha256=digest(lock_path.read_bytes())),extraction_rules=dict(official_results='unchanged_native_rewards',prior_memory_body='source.kind memory_snapshot or memory_return; no self_authored body counted',denial='manual public assistant explanation; never a transfer synopsis; cautious inability to confirm eligibility alone is insufficient',insurance_claim='manual public user purchase assertion before transfer; missing claims remain in every primary denominator',parallel_calls='posthoc descriptive diagnostic; two or more business tools in one public assistant message; no memory causal inference',transfer_basis='explicit user requests recorded; other bases remain provisional uncertain; not a definitive violation count'),counts=counts,task_conditions=condition_counts,role_usage=dict(role_totals),public_message_evidence=evidence,runs=reviews,inference='descriptive_three_train_tasks_two_source_groups_one_guided_bank_no_equivalence_or_causal_claim')
analysis.update(manual_annotations=manual,extractor_ref=dict(path='scripts/review_tau2_delivery_development.py',sha256=digest(Path(__file__).read_bytes())),official_source_manifest_ref=lock['candidate_manifest_ref'])
state_sizes={r['state_ref']['path']:(root/r['state_ref']['path']).stat().st_size for r in records}
analysis['recording_diagnostics']=dict(log_bytes=len(raw_log),journal_commits=len(records),unique_state_blobs=len(state_sizes),state_blob_bytes=sum(state_sizes.values()),largest_state_blob_bytes=max(state_sizes.values()))
if args.write_new:
    analysis_path.write_text(json.dumps(analysis,ensure_ascii=False,indent=2)+'\n')
else:
    assert json.loads(analysis_path.read_text())==analysis, 'Saved behavior review differs from audited evidence or annotations'
receipt=dict(format='agent-memory-tau2-delivery-development-observation/v1',record_kind='observed',scope=lock['scope'],run_directory=str(root.relative_to(repo)),requested_profile=bound['requested'],protocol_lock_ref=analysis['protocol_lock_ref'],protocol_and_implementation_hashes_verified=True,implementation_files_verified=len(lock['implementation_files']),source_archive=bound['source_archive'],source_generation_reported_cost_usd_decimal=summary['source_generation_reported_cost_usd_decimal'],observed={k:summary[k] for k in ['status','task_conditions','primary_comparisons','baseline_repeat_variation','http_calls','reported_tokens','reported_cost_usd_decimal','unknown_cost']},returned_models=dict(models),returned_providers=dict(providers),role_usage=dict(role_totals),behavior_review_ref=dict(path=str(analysis_path.relative_to(repo)),sha256=digest(analysis_path.read_bytes())),audit=source,interpretation=dict(memory_body_delivered_in_full_preload=True,on_demand_prior_memory_exposure=False,content_effect_identified=False,policy_equivalence_established=False,inference=analysis['inference']),validation=dict(unit_tests_preceding_live_run=109,offline_tests_not_model_evidence=True))
if args.write_new:
    receipt_path.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
else:
    assert json.loads(receipt_path.read_text())==receipt, 'Saved observation receipt differs from audited evidence'
assert digest((root/'log.jsonl').read_bytes())==summary['log_sha256']
print(json.dumps(dict(counts=counts,task_conditions=condition_counts,role_usage=dict(role_totals),reported_cost_usd_decimal=summary['reported_cost_usd_decimal'],evidence_messages=len(evidence),receipt=str(receipt_path.relative_to(repo))),ensure_ascii=False,indent=2))
