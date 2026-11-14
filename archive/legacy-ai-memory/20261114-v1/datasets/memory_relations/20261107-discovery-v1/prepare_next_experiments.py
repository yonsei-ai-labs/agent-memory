"""Save concrete, unexecuted interventions grounded in exact source spans."""
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[3]
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'experiments/swe_memory/scripts'))
from records import now,write_json
from build_sources_and_effects import ref


def main():
    assert not (HERE/'artifact-index.json').exists(),'Frozen dataset; make a new version.'
    bank=json.loads((HERE/'source-bank.json').read_text())['memory_cards']
    cards={r['source_instance_id']:r for r in bank if r['kind']=='procedure'}
    span_specs=[
        ('validation_order','scikit-learn__scikit-learn-10508',
         'after fitted-state and column_or_1d validation',
         'Compare fitted-empty,unfitted-empty and invalid-shape contexts.',
         'The remaining card still mentions the tentative check and validation limits. This removes one explicit ordering phrase,not every possible inference.'),
        ('configuration_scope','sphinx-doc__sphinx-8282',
         "when the setting is 'none' or 'description'",
         'Compare none/description with signature contexts.',
         'The source problem still names none. Removing this phrase does not erase all configuration evidence.'),
        ('uncertainty_warning','pvlib__pvlib-python-1154',
         'Do not assume this handles NaN GHI or other invalid values; those cases were unverified.',
         'Compare exact-zero with NaN inputs; score output contract and verification behavior separately.',
         'Exactly-zero scope remains elsewhere. A null effect would not show that applicability conditions are generally unnecessary.'),
    ]
    components=[]
    for name,source,span,comparison,caveat in span_specs:
        card=cards[source];text=card['text'];assert text.count(span)==1
        removed=text.replace(span,'',1)
        components.append(dict(component=name,source_instance_id=source,memory_id=card['memory_id'],
            selected_verbatim_span=span,full_memory=text,memory_minus_selected_span=removed,
            full_sha256=hashlib.sha256(text.encode()).hexdigest(),minus_sha256=hashlib.sha256(removed.encode()).hexdigest(),
            comparison_contexts=comparison,provenance=card['original_card_file'],
            status='proposed_not_executed',utility_label=None,
            caveat=caveat,
            requirement='Freeze the complete two-string predictor and actor prompts; check whether the removed information survives in source evidence or other prompt sections.'))
    source='django__django-11019'
    episode_path=HERE/'evidence/source-episodes'/(source+'.json');episode=json.loads(episode_path.read_text())
    command=next(c['event']['action']['command'] for c in episode['commands'] if c['agent_command_index']==17)
    setup=command[:command.index('from django import forms')]
    restoration=dict(component='source_setup_information',source_instance_id=source,
        baseline_summary=cards[source]['text'],verbatim_setup_excerpt=setup,source_command_index=17,
        provenance=ref(episode_path),status='proposed_not_executed',utility_label=None,
        excerpt_is_complete_shell_command=False,
        caveat='This is real source process information absent from the card. Configuration paths are version-scoped and must be checked in the target. '
            'The current fixtures verify setup prerequisites but no memory exposure effect.')
    candidates=[]
    for iid,reason in [
        ('sympy__sympy-19007','BlockMatrix element indexing versus historical outer-grid construction.'),
        ('sympy__sympy-22840','Symbolic indexing/CSE versus historical concrete block construction.'),
        ('sphinx-doc__sphinx-8595','Empty __all__ semantics; a potential contrast between general boundary reasoning and unrelated annotation instructions.'),
        ('sphinx-doc__sphinx-8721','Builder configuration scope; distinguish a reusable configuration principle from a specific annotation recipe.'),
        ('django__django-15213','Old Query helper positive availability; current ExpressionWrapper issue is a different mechanism.')]:
        snapshot=HERE/'evidence/public-code'/(iid+'.json')
        candidates.append(dict(target_instance_id=iid,reason=reason,public_context=ref(snapshot),
            new_agent_trial=False,current_gold_test_read_by_this_expansion=False,
            official_original_and_gold_controls='not_certified_by_this_expansion',
            runtime_precondition=('prepare_a_matching_image_first' if iid=='django__django-15213' else 'verify_actor_environment_and_official_controls_before_calls'),
            no_assumption='No prior success,expected memory gain,or independent-source status is assigned.'))
    write_json(HERE/'next-experiments.json',dict(at=now(),status='proposal_only_no_paid_actor_run',
        main_question='Which source-grounded component changes errors,cost and submitted success under specified current conditions?',
        stages=[
            dict(stage=1,name='Independent semantic review and new-source collection',
                requirement='Review bounded labels without current actor outcomes; reserve new source families before selecting on observed memory gains.'),
            dict(stage=2,name='Small controlled memory-exposure experiment',
                arms=['no_memory','fixed_summary','fixed_summary_plus_source_setup'],
                primary_contrast='fixed_summary_plus_source_setup minus fixed_summary',
                followup_arm='source_setup_only if useful to distinguish environment guidance from combination benefit',
                outcomes=['environment_error_count','inspection_commands_and_seconds','total_api_and_selector_cost','intermediate_patch_quality','final_submitted_success'],
                interpretation='Controlled tasks establish local exposure mechanisms;they are not new SWE-bench solve results.'),
            dict(stage=3,name='Fresh source-target benchmark confirmation',
                controls=['public_base_and_actor_image_match','original_fails_and_gold_passes','complete_expected_test_parsing'],
                assignment='Randomize provide/withhold or full/minus-component within source-target blocks;fix contents,decision state,reader,budget and final submission reminder.',
                analysis='Report block-level success/cost differences and uncertainty;keep all assignments including normal non-submission. External interruption stays unknown.'),
            dict(stage=4,name='Frozen learned policy evaluation',
                baselines=['always_provide','always_withhold','explicit_precondition_rules','similarity_or_reranker','learned_policy'],
                costs='Include shared inspection,selection,inference,tool and reader costs.',
                prohibited_inference='A contradicted relation is not automatically harmful or a withhold label;it may be useful as a warning.')],
        exact_span_interventions=components,missing_process_restoration=restoration,
        context_only_candidate_tasks=candidates,
        wording_followup='After content effects are interpretable,hold all function names,fix information and validation facts constant while varying descriptive versus directive wording. '
            'The existing facts/procedure cards are not that control.',
        stopping_and_claim_policy='Predeclare outcomes,repeats,resource limits and uncertainty before confirmation. '
            'If only errors/cost change,claim that effect; if success intervals overlap meaningful benefit and harm,leave success unresolved. '
            'Do not generate individual helpful/harmful labels from one pair of stochastic outcomes.'))


if __name__=='__main__':
    main()
