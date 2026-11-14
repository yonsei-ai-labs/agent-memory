"""Validate evidence, grouped views and diagnostic baselines without model calls."""
from collections import Counter, defaultdict
import ast
import hashlib
import json
from pathlib import Path
import sys

from tokenizers import Tokenizer

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'experiments/swe_memory/scripts'))
from records import now, write_json
from build_sources_and_effects import ref, dump_lines


def read(path):
    return json.loads(path.read_text())


def lines(name):
    return [json.loads(line) for line in (HERE / name).read_text().splitlines()]


def majority(values):
    counts = Counter(values)
    return min(counts, key=lambda value: (-counts[value], value))


def metrics(truth, predictions):
    f1 = []
    for label in range(3):
        tp = sum(a == b == label for a, b in zip(truth, predictions))
        fp = sum(a != label and b == label for a, b in zip(truth, predictions))
        fn = sum(a == label and b != label for a, b in zip(truth, predictions))
        if 2 * tp + fp + fn:
            f1.append(2 * tp / (2 * tp + fp + fn))
    return dict(n=len(truth), accuracy=sum(a == b for a, b in zip(truth, predictions))/len(truth),
                macro_f1_over_present_labels=sum(f1)/len(f1))


def graph_groups(rows, transfers):
    parent = {}
    def find(node):
        parent.setdefault(node, node)
        if parent[node] != node:
            parent[node] = find(parent[node])
        return parent[node]
    for row in rows + transfers:
        a, b = find(row['source_instance_id']), find(row['target_instance_id'])
        parent[b] = a
    components = defaultdict(list)
    for node in parent:
        components[find(node)].append(node)
    groups = {node: min(members) for members in components.values() for node in members}
    return groups, [sorted(members) for members in components.values()]


def verify_refs(value, seen):
    if isinstance(value, dict):
        if 'path' in value and 'sha256' in value:
            path = ROOT / value['path']
            if value['path'] not in seen:
                assert path.is_file(), value['path']
                assert hashlib.sha256(path.read_bytes()).hexdigest() == value['sha256'], value['path']
                seen.add(value['path'])
        for item in value.values():
            verify_refs(item, seen)
    elif isinstance(value, list):
        for item in value:
            verify_refs(item, seen)


def expected_label(row):
    """Independent read of stored observations / complete parsed inventories."""
    label_basis = row['annotation']['label_basis']; facet = row['facet']
    if row['stratum'] != 'known_current_evidence':
        return 2
    if 'probe_id' in label_basis:
        d = read(HERE/'evidence/runtime'/(label_basis['probe_id']+'.json'))['observed']
        value = d.get('result', {})
        checks = {
            'initialization_prerequisites': lambda: d['kind'] == 'returned' and value['rendered'],
            'validation_before_shortcut': lambda: d['kind'] == 'returned' and value['size'] == 0,
            'output_contract_change': lambda: value['dtype'].startswith(('int','uint')),
            'numeric_guard_scope': lambda: value['ghi_is_exact_zero'],
            'source_observation_transfer': lambda: value['is_zero'],
            'namespace_reuse': lambda: value['module_a_value'] == value['module_b_value'],
            'lookup_resolution': lambda: value['value_repr'] == "<class 'str'>",
            'config_guard': lambda: value['output'] == '(value)',
            'dimension_normalization_guard': lambda: value['dimensionless'],
            'container_representation': lambda: value['grid_shape'] == [1,1] and value['cell_repr'].startswith('Matrix('),
            'legacy_alias_identity': lambda: value['legacy_alias_same_class'],
            'canonical_module_change': lambda: value['canonical_module'] == 'sklearn.preprocessing.label',
        }
        return 0 if checks[facet]() else 1
    location = 'public-extra-code' if facet == 'shared_implementation_scope' or label_basis.get('extra') else 'public-code'
    context = read(HERE/'evidence'/location/(row['target_instance_id']+'.json'))
    file = next(f for f in context['files'] if f['path'] == label_basis['file'])
    if facet == 'source_path_reorganization':
        return 0 if file['exists_at_base'] else 1
    tree = ast.parse(file['text'])
    if facet == 'keyword_compatibility':
        node = next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name == label_basis['function'])
        condition = label_basis['keyword'] in [a.arg for a in node.args.args + node.args.kwonlyargs]
    elif facet == 'helper_availability':
        condition = any(isinstance(n,ast.FunctionDef) and n.name == label_basis['symbol'] for n in ast.walk(tree))
    elif facet == 'versioned_api_availability':
        node = next(n for n in ast.walk(tree) if isinstance(n,ast.ClassDef) and n.name == 'Query')
        condition = any(isinstance(n,ast.FunctionDef) and n.name == label_basis['symbol'] for n in node.body)
    elif facet == 'shared_implementation_scope':
        node = next(n for n in ast.walk(tree) if isinstance(n,ast.ClassDef) and n.name == label_basis['class_name'])
        condition = any(isinstance(n,ast.Name) and n.id == 'BaseMixture' for n in node.bases)
    elif facet == 'memory_target_family':
        node = next(n for n in ast.walk(tree) if isinstance(n,ast.ClassDef) and n.name == label_basis['class_name'])
        family = label_basis['source_family']
        assert family in row['input']['candidate_text']
        condition = node.name == family or any(isinstance(n,ast.Name) and n.id == family for n in node.bases)
    else:
        raise ValueError(facet)
    return 0 if condition else 1


