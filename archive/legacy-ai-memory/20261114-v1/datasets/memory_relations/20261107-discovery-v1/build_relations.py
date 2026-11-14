"""Build bounded relationship supervision and keep utility judgments unlabelled.

Current evidence goes on the state side; memory and source evidence go on the
candidate side. Labels and probe verdict flags never become predictor features.
Raw current observations are explicit inspection inputs, not pre-action evidence.
"""
from collections import Counter, defaultdict
import ast
import copy
import csv
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'experiments/swe_memory/scripts'))
from records import now, write_json
from build_sources_and_effects import ref, dump_lines

HERE = Path(__file__).resolve().parent
LABELS = ['supported', 'contradicted', 'not_established']
INSTRUCTION = ('Assess the bounded relationship asked in the question between a historical development memory '
    'and the currently available evidence. Answer supported, contradicted, or not_established. '
    'A historical fact or compatible precondition does not establish performance benefit.')
ANSI = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def j(path):
    return json.loads(path.read_text())


def load_context(iid, extra=False):
    return j(HERE / ('evidence/public-extra-code' if extra else 'evidence/public-code') / (iid + '.json'))


def file_data(context, path):
    return next(row for row in context['files'] if row['path'] == path)


def definition(context, path, name):
    data = file_data(context, path)
    if not data['exists_at_base']:
        return None
    return next((node for node in ast.walk(ast.parse(data['text']))
                 if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == name), None)


def excerpt(context, path, name, maximum_lines=45):
    node = definition(context, path, name)
    if node is None:
        return 'The selected definition was not found in this collected public module.'
    lines = file_data(context, path)['text'].splitlines()
    end = min(node.end_lineno, node.lineno + maximum_lines - 1)
    return '\n'.join(lines[node.lineno - 1:end])


