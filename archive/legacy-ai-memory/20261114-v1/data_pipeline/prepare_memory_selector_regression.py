"""Freeze common-input retention and surface-robustness tests without new training."""
import argparse
from collections import Counter
import json
from pathlib import Path
import zipfile

from data_pipeline.memory_decision_dataset import ROOT, digest, readl, write, write_rows
from data_pipeline.prepare_memory_need_sources import MODELS
from data_pipeline.prepare_memory_state_screen import memory_text
from data_pipeline.travel_retrieval import annotations, PAIR_INSTRUCTION
from training.memory_selector.memory_need_learning import INSTRUCTION
from training.memory_selector.travel_state_protocol import references

PRIOR = ROOT / 'runs/memory-need-20261111-v1/training'
NEED = ROOT / 'datasets/memory_need/20261111-v1'
RETRIEVAL = ROOT / 'datasets/travel_retrieval/20261109-full-request-v1'
SEARCH = ROOT / 'evaluation/results/travel-candidate-search-20261109-v1/predictions.zip'
VARIANTS = ('original', 'plain', 'distractor')


def verify(directory, index='manifest.json', field='sha256'):
    manifest = json.loads((directory / index).read_text())
    for name, sha in manifest[field].items():
        if digest((directory / name).read_bytes()) != sha:
            raise ValueError('Changed source: ' + name)


def alternate_surface(case, variant):
    """Keep all facts, choices and branch conditions; change only selector wording."""
    if variant == 'original':
        return case['goal'], case['selection_context']
    if variant not in VARIANTS:
        raise ValueError('Unknown surface')
    goal = case['goal'].replace('For my dinner, choose the same restaurant as ',
                               'For dinner, use the restaurant recorded for ')
    goal = goal.replace('For my accommodation, choose the same hotel as ',
                        'For accommodation, use the hotel recorded for ')
    goal = goal.replace('start by reusing ', 'begin with ')
    goal = goal.replace('If the current tool report says FIRST_ATTEMPT_FAILED, instead reuse ',
                        'If checkout of that initial plan fails, instead use ')
    if case['mode'] == 'observed_component':
        status = 'These earlier lookup results have been verified and remain current.'
    elif case['state'] == 0:
        status = 'Checkout for the initial plan has not been attempted.'
    else:
        status = 'Checkout for the initial plan was declined. Proceed with the backup.'
    if variant == 'distractor':
        unrelated = ('An unrelated map lookup failed earlier. ' if case['state'] == 0
                     or case['mode'] == 'observed_component'
                     else 'An unrelated map lookup succeeded earlier. ')
        status = unrelated + status
    return goal, status + '\n' + memory_text(case['observed'])


def state_text_rule(goal, context, candidates):
    """Reads only displayed text. It knows the frozen templates; not an independent oracle."""
    refs = references(goal)
    if len(refs) == 4:
        status = context.split('\n', 1)[0]
        failed = ('FIRST_ATTEMPT_FAILED:' in status or
                  'Checkout for the initial plan was declined.' in status)
        refs = refs[2:] if failed else refs[:2]
    if len(refs) != 2:
        raise ValueError('Unparsed public references')
    selected = []
    for ref in refs:
        prefix = f"Recorded plan for {ref['person']}. Day {ref['day']}. {ref['slot'].title()}:"
        if prefix in context:
            continue
        selected += [m['memory_id'] for m in candidates if m['text'].startswith(prefix)]
    return selected


def pair_text(case, record, prompt):
    if prompt not in ('reference', 'need'):
        raise ValueError('Unknown instruction')
    instruction = PAIR_INSTRUCTION if prompt == 'reference' else INSTRUCTION
    context = ('\nCurrent observations:\n' + case['context']) if case['context'] else ''
    return 'Instruct: ' + instruction + '\nQuery: ' + case['goal'] + context + '\nPast record:\n' + record['text']


