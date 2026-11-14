"""Full-request, full-history explicit-reference retrieval from pinned MemoryArena.

Public reference plans are the histories. This is a retrieval diagnostic, not
new agent trajectories, natural implicit relevance, or an official Arena score.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from data_pipeline.memoryarena_travel import (
    DATA_SHA, DATA_REVISION, cards_for_plan, digest, group_split, name_from_query,
    paragraphs, reference_from_clause, write_json, write_jsonl,
)

QUERY_INSTRUCTION = (
    "Given a current travel request, retrieve past travel records that it explicitly "
    "references. Match each referenced person, day and item, including comparison baselines."
)
PAIR_INSTRUCTION = (
    "Determine whether this past travel record is explicitly referenced anywhere in the "
    "current travel request, either to reuse a choice or as a comparison baseline. "
    "Match the referenced person, day and item; merely appearing in the travel party is not enough."
)


def annotations(question, cards):
    names = sorted({c['person'] for c in cards})
    refs, problems = [], []
    for clause, start, end in paragraphs(question):
        ref, reason = reference_from_clause(clause, names)
        if ref:
            matches = [c for c in cards if all(c[k] == ref[k] for k in ('person', 'day', 'slot'))]
            if len(matches) != 1:
                problems.append('absent_or_ambiguous_record')
            else:
                refs.append({'memory_id': matches[0]['memory_id'], 'reference': ref,
                             'clause': clause, 'span': [start, end]})
        elif any(re.search(r'\b' + re.escape(name) + r'\b', clause) for name in names):
            problems.append(reason)
        elif re.search(r'\b(same|join|share|previous|earlier|first traveler|first traveller|their|his|her)\b', clause, re.I):
            problems.append('unparsed_possible_reference')
    if not refs:
        problems.append('no_covered_explicit_reference')
    return refs, problems


def build(source, output):
    if output.exists():
        raise FileExistsError(output)
    if digest(source) != DATA_SHA:
        raise ValueError('Unexpected source revision')
    originals = [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
    split_plan = group_split(originals)
    assignment = {id_: g for g in split_plan['groups'] for id_ in g['row_ids']}
    queries, labels, evidence, memories = [], [], [], {}
    excluded = Counter()
    for original in originals:
        row_id = original['id']
        group = assignment[row_id]
        cards = cards_for_plan(row_id, original['base_person']['name'], 0, original['base_person']['daily_plans'])
        for turn, question in enumerate(original['questions'], 1):
            refs, problems = annotations(question, cards)
            if problems:
                excluded.update((group['split'], reason) for reason in set(problems))
            else:
                query_id = f'travel-bank-{row_id}-turn-{turn}'
                positives = sorted({r['memory_id'] for r in refs})
                ids = [c['memory_id'] for c in cards]
                if not positives or len(ids) != len(set(ids)) or not set(positives) < set(ids):
                    raise ValueError('Invalid complete bank')
                # Candidate order has no relationship with the requested person/day.
                ids.sort(key=lambda id_: hashlib.sha256((query_id + ':' + id_).encode()).hexdigest())
                for card in cards:
                    if card['prior_round'] >= turn:
                        raise ValueError('Future memory')
                    record = dict(card, source_row_id=row_id, source_group=group['source_group'], split=group['split'])
                    if card['memory_id'] in memories and memories[card['memory_id']] != record:
                        raise ValueError('Inconsistent repeated memory')
                    memories[card['memory_id']] = record
                base = {'query_id': query_id, 'base_query_id': query_id, 'split': group['split'],
                        'source_group': group['source_group'], 'source_row_id': row_id,
                        'current_round': turn, 'question': question,
                        'candidate_ids': ids, 'condition': 'available'}
                queries.append(base)
                labels.append({'query_id': query_id, 'positive_ids': positives})
                evidence.append({'base_query_id': query_id, 'source_row_id': row_id, 'current_round': turn,
                                 'references': refs, 'original_question_sha256': hashlib.sha256(question.encode()).hexdigest()})
                # Missing-evidence diagnostic only: query unchanged, all referenced records absent.
                absent = dict(base, query_id=query_id + '--missing', condition='missing',
                              candidate_ids=[id_ for id_ in ids if id_ not in positives])
                queries.append(absent)
                labels.append({'query_id': absent['query_id'], 'positive_ids': []})
            person = name_from_query(question)
            if person in {c['person'] for c in cards}:
                raise ValueError('Repeated speaker')
            cards.extend(cards_for_plan(row_id, person, turn, original['answers'][turn - 1]))
    output.mkdir(parents=True)
    write_jsonl(output / 'queries.jsonl', queries)
    write_jsonl(output / 'supervision.jsonl', labels)
    write_jsonl(output / 'provenance.jsonl', evidence)
    write_jsonl(output / 'memories.jsonl', sorted(memories.values(), key=lambda r: r['memory_id']))
    write_json(output / 'split-plan.json', split_plan)
    label_map = {r['query_id']: r['positive_ids'] for r in labels}
    available = [q for q in queries if q['condition'] == 'available']
    summary = {
        'status': 'draft_requires_review',
        'base_plans': dict(Counter(g['split'] for g in split_plan['groups'])),
        'base_requests': dict(Counter(q['split'] for q in available)),
        'queries_with_missing_diagnostic': dict(Counter(q['split'] for q in queries)),
        'memories': len(memories),
        'bank_sizes': {k: f([len(q['candidate_ids']) for q in available]) for k, f in [('min', min), ('max', max)]},
        'positive_counts': dict(sorted(Counter(len(label_map[q['query_id']]) for q in available).items())),
        'excluded_question_reasons': [{'split': split, 'reason': reason, 'count': n} for (split, reason), n in sorted(excluded.items())],
    }
    write_json(output / 'summary.json', summary)
    write_json(output / 'contract.json', {
        'task': 'full_request_explicit_travel_retrieval', 'query_instruction': QUERY_INSTRUCTION,
        'pair_instruction': PAIR_INSTRUCTION, 'history_mode': 'public_reference_history',
        'split_unit': 'identical_base_daily_plan', 'source_revision': DATA_REVISION, 'source_sha256': DATA_SHA,
        'official_benchmark_score': False, 'utility_labels': False,
        'previous_test_exposure': 'Same 8 base plans were already inspected in the earlier clause retrieval and utility pilots; this is a broader-condition diagnostic, not fresh-source confirmation.',
        'relevance': 'canonical explicitly referenced person/day/item records; all referenced records are positive, including multiple roles of one record',
        'not_covered': ['implicit applicability', 'alternative records with the same value', 'memory benefit or harm', 'real generated histories'],
        'missing_diagnostic': 'remove all canonically referenced records from the full bank without changing query text; no natural absence claim',
        'rule_baseline': 'same conservative parser as annotation, so expected perfect by construction; not independent labeling',
    })
    write_json(output / 'manifest.json', {'sha256': {p.name: digest(p) for p in sorted(output.iterdir())},
                                        'generator_sha256': digest(__file__)})
    return summary


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(build(a.source, a.output), indent=2))