def diagnostics(rows, lengths, source_by_id):
    repositories = sorted({source_by_id[r['source_instance_id']]['repo'] for r in rows})
    features = {
        'state_length_stump': lambda r: lengths[r['sample_id']]['state_text'],
        'candidate_length_stump': lambda r: lengths[r['sample_id']]['candidate_text'],
    }
    predictions = defaultdict(dict); fold_metrics = []
    for repo in repositories:
        train = [r for r in rows if source_by_id[r['source_instance_id']]['repo'] != repo]
        test = [r for r in rows if source_by_id[r['source_instance_id']]['repo'] == repo]
        default = majority([r['label'] for r in train])
        source_map = {}
        for outcome in [True,False]:
            labels = [r['label'] for r in train if source_by_id[r['source_instance_id']]['actual_source_official_resolved'] == outcome]
            source_map[outcome] = majority(labels) if labels else default
        models = {
            'training_majority': lambda r: default,
            'source_outcome_only': lambda r: source_map[source_by_id[r['source_instance_id']]['actual_source_official_resolved']],
            'question_template_lookup': lambda r: (
                majority([v['label'] for v in train if v['annotation']['current_question'] == r['annotation']['current_question']])
                if any(v['annotation']['current_question'] == r['annotation']['current_question'] for v in train) else default),
            'missing_current_cue_then_majority': lambda r: 2 if 'current fixture,configuration and relevant inspection are not observed' in r['input']['state_text'] else default,
        }
        for name, feature in features.items():
            values = sorted({feature(r) for r in train})
            best = None
            for threshold in values:
                left = [r['label'] for r in train if feature(r) <= threshold]
                right = [r['label'] for r in train if feature(r) > threshold]
                a, b = majority(left) if left else default, majority(right) if right else default
                errors = sum(r['label'] != (a if feature(r) <= threshold else b) for r in train)
                candidate = (errors,threshold,a,b)
                if best is None or candidate < best:
                    best = candidate
            _, threshold, a, b = best
            models[name] = lambda r, feature=feature, threshold=threshold, a=a, b=b: a if feature(r) <= threshold else b
        for name, predict in models.items():
            inferred = [predict(r) for r in test]
            predictions[name].update({r['sample_id']:p for r,p in zip(test,inferred)})
            fold_metrics.append(dict(held_repository=repo,baseline=name,**metrics([r['label'] for r in test],inferred)))
    aggregate = []
    for name, inferred in predictions.items():
        for subset_name, subset in [('all_views',rows),('known_current_evidence',[r for r in rows if r['stratum']=='known_current_evidence'])]:
            aggregate.append(dict(baseline=name,subset=subset_name,**metrics([r['label'] for r in subset], [inferred[r['sample_id']] for r in subset])))
    ceilings = []
    for side in ['state_text','candidate_text','question_plus_candidate_text']:
        for subset_name, subset in [('all_views',rows),('known_current_evidence',[r for r in rows if r['stratum']=='known_current_evidence'])]:
            groups=defaultdict(list)
            for r in subset:
                text = (r['input']['state_text'].split('\n\n',1)[0]+'\n\n'+r['input']['candidate_text']
                        if side=='question_plus_candidate_text' else r['input'][side])
                groups[text].append(r['label'])
            correct=sum(max(Counter(values).values()) for values in groups.values())
            ceilings.append(dict(input_side=side,subset=subset_name,n=len(subset),
                empirical_identical_input_accuracy_ceiling=correct/len(subset),
                conflicting_identical_input_groups=sum(len(set(v))>1 for v in groups.values())))
    write_json(HERE/'baseline-diagnostics.json',dict(at=now(),protocol='Leave one repository out; fit each trivial baseline on other repositories only.',
        aggregate=aggregate,folds=fold_metrics,identical_input_ceilings=ceilings,
        warning='Tiny discovery diagnostics,not neural-model scores. Ceilings are in-sample information bounds,not cross-validation accuracy. '
            'For a fair memory-only ablation retain the question but remove current observations; candidate_text alone also removes task identity. '
            'Missing-current language deliberately reveals its control condition; report the seven withheld views separately. '
            'Facet templates and source outcome/length may remain shortcuts. Strong rule/execution baselines are still required for any model claim.'))