def prepare(output):
    if output.exists():
        raise FileExistsError(output)
    verify(NEED); verify(RETRIEVAL, 'artifact-index.json')
    search_index = json.loads((SEARCH.parent / 'shared-manifest.json').read_text())
    if digest(SEARCH.read_bytes()) != search_index['archive_sha256']:
        raise ValueError('Changed old rankings')
    if json.loads((PRIOR / 'run-status.json').read_text())['status'] != 'complete':
        raise ValueError('Incomplete training')
    cases, labels, rules = [], [], {}
    with zipfile.ZipFile(SEARCH) as z:
        ranks = {r['query_id']: r for r in map(json.loads, z.read('artifacts/retriever/test/rankings.jsonl').splitlines())}
    memories = {m['memory_id']: m for m in readl(RETRIEVAL / 'memories.jsonl')}
    ys = {y['query_id']: y['positive_ids'] for y in readl(RETRIEVAL / 'supervision.jsonl')}
    for q in readl(RETRIEVAL / 'queries.jsonl'):
        if q['split'] != 'test' or q['condition'] != 'available':
            continue
        candidates = [dict(memory_id=mid, text=memories[mid]['text'], evidence=memories[mid]['text'])
                      for mid in ranks[q['query_id']]['hybrid'][:32]]
        refs, problems = annotations(q['question'], [memories[mid] for mid in q['candidate_ids']])
        if problems or {r['memory_id'] for r in refs} != set(ys[q['query_id']]):
            raise ValueError('Reference annotation changed')
        sid = 'retention:' + q['query_id']
        cases.append({'case_id': sid, 'parent_id': q['query_id'], 'family_id': q['source_group'],
                      'track': 'retention', 'variant': 'original', 'goal': q['question'], 'context': '',
                      'candidates': candidates, 'max_records': 10, 'memory_token_budget': None,
                      'prompts': ['reference', 'need']})
        labels.append({'case_id': sid, 'required_memory_ids': ys[q['query_id']]})
        candidate_refs, _ = annotations(q['question'], [memories[m['memory_id']] for m in candidates])
        ref_ids = {r['memory_id'] for r in candidate_refs}
        if ref_ids != {r['memory_id'] for r in refs} & {m['memory_id'] for m in candidates}:
            raise ValueError('Candidate-only rule differs')
        rules[sid] = [m['memory_id'] for m in candidates if m['memory_id'] in ref_ids][:10]
    need_labels = {y['state_id']: y for y in readl(NEED / 'labels-confirmation.jsonl')}
    rankings = {r['state_id']: r for r in readl(PRIOR / 'candidate-rankings.jsonl')}
    original = [c for c in readl(NEED / 'cases.jsonl') if c['fit_split'] == 'confirmation']
    for parent in original:
        bank = {m['memory_id']: m for m in parent['bank']}
        candidates = [{k: bank[mid][k] for k in ('memory_id', 'text', 'evidence')}
                      for mid in rankings[parent['state_id']]['candidate_ids']]
        for variant in VARIANTS:
            goal, context = alternate_surface(parent, variant)
            sid = 'state:' + variant + ':' + parent['state_id']
            case = {'case_id': sid, 'parent_id': parent['state_id'], 'family_id': parent['family_id'],
                    'track': 'state', 'variant': variant, 'goal': goal, 'context': context,
                    'candidates': candidates, 'max_records': parent['max_records'],
                    'memory_token_budget': parent['memory_token_budget'], 'prompts': ['need']}
            y = need_labels[parent['state_id']]
            rule = state_text_rule(goal, context, candidates)
            if set(rule) != set(y['required_memory_ids']):
                raise ValueError('Transformation changed reference/known-state rule')
            cases.append(case); rules[sid] = rule
            labels.append({'case_id': sid, 'required_memory_ids': y['required_memory_ids'],
                           'mode': parent['mode'], 'state': parent['state']})
    models = [{'name': 'old_reader', 'adapter': str(MODELS / 'reader/adapter'),
               'head': str(MODELS / 'reader/head-weights.npz')}]
    for view in ('goal', 'state'):
        for seed in (731, 1729):
            name = f'{view}--seed{seed}'
            models.append({'name': name, 'adapter': str(PRIOR / name / 'best-adapter'),
                           'head': str(PRIOR / name / 'best-head.npz')})
    for m in models:
        paths = [Path(m['head']), *sorted(Path(m['adapter']).glob('*'))]
        m['sha256'] = {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in paths if p.is_file()}
        m['adapter'] = str(Path(m['adapter']).relative_to(ROOT));m['head'] = str(Path(m['head']).relative_to(ROOT))
    if Counter(c['track'] for c in cases) != {'retention': 219, 'state': 108}:
        raise ValueError('Unexpected evaluation inventory')
    output.mkdir(parents=True)
    write_rows(output / 'cases.jsonl', cases);write_rows(output / 'labels.jsonl', labels)
    write(output / 'rules.json', rules);write(output / 'models.json', models)
    write(output / 'protocol.json', {
        'experiment': 'memory-selector-regression-20261111-v1', 'models': [m['name'] for m in models],
        'training_updates': 0, 'candidate_retrieval': 'Previously frozen learned-hybrid top32; identical across models and surface variants',
        'state_input': 'All five models see the same goal, progress and observed facts, regardless of original training view',
        'retention': 'Previously exposed 219 queries / 8 base plans; public reference histories, no worker utility labels',
        'retention_metric': 'Unthresholded Recall@10 primary; threshold .5 exact-set and recall secondary. No token cap in historical protocol, same K=10 for all.',
        'prompt_control': 'Both historical reference and current need instructions for every retention checkpoint; report both, do not choose the better one.',
        'state_metric': 'Threshold .5 exact need set, complete evidence, redundant delivery and paired correctness under original K/token caps',
        'state_variants': 'Original, meaning-preserving plain wording, and unrelated lookup failure/success distractors; all36 per variant, same6 exposed held families',
        'rule_scope': 'Text-only template-aware rule; retention uses the annotation parser. Neither is an independent general-purpose baseline.',
        'new_natural_sources': 0, 'new_independent_families': 0, 'sealed_source_dev_test_used': False,
        'worker_plan': 'Reuse exact canonical worker messages only; selector-only rewrites do not change worker prompt. Complete missing calls in one bounded run if <=180; otherwise report utility as unmeasured and do not subsample.',
        'worker_calls_cap': 180, 'local_inference_wall_limit_seconds': 2400,
        'limits': 'Regression and controlled wording tests, not fresh-source confirmation or official benchmark; no threshold, checkpoint, prompt, or variant tuning after scoring.',
        'source_sha256': {'need_manifest': digest((NEED/'manifest.json').read_bytes()),
                          'retention_index': digest((RETRIEVAL/'artifact-index.json').read_bytes()),
                          'prior_search_archive': digest(SEARCH.read_bytes()),
                          'need_candidate_rankings': digest((PRIOR/'candidate-rankings.jsonl').read_bytes())}})
    write(output / 'manifest.json', {'sha256': {p.name: digest(p.read_bytes()) for p in output.iterdir() if p.is_file()},
                                    'generator_sha256': digest(Path(__file__).read_bytes())})
    return {'cases': len(cases), 'models': len(models), 'state_families': len({c['family_id'] for c in original}), 'new_training': False}


if __name__ == '__main__':
    p = argparse.ArgumentParser();p.add_argument('--output', type=Path, required=True)
    print(json.dumps(prepare(p.parse_args().output), indent=2))
