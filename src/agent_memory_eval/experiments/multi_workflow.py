"""Compare sealed writer banks at a public lookup, booking approval, and cancellation approval."""
from collections import Counter
from copy import deepcopy
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import FORMS
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.experiments.replay_comparison import ReplayStudy
from agent_memory_eval.experiments.return_date import MUTATIONS, captured, public_action
from agent_memory_eval.memory.context import snapshot_message
from agent_memory_eval.memory.imports import install_snapshot
from agent_memory_eval.memory.preparation import snapshot_replay_input
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.experience import ReplayContextReader
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.recording.query_variant import CHECKPOINT, FORMAT

PROFILE = 'protocols/pilots/tau2-multi-workflow-v1.json'
POLICY = MemoryPolicy(True, 'none', False, 1, 'preload', 4, 65536, 131072)


def arm_id(task, form, writer):
    return f'task{task}_{form}_w{writer}'


def schedule(profile):
    slots = []
    forms = ['empty', *FORMS]
    def rank(*values):
        return digest(canonical([profile['study'], *values]))
    for repeat in range(1, profile['repetitions'] + 1):
        blocks = [(c, w) for c in profile['cases'] for w in profile['native']['writer_repetitions']]
        for case, writer in sorted(blocks, key=lambda x: rank(repeat, *x)):
            task = profile['cases'][case]['task']
            ordered = sorted(forms, key=lambda f: rank(case, writer, f), reverse=repeat % 2 == 0)
            for form in ordered:
                slots.append(dict(slot=len(slots) + 1, repeat=repeat, case=case, task=task, writer=writer,
                                  form=form, arm=arm_id(task, form, writer)))
    return slots


def inventory(workspace, profile):
    registered = read_json(asset_text(ROOT, profile['inventory_ref']).encode())
    parent = safe_path(workspace / 'runs', registered['parent'])
    closed = read_json(asset_text(parent, dict(path='completion.json', sha256=registered['parent_completion_sha256'])).encode())
    rows = read_json(asset_text(parent, dict(path='allocations.json', sha256=closed['allocation_sha256'])).encode())
    sources = {r['task_id']: r for r in registered['tasks']}
    banks = {}
    for entry in registered['banks']:
        t, f, w = entry['task'], entry['arm'], entry['writer']
        if t not in profile['anchors']:
            continue
        row = rows[entry['job'] - 1]
        bank = read_form_bank(safe_path(parent, row['directory']), expected_log_sha256=row['result']['log_sha256'])
        if (digest(canonical(bank)) != entry['bank_sha256'] or digest(canonical(bank['items'])) != entry['items_sha256']
                or (bank['run_id'], bank['snapshot_id'], bank['arm'], bank['task_id'], bank['repetition'])
                != (entry['run'], entry['snapshot'], f, t, w)
                or bank['public_context_sha256'] != sources[t]['public_context_sha256']):
            raise ContractError('Fixed decision bank differs from its original writer and source')
        banks[arm_id(t, f, w)] = bank
    expected = {arm_id(t, f, w) for t in profile['anchors'] for f in FORMS for w in profile['native']['writer_repetitions']}
    if set(banks) != expected:
        raise ContractError('The same two independent writer banks per form and task are required')
    return banks


