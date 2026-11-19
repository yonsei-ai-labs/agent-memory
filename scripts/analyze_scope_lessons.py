"""Audit two writing instructions from first-pass input through the official native grade."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import re

from agent_memory_eval.analysis.forms_results import contrast
from agent_memory_eval.analysis.fresh_behavior import tabulate_groups
from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.experiments.forms_write import read_form_bank, read_form_experience
from agent_memory_eval.experiments.fresh_forms import scenario_customer
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path
from analyze_bank_transfer import diagnostic, masked_turns
from analyze_fresh_forms import accounting
from analyze_frozen_bank_repeats import WRITES, analyze_registered
from run_frozen_bank_repeats import sealed
from run_scope_lessons import PROFILE, bind_banks, prepare


def case_id(run):
    return digest(canonical(['scope-lesson-review/v1',run]))[:16]


def writer_packet(folder):
    close = read_json((folder/'completion.json').read_bytes())
    rows = sealed(folder,'allocations.json',close['allocation_sha256'])
    cases = {}
    for row in rows:
        if row['status'] not in {'completed','empty'}:
            continue
        bank = read_form_bank(safe_path(folder,row['directory']),expected_log_sha256=row['result']['log_sha256'])
        cases[case_id(row['run_id'])] = [dict(title=i['title'],body=i['body']) for i in bank['items']]
    return dict(kind='masked-writer-text/v1',cases=dict(sorted(cases.items())))


def episode_packet(folder, *, partial=False):
    if partial:
        manifest = read_json((folder/'manifest.json').read_bytes())['preflight']
    else:
        close = read_json((folder/'completion.json').read_bytes())
        manifest = sealed(folder,'manifest.json',close['manifest_sha256'])['preflight']
    cases = {}
    for name, slots in manifest['scheduled'].items():
        shard = folder/name
        if partial and not (shard/'allocations.json').exists():
            continue
        rows = (read_json((shard/'allocations.json').read_bytes()) if partial else
                sealed(shard,'allocations.json',close['shards'][name]['allocation_sha256']))
        for row,slot in zip(rows,slots,strict=True):
            if any(row[k] != v for k,v in slot.items()):
                raise ContractError('Masked packet disagrees with the original allocation')
            res = row.get('result',{})
            ref = res.get('report_ref') or res.get('native_report_ref') or res.get('partial_trajectory_ref')
            if not ref:
                continue
            report = BlobStore(safe_path(shard,row['directory'])).get(ref)
            messages = report['messages'] if isinstance(report,dict) else report
            replies = {m['id']:m for m in messages if m['role']=='tool'}
            turns = masked_turns(messages)
            for turn in turns:
                raw = messages[turn['turn']].get('tool_calls') or []
                for c,r in zip(turn['calls'],raw,strict=True):
                    reply = replies.get(r['id'])
                    c['executed'] = reply is not None and not reply.get('error') and reply.get('execution_status')!='rejected'
            cases[case_id(row['run_id'])] = dict(task=row['task_id'],turns=turns)
    return dict(kind='masked-native-dialogues/v1',cases=dict(sorted(cases.items())))


def check_review(packet, review):
    if review['packet_sha256'] != digest(canonical(packet)) or set(review['cases']) != set(packet['cases']):
        raise ContractError('Annotation does not cover the exact condition-masked texts')


def writer_evidence(manifest,state,packet,review):
    check_review(packet,review)
    parent = state['parent']
    close = sealed(parent,'completion.json',manifest['parent_completion_sha256'])
    ctl = sealed(parent,'control.json',close['control_sha256'])
    cfg = state['cfg']
    rows = sealed(parent,'allocations.json',close['allocation_sha256'])
    conditions = {c['id']:c['writer_instruction_sha256'] for c in manifest['conditions']}
    entries = []
    firsts = defaultdict(set)
    for row in rows:
        run, source, arm = row['run_id'], state['sources'][row['task_id']], row['arm']
        key = case_id(run)
        entry = dict(task_id=row['task_id'],arm=arm,writer=row['repetition'],run=run,status=row['status'],case=key,
                     public_context_sha256=source.public_context_sha256,source_run=source.source_run_id)
        entries.append(entry)
        if row['status']=='not_started':
            if (parent/row['directory']).exists():
                raise ContractError('Unstarted writer produced an artifact')
            continue
        res,folder = row['result'],safe_path(parent,row['directory'])
        if res['log_sha256'] != digest((folder/'log.jsonl').read_bytes()) or res['run_id'] != run:
            raise ContractError('Writer journal seal differs')
        evidence = read_experiment(folder)
        scan, journal, _ = evidence
        blobs = BlobStore(folder)
        audit = audit_requests(folder,evidence=evidence)
        writing = journal['memory_writes'][run]
        capsule = blobs.get(writing['experience_ref'])
        read_form_experience(blobs,scan,capsule)
        proof = blobs.get(capsule['source_ref'])
        if (proof['source_run_id'] != source.source_run_id or proof['source_end_event_id'] != source.source_end_event_id
                or proof['source_log_sha256'] != source.source_log_sha256 or writing['feedback']['level'] != 'none'
                or conditions[arm] != digest(writing['instruction'].encode())):
            raise ContractError('Writer did not use the registered historical input or instruction')
        wires = [blobs.get(e['payload']['request_ref']) for e in scan['events'].values() if e['type']=='model.request_started']
        expected = [dict(role='system',content=writing['instruction']),dict(role='user',content=source.public_context),
                    dict(role='user',content=canonical(dict(kind='evaluation_feedback',level='none')).decode())]
        seed = cfg['writer_seeds'][row['repetition']-1]
        if any(w['messages'][:3]!=expected or w['seed']!=seed or [t['function']['name'] for t in w['tools']]!=['memory_store']
               for w in wires) or (wires and len(wires[0]['messages'])!=3):
            raise ContractError('Actual writer prompt, public context, seed or tools changed')
        if wires:
            firsts[row['task_id'],seed].add(digest(canonical(dict(wires[0],messages=expected[1:]))))
        unknown,receipt = accounting(row,scan,blobs,ctl['model_catalog_entries'])
        bill = res['billing_by_role'].get('writer',{})
        if (Decimal(bill.get('reported_usd','0'))!=Decimal(audit['observed_cost_usd_decimal'])
                or len(unknown)!=audit['unknown_cost_requests']):
            raise ContractError('Writer accounting differs from its journal')
        entry.update(instruction_sha256=conditions[arm],writer_seed=seed,reported_usd=bill.get('reported_usd','0'),
                     calls=bill.get('http_calls',0),unknown=len(unknown),reservation=receipt,
                     journal_path=str(folder.relative_to(parent.parent.parent)/'log.jsonl'),journal_sha256=res['log_sha256'])
        if key in packet['cases']:
            notes, labels = packet['cases'][key], review['cases'][key]
            if set(labels) != {'unit','recheck'}:
                raise ContractError('Missing writer coding dimension')
            for quote in labels.values():
                if quote is not None and (not quote.strip() or not any(quote in n[f] for n in notes for f in ('title','body'))):
                    raise ContractError('Writer annotation lacks an exact quote in the saved bank')
            entry.update(notes=notes,writer_codes=labels)
    spent = sum((Decimal(e.get('reported_usd','0')) for e in entries),Decimal(0))
    reserve = sum((Decimal((e.get('reservation') or {}).get('usd','0')) for e in entries),Decimal(0))
    if (any(len(v)!=1 for v in firsts.values()) or sum(e.get('calls',0) for e in entries)!=close['http_calls']
            or abs(spent - Decimal(str(close['reported_usd'])))>Decimal('0.00000001')
            or reserve!=Decimal(close['reserved_usd']) or spent+reserve>Decimal(str(cfg['writer_guards']['max_usd']))):
        raise ContractError('Paired writer wires or stage accounting disagree')
    return entries


def turn_codes(case, annotation):
    turns = {m['turn']:m for m in case['turns']}
    task = case['task']
    fields = {'questions','approval','proposal'}
    if task in {'6','13','31'}:
        fields.add('explanation')
    if task=='6':
        fields.add('first_transfer_refusal')
    if task=='37':
        fields.update(['past_denial','other_denial'])
    if set(annotation)!=fields or annotation['questions']!=sorted(set(annotation['questions'])):
        raise ContractError('Wrong turn-coding fields or duplicate questions')
    def evidence(i,role):
        if i is None:
            return None
        if type(i) is not int or i not in turns or turns[i]['role']!=role or not turns[i]['text']:
            raise ContractError('Invalid text evidence turn')
        return dict(turn=i,text=turns[i]['text'])
    questions = []
    for i in annotation['questions']:
        after = next((t['turn'] for t in case['turns'] if t['role']=='user' and t['turn']>i),None)
        questions.append(dict(question=evidence(i,'assistant'),next_user=evidence(after,'user')))
    result = {k:evidence(i,'user' if k in {'approval','first_transfer_refusal'} else 'assistant')
              for k,i in annotation.items() if k!='questions'}
    result['questions'] = questions
    writes = [m['turn'] for m in turns.values() if any(c['executed'] and c['name'] in WRITES for c in m['calls'])]
    a,p = annotation['approval'],annotation['proposal']
    if (a is None)!=(p is None) or a is not None and not (writes and p<a<min(writes)):
        raise ContractError('Approval must follow the matching proposal and precede the first successful write')
    return result


def time_check(source, writers, episodes, packet):
    """Post-hoc arithmetic check on a specific saved claim, using the writer's public input."""
    ctx = read_json(source.public_context.encode())
    clock = re.search(r'current time is (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)',ctx['task_context']['content'])
    matching = []
    for i,m in enumerate(ctx['messages']):
        if m['role']!='tool' or not m.get('content'):
            continue
        try:
            data=read_json(m['content'].encode())
        except (ValueError,ContractError):
            continue
        if isinstance(data,dict) and data.get('created_at')=='2024-05-14T16:03:16':
            matching.append((i,data))
    if not clock or not matching:
        raise ContractError('Cited time calculation lacks its public source')
    i,booking=matching[0]
    elapsed=datetime.fromisoformat(clock[1])-datetime.fromisoformat(booking['created_at'])
    cases=[]
    for w in writers:
        if w['task_id']!=source.task_id:
            continue
        for note in w.get('notes',[]):
            if '34 hours 57 minutes' not in note['body']:
                continue
            uses=[]
            for e in episodes:
                if e['source_writer_run_id']==w['run']:
                    case=packet['cases'].get(case_id(e['run_id']))
                    hits=[t['turn'] for t in (case or {}).get('turns',[]) if t['role']=='assistant'
                          and re.search(r'\b34\s*(?:hours?|hrs?)\b',t['text'],re.I)]
                    uses.append(dict(job=e['index'],body_exposed=e.get('body_exposed',False),official_success=e['official_success'],
                                     reviewable=case is not None,spoken_34_hours_turns=hits))
            cases.append(dict(writer=w['run'],arm=w['arm'],task_id=w['task_id'],title=note['title'],uses=uses))
    return dict(source_run=source.source_run_id,source_public_sha256=source.public_context_sha256,
                reservation=booking['reservation_id'],tool_message=i,created_at=booking['created_at'],public_clock=clock[1],
                elapsed_seconds=int(elapsed.total_seconds()),within_24_hours=elapsed.total_seconds()<86400,
                saved_claims=cases)


