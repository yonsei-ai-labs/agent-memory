"""Build source-grounded design examples and synthetic calculations; no model calls."""
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'docs/design-data/component-intervention-20261107'
OUT.mkdir(parents=True, exist_ok=True)

def sha(data):
    return hashlib.sha256(data).hexdigest()

def write(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

source_path = ROOT / 'docs/reports/data/prescription-validation-20261107/cases.json'
cases = json.loads(source_path.read_text())['cases']
card = next(iter(cases.values()))['source_card']
assert sha(card['text'].encode()) == card['text_sha256']
process_path = ROOT / 'docs/reports/data/literature-revisit-20261107/source-process-evidence.json'
process = json.loads(process_path.read_text())
draft_path = ROOT / 'docs/reports/data/literature-revisit-20261107/process-memory-prototype.json'
process_draft = json.loads(draft_path.read_text())

units = [dict(id='S', text=card['text'], kind='unchanged_existing_source_summary',
              evidence_path=str(source_path.relative_to(ROOT)), evidence_sha256=sha(source_path.read_bytes()),
              original_text_sha256=card['text_sha256'])]
for evidence, draft in zip(process['units'], process_draft['units']):
    assert evidence['unit_id'] == draft['unit_id']
    units.append(dict(id=evidence['unit_id'], kind='source_observation',
        information_group=('V_verification_procedure' if evidence['unit_id']=='source_native_test_runner' else 'E_environment_observation'),
        text=draft['observed_fact'] + ' This is a recorded source observation; current target applicability is unverified.',
        evidence_path=str(process_path.relative_to(ROOT)), evidence_sha256=evidence['evidence_sha256'],
        source_agent_command_index=evidence['source_agent_command_index']))
order = [unit['id'] for unit in units]
by_id = {unit['id']:unit for unit in units}

def render(ids):
    return '\n'.join(by_id[name]['text'] for name in ids)

specs = {'S': ['S'], 'M': order.copy(), 'M_minus_O': ['S']}
for unit in units[1:]:
    specs['M_minus_' + unit['id']] = [name for name in order if name != unit['id']]
    specs['restored_' + unit['id']] = order.copy()
variants = {name:dict(unit_ids=ids, text=render(ids), text_sha256=sha(render(ids).encode()),
                     characters=len(render(ids))) for name,ids in specs.items()}
assert variants['M_minus_O']['text'] == variants['S']['text'] == card['text']
write('ablation-example.json', dict(status='design_only_not_in_execution_bank', model_calls=0,
    source_run=process['source_run'], source_task_id=process['source_task']['instance_id'],
    created_after_existing_target_outcomes=True,
    allowed_use='Construction review and calibration only; not independent confirmation.',
    target_gold_used_to_write_claims=False, units=units, original_unit_order=order, variants=variants,
    caveats=['M minus O exactly equals S; the added operational bundle includes information, length and structure.',
        'Restored variants are byte-identical to M and are construction checks, not distinct treatments.',
        'Current target tool state is not inferred from the source snapshot.',
        'R must cover source evidence for S and O; no R selection is finalized here.',
        'No complete API prompt, execution bank, allocation or model config is changed.']))

p = dict(N=.40, O=.70, C=.35, M=.60)
effects = dict(net_memory=p['M']-p['N'], concrete_given_operational=p['M']-p['O'],
               operational_given_concrete=p['M']-p['C'], interaction=p['M']-p['O']-p['C']+p['N'])
alpha = .05
sensitivity = [dict(cases=j,repeats_per_arm=2,arms=4,total_runs=j*2*4,
    hoeffding_halfwidth=math.sqrt(math.log(2/alpha)/(j*2))) for j in [8,32,64]]
write('numeric-design-examples.json', dict(synthetic=True, empirical_observations=False, model_calls=0,
    interpretation='Illustrative probabilities and analytic uncertainty bounds; not new experiment results or a power guarantee.',
    probabilities=p, effects=effects,
    sensitivity=dict(alpha=alpha, formula='sqrt(log(2/alpha)/(J*R))', rows=sensitivity,
        assumptions=['Fixed preselected case panel', 'Equal case weights',
            'Independent Bernoulli trials within each treatment/case', 'One prespecified contrast',
            'No claim of generalization to new source/task populations']),
    cases_not_independent_warning='Shared baselines, crossed memories, source clusters and drift require their own covariance/uncertainty treatment.'))

write('proposed-study.json', dict(status='proposal_not_frozen_not_executed', model_calls=0,
    main_note='docs/ai-memory-intervention-study-plan-2026-11-07.md',
    external_review=dict(status='User-provided review, not independently rerun in this design task',
        reported_raw_hash_checks=18390,reported_local_tests=60,
        accepted_issues=['Delivery versus patch attainment', 'Information versus phrasing',
            'Concrete-recipe versus principle applicability', 'Source/task diversity and prompt position']),
    primary_hypothesis='Adding source-grounded operational evidence to a fixed summary changes delivered official success.',
    arms=dict(N='No external source episode',R='Frozen-rule source excerpts covering S and O',
              S='Fixed source solution summary',M='Exactly S plus operational evidence O'),
    primary_contrast='p(M)-p(S)', secondary_contrasts=['p(M)-p(N)','p(R)-p(N)','p(S)-p(R)'],
    information_groups=dict(E=['source_rg_unavailable','source_pytest_unavailable'],
                            V=['source_native_test_runner']),
    following_feedback=dict(
        primary_success_separate_from_secondary_error_cost=True,
        successful_trials_with_observed_tool_error=14,successful_trials=15,
        failed_trials_with_observed_tool_error=31,failed_trials=33,
        interpretation='The observational count identifies a candidate, not a causal failure mechanism.'),
    followup_controls=[dict(arms=['N','O','S','S+O'],
        question='Does operational information help alone or in combination with S?',
        interaction='p(S+O)-p(S)-p(O)+p(N)'),
        dict(arms=['S','S+E','S+V','S+E+V'],
            question='Distinguish environment observations from source verification procedures.'),
        dict(arms=['N','M','A_env','A_env+M'],
            question='Does past experience add value when current public environment facts are already supplied?',
            contrast='p(A_env+M)-p(A_env)',
            limits=['Same text with different researcher labels is not a distinct treatment.',
                'Count environment-audit generation costs and source-memory generation costs consistently.',
                'This is a follow-up competing baseline, not a prerequisite for the first pilot.'])],
    next_axes=[dict(name='Within-bundle deletion and correction',
        contrasts=['M versus M-u','M-u+u* versus M'], caveat='Fresh confirmation after selection; restoration to M is not a novel treatment.'),
        dict(name='Same propositions, observational versus directive',
            requirements=['Proposition inventory', 'Same identifiers, premises, tests, outcomes and limits',
                'Balanced multiple templates', 'N baseline']),
        dict(name='Concrete-recipe and principle applicability',
            labels=['direct_recipe','principle_transfer','operational_compatibility'],
            label_values=['yes','no','unknown'],caveat='Natural applicability strata are not randomized state interventions.'),
        dict(name='Action-conditioned verification',
            arms=['No extra guidance','Broad checking guidance','Action-premise checking guidance'],
            controls=['General review', 'No-memory checking'],
            caveat='Guidance, mandatory tools, fixed-candidate diagnosis and free replanning are distinct interventions.')],
    main_endpoint='Actual within-budget submission passing official evaluation, all assigned trials',
    diagnostics=['First observed passing checkpoint, post-run grading only',
        'Passing-checkpoint without successful delivery, all-assigned numerator',
        'Final-workspace regressions with evaluation coverage', 'All-assigned API/host costs',
        'Observed command errors and premise-check actions'],
    calibration=dict(use='Previously explored tasks only; not confirmation',
        requirements=['Common final budget/submission notice', 'Passive checkpoints plus final workspace snapshot',
            'No interim official feedback to actor', 'Budget and checkpoint grid fixed before new outcomes'],
        candidate_response_limits=[10,20],final_limits=None),
    scale_example=dict(source_episodes=4,targets_per_source=2,arms=4,repeats=2,total_target_runs=64,
        status='Pilot arithmetic only, not sufficient-power claim or finalized case list'),
    before_launch=['New source/target list and original/gold controls',
        'Target-blind source evidence and memory edits', 'Fixed ordering, hashes and actual input diff audit',
        'Randomized block allocation and case/source weighting',
        'Error, stop, cost and multiplicity rules', 'Practical effect/noninferiority margins and uncertainty method',
        'Independent confirmation split including source separation'],
    limitations=['Current 48 trials are hypothesis-generation data.',
        'No direct-recipe positive transfer case established in the old two targets.',
        'Task edits define auxiliary probes rather than standard SWE scores.',
        'API-level input effects do not identify neural attention mechanisms.']))

old_papers=json.loads((ROOT/'docs/reports/data/literature-revisit-20261107/papers.json').read_text())['papers']
keep={'memory_tax','faulty_memories','basm','memharness','action_verification'}
write('paper-experiment-structures.json',dict(as_of='2026-11-07',model_calls=0,
    papers=[{k:p[k] for k in ['key','title','arxiv_id','read_version','html_url','html_sha256',
                              'inspected_sections','interpretation_limit']} for p in old_papers if p['key'] in keep],
    scope='Selected intervention and diagnostic designs rechecked in primary full text; claims and proposed adaptations are distinguished in the note.'))

audit_path=ROOT/'docs/reports/data/literature-revisit-20261107/tool-availability-observations.json'
trial_path=ROOT/'docs/reports/data/prescription-replication-20261107/combined-results.json'
audits=json.loads(audit_path.read_text())['rows']
trials={r['run_id']:r for r in json.loads(trial_path.read_text())['rows']}
counts={};rows=[]
for row in audits:
    success=bool(trials[row['run_id']]['delivered_success'])
    error=bool(row['rg_unavailable'] or row['pytest_unavailable'])
    key=(success,error);counts[key]=counts.get(key,0)+1
    rows.append(dict(run_id=row['run_id'],instance_id=row['instance_id'],arm=row['arm'],
                     delivered_success=success,observed_tool_error=error))
assert counts=={(True,True):14,(True,False):1,(False,True):31,(False,False):2}
write('tool-error-outcome-crosscheck.json',dict(model_calls=0,post_hoc_descriptive=True,
    inputs=[dict(path=str(p.relative_to(ROOT)),sha256=sha(p.read_bytes())) for p in [audit_path,trial_path]],
    counts=[dict(delivered_success=k[0],observed_tool_error=k[1],runs=v) for k,v in counts.items()],
    rows=rows,interpretation='Successful trials also encounter tool errors; association is not a causal failure diagnosis or the effect of avoidance.'))
print(json.dumps(dict(variants=len(variants), grounded_process_units=3, synthetic=True, new_model_calls=0)))
