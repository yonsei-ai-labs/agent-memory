"""Create a review-only decomposition example. No model or network calls."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
source=ROOT/'docs/reports/data/memory-content-analysis-20261106/content-units.json'
destination=ROOT/'docs/design-data/content-validation-example-20261107.json'
assert not destination.exists(), 'Do not overwrite an existing design example.'
card=json.loads(source.read_text())['cards']['sklearn10508-procedure']
units=card['units'];original=card['source_card']['text']
assert ''.join(u['verbatim'] for u in units)==original
assert hashlib.sha256(original.encode()).hexdigest()==card['source_card']['text_sha256']
assert units[1]['role']=='specific_fix_and_validation_order'
prescription=units[1]['verbatim']
observations=''.join(u['verbatim'] for i,u in enumerate(units) if i!=1)
cue=("Applicability check: before transferring the source's empty-input return, "
     "establish that the current failure involves zero-length input in a compatible "
     "LabelEncoder.transform path and that fitted-state and shape validation are "
     "preserved. If the failure is on non-empty inputs or in another API, do not "
     "treat that empty-input return as a fix for it.")
cue_block='\n\n'+cue
header=("<past_experience id=\"sklearn10508-procedure\">\n"
        "Verified source metadata: scikit-learn__scikit-learn-10508; official_resolved=false.\n"
        "This is a record of a past source attempt. Local checks and official outcomes "
        "concern that attempt. Verify applicability against the current code.\n\n")
render=lambda body: header+body+'\n</past_experience>'
texts={'N':'','F':render(observations),'FP':render(original),'FPA':render(original+cue_block)}
assert texts['FP'].count(prescription)==1
assert texts['FP'].replace(prescription,'',1)==texts['F']
assert texts['FPA'].replace(cue_block,'',1)==texts['FP']
assert all('official_resolved=false' in texts[k] for k in ('F','FP','FPA'))
data=dict(
    created_at=datetime.now(timezone.utc).isoformat(),status='draft_not_executed',
    purpose='Review-only example using an already analyzed source, not a runnable memory bank or confirmation experiment.',
    model_calls=0,experiment_manifest_created=False,
    source_annotations=str(source.relative_to(ROOT)),
    source_annotations_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    source_instance_id=card['source_card']['source_instance_id'],
    original_source_body_sha256=card['source_card']['text_sha256'],
    common_base_instruction='Investigate the current problem and verify proposed changes against current code and tests.',
    shared_memory_metadata='Same source card ID, source official outcome, historical validation notice and delivery position in all memory variants; no arm label or content hash rendered.',
    component_F=dict(unit_ids=[u['unit_id'] for i,u in enumerate(units) if i!=1],text=observations,
        interpretation='Source observations, local validation, limitations and official outcome. Source attempt implementation detail P is omitted; this is not a current-task validation result.'),
    component_P=dict(unit_id=units[1]['unit_id'],text=prescription,
        interpretation='Verbatim description of a specific source attempt, not a universally verified fix or a pure imperative-only intervention.'),
    component_A=dict(text=cue,provenance='Researcher-authored scope instruction derived from source constraints; not source-observed prose or target-solution information.'),
    variants={k:dict(rendered_experience=v,characters=len(v),sha256=hashlib.sha256(v.encode()).hexdigest()) for k,v in texts.items()},
    validation=dict(original_body_preserved_in_FP=True,only_P_differs_between_F_and_FP=True,
        only_A_differs_between_FP_and_FPA=True,source_outcome_constant_across_memory_variants=True),
    estimands=dict(primary='p(FP)-p(F): incremental source-prescription package effect conditional on F.',
        secondary=['p(F)-p(N): whole F provision package, including source metadata and notice.',
                   'p(FPA)-p(FP): added concrete applicability cue effect conditional on prescription.'],
        not_identified='Prescription-by-cue interaction requires the additional F+A condition.'),
    limitations=['F, P and the cue are visible design materials, not new experimental outcomes.',
        'P addition changes information specificity and length; this is not a pure instruction-tone effect.',
        'The source officially failed; the omitted diagnosis of inverse_transform failure is not retroactively added.',
        'The current previously studied targets do not establish a compatible positive-transfer case.',
        'New source/target selection, sample size, model settings, budgets, order, outcomes and stopping rules remain to be frozen before any paid run.'],
)
destination.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
print('Wrote review-only example; assertions passed; model/network calls=0.')
print({k:v['characters'] for k,v in data['variants'].items()})
