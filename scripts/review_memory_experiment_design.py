"""Reanalyse existing memory trials by customer, exposure, candidate reads and public disclosures."""
import argparse
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import FORMS, write_json
from agent_memory_eval.experiments.fresh_forms import scenario_customer
from agent_memory_eval.recording.assets import asset_text
from analyze_multi_workflow import transition

ANALYSES = Path('artifacts/analyses')
RESULTS = {
    'multi-workflow-results': '623c1d88ac2116ca3cd65f56edb3769d1b91e28e5c7ad8c561850123bf9932eb',
    'multi-workflow-profile-results': '82ec0625237021cb9661274aedc74333641fadb2dea60c1a65309a673e5fa3c6',
    'multi-workflow-baggage-replay': 'a770380d06ff19ce4a672830578d11083955d24a1e24581236dbc691adff1dc1',
    'fixed-bank-repeats-results': '6c5c05652a432fb54fc006f069510c79451a78fa4edc859cd290f61f4da28067',
    'frozen-bank-preflight': '898da02d03f118991c6f5fb2850d38f42fc90a20b5ee81bc17acc1830866fa26',
}
TASKS = ('3', '14', '42')
CONTROLS = ('no_memory', 'empty_preload')
# Code the customer's express statement before the first booking proposal. Generic assent is not an express number.
CATEGORIES = ('zero_checked', 'extra_or_allowance_only', 'explicit_positive', 'unresolved_conflict', 'no_quantity_statement')


def result_file(workspace, key):
    filename = ANALYSES / ('20261119-' + key + '.json')
    return read_json(asset_text(workspace, dict(path=str(filename), sha256=RESULTS[key])).encode())


def booking_wording(workspace, native):
    packet, cases = {}, []
    for e in native:
        if e['task_id'] != '14':
            continue
        graded = 'grade' in e
        path = e['report_path'] if graded else e['partial_path']
        sha = e['grade']['report_sha256'] if graded else Path(path).name
        report = read_json(asset_text(workspace, dict(path=path, sha256=sha)).encode())
        messages = report['messages'] if isinstance(report,dict) else report
        # Keep attempts which the business tool later rejected. The chosen action predates that result.
        bookings = [(i,c) for i,m in enumerate(messages) if m['role']=='assistant'
                    for c in m.get('tool_calls') or [] if c['name']=='book_reservation']
        end = bookings[0][0] if bookings else len(messages)
        words = [m['content'] for m in messages[:end] if m['role']=='user' and m.get('content')]
        key = digest(canonical(words))
        packet[key] = words
        quantities = [c['arguments'].get('total_baggages') for _,c in bookings]
        cases.append(dict(index=e['index'], arm=e['arm'], review_id=key, report_sha256=sha,
            graded=graded, first_booking_turn=bookings[0][0] if bookings else None, proposed_quantities=quantities))
    if len(cases) != 40:
        raise ContractError('The public wording review must include all 40 booking assignments')
    return dict(sorted(packet.items())), cases


def join_wording(packet, cases, labels, interventions):
    if labels.keys() != packet.keys():
        raise ContractError('Missing or extra masked customer transcript')
    original = {r['index']:r for r in interventions['rows']}
    if original.keys() != {r['index'] for r in cases}:
        raise ContractError('Original DB replay population changed')
    counts = defaultdict(Counter)
    for row in cases:
        label = labels[row['review_id']]
        category, quotes = label['category'], label['evidence']
        if category not in CATEGORIES or not isinstance(quotes,list) or bool(quotes) != (category!='no_quantity_statement'):
            raise ContractError('Unknown wording code or missing supporting customer quotation')
        words = packet[row['review_id']]
        for q in quotes:
            if type(q['utterance']) is not int or not 0 <= q['utterance'] < len(words) or not q['quote'] or q['quote'] not in words[q['utterance']]:
                raise ContractError('Wording label does not cite the blinded public prefix')
        values = row['proposed_quantities']
        if values:
            outcome = 'first_proposed_' + str(values[0])
        else:
            outcome = 'no_proposal_before_end' if row['graded'] else 'partial_no_proposal'
        row.update(wording=category, first_proposal_outcome=outcome)
        counterfactual = original[row['index']]
        if row['graded'] and counterfactual['native_report_sha256'] != row['report_sha256']:
            raise ContractError('Wording and DB counterfactual use different transcripts')
        if counterfactual['status']=='replayed':
            row.update(executed_quantity=counterfactual['old'], db_before=counterfactual['recorded_db_match'],
                       db_after_quantity_change=counterfactual['edited_db_match'])
        counts[category][outcome] += 1
    return dict(wording_by_first_proposal=dict(counts), cases=cases, labels_sha256=digest(canonical(labels)))