def build():
    assert not (HERE / 'artifact-index.json').exists(), 'Frozen dataset; use a new version.'
    originals = j(HERE / 'source-bank.json')['memory_cards']
    memories = {row['source_instance_id']: row for row in originals if row['kind'] == 'procedure'}
    contexts = {p.stem: j(p) for p in (HERE / 'evidence/public-code').glob('*.json')}
    rows = []

    def candidate(source, override_text=None):
        memory = memories[source]
        text = override_text if override_text is not None else memory['text']
        proof = memory['focused_observation']
        evidence = (f'Historical focused command:\n{proof["command"]}\nSource-local exit status: {proof["returncode"]}\n'
            + ANSI.sub('', proof['output_excerpt'])[-450:]
            + '\nLater official SOURCE outcome: resolved=' + str(memory['actual_source_official_resolved']).lower()
            + '; evaluation_complete=' + str(memory['source_evaluation_complete']).lower())
        if source == 'scikit-learn__scikit-learn-10508':
            episode = j(HERE / 'evidence/source-episodes' / (source + '.json'))
            location = next(r for r in episode['commands'] if r['agent_command_index'] == 0)
            evidence += '\nAdditional recorded SOURCE inspection command (command 0):\n' + location['event']['action']['command']
        return 'Historical memory, scoped to its source experience:\n' + text + '\n\nSource evidence:\n' + evidence

    def add(identifier, source, iid, facet, question, current, label, rationale, provenance,
            origin, label_basis, memory_text=None, stage='before_controlled_fixture', stratum='known_current_evidence'):
        context = contexts[iid]; public = context['public_task']; memory = memories[source]
        forward = memory['available_at'] < public['created_at']
        assert forward, (source, iid, 'Not a forward historical context')
        header = (f'Current repository: {public["repo"]}; checkout version: {public["version"]}.\n'
                  + 'Public task title: ' + public['problem_statement'].splitlines()[0])
        row = dict(sample_id=identifier, task_kind='bounded_memory_context_relation_v1', facet=facet,
            source_instance_id=source, source_run=memory['source_run'], target_instance_id=iid,
            memory_id=memory['memory_id'] if memory_text is None else memory['memory_id'] + ':source_setup_excerpt',
            memory_origin='original_card' if memory_text is None else 'verbatim_source_command_setup_excerpt',
            input=dict(state_text='Relationship question:\n' + question + '\n\n' + header + '\n\nAvailable current evidence:\n' + current,
                       candidate_text=candidate(source, memory_text)),
            label=label, label_name=LABELS[label], stratum=stratum,
            annotation=dict(rationale=rationale, evidence_type=origin, label_basis=label_basis,
                current_question=question, utility_label=None,
                meaning='The label concerns the bounded relationship question, never overall memory utility.'),
            provenance=dict(source=[memory['source_evidence'], memory['original_card_file']], current=provenance),
            decision_stage=stage, temporal_forward=True, source_available_at=memory['available_at'],
            target_public_issue_created_at=public['created_at'], split='discovery_unsplit',
            independent_confirmation_eligible=False,
            review_status='single_researcher_evidence_reviewed_not_independently_annotated')
        rows.append(row)
        return row

    def runtime_case(probe, source, facet, question, label, rationale, current=None, text=None):
        path = HERE / 'evidence/runtime' / (probe + '.json'); evidence = j(path)
        job = evidence['specification']; iid = job['context_task']
        stage = 'before_controlled_fixture'
        if current is None:
            # Exclude all result dictionaries and measurement flags from predictor input.
            program = job['program'].split('\nresult=', 1)[0]
            current = 'A common current inspection program:\n' + program
            observed = evidence['observed']
            if observed['kind'] == 'exception':
                raw = {k: observed[k] for k in ['exception_type', 'message']}
            else:
                # Keep measurements; discard every memory-related verdict flag.
                allowed = {'rendered','values','dtype','size','output_repr','value_repr',
                           'factor','dimension','grid_shape','cell_type','cell_repr'}
                raw = {k:v for k,v in observed['result'].items() if k in allowed}
            current += '\nObserved raw result of this inspection:\n' + json.dumps(raw, sort_keys=True)
            stage = 'after_controlled_probe'
        return add(probe + ':' + facet, source, iid, facet, question, current, label, rationale,
            [ref(path), ref(HERE / 'evidence/public-code' / (iid + '.json'))],
            'controlled_runtime_fixture' if facet not in ['config_guard','namespace_reuse'] else 'controlled_predicate_operationalization',
            dict(probe_id=probe, observed_kind=evidence['observed']['kind']), text, stage=stage)

    #1. Initialization: exact setup excerpt from a failed source episode, not a new invented prescription.
    episode = j(HERE / 'evidence/source-episodes/django__django-11019.json')
    setup_command = next(r['event']['action']['command'] for r in episode['commands'] if r['agent_command_index'] == 17)
    setup_text = 'Recorded source setup excerpt (not a complete shell command):\n' + setup_command[:setup_command.index('from django import forms')]
    for mode in ['missing','settings_only','full']:
        path = HERE / 'evidence/runtime' / ('django-init-' + mode + '.json'); observed = j(path)['observed']
        label = 0 if observed['kind'] == 'returned' and observed['result'].get('rendered') else 1
        runtime_case('django-init-' + mode, 'django__django-11019', 'initialization_prerequisites',
            'Does the staged program already satisfy BOTH the settings and app-registry setup prerequisites illustrated by this memory?',
            label, 'Current rendering result and the displayed setup distinguish no settings,settings only,and complete setup.', text=setup_text)
    #2. Empty-input advice depends on fitted state and shape checks; empty dtype is a separate contract.
    for mode in ['fitted_empty','fitted_nonempty','unfitted_empty','invalid_shape','unseen_label']:
        probe = 'label-encoder-' + mode; observed = j(HERE / 'evidence/runtime' / (probe + '.json'))['observed']
        label = 0 if observed['kind'] == 'returned' and observed['result']['size'] == 0 else 1
        runtime_case(probe, 'scikit-learn__scikit-learn-10508', 'validation_before_shortcut',
            'Does the staged call reach the empty-result path after the fitted-state and shape validations described by memory?',
            label, 'A nonempty result or validation exception does not reach the source empty shortcut. Empty-looking invalid shapes are distinct.')
    empty = j(HERE / 'evidence/runtime/label-encoder-fitted_empty.json')['observed']['result']
    runtime_case('label-encoder-fitted_empty', 'scikit-learn__scikit-learn-10508', 'output_contract_change',
        'Does the current empty output retain the integer dtype observed in the historical source recipe?',
        0 if empty['dtype'].startswith(('int','uint')) else 1,
        'This checkout returns float64 for empty transform despite the source intp observation. A changed dtype is not a measured memory harm.')
    #3. Zero versus NaN and inconsistent zero-GHI observations are intentionally separate.
    for mode in ['zero','nan','positive','consistent_zero']:
        probe = 'pvlib-zero-guard-' + mode; observed = j(HERE / 'evidence/runtime' / (probe + '.json'))['observed']['result']
        runtime_case(probe, 'pvlib__pvlib-python-1154', 'numeric_guard_scope',
            'Does the staged input satisfy the exact-zero condition that triggered the source workaround?',
            0 if observed['ghi_is_exact_zero'] else 1,
            'The source condition is exact zero; NaN and positive GHI do not satisfy it.')
    for mode in ['zero','consistent_zero']:
        probe = 'pvlib-zero-guard-' + mode; result = j(HERE / 'evidence/runtime' / (probe + '.json'))['observed']['result']
        runtime_case(probe, 'pvlib__pvlib-python-1154', 'source_observation_transfer',
            'Does the source zero-output observation carry over to this displayed zero-GHI call in the current implementation?',
            0 if result['is_zero'] else 1,
            'Current zeroGHI/DHI0 returns0,while zeroGHI/DHI100 returns a nonzero diffuse term. The source fact remains historical; no agent effect is measured.')
    #4. Namespace changes make identical expression strings unsafe to cache by string alone.
    for mode in ['different','same_true','same_false']:
        probe = 'pytest-namespace-' + mode; evidence = j(HERE / 'evidence/runtime' / (probe + '.json'))
        fixture = evidence['specification']['fixture']; equivalent = evidence['observed']['result']['same_result']
        current = ('The same expression string is evaluated separately in two current module bindings.\n'
                   + json.dumps(fixture, sort_keys=True))
        runtime_case(probe, 'pytest-dev__pytest-7373', 'namespace_reuse',
            'Would reusing one expression-only result preserve the outcomes of these TWO displayed module evaluations?',
            0 if equivalent else 1,
            'Only this explicit pair is evaluated. Matching results do not establish that expression-only caching is generally safe.', current)
    #5. Sphinx catches NameError: successful wrapper return is not resolved type information.
    for mode in ['known','missing','qualified_known']:
        probe = 'sphinx-alias-' + mode; result = j(HERE / 'evidence/runtime' / (probe + '.json'))['observed']['result']
        runtime_case(probe, 'sphinx-doc__sphinx-8435', 'lookup_resolution',
            'Does the current namespace allow the source-related lookup to resolve the annotation to a type rather than leave its string unresolved?',
            0 if result['resolved_to_str_type'] else 1,
            'The wrapper can return normally with an unresolved string. A populated qualified namespace can also resolve successfully; qualified names are not universally invalid.')
    for mode in ['none','description','signature']:
        current = ("Current configuration: autodoc_typehints=" + repr(mode)
            + '\nThe current renderer is stringify_signature; no historical guard is applied in this input.')
        result = j(HERE / 'evidence/runtime' / ('sphinx-config-' + mode + '.json'))['observed']['result']
        runtime_case('sphinx-config-' + mode, 'sphinx-doc__sphinx-8282', 'config_guard',
            'Does the current setting fall within the annotation-suppression conditions specified by memory?',
            0 if result['suppression_guard_active'] else 1,
            'This is an operationalization of the source condition,not an independent test of all Sphinx configuration semantics.', current)
    #6. Physical equivalence is not identical to a dimensionless normalization guard.
    for mode in ['ratio','second','velocity']:
        probe = 'sympy-dimension-' + mode; result = j(HERE / 'evidence/runtime' / (probe + '.json'))['observed']['result']
        runtime_case(probe, 'sympy__sympy-24066', 'dimension_normalization_guard',
            'Does the displayed expression meet the eligibility condition for the historical normalization?',
            0 if result['dimensionless'] else 1,
            'The ratio is dimensionless; second and velocity remain dimensional. Do not normalize an equivalent dimensional quantity to Dimension1.')
    for mode in ['inferred_shape','explicit_shape']:
        probe = 'sympy-block-' + mode; result = j(HERE / 'evidence/runtime' / (probe + '.json'))['observed']['result']
        runtime_case(probe, 'sympy__sympy-18621', 'container_representation',
            'Does the displayed construction preserve an entire matrix as one outer block-grid cell,as required by the source recipe?',
            0 if result['cell_is_matrix'] else 1,
            'Inferred nested dense construction flattens entries; explicit outer1x1 dimensions preserve the block object.')
    #7. A Git path can disappear while a generated import compatibility alias survives.
    for suffix in ['12471','15512','15535']:
        probe = 'sklearn-alias-' + suffix; evidence = j(HERE / 'evidence/runtime' / (probe + '.json'))
        result = evidence['observed']['result']; iid = evidence['specification']['context_task']
        wrapper_path = HERE / 'evidence/compatibility-wrappers' / (iid + '.json')
        wrapper = j(wrapper_path)['wrapper']['text']
        view = ('Current canonical module and defining file from public introspection:\n'
            + json.dumps({k: result[k] for k in ['canonical_module','definition_file','legacy_module_file']}, sort_keys=True))
        if len(wrapper) < 4000:
            view += '\nComplete generated legacy wrapper:\n' + wrapper
        else:
            view += '\nThe original public module contains the LabelEncoder class definition.'
        row = runtime_case(probe, 'scikit-learn__scikit-learn-10508', 'legacy_alias_identity',
            'Does the old module import associated with this memory still expose the same class as the current canonical import?',
            0 if result['legacy_alias_same_class'] else 1,
            'Runtime identity is checked separately from tracked-file existence. A missing old Git path is not proof that its import is unusable.', view)
        row['provenance']['current'].append(ref(wrapper_path)); row['decision_stage'] = 'after_passive_runtime_inspection'
        canonical_row = runtime_case(probe, 'scikit-learn__scikit-learn-10508', 'canonical_module_change',
            'Is the current canonical class implementation still located in the original module path named by this memory?',
            0 if result['canonical_module'] == 'sklearn.preprocessing.label' else 1,
            'The original module remains canonical in12471,but newer versions define the class in _label while retaining a legacy alias.', view)
        canonical_row['decision_stage'] = 'after_passive_runtime_inspection'
    #8. Exact public signatures and definitions: limited binding/existence questions.
    for iid in ['sphinx-doc__sphinx-8627','sphinx-doc__sphinx-10451']:
        context = contexts[iid]
        for path, name in [('sphinx/util/inspect.py','stringify_signature'), ('sphinx/util/typing.py','stringify')]:
            node = definition(context, path, name); assert isinstance(node, ast.FunctionDef)
            names = [a.arg for a in node.args.args + node.args.kwonlyargs]
            current = 'Selected CURRENT callable definition:\n' + excerpt(context,path,name,maximum_lines=15)
            add(iid + ':' + name + ':keyword', 'sphinx-doc__sphinx-8282', iid, 'keyword_compatibility',
                'Does this current callable explicitly declare the keyword parameter used by the historical rendering prescription?',
                current, 0 if 'show_annotation' in names else 1,
                'This is Python signature compatibility only; it does not establish that invoking the recipe solves the current bug.',
                [ref(HERE / 'evidence/public-code' / (iid + '.json'))], 'public_ast',
                dict(file=path, function=name, keyword='show_annotation'), stage='after_common_public_inspection')
        for role, name in [('introduced_helper','_apply_type_aliases'),('shared_lookup','get_type_hints')]:
            path = 'sphinx/ext/autodoc/__init__.py' if role == 'introduced_helper' else 'sphinx/util/typing.py'
            text = file_data(context,path)['text']; names = sorted({n.name for n in ast.walk(ast.parse(text)) if isinstance(n,ast.FunctionDef)})
            question = ('Is the helper introduced in the historical submitted approach already defined in this shown current module?'
                        if role == 'introduced_helper' else 'Does this current module provide the type-hint lookup function used by memory?')
            current = 'Complete function-name inventory of ' + path + ':\n' + json.dumps(names)
            add(iid + ':' + role, 'sphinx-doc__sphinx-8435', iid, 'helper_availability', question,
                current, 0 if name in names else 1,
                'An actor-introduced helper can be absent in upstream current code while the standard lookup dependency remains present.',
                [ref(HERE / 'evidence/public-code' / (iid + '.json'))], 'public_ast', dict(file=path,symbol=name),
                stage='after_common_public_inspection')
    for iid in ['django__django-15213','django__django-16041','django__django-16910']:
        context = contexts[iid]; node = definition(context,'django/db/models/sql/query.py','Query'); assert isinstance(node,ast.ClassDef)
        names = [n.name for n in node.body if isinstance(n,ast.FunctionDef)]
        add(iid + ':query-helper', 'django__django-14667', iid, 'versioned_api_availability',
            'Does the concrete Query inspection helper with the empty-name early return described by memory still exist in this current class?',
            'Complete direct method inventory of the current public Query class:\n' + json.dumps(names),
            0 if 'deferred_to_data' in names else 1,
            'The helper is present in15213 but removed in later select-mask APIs. This does not prove the underlying only/defer principle is unusable.',
            [ref(HERE / 'evidence/public-code' / (iid + '.json'))], 'public_ast',
            dict(file='django/db/models/sql/query.py',symbol='deferred_to_data'),stage='after_common_public_inspection')
    for iid in ['scikit-learn__scikit-learn-13142','scikit-learn__scikit-learn-15512']:
        context = load_context(iid, extra=True)
        for name, possible in [('GaussianMixture',['sklearn/mixture/gaussian_mixture.py','sklearn/mixture/_gaussian_mixture.py']),
                              ('BayesianGaussianMixture',['sklearn/mixture/bayesian_mixture.py','sklearn/mixture/_bayesian_mixture.py']),
                              ('AffinityPropagation',['sklearn/cluster/affinity_propagation_.py','sklearn/cluster/_affinity_propagation.py'])]:
            path = next(p for p in possible if file_data(context,p)['exists_at_base']); node = definition(context,path,name)
            assert isinstance(node,ast.ClassDef)
            bases = [ast.unparse(n) for n in node.bases]
            header = '\n'.join(file_data(context,path)['text'].splitlines()[node.lineno-1:node.lineno+2])
            add(iid + ':' + name + ':inheritance', 'scikit-learn__scikit-learn-11281', iid, 'shared_implementation_scope',
                'Does the selected current estimator directly use the shared base implementation targeted by the historical change?',
                'Selected current estimator definition:\n' + header,
                0 if 'BaseMixture' in bases else 1,
                'The mixture estimators inherit BaseMixture;AffinityPropagation does not. A shared clusterer interface is not the same implementation path.',
                [ref(HERE / 'evidence/public-extra-code' / (iid + '.json'))], 'public_ast', dict(file=path,class_name=name),
                stage='after_common_public_inspection')
    for iid in ['pytest-dev__pytest-7432','pytest-dev__pytest-8906']:
        context = contexts[iid]; inventory = {r['path']:r['exists_at_base'] for r in context['files']}
        exists = inventory['src/_pytest/mark/evaluate.py']
        add(iid + ':original-helper-file', 'pytest-dev__pytest-7373', iid, 'source_path_reorganization',
            'Is the original implementation file named by memory still tracked at the current public checkout?',
            'Current tracked-file inventory for these complete collected paths:\n' + json.dumps(inventory,sort_keys=True),
            0 if exists else 1,
            'The old implementation path is absent. Condition evaluation persists elsewhere; absence does not refute the namespace-dependency principle.',
            [ref(HERE / 'evidence/public-code' / (iid + '.json'))], 'public_git_tree',
            dict(file='src/_pytest/mark/evaluate.py'),stage='after_common_public_inspection')
    # Fully cross two REAL memories with two public implementation families.
    # Both memory and state change the answer, including supported/contradicted flips.
    family_grids = [
        dict(sources=[('scikit-learn__scikit-learn-10508','LabelEncoder'),
                      ('scikit-learn__scikit-learn-11281','BaseMixture')],
             states=[('scikit-learn__scikit-learn-12471','sklearn/preprocessing/label.py','LabelEncoder',False),
                     ('scikit-learn__scikit-learn-13142','sklearn/mixture/gaussian_mixture.py','GaussianMixture',True)]),
        dict(sources=[('django__django-14667','Query'),('django__django-11019','Media')],
             states=[('django__django-16041','django/db/models/sql/query.py','Query',False),
                     ('django__django-16041','django/forms/widgets.py','Media',False)]),
    ]
    for grid in family_grids:
        for iid,path,name,extra in grid['states']:
            context=load_context(iid,extra=extra);node=definition(context,path,name)
            assert isinstance(node,ast.ClassDef)
            names={name}|{ast.unparse(base) for base in node.bases}
            current='Selected current implementation class and its direct bases:\n' + '\n'.join(
                file_data(context,path)['text'].splitlines()[node.lineno-1:node.lineno+2])
            for source,family in grid['sources']:
                assert family in candidate(source)
                add(iid+':'+name+':family:'+source,source,iid,'memory_target_family',
                    'Is the inspected class in the implementation family whose methods the historical tentative edit targeted?',
                    current,0 if family in names else 1,
                    'Bounded implementation-family membership: no claim that the old edit solves the current task or that cross-family principles are useless.',
                    [ref(HERE/'evidence'/('public-extra-code' if extra else 'public-code')/(iid+'.json'))],
                    'crossed_real_source_public_class',dict(file=path,class_name=name,source_family=family,extra=extra),
                    stage='after_common_public_inspection')
    # Same state+same question, different same-repository memory: state-only cannot resolve both.
    controls = [
        ('scikit-learn__scikit-learn-13142:GaussianMixture:inheritance','scikit-learn__scikit-learn-10508','BaseMixture'),
        ('sphinx-doc__sphinx-8627:stringify_signature:keyword','sphinx-doc__sphinx-8435','show_annotation'),
        ('django__django-16910:query-helper','django__django-11019','deferred_to_data'),
        ('sympy-dimension-ratio:dimension_normalization_guard','sympy__sympy-18621','is_dimensionless'),
        ('label-encoder-fitted_empty:validation_before_shortcut','scikit-learn__scikit-learn-11281','zero-length'),
    ]
    for parent_id, other_source, absent_anchor in controls:
        parent = next(r for r in rows if r['sample_id'] == parent_id)
        text = candidate(other_source); assert absent_anchor not in text
        row = copy.deepcopy(parent)
        row.update(sample_id=parent_id + ':different_memory', source_instance_id=other_source,
            source_run=memories[other_source]['source_run'], memory_id=memories[other_source]['memory_id'],
            memory_origin='original_card', label=2, label_name=LABELS[2], stratum='same_state_counter_memory')
        row['input']['candidate_text'] = text
        row['provenance']['source'] = [memories[other_source]['source_evidence'],memories[other_source]['original_card_file']]
        row['annotation'] = dict(rationale='The alternate same-repository memory does not specify the premise asked by this question. '
            'Do not borrow the removed candidate premise from the original case.', evidence_type='researcher_checked_missing_memory_premise',
            label_basis=dict(parent_case=parent_id, absent_required_anchor=absent_anchor),
            current_question=parent['annotation']['current_question'], utility_label=None,
            meaning='Missing source premise is not evidence that the memory is generally useless.')
        row['temporal_forward'] = memories[other_source]['available_at'] < row['target_public_issue_created_at']
        row['source_available_at'] = memories[other_source]['available_at']
        assert row['temporal_forward']
        rows.append(row)
    # Explicit missing-observation controls stay separately identified; not extra independent experience.
    parents = []
    for facet in ['initialization_prerequisites','validation_before_shortcut','numeric_guard_scope',
                  'lookup_resolution','config_guard','dimension_normalization_guard','container_representation']:
        parents.append(next(r for r in rows if r['facet'] == facet and r['label'] == 0))
    for parent in parents:
        row = copy.deepcopy(parent); row['sample_id'] += ':current_unobserved'
        row['label'] = 2; row['label_name'] = LABELS[2]; row['stratum'] = 'withheld_current_evidence_control'
        question = parent['annotation']['current_question']
        row['input']['state_text'] = ('Relationship question:\n' + question
            + '\n\nAt this decision point,the current fixture,configuration and relevant inspection are not observed. '
              'Only the historical experience is available; do not assume its input or environment repeats.')
        row['annotation'] = dict(rationale='The decisive CURRENT observations were withheld in every duplicate input location.',
            evidence_type='controlled_information_withholding', label_basis=dict(parent_case=parent['sample_id']),
            current_question=question, utility_label=None,
            meaning='Information-availability control,not a new environment or an observed memory harm.')
        rows.append(row)
    assert len({r['sample_id'] for r in rows}) == len(rows)
    assert all(r['annotation']['utility_label'] is None for r in rows)
    dump_lines(HERE / 'relations.jsonl', rows)
    dump_lines(HERE / 'learner-inputs.jsonl', [dict(sample_id=r['sample_id'],input=r['input']) for r in rows])
    dump_lines(HERE / 'supervision.jsonl', [dict(sample_id=r['sample_id'],label=r['label'],label_name=r['label_name'],
        source_instance_id=r['source_instance_id'],target_instance_id=r['target_instance_id'],facet=r['facet'],stratum=r['stratum']) for r in rows])
    table = [dict(sample_id=r['sample_id'],source=r['source_instance_id'],target=r['target_instance_id'],facet=r['facet'],
        stratum=r['stratum'],label=r['label_name'],question=r['annotation']['current_question'],
        rationale=r['annotation']['rationale'],decision_stage=r['decision_stage']) for r in rows]
    with (HERE / 'case-table.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(table[0]));writer.writeheader();writer.writerows(table)

    # These actual source→target candidates have no invented applicability or utility labels.
    transfers=[]
    for memory in originals:
        for iid in memory['eligible_target_ids']:
            if iid not in contexts:continue
            public=contexts[iid]['public_task'];forward=memory['available_at']<public['created_at']
            assert forward,(memory['memory_id'],iid)
            transfers.append(dict(candidate_id=memory['memory_id']+'->'+iid,source_instance_id=memory['source_instance_id'],
                target_instance_id=iid,memory_id=memory['memory_id'],kind=memory['kind'],
                input=dict(state_text='Current public task:\n'+public['problem_statement'],candidate_text=memory['text']),
                current_applicability_label=None,utility_label=None,temporal_forward=True,
                review_status='candidate_requires_case_specific_scope_review',
                provenance=[memory['source_evidence'],memory['original_card_file'],ref(HERE/'evidence/public-code'/(iid+'.json'))],
                warning='Facts/procedure cards differ in information content;these are not matched-wording treatments.'))
    dump_lines(HERE/'transfer-candidates.jsonl',transfers)
    source_audits=[]
    for source,memory in memories.items():
        for facet,label in [('local_focused_check',int(memory['focused_observation']['returncode']!=0)),
                            ('official_source_resolution',int(not memory['actual_source_official_resolved']))]:
            source_audits.append(dict(sample_id=source+':'+facet,source_instance_id=source,facet=facet,
                input=dict(state_text=('Question: Did the recorded focused source command complete with exit0?'
                    if facet=='local_focused_check' else 'Question: Was the final SOURCE submission officially resolved?'),
                    candidate_text=candidate(source)),label=label,label_name=LABELS[label],
                stratum='source_provenance_diagnostic_only',utility_label=None,
                note='Source evidence reading,not current applicability or a new independent transfer case.'))
    dump_lines(HERE/'source-provenance-audits.jsonl',source_audits)
    stats=dict(at=now(),model_api_calls=0,neural_training_runs=0,relation_rows=len(rows),
        known_relation_rows=sum(r['stratum']=='known_current_evidence' for r in rows),
        counter_memory_controls=sum(r['stratum']=='same_state_counter_memory' for r in rows),
        missing_evidence_views=sum(r['stratum']=='withheld_current_evidence_control' for r in rows),
        source_experiences=len({r['source_instance_id'] for r in rows}),
        repositories=sorted({memories[r['source_instance_id']]['repo'] for r in rows}),
        context_tasks=len({r['target_instance_id'] for r in rows}),
        label_counts=dict(Counter(r['label_name'] for r in rows)),
        by_facet={facet:dict(Counter(r['label_name'] for r in rows if r['facet']==facet)) for facet in sorted({r['facet'] for r in rows})},
        transfer_candidates=len(transfers),unique_source_target_pairs=len({(r['source_instance_id'],r['target_instance_id']) for r in transfers}),
        source_provenance_diagnostics=len(source_audits),actual_memory_utility_labels=0,
        interpretation='Discovery corpus. Controlled fixtures,public-code relations,counter-memory and missing-evidence views are distinct strata. '
            'Rows do not equal independent experiences; all source families were already present in the project. '
            'No new actor run or gain/harm label is produced.')
    write_json(HERE/'coverage.json',stats)
    write_json(HERE/'task-contract.json',dict(schema_version=1,task='bounded_memory_context_relation_v1',
        query_instruction=INSTRUCTION,label_order=LABELS,predictor_allowlist=['input.state_text','input.candidate_text'],
        state_side='Current evidence and a generic relational question only.',
        candidate_side='Historical memory and source evidence. Neither is duplicated on the state side.',
        unknown_semantics='The source premise or current evidence needed for the stated question is not established. Never an automatic withhold/harm label.',
        decision_stages=['before_controlled_fixture','after_controlled_probe','after_common_public_inspection','after_passive_runtime_inspection'],
        grouping='Keep source-target connected families and all variants/views together; repo holdout is an additional diagnostic.',
        training_status='Discovery functional supervision. Read semantic review notes and run integrity validation; not a new independent policy test.',
        required_evaluations=['By facet and stratum','Same memory/question across changed current states','Same state/question across changed memory',
            'State-only,memory-only,structural/exec-rule baselines','Separate actual policy success/cost on fresh tasks']))
    print(json.dumps({k:v for k,v in stats.items() if k not in ['by_facet','interpretation']},ensure_ascii=False))


if __name__=='__main__':
    build()