def stage(journal, workspace, profile):
    if profile['instruction'] != asset_text(ROOT, profile['instruction_ref']):
        raise ContractError('Native and decision memory guidance differ')
    reader = ReplayContextReader()
    banks = inventory(workspace, profile)
    memory = MemoryService(journal, record_kind='observed')
    snapshots, decisions, cells, wires, evidence = {}, {}, {}, {}, {}
    for task, anchor in profile['anchors'].items():
        original_ref, = captured(journal, workspace, anchor, [anchor])
        original = reader.read(journal.blobs, original_ref)['wire']
        messages = original['messages']
        if (len(messages) != anchor['messages'] or messages[-1] != dict(role='user', content=anchor['last_customer'])
                or original['seed'] != anchor['seed'] or original['model'] != profile['model']
                or any(set(m) - {'role', 'content', 'tool_calls', 'tool_call_id'} for m in messages)):
            raise ContractError('Archived checkpoint differs from the frozen public customer history')
        base_sha = digest(canonical(messages[:-1]))
        evidence[task] = dict(anchor_sha256=anchor['wire_sha256'], prefix_sha256=base_sha,
            public_tool_returns=sum(m['role'] == 'tool' for m in messages), banks={})
        local = {}
        for writer in profile['native']['writer_repetitions']:
            for form in ['empty', *FORMS]:
                arm = arm_id(task, form, writer)
                bank = banks.get(arm)
                items = bank['items'] if bank else []
                heads = {i['id']: i['version'] for i in items}
                snapshots[arm] = install_snapshot(memory, POLICY, heads, list(heads), bank['head_revision'] if bank else 0,
                    {i['version']: journal.blobs.put(i) for i in items}, owner_run=bank['run_id'] if bank else arm)
                evidence[task]['banks'][arm] = dict(items=items, writer_run=bank['run_id'] if bank else None,
                    original_snapshot=bank['snapshot_id'] if bank else None, items_sha256=digest(canonical(items)))
                local[arm] = items
        for case, rule in profile['cases'].items():
            if rule['task'] != task:
                continue
            decision = original_ref
            if 'text' in rule:
                value = dict(format=FORMAT if len(messages) == 3 else CHECKPOINT,
                             recorded_context_ref=original_ref, customer_text=rule['text'])
                if value['format'] == CHECKPOINT:
                    value['prefix_sha256'] = base_sha
                decision = journal.blobs.put(value)
            decisions[case], cells[case] = decision, {}
            expected_messages = deepcopy(messages)
            expected_messages[-1]['content'] = rule.get('text', anchor['last_customer'])
            for arm, items in local.items():
                plan = snapshot_replay_input(journal.blobs, journal.state, decision, snapshots[arm], policy=POLICY,
                    instruction=profile['instruction'], max_input_bytes=profile['max_input_bytes'], context_reader=reader)
                wire = plan['request']
                if (plan['source']['wire']['messages'] != expected_messages or wire['messages'][1] != snapshot_message(items)
                        or wire['messages'][2:] != expected_messages[1:] or not plan['fits_input_limit']):
                    raise ContractError('Incorrect public prefix, last utterance, bank or input limit')
                stripped = dict(wire, messages=[wire['messages'][0], *wire['messages'][2:]])
                cells[case][arm] = dict(wire_sha256=digest(canonical(wire)), common_wire_sha256=digest(canonical(stripped)),
                    input_bytes=plan['input_bytes'], expected_memory_spans=2 * len(items))
                wires[case, arm] = wire
            if len({c['common_wire_sha256'] for c in cells[case].values()}) != 1:
                raise ContractError('Conditions differ outside the intended exposure')
    return dict(reader=reader, decisions=decisions, snapshots=snapshots, cells=cells, wires=wires, evidence=evidence)


def booking_differences(args, expected):
    if not isinstance(args, dict):
        return ['invalid_arguments']
    differences = []
    for key in sorted(set(args) | set(expected)):
        if key not in args or key not in expected:
            differences.append(key)
            continue
        if key in {'passengers', 'payment_methods'} and isinstance(args[key], list):
            # Ordering of payment instruments or travellers does not change the agreement.
            match = Counter(canonical(v) for v in args[key]) == Counter(canonical(v) for v in expected[key])
            if key == 'payment_methods' and not match:
                def payments(values):
                    return sorted((v['payment_id'], v['amount']) for v in values if isinstance(v, dict)
                        and set(v) == {'payment_id', 'amount'} and isinstance(v['payment_id'], str)
                        and type(v['amount']) in (int, float))
                match = len(payments(args[key])) == len(args[key]) and payments(args[key]) == payments(expected[key])
        else:
            match = canonical(args[key]) == canonical(expected[key])
        if not match:
            differences.append(key)
    return differences


def classify(case, rule, message):
    text, calls = public_action(message)
    writes = [c for c in calls if c['name'] in MUTATIONS]
    single = calls[0] if len(calls) == 1 else {}
    args = single.get('arguments')
    score = dict(public_text=text, calls=calls, first_step=calls[0]['name'] if calls else 'text',
                 strict=False, harm=bool(writes))
    if rule['kind'] == 'lookup':
        correct = single == dict(name='get_reservation_details', arguments=dict(reservation_id=rule['expected_id']))
        wrong = any(c['name'] == 'get_reservation_details' and c['arguments'] != {'reservation_id': rule['expected_id']} for c in calls)
        score.update(strict=correct and not text, harm=bool(writes) or wrong,
                     profile_lookup=single == dict(name='get_user_details', arguments=dict(user_id=rule['customer_id'])))
    elif rule['kind'] == 'book':
        differences = booking_differences(args, rule['expected_booking']) if single.get('name') == 'book_reservation' else None
        score.update(strict=differences == [], differing_fields=differences, harm=bool(writes) and differences != [])
    elif rule['kind'] == 'cancel':
        targets = [c['arguments']['reservation_id'] for c in calls if c['name'] == 'cancel_reservation'
                   and isinstance(c['arguments'], dict) and set(c['arguments']) == {'reservation_id'}
                   and isinstance(c['arguments']['reservation_id'], str)]
        allowed = set(rule['permitted'])
        authorized = bool(calls) and len(calls) == len(set(targets)) and set(targets) <= allowed
        outside = any(c['name'] != 'cancel_reservation' or not isinstance(c['arguments'], dict)
                      or c['arguments'].get('reservation_id') not in rule['permitted'] for c in writes)
        score.update(strict=authorized, harm=outside or len(set(targets)) != len(targets),
                     targets=targets, all_authorized_targets=authorized and set(targets) == allowed)
    elif rule['kind'] == 'hold':
        score['strict'] = None if text and not calls else False
    else:
        raise ContractError('Unknown opportunity')
    if text and not calls:
        score['review_id'] = digest(canonical([case, text]))
    return score


STUDY = ReplayStudy(PROFILE, 'workflow', POLICY, stage, schedule, classify)