def condition_rows(episodes, *, suffix):
    cells = {}
    for e in episodes:
        task, arm = e['task_id'], e['arm']
        if task not in TASKS or (suffix and arm not in CONTROLS and not arm.endswith(suffix)):
            continue
        form = arm.removesuffix(suffix) if suffix else arm
        if form not in (*FORMS, *CONTROLS):
            raise ContractError('Unexpected recorded condition')
        key = (task, e['writer_block'], e['solver_seed'], form)
        if key in cells:
            raise ContractError('Duplicated task, writer, solver and condition')
        cells[key] = e
    return cells


def repeat_comparison(workspace, prior, latest, inventory):
    old = condition_rows(prior['episodes'], suffix='')
    new = condition_rows(latest['native_evidence'], suffix='__preload')
    banks = {(b['task'],b['writer'],b['arm']):b for b in inventory['banks'] if b['task'] in TASKS}
    links = {(b['task'],b['writer'],b['form']):b for b in latest['bank_links']}
    if banks.keys()!=links.keys() or len(banks)!=24:
        raise ContractError('The two cohorts do not contain the same 24 original banks')
    for key, link in links.items():
        b=banks[key]
        if (b['run'], b['items_sha256']) != (link['writer_run'],link['items_sha256']):
            raise ContractError('The matched saved content differs')
    results = {}
    for name, cells, seeds, source in [('earlier',old,(4,5),prior['study']),('latest',new,(6,7),latest['native']['study'])]:
        block_keys = [(t,w,s) for t in TASKS for w in (1,2) for s in seeds]
        if set(cells) != {(*k,f) for k in block_keys for f in (*FORMS,*CONTROLS)}:
            raise ContractError('Unequal assignment when restricting the two studies to common tasks')
        blocks, summary = [], {}
        for key in block_keys:
            t,w,s=key
            scores={f:cells[*key,f]['official_success'] for f in (*FORMS,*CONTROLS)}
            for f in FORMS:
                if cells[*key,f]['source_writer_run_id']!=banks[t,w,f]['run']:
                    raise ContractError('Episode points to a different source bank')
            blocks.append(dict(task=t, writer=w, seed=s, scores=scores))
        for task, subset in [('all',blocks), *((t,[r for r in blocks if r['task']==t]) for t in TASKS)]:
            summary[task]={}
            for f in (*FORMS,*CONTROLS):
                successes = [b['scores'][f] for b in subset]
                summary[task][f]=dict(success=sum(x is True for x in successes), graded=sum(x is not None for x in successes),
                    paired={control:dict(Counter(transition(b['scores'][f],b['scores'][control]) for b in subset)) for control in CONTROLS})
        instructions={}
        for task in TASKS:
            for wave in (1,2):
                control=read_json((workspace/'runs'/source/f'task-{int(task):02d}-wave-{wave}'/'control.json').read_bytes())
                ref=control['design']['second_pass']['solve_instruction_ref']
                asset_text(workspace,ref)
                instructions[ref['sha256']]=ref
        if len(instructions)!=1:
            raise ContractError('Worker guidance is not constant within the historical comparison')
        results[name]=dict(study=source,seeds=list(seeds),instruction=next(iter(instructions.values())),cells=blocks,summary=summary)
    return results