def run():
    assert not (HERE/'artifact-index.json').exists(), 'Frozen dataset; create a new version.'
    rows = lines('relations.jsonl'); inputs = lines('learner-inputs.jsonl'); labels = lines('supervision.jsonl')
    transfers=lines('transfer-candidates.jsonl'); effects=lines('effect-observations.jsonl')
    cards=read(HERE/'source-bank.json')['memory_cards']
    source_by_id={r['source_instance_id']:r for r in cards if r['kind']=='procedure'}
    for card in cards:
        assert hashlib.sha256(card['text'].encode()).hexdigest()==card['text_sha256']
        original=read(ROOT/card['original_card_file']['path'])['cards']
        assert next(c for c in original if c['id']==card['memory_id'])['text']==card['text']
    for path in (HERE/'evidence/public-code').glob('*.json'):
        for file in read(path)['files']:
            if file['exists_at_base']:
                assert hashlib.sha256(file['text'].encode()).hexdigest()==file['base_sha256'],(path,file['path'])
    assert len(rows)==74 and len(inputs)==len(labels)==len(rows)
    by_id={r['sample_id']:r for r in rows}; assert len(by_id)==len(rows)
    for r,x,y in zip(rows,inputs,labels):
        assert set(x)=={'sample_id','input'} and set(x['input'])=={'state_text','candidate_text'}
        assert x['sample_id']==y['sample_id']==r['sample_id'] and x['input']==r['input'] and y['label']==r['label']
        assert r['annotation']['utility_label'] is None and r['label']==expected_label(r),r['sample_id']
        assert r['source_available_at'] < r['target_public_issue_created_at']
        state=r['input']['state_text']
        assert 'Historical focused command:' not in state and 'Later official SOURCE outcome:' not in state
        forbidden=['resolved_to_str_type','legacy_alias_same_class','suppression_guard_active','ghi_is_exact_zero',
            '"dimensionless":','"cell_is_matrix":','"same_result":','"is_zero":','official_resolved','test_patch','FAIL_TO_PASS']
        assert not any(token in state for token in forbidden),r['sample_id']
        if r['stratum']=='same_state_counter_memory':
            parent=by_id[r['annotation']['label_basis']['parent_case']]
            assert state==parent['input']['state_text'] and r['input']['candidate_text']!=parent['input']['candidate_text']
            assert r['annotation']['label_basis']['absent_required_anchor'] not in r['input']['candidate_text']
        if r['stratum']=='withheld_current_evidence_control':
            parent=by_id[r['annotation']['label_basis']['parent_case']]
            assert r['input']['candidate_text']==parent['input']['candidate_text'] and state!=parent['input']['state_text']
    seen=set()
    for name in ['relations.jsonl','transfer-candidates.jsonl','effect-observations.jsonl','source-bank.json']:
        data=lines(name) if name.endswith('.jsonl') else read(HERE/name)
        verify_refs(data,seen)
    assert len({r['observation_id'] for r in effects})==116
    assert sum(r['labels']['submitted_success'] is None for r in effects)==1
    assert all(r['counterfactual_labels'] is None and r['individual_memory_utility_label'] is None for r in effects)
    assert all(r['utility_label'] is None and r['current_applicability_label'] is None for r in transfers)
    runtime=[]
    for path in sorted((HERE/'evidence/runtime').glob('*.json')):
        d=read(path)
        assert d['specification']['interpreter']=='/opt/miniconda3/envs/testbed/bin/python',path
        assert d['execution']['returncode']==0,path
        runtime.append(d)
    assert len(runtime)==29
    tokenizer_path=ROOT/'training/memory_selector/outputs/20261107T074128Z/artifacts/tokenizer/tokenizer.json'
    tokenizer=Tokenizer.from_file(str(tokenizer_path));tokenizer.no_truncation();tokenizer.no_padding()
    lengths={r['sample_id']:{side:len(tokenizer.encode(text).ids) for side,text in r['input'].items()} for r in rows}
    assert max(n for pair in lengths.values() for n in pair.values()) < 2048
    write_json(HERE/'token-audit.json',dict(tokenizer=ref(tokenizer_path),truncation=False,padding=False,
        max_field_tokens=max(n for pair in lengths.values() for n in pair.values()),limit_per_field=2048,
        instruction_must_be_added_by_loader=True,rows=lengths))
    groups,components=graph_groups(rows,transfers)
    repositories=sorted({source_by_id[r['source_instance_id']]['repo'] for r in rows})
    folds=[]
    for repo in repositories:
        valid=[r for r in rows if source_by_id[r['source_instance_id']]['repo']==repo]
        train=[r for r in rows if source_by_id[r['source_instance_id']]['repo']!=repo]
        assert not ({groups[r['source_instance_id']] for r in train}&{groups[r['source_instance_id']] for r in valid})
        folds.append(dict(held_repository=repo,train_sample_ids=[r['sample_id'] for r in train],
            validation_sample_ids=[r['sample_id'] for r in valid],test_sample_ids=[]))
    write_json(HERE/'group-splits.json',dict(at=now(),official_test_set=False,connected_components=components,
        sample_group={r['sample_id']:groups[r['source_instance_id']] for r in rows},folds=folds,
        policy='Discovery leave-one-repository-out validation only. All views and linked source/target nodes stay together. '
            'All source families are already known in the project. Reserve genuinely new sources for independent confirmation.'))
    contrasts=defaultdict(list)
    for r in rows:
        if r['stratum']=='known_current_evidence':
            contrasts[(r['input']['candidate_text'],r['annotation']['current_question'])].append(r)
    matched=[dict(source_instance_id=values[0]['source_instance_id'],question=values[0]['annotation']['current_question'],
        sample_ids=[r['sample_id'] for r in values],labels=[r['label_name'] for r in values])
        for values in contrasts.values() if len({r['label'] for r in values})>1]
    by_state=defaultdict(list)
    for r in rows:
        by_state[r['input']['state_text']].append(r)
    memory_matched=[dict(sample_ids=[r['sample_id'] for r in values],labels=[r['label_name'] for r in values],
        contains_unknown_control=any(r['label']==2 for r in values))
        for values in by_state.values() if len({r['label'] for r in values})>1]
    write_json(HERE/'paired-contrasts.json',dict(same_memory_question_changed_current_with_label_change=matched,
        same_state_changed_memory=memory_matched,
        missing_memory_premise_controls=[dict(parent=r['annotation']['label_basis']['parent_case'],counter=r['sample_id'])
            for r in rows if r['stratum']=='same_state_counter_memory'],
        warning='Mechanism relation contrasts,not observed performance counterfactuals.'))
    diagnostics(rows,lengths,source_by_id)
    result=dict(at=now(),model_api_calls=0,neural_training_runs=0,relation_rows=len(rows),
        verified_reference_files=len(seen),label_checks=len(rows),temporal_forward_checks=len(rows),
        verbatim_original_cards_checked=len(cards),public_base_file_hashes_checked=True,
        correct_environment_runtime_probes=len(runtime),expected_probe_exceptions=sum(r['observed']['kind']=='exception' for r in runtime),
        paired_current_contrasts=len(matched),paired_memory_contrasts=len(memory_matched),
        counter_memory_unknown_controls=5,fully_crossed_real_memory_grids=2,missing_current_controls=7,
        group_components=len(components),group_folds=len(folds),actual_action_observations=len(effects),
        complete_action_outcomes=sum(r['labels']['submitted_success'] is not None for r in effects),
        public_contexts=len(list((HERE/'evidence/public-code').glob('*.json'))),
        labelled_relation_contexts=len({r['target_instance_id'] for r in rows}),
        preparation_failures_excluded=True,predictor_schema_and_verdict_flag_checks='passed',
        semantic_review='Single researcher evidence review; no independent second annotation or new policy effect established.',
        all_checks_passed=True)
    write_json(HERE/'validation.json',result);print(json.dumps(result))


if __name__=='__main__':
    run()