def evaluate(workspace,writer_review,episode_review):
    manifest,state = prepare(workspace)
    folder = workspace/'runs'/manifest['study']
    seal = read_json((folder/'completion.json').read_bytes())
    reg = sealed(folder,'manifest.json',seal['manifest_sha256'])
    manifest,state = bind_banks(manifest,state,reg['implementation'])
    wp,ep = writer_packet(state['parent']),episode_packet(folder)
    writers = writer_evidence(manifest,state,wp,writer_review)
    check_review(ep,episode_review)
    result = analyze_registered(workspace,manifest,state,transfer=True)
    cfg = state['cfg']
    base = state['checkout']/'data/tau2/domains/airline'
    db = read_json((base/'db.json').read_bytes())
    native = {t['id']:t for t in read_json((base/'tasks.json').read_bytes())}
    opportunities = {p['target']:p for p in cfg['pairs']}
    by_writer = {w['run']:w for w in writers}
    arms = cfg['forms']+cfg['controls']
    metrics = defaultdict(lambda: {arm:Counter() for arm in arms})
    episodes = result['episodes']
    for e in episodes:
        task,arm,key = e['task_id'],e['arm'],case_id(e['run_id'])
        if key not in ep['cases']:
            if e.get('grade') or e.get('partial_path'):
                raise ContractError('A recorded trajectory was omitted from turn review')
            continue
        ref = e.get('report_path') or e['partial_path']
        data = read_json(safe_path(workspace,ref).read_bytes())
        messages = data['messages'] if isinstance(data,dict) else data
        bank_text = canonical(by_writer[e['source_writer_run_id']]['notes']).decode().lower() if e.get('body_exposed') else ''
        diag = diagnostic(messages,e,task,scenario_customer(native[e['source_task_id']],db),
                          scenario_customer(native[task],db),db,bank_text)
        reads = {r['identifier'] for r in diag['reservation_reads']}
        diag['read_nominated'] = {i:i in reads for i in opportunities[task]['reservation_ids']}
        diag['dialogue_evidence'] = turn_codes(ep['cases'][key],episode_review['cases'][key])
        user_turns = [t for t in ep['cases'][key]['turns'] if t['role']=='user']
        if task=='13':
            diag['customer_said_requested_destination'] = [dict(turn=t['turn'],text=t['text']) for t in user_turns
                if re.search(r'\b(?:LAS|Las Vegas)\b',t['text'],re.I)]
        e['behavior'] = diag
        m = metrics[task][arm]
        m['trajectories'] += 1
        m['read_all_nominated'] += all(diag['read_nominated'].values())
        m['current_profile_read'] += bool(diag['current_profile_read_turns'])
        m['source_only_identifier'] += bool(diag['source_only_identifier_hits'])
        m['handoff'] += bool(diag['handoffs'])
        m['executed_mutation'] += bool(diag['executed_write_turns'])
        if diag['handoffs']:
            m['handoff_before_any_mutation'] += (not diag['executed_write_turns']
                                                or diag['human_transfer_turns'][0]<min(diag['executed_write_turns']))
        if task=='13':
            m['customer_said_requested_destination'] += bool(diag['customer_said_requested_destination'])
        if task=='6':
            refusal=diag['dialogue_evidence']['first_transfer_refusal']
            m['handoff_after_expressed_refusal'] += bool(refusal and any(t>refusal['turn'] for t in diag['human_transfer_turns']))
            m['first_handoff_after_expressed_refusal'] += bool(refusal and diag['human_transfer_turns']
                                                              and diag['human_transfer_turns'][0]>refusal['turn'])
        m['any_task_specific_question'] += bool(diag['dialogue_evidence']['questions'])
        m['task_specific_questions'] += len(diag['dialogue_evidence']['questions'])
        for k,quote in diag['dialogue_evidence'].items():
            if k not in {'questions','proposal'}:
                m[k] += bool(quote)
        mutations = e.get('mutations',e.get('partial_mutations',{}))
        if mutations.get('aligned'):
            m['all_reference_writes_exact'] += all(a['all_requested_fields'] for a in mutations['aligned'])
        m['any_extra_write'] += bool(mutations.get('extra_write_turns'))
    groups = {t['task_id']:t['customer_group'] for t in manifest['tasks']}
    strata = {s:[p['target'] for p in cfg['pairs'] if p['stratum']==s] for s in dict.fromkeys(p['stratum'] for p in cfg['pairs'])}
    rows = [dict(task_id=e['task_id'],arm=e['arm'],repetition=e['repetition'],index=e['index'],
                 result=dict(official_success=e['official_success'])) for e in episodes]
    comparisons = [(arms[1],arms[0]),*((arm,c) for arm in cfg['forms'] for c in cfg['controls']),(arms[3],arms[2])]
    result['paired_by_stratum'] = {s:[contrast([r for r in rows if r['task_id'] in tasks],a,b,groups) for a,b in comparisons]
                                   for s,tasks in dict(all=list(groups),**strata).items()}
    result['outcomes_by_stratum'] = tabulate_groups(episodes,strata,arms)
    result['behavior_by_task'] = dict(metrics)
    result['writers'] = writers
    result['writer_content'] = {arm:dict(planned=sum(w['arm']==arm for w in writers),
        nonempty=sum(w['arm']==arm and bool(w.get('notes')) for w in writers),
        **{k:sum(w['arm']==arm and bool(w.get('writer_codes',{}).get(k)) for w in writers) for k in ('unit','recheck')},
        both=sum(w['arm']==arm and bool(w.get('writer_codes')) and all(w['writer_codes'].values()) for w in writers))
        for arm in cfg['forms']}
    writer_usd = sum((Decimal(w.get('reported_usd','0')) for w in writers),Decimal(0))
    writer_res = sum((Decimal((w.get('reservation') or {}).get('usd','0')) for w in writers),Decimal(0))
    result['stage_costs'] = dict(writer_usd=str(writer_usd),solver_usd=result['new_known_usd'],
        total_verified_usd=str(writer_usd+Decimal(result['new_known_usd'])),writer_reserved_usd=str(writer_res),
        solver_reserved_usd=result['reserved_usd'],writer_calls=sum(w.get('calls',0) for w in writers),
        solver_calls=result['http_calls'],writer_unknown=sum(w.get('unknown',0) for w in writers),solver_unknown=result['unpriced_requests'])
    result['review_hashes'] = dict(writers=digest(canonical(writer_review)),episodes=digest(canonical(episode_review)))
    result['exploratory_writer_time_check'] = time_check(state['sources']['14'],writers,episodes,ep)
    if writer_usd+writer_res+Decimal(result['new_known_usd'])+Decimal(result['reserved_usd']) > Decimal(str(cfg['total_usd_limit'])):
        raise ContractError('Combined spend and reserves exceed the study ceiling')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace',type=Path,default=ROOT)
    p.add_argument('--packet',choices=('writers','episodes'))
    p.add_argument('--partial',action='store_true')
    p.add_argument('--writer-review',type=Path)
    p.add_argument('--episode-review',type=Path)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.output.exists():
        p.error('Use a new output path')
    workspace=args.workspace.resolve()
    cfg=read_json((ROOT/PROFILE).read_bytes())
    if args.packet=='writers':
        value=writer_packet(workspace/'runs'/cfg['parent'])
    elif args.packet=='episodes':
        value=episode_packet(workspace/'runs'/cfg['name'],partial=args.partial)
    else:
        value=evaluate(workspace,read_json(args.writer_review.read_bytes()),read_json(args.episode_review.read_bytes()))
    write_json(args.output,value)