def unread_variation(episodes):
    groups = defaultdict(list)
    for e in episodes:
        if e['mode']=='on_demand':
            if any(e[k] for k in ('list_requests','get_requests','list_returns','get_returns','title_spans','body_spans')):
                raise ContractError('A supposedly unread arm actually requested or received memory')
            groups[e['task_id'],e['writer_block'],e['solver_seed']].append(e)
    counts, blocks = Counter(), []
    for (task, writer, seed), block in sorted(groups.items()):
        if sorted(e['form'] for e in block)!=sorted(FORMS):
            raise ContractError('Incomplete allocation of four voluntarily unread banks')
        scores={e['form']:e['official_success'] for e in block}
        pairs=Counter('missing' if None in (a,b) else 'agree' if a==b else 'discordant'
                      for a,b in combinations(scores.values(),2))
        counts.update(pairs)
        blocks.append(dict(task=task,writer=writer,seed=seed,scores=scores,mixed=True in scores.values() and False in scores.values(),pairs=dict(pairs)))
    return dict(blocks=blocks,mixed_blocks=sum(b['mixed'] for b in blocks),shared_pair_counts=dict(counts))


def profile_choices(raw):
    rule=read_json((ROOT/'protocols/pilots/tau2-multi-workflow-profile-step-v1.json').read_bytes())['cases']['after_profile']
    target = rule['expected_id']
    summaries = defaultdict(Counter)
    banks = []
    for arm,b in sorted(raw['first_pass_evidence']['banks'].items()):
        rows=[r for r in raw['rows'] if r['arm']==arm]
        if len(rows)!=2 or any(r['score'] is None for r in rows):
            raise ContractError('Expected two observed decisions for each masked writer block')
        f=rows[0]['form']
        q=summaries[f]
        q['writer_blocks']+=1
        q['saved_nonempty']+=bool(b['items'])
        results=[]
        for r in rows:
            score=r['score']
            ids=score['requested_ids']
            if len(ids)!=len(score['calls']) or len(ids)!=len(set(ids)) or set(ids)-set(rule['candidates']):
                raise ContractError('First-reply candidate metrics need distinct, in-profile lookups')
            q.update(replies=1, direct=int(score['strict']), target_in_batch=int(target in ids),
                     business_reads_proposed=len(ids), non_target_reads_proposed=sum(i!=target for i in ids))
            results.append(dict(slot=r['slot'],direct=score['strict'],target_in_batch=target in ids,requested_count=len(ids)))
        banks.append(dict(arm=arm,form=f,writer=rows[0]['writer'],writer_run=b['writer_run'],items_sha256=b['items_sha256'],
            target_in_title=any(target in i['title'] for i in b['items']),target_in_body=any(target in i['body'] for i in b['items']),results=results))
    return dict(summary=dict(summaries),banks=banks,candidates=len(rule['candidates']),target_position=rule['candidates'].index(target)+1)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=['packet','review'])
    p.add_argument('--workspace',type=Path,default=ROOT)
    p.add_argument('--labels',type=Path)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.output.exists() or (args.mode=='review')!=bool(args.labels):
        p.error('Choose a new output and supply masked labels only for the review')
    workspace=args.workspace.resolve()
    latest=result_file(workspace,'multi-workflow-results')
    packet,cases=booking_wording(workspace,latest['native_evidence'])
    result=packet
    if args.mode=='review':
        result=dict(inputs=RESULTS, common_banks=repeat_comparison(workspace,result_file(workspace,'fixed-bank-repeats-results'),
                    latest,result_file(workspace,'frozen-bank-preflight')),unread=unread_variation(latest['native_evidence']),
                    profile=profile_choices(result_file(workspace,'multi-workflow-profile-results')),
                    baggage=join_wording(packet,cases,read_json(args.labels.read_bytes()),result_file(workspace,'multi-workflow-baggage-replay')))
    write_json(args.output,result)


if __name__=='__main__':
    main()
