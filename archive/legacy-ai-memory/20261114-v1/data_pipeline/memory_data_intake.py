"""Read-only checks of a pre-execution memory-relation packet.

Checks declared evidence and input visibility, not semantic completeness or
whether an LLM will benefit. No model, network, or dataset mutation.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUGMENTATION = ROOT / 'datasets/memory_state_augmentation/20261112-v1'
JUDGMENTS = {'needed', 'not_needed', 'insufficient'}


def check(condition, message):
    if not condition:
        raise ValueError(message)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def unique(rows, key):
    result = {}
    for row in rows:
        check(row[key] not in result, 'Duplicate ' + key)
        result[row[key]] = row
    return result


def audit(packet):
    check(packet['version'] == 'memory-relation-intake-v1', 'Unsupported intake version')
    check(packet['stage'] == 'pre_execution', 'This checker does not validate worker outcomes')
    sources = unique(packet['sources'], 'source_id')
    events = unique(packet['events'], 'event_id')
    cases = unique(packet['cases'], 'state_id')
    check(bool(cases), 'Empty packet')
    root_splits = defaultdict(set)
    for source in sources.values():
        check(source['split'] in ('train', 'dev', 'evaluation'), 'Unknown split')
        check(bool(source['root_family_id']), 'Missing root family')
        check(isinstance(source['prior_use'], list), 'Missing use history')
        check(not source['new_to_project'] or not source['prior_use'], 'Previously used source claimed new')
        root_splits[source['root_family_id']].add(source['split'])
    check(all(len(splits) == 1 for splits in root_splits.values()), 'Root family crosses splits')
    for event in events.values():
        check(event['source_id'] in sources, 'Unregistered event source')
        check(type(event['step']) is int and event['step'] >= 0, 'Invalid event step')
        check(isinstance(event['public_text'], str) and bool(event['public_text']), 'Missing public evidence text')
    inputs = {}; judgments_by_hash = defaultdict(set); splits_by_hash = defaultdict(set)
    for sid, case in cases.items():
        check(case['source_id'] in sources, 'Unregistered case source')
        check(type(case['decision_step']) is int and case['decision_step'] >= 0, 'Invalid decision step')
        check(bool(case['candidate_scope']), 'Undeclared candidate scope')
        check(case['input_scope'] == 'rendered_pair_text_before_model_tokenization', 'Unspecified input boundary')
        check(bool(case['inputs']), 'Case has no candidate inputs')
        for inp in case['inputs']:
            key = sid, inp['memory_id']
            check(key not in inputs, 'Duplicate case/candidate input')
            check(bool(inp['text']) and digest(inp['text']) == inp['sha256'], 'Input hash mismatch')
            inputs[key] = inp
    seen = set(); counts = Counter(); evidence_refs = 0
    for row in packet['relations']:
        key = row['state_id'], row['memory_id']
        check(key in inputs and key not in seen, 'Unmatched or duplicate relation')
        seen.add(key)
        case = cases[row['state_id']]; source = sources[case['source_id']]; inp = inputs[key]
        judgment = row['judgment']
        check(judgment in JUDGMENTS, 'Unknown relation judgment')
        check('utility' in row and row['utility'] is None, 'Unmeasured utility must remain null')
        check(bool(row['evidence']) or judgment == 'insufficient', 'No declared evidence for determinate judgment')
        if judgment == 'insufficient':
            check(bool(row.get('missing_premises')), 'Missing premise must be named')
        for ref in row['evidence']:
            check(ref['event_id'] in events, 'Unknown premise event')
            event = events[ref['event_id']]
            check(event['source_id'] == case['source_id'], 'Evidence belongs to another source')
            check(event['scenario_id'] in ('*', row['state_id']), 'Evidence belongs to another scenario')
            check(event['step'] <= case['decision_step'], 'Future evidence at decision time')
            start, end = ref['start'], ref['end']
            check(type(start) is int and type(end) is int and 0 <= start < end <= len(inp['text']), 'Invalid evidence offsets')
            check(inp['text'][start:end] == event['public_text'], 'Declared premise absent from actual text')
            evidence_refs += 1
        judgments_by_hash[inp['sha256']].add(judgment)
        splits_by_hash[inp['sha256']].add(source['split'])
        counts[judgment] += 1
    check(seen == set(inputs), 'Missing candidate annotation')
    check(all(len(v) == 1 for v in judgments_by_hash.values()), 'Identical model input has conflicting judgments')
    check(all(len(v) == 1 for v in splits_by_hash.values()), 'Identical model input crosses splits')
    return dict(status='mechanical_checks_passed', sources=len(sources), root_families=len(root_splits),
                cases=len(cases), candidate_inputs=len(inputs), unique_inputs=len(judgments_by_hash),
                judgments=dict(counts), evidence_spans_checked=evidence_refs, utility_labels=0,
                source_split_overlap=0, contradictory_inputs=0,
                semantic_completeness_verified=False, independent_human_review=False,
                tokenizer_or_truncation_verified=False,
                scope='Declared evidence offsets, source/scenario/time, input identity and null utility only; not independent semantic validation or worker benefit.')


def augmentation_packet(data=AUGMENTATION):
    """Adapt the existing four-reference training view, without editing it."""
    from data_pipeline.augment_memory_states import audit as audit_augmentation, materialized_cases, pair_text, read
    audit_augmentation(data)
    all_cases = {c['state_id']: c for c in materialized_cases(data)}
    pairs = read(data / 'training-pairs.jsonl')
    by_state = defaultdict(list)
    for pair in pairs:
        by_state[pair['state_id']].append(pair)
    sources = {}; events = {}; cases = []; relations = []
    for sid, c in all_cases.items():
        source = c['source_group']
        sources[source] = dict(source_id=source, root_family_id=c['family_id'], split='train',
                               new_to_project=False, prior_use=['memory_need_train'])
        context_id = sid + ':current-snapshot'
        events[context_id] = dict(event_id=context_id, source_id=source, scenario_id=sid, step=1,
                                  public_text=c['selection_context'])
        inputs = []; memories = {m['memory_id']: m for m in c['bank']}
        for pair in by_state[sid]:
            mid = pair['memory_id']; memory = memories[mid]; text = pair_text(c, memory)
            memory_event = source + ':record:' + mid
            events[memory_event] = dict(event_id=memory_event, source_id=source, scenario_id='*', step=0,
                                        public_text=memory['text'])
            inputs.append(dict(memory_id=mid, text=text, sha256=digest(text)))
            evidence = []
            for eid in (context_id, memory_event):
                quote = events[eid]['public_text']; start = text.index(quote)
                evidence.append(dict(event_id=eid, start=start, end=start + len(quote)))
            relations.append(dict(state_id=sid, memory_id=mid,
                                  judgment='needed' if pair['label'] else 'not_needed',
                                  relation_type=pair['role'], evidence=evidence, utility=None))
        cases.append(dict(state_id=sid, source_id=source, decision_step=1,
                           candidate_scope='four_explicit_goal_references',
                           input_scope='rendered_pair_text_before_model_tokenization', inputs=inputs))
    return dict(version='memory-relation-intake-v1', stage='pre_execution',
                provenance='Existing controlled snapshots; step 0/1 is a logical provenance ordering, not a natural agent trajectory.',
                sources=list(sources.values()), events=list(events.values()), cases=cases, relations=relations)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--packet', type=Path)
    group.add_argument('--check-current-augmentation', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    packet = json.loads(args.packet.read_text()) if args.packet else augmentation_packet()
    result = audit(packet)
    result['packet_sha256'] = digest(json.dumps(packet, ensure_ascii=False, sort_keys=True))
    result['checker_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
