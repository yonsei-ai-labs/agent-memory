"""Train-only state/observation contrasts from recorded travel memories.

No new histories, utility labels, worker calls, or training are generated.
Banks are normalized so repeated views do not duplicate historical records.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
from itertools import product
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'datasets/memory_need/20261111-v1'
DATA = ROOT / 'datasets/memory_state_augmentation/20261112-v1'
REFERENCE = re.compile(r"([A-Za-z]+)'s (dinner|accommodation) on day (\d+)")
CATALOG_MARKER = 'In catalog names, \\| denotes a literal vertical bar.\nCurrent catalog columns:'
STATUSES = (
    ('Initial itinerary checkout: NOT_ATTEMPTED.', 'Initial itinerary checkout: FAILED.'),
    ('The first checkout has not been attempted. No failure of that checkout has been reported.',
     'The first checkout attempt failed. The fallback plan must now be used.'),
    ('Event 1: an unrelated map lookup failed. Event 2: the initial itinerary checkout has not started.',
     'Event 1: an unrelated map lookup failed. Event 2: the initial itinerary checkout ended in failure.'),
)


def check(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def read(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def dump(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def writel(path, rows):
    Path(path).write_text(''.join(canonical(r) + '\n' for r in rows))


def unique(rows, key):
    result = {}
    for r in rows:
        check(r[key] not in result, 'Duplicate ' + key)
        result[r[key]] = r
    return result


def references(text):
    return [(person, slot, int(day)) for person, slot, day in REFERENCE.findall(text)]


def core_records(case):
    refs = references(case['goal'])
    check(len(refs) == 4, 'Expected primary and fallback references')
    out = []
    for person, slot, day in refs:
        found = [m for m in case['bank'] if (m['person'], m['slot'], m['day']) == (person, slot, day)]
        check(len(found) == 1 and found[0]['choice'] is not None, 'Invalid core historical reference')
        out.append(found[0]['memory_id'])
    check(len(set(out)) == 4, 'Repeated core record')
    return out


def describe_pair(records):
    return ' and '.join(f"{r['person']}'s {r['slot']} on day {r['day']}" for r in records)


def goal_text(primary, fallback, frame):
    p, f = describe_pair(primary), describe_pair(fallback)
    return (
        f'Default plan: {p}. If the first checkout attempt fails, use this fallback instead: {f}. Both dinner and accommodation must currently be available.',
        f'For dinner and accommodation, begin with {p}. Switch to {f} only when the initial checkout attempt has failed. Use places that are available now.',
        f'Following a failed initial checkout, use {f}. While no initial checkout failure has been reported, use {p}. Each chosen place must currently be available.',
    )[frame]


def visible_rule(case, bank):
    """Check the relationship using rendered public text, never hidden labels."""
    refs = references(case['goal'])
    check(len(refs) == 4, 'Bad rendered reference count')
    if case['goal'].startswith('Following a failed initial checkout,'):
        fallback, primary = refs[:2], refs[2:]
    else:
        primary, fallback = refs[:2], refs[2:]
    states = {text: branch for texts in STATUSES for branch, text in enumerate(texts)}
    check(case['status_text'] in states, 'Unrecognized public status')
    active = fallback if states[case['status_text']] else primary
    visible = {m['memory_id'] for m in bank if m['evidence'] in case['selection_context']}
    check(visible == set(case['observed_memory_ids']), 'Public observations and metadata disagree')
    needed = []
    for ref in active:
        matches = [m for m in bank if (m['person'], m['slot'], m['day']) == ref]
        check(len(matches) == 1, 'Ambiguous public reference')
        if matches[0]['memory_id'] not in visible:
            needed.append(matches[0]['memory_id'])
    return needed


def make_case(parent, frame, branch, mask):
    core = core_records(parent); bank = {m['memory_id']: m for m in parent['bank']}
    primary, fallback = [bank[m] for m in core[:2]], [bank[m] for m in core[2:]]
    observed = [mid for i, mid in enumerate(core) if mask & (1 << i)]
    status = STATUSES[frame][branch]
    if observed:
        prefix = ('Verified historical lookup: ', 'A completed check established: ', 'Completed historical observation: ')[frame]
        facts = '\n'.join(prefix + bank[mid]['evidence'] for mid in observed)
    else:
        facts = ('(No historical choices have been checked in the current session.)',
                 'No past booking values are currently established.',
                 'Historical lookup results: none.')[frame]
    sid = 'state-aug-v1:' + parent['family_id'][:16] + f':f{frame}:b{branch}:o{mask:02x}'
    case = dict(state_id=sid, family_id=parent['family_id'], source_group='travel:' + parent['family_id'],
                split='train', bank_id=parent['family_id'], template_id=frame,
                controlled_branch=branch, observed_mask=mask,
                goal=goal_text(primary, fallback, frame), status_text=status,
                observed_memory_ids=observed, selection_context=status + '\n' + facts,
                current_state_origin='controlled augmentation; not a naturally observed agent episode')
    active = set(core[2 * branch:2 * branch + 2]); inactive = set(core) - active
    roles = {}
    for mid, m in bank.items():
        roles[mid] = ('already_observed' if mid in observed else 'needed_now') if mid in active else (
            'inactive_branch' if mid in inactive else ('unavailable_source' if m['choice'] is None else 'other_reference'))
    needed = [mid for mid in core if roles[mid] == 'needed_now']
    label = dict(state_id=sid, required_memory_ids=needed, active_memory_ids=[mid for mid in core if mid in active],
                 inactive_memory_ids=[mid for mid in core if mid in inactive], candidate_roles=roles,
                 acceptable={bank[mid]['slot']: [bank[mid]['choice']] for mid in core if mid in active},
                 utility=None, actual_gain_or_harm=None,
                 annotation='Deterministic current-state relation; no human reannotation or worker-effect measurement')
    check(visible_rule(case, parent['bank']) == needed, 'Public rule and relation annotation disagree')
    return case, label


def pair_text(case, memory):
    # Match the prior reader's actual query/candidate representation. Bank order
    # is absent, so order permutations must not count as new training inputs.
    from training.memory_selector.memory_need_learning import pair_text as existing
    return existing(case, memory, 'state')


def materialized_cases(data=DATA):
    """Join normalized banks for downstream pair scorers; no outcome labels."""
    data = Path(data); banks = unique(read(data / 'banks.jsonl'), 'bank_id')
    return [dict(c, bank=banks[c['bank_id']]['records']) for c in read(data / 'cases.jsonl')]


def build(dest=DATA):
    dest = Path(dest)
    check(not dest.exists(), 'Output already exists')
    source_manifest = json.loads((SOURCE / 'manifest.json').read_text())
    for name, digest in source_manifest['sha256'].items():
        check(sha((SOURCE / name).read_bytes()) == digest, 'Changed source: ' + name)
    source_cases = read(SOURCE / 'cases.jsonl')
    source_records = json.loads((SOURCE / 'source-records.json').read_text())
    parents = sorted([c for c in source_cases if c['fit_split'] == 'train' and c['mode'] == 'retry_branch' and c['state'] == 0], key=lambda c: c['family_id'])
    check(len(parents) == 11 and len({c['family_id'] for c in parents}) == 11, 'Unexpected training sources')
    blocked = {c['family_id'] for c in source_cases if c['fit_split'] != 'train'}
    check(not blocked & {c['family_id'] for c in parents}, 'Source split leakage')
    banks = []; cases = []; labels = []; pairs = []; contrasts = []
    for parent in parents:
        check(all(source_records[m['memory_id']] == m for m in parent['bank']), 'Historical bank changed')
        core = core_records(parent)
        check(CATALOG_MARKER in parent['public_text'], 'Missing original current catalog')
        banks.append(dict(bank_id=parent['family_id'], source_parent_state_id=parent['state_id'],
                          source_catalog_suffix=parent['public_text'].split(CATALOG_MARKER, 1)[1],
                          records=parent['bank'], core_memory_ids=core))
        index = {}
        for frame, branch, mask in product(range(3), range(2), range(16)):
            c, l = make_case(parent, frame, branch, mask); cases.append(c); labels.append(l)
            index[frame, branch, mask] = c['state_id']
            for mid in core:
                memory = next(m for m in parent['bank'] if m['memory_id'] == mid)
                pairs.append(dict(state_id=c['state_id'], memory_id=mid, source_group=c['source_group'], split='train',
                                  label=int(l['candidate_roles'][mid] == 'needed_now'), role=l['candidate_roles'][mid],
                                  input_sha256=sha(pair_text(c, memory).encode()),
                                  scope='four explicit goal references; full bank retained separately'))
        for frame, mask in product(range(3), range(16)):
            contrasts.append(dict(kind='branch_change', first=index[frame, 0, mask], second=index[frame, 1, mask]))
        for frame, branch, mask in product(range(3), range(2), range(16)):
            for bit in range(4):
                if not mask & (1 << bit):
                    contrasts.append(dict(kind='observation_addition', added_memory_id=core[bit],
                                          first=index[frame, branch, mask], second=index[frame, branch, mask | (1 << bit)]))
        for branch, mask, frame in product(range(2), range(16), (1, 2)):
            contrasts.append(dict(kind='equivalent_wording', first=index[0, branch, mask], second=index[frame, branch, mask]))
    dest.mkdir(parents=True)
    for name, rows in (('banks.jsonl', banks), ('cases.jsonl', cases), ('labels.jsonl', labels),
                       ('training-pairs.jsonl', pairs), ('contrasts.jsonl', contrasts)):
        writel(dest / name, rows)
    # A separate key lets a reviewer inspect public evidence before seeing the
    # constructed labels. Opaque review IDs do not reveal branch/mask codes.
    lookup = {c['state_id']: c for c in cases}; lab_by_id = {l['state_id']: l for l in labels}
    packet = []; review_key = []
    for number, (frame, branch, mask) in enumerate(product(range(3), range(2), (0, 1, 3, 12))):
        parent = parents[number % len(parents)]
        c, _ = make_case(parent, frame, branch, mask); sid = c['state_id']
        review_id = sha(('state-augmentation-review:' + sid).encode())[:16]
        core = core_records(parent)
        packet.append(dict(review_id=review_id, goal=c['goal'], current_observations=c['selection_context'],
                           candidate_records=[dict(memory_id=m['memory_id'], text=m['evidence']) for mid in core for m in parent['bank'] if m['memory_id'] == mid],
                           question='Which candidate records supply facts still needed now? Mark ambiguous premises separately.'))
        review_key.append(dict(review_id=review_id, state_id=sid, source_group=lookup[sid]['source_group'],
                               required_memory_ids=lab_by_id[sid]['required_memory_ids'], review_status='not_independently_reviewed'))
    writel(dest / 'review-packet.jsonl', packet); writel(dest / 'review-key.jsonl', review_key)
    protocol = dict(experiment='memory-state-augmentation-20261112-v1',
        dimensions=dict(source_families=11, branch_states=2, observation_subsets=16, wording_forms=3),
        model_input='goal + selection_context + one original memory text; no hidden labels or branch metadata',
        source_policy='All existing train families only. No dev/confirmation transformation and no new-b access.',
        utility_policy='All augmented utility labels remain null. Text/state changes require new worker measurement; no cache copying.',
        independent_sources_added=0, new_worker_calls=0, new_training=0,
        evaluation_scope='Training augmentation only; original evaluation is preserved and already observed, not a fresh confirmation.',
        wording_review='Rule-checked controlled English templates; independent semantic review pending',
        sampling='Primary training pairs cover the four mentioned references; unrelated full-bank negatives are separate.',
        budget_policy='No new worker budget imposed. Freeze a common context budget and always-provide baseline before future outcome measurement.')
    dump(dest / 'protocol.json', protocol)
    paths = [SOURCE / 'manifest.json', SOURCE / 'cases.jsonl', SOURCE / 'labels-train.jsonl', SOURCE / 'source-records.json',
             Path(__file__), ROOT / 'training/memory_selector/memory_need_learning.py']
    manifest = dict(source_sha256={str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in paths},
                    sha256={p.name: sha(p.read_bytes()) for p in sorted(dest.iterdir())})
    dump(dest / 'manifest.json', manifest)
    result = audit(dest)
    dump(dest / 'audit.json', result)
    manifest['sha256']['audit.json'] = sha((dest / 'audit.json').read_bytes())
    dump(dest / 'manifest.json', manifest)
    return result


def audit(data=DATA):
    data = Path(data); manifest = json.loads((data / 'manifest.json').read_text())
    for name, expected in manifest['source_sha256'].items():
        check(sha((ROOT / name).read_bytes()) == expected, 'Changed parent/code: ' + name)
    for name, expected in manifest['sha256'].items():
        check(sha((data / name).read_bytes()) == expected, 'Changed generated file: ' + name)
    banks = unique(read(data / 'banks.jsonl'), 'bank_id')
    cases = unique(read(data / 'cases.jsonl'), 'state_id'); labels = unique(read(data / 'labels.jsonl'), 'state_id')
    check(set(cases) == set(labels), 'Missing state/annotation')
    parents = {c['family_id']: c for c in read(SOURCE / 'cases.jsonl') if c['fit_split'] == 'train' and c['mode'] == 'retry_branch' and c['state'] == 0}
    check(set(banks) == set(parents), 'Non-train or omitted source')
    role_counts = Counter(); actual_pairs = {}; needed_states = 0
    for bid, bank in banks.items():
        check(bank['records'] == parents[bid]['bank'], 'Historical facts modified')
    for sid, c in cases.items():
        l = labels[sid]; records = banks[c['bank_id']]['records']; mids = {m['memory_id'] for m in records}
        expected_c, expected_l = make_case(parents[c['family_id']], c['template_id'], c['controlled_branch'], c['observed_mask'])
        check(c == expected_c and l == expected_l, 'Case/annotation differs from controlled derivation')
        check(l['utility'] is None and l['actual_gain_or_harm'] is None, 'Unmeasured effect label copied')
        check(set(l['candidate_roles']) == mids and set(c['observed_memory_ids']) <= mids, 'Unannotated or unknown record')
        check(visible_rule(c, records) == l['required_memory_ids'], 'Public rule mismatch')
        needed_states += bool(l['required_memory_ids']); role_counts.update(l['candidate_roles'].values())
    rows = read(data / 'training-pairs.jsonl')
    check(len(rows) == 4 * len(cases), 'Missing hard-reference pairs')
    seen = set(); conflicts = defaultdict(set)
    for r in rows:
        sid, mid = r['state_id'], r['memory_id']; c, l = cases[sid], labels[sid]
        check((sid, mid) not in seen, 'Duplicate training pair'); seen.add((sid, mid))
        bank = banks[c['bank_id']]
        check(mid in bank['core_memory_ids'] and r['split'] == 'train' and r['source_group'] == c['source_group'], 'Invalid supervision source')
        check(r['role'] == l['candidate_roles'][mid] and r['label'] == int(r['role'] == 'needed_now'), 'Wrong pair target')
        memory = next(m for m in bank['records'] if m['memory_id'] == mid)
        h = sha(pair_text(c, memory).encode()); check(h == r['input_sha256'], 'Actual encoder input changed')
        conflicts[h].add(r['label'])
    check(all(len(v) == 1 for v in conflicts.values()), 'Identical model input with contradictory labels')
    changes = Counter()
    for edge in read(data / 'contrasts.jsonl'):
        a, b = cases[edge['first']], cases[edge['second']]; la, lb = labels[a['state_id']], labels[b['state_id']]
        check(a['family_id'] == b['family_id'] and a['bank_id'] == b['bank_id'], 'Cross-source contrast')
        if edge['kind'] == 'branch_change':
            check(a['goal'] == b['goal'] and a['observed_memory_ids'] == b['observed_memory_ids'], 'Unmatched branch contrast')
        elif edge['kind'] == 'observation_addition':
            added = edge['added_memory_id']
            check(a['goal'] == b['goal'] and a['status_text'] == b['status_text'], 'State changed in observation contrast')
            check(set(b['observed_memory_ids']) - set(a['observed_memory_ids']) == {added}, 'Multiple facts changed')
            check(set(lb['required_memory_ids']) == set(la['required_memory_ids']) - {added}, 'Observation monotonicity violated')
        else:
            check(edge['kind'] == 'equivalent_wording' and la == dict(lb, state_id=la['state_id']), 'Meaning/label changed by wording')
        changes[edge['kind']] += 1
        changes[edge['kind'] + '/needed_set_changed'] += set(la['required_memory_ids']) != set(lb['required_memory_ids'])
    check(len(cases) == 1056 and len(banks) == 11, 'Unexpected design size')
    packet = read(data / 'review-packet.jsonl'); keys = unique(read(data / 'review-key.jsonl'), 'review_id')
    check(len(packet) == 24 and {r['review_id'] for r in packet} == set(keys), 'Broken blind review packet')
    check(all(not set(r) & {'state_id', 'controlled_branch', 'observed_mask', 'required_memory_ids', 'candidate_roles'} for r in packet), 'Annotation leaked into blind review packet')
    check(all(k['required_memory_ids'] == labels[k['state_id']]['required_memory_ids'] for k in keys.values()), 'Review key mismatch')
    return dict(status='passed', source_families=len(banks), independent_sources_added=0, states=len(cases),
                original_records=sum(len(b['records']) for b in banks.values()), full_bank_candidate_pairs=sum(role_counts.values()),
                training_reference_pairs=len(rows), positive_training_pairs=sum(r['label'] for r in rows),
                unique_actual_training_inputs=len(conflicts), contradictory_actual_inputs=0,
                needed_states=needed_states, no_need_states=len(cases) - needed_states,
                full_bank_roles=dict(role_counts), contrasts=dict(changes), actual_utility_labels=0,
                all_negative_accuracy_on_training_pairs=1 - sum(r['label'] for r in rows) / len(rows),
                all_negative_accuracy_on_full_bank=1 - role_counts['needed_now'] / sum(role_counts.values()),
                blind_review_cases=len(packet), independent_human_reviews=0,
                new_worker_calls=0, new_training=0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('build', 'audit')); parser.add_argument('--data', type=Path, default=DATA)
    args = parser.parse_args()
    print(json.dumps(build(args.data) if args.mode == 'build' else audit(args.data), ensure_ascii=False, indent=2))
