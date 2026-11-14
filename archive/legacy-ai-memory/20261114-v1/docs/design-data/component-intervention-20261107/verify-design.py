"""Validate design examples and preserved evidence; no network or model calls."""
import ast
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
NOTE = ROOT / 'docs/ai-memory-intervention-study-plan-2026-11-07.md'

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def read(name):
    return json.loads((OUT / name).read_text())

def write(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')

cohorts=['framing-smoke-20261105','new-content-transfer-20261105',
    'memory-content-analysis-20261106','component-feasibility-20261105',
    'prescription-validation-20261107','prescription-replication-20261107','literature-revisit-20261107']
preservation=[]
for cohort in cohorts:
    index=ROOT/'docs/reports/data'/cohort/'artifact-index.json'
    files=json.loads(index.read_text())['files']
    mismatches=[r['path'] for r in files if not (ROOT/r['path']).is_file()
                or sha(ROOT/r['path'])!=r['sha256']]
    assert not mismatches,(cohort,mismatches)
    preservation.append(dict(index=str(index.relative_to(ROOT)),entries=len(files),mismatches=[]))
assert sum(r['entries'] for r in preservation)==1715
expected={'runs.json':'0271d6f77e6231593c764a9aa8c9c10c969601adc3b8ad4dbe71f26f52b0214e',
          'runs.csv':'aecab9b24dfcabb72eeb31ab4e5e2a358d24007c6c66b3b3dca68e87cae2a935'}
for name,digest in expected.items():
    assert sha(ROOT/'docs/reports/data'/name)==digest
assert len(json.loads((ROOT/'docs/reports/data/runs.json').read_text()))==278

data=read('ablation-example.json');units={u['id']:u for u in data['units']};order=data['original_unit_order']
assert data['status']=='design_only_not_in_execution_bank'
assert data['created_after_existing_target_outcomes'] is True
assert data['model_calls']==0
assert len(order)==4 and len(data['variants'])==9
for name,variant in data['variants'].items():
    text='\n'.join(units[u]['text'] for u in variant['unit_ids'])
    assert text==variant['text']
    assert hashlib.sha256(text.encode()).hexdigest()==variant['text_sha256']
    assert len(text)==variant['characters']
    if name.startswith('M_minus_source_'):
        removed=name.removeprefix('M_minus_')
        assert variant['unit_ids']==[u for u in order if u!=removed]
    if name.startswith('restored_'):
        assert variant==data['variants']['M']
assert data['variants']['M_minus_O']==data['variants']['S']
cases=json.loads((ROOT/units['S']['evidence_path']).read_text())['cases']
card=next(iter(cases.values()))['source_card']
assert units['S']['text']==card['text']
assert units['S']['original_text_sha256']==card['text_sha256']
assert sha(ROOT/units['S']['evidence_path'])==units['S']['evidence_sha256']
process=json.loads((ROOT/'docs/reports/data/literature-revisit-20261107/source-process-evidence.json').read_text())
source_units={u['unit_id']:u for u in process['units']}
for uid in order[1:]:
    assert units[uid]['evidence_sha256']==source_units[uid]['evidence_sha256']
    assert units[uid]['source_agent_command_index']==source_units[uid]['source_agent_command_index']
assert [units[u]['information_group'] for u in order[1:]]==[
    'E_environment_observation','E_environment_observation','V_verification_procedure']

numeric=read('numeric-design-examples.json')
assert numeric['synthetic'] is True and numeric['empirical_observations'] is False
effects=numeric['effects']
for key,expected_value in dict(net_memory=.20,concrete_given_operational=-.10,
    operational_given_concrete=.25,interaction=-.05).items():
    assert math.isclose(effects[key],expected_value,abs_tol=1e-12)
for r in numeric['sensitivity']['rows']:
    assert r['total_runs']==r['cases']*r['repeats_per_arm']*r['arms']
    assert math.isclose(r['hoeffding_halfwidth'],math.sqrt(math.log(40)/(r['cases']*r['repeats_per_arm'])))

cross=read('tool-error-outcome-crosscheck.json')
for inp in cross['inputs']:
    assert sha(ROOT/inp['path'])==inp['sha256']
counts={(r['delivered_success'],r['observed_tool_error']):r['runs'] for r in cross['counts']}
assert counts=={(True,True):14,(True,False):1,(False,True):31,(False,False):2}
assert len(cross['rows'])==48 and len({r['run_id'] for r in cross['rows']})==48
for key,count in counts.items():
    assert sum((r['delivered_success'],r['observed_tool_error'])==key for r in cross['rows'])==count

plan=read('proposed-study.json')
assert plan['status']=='proposal_not_frozen_not_executed'
assert plan['primary_contrast']=='p(M)-p(S)'
assert plan['calibration']['final_limits'] is None
assert len(read('paper-experiment-structures.json')['papers'])==5
for path in OUT.glob('*.py'):
    ast.parse(path.read_text())
for path in OUT.glob('*.json'):
    json.loads(path.read_text())

links=[]
for path in [NOTE,OUT/'README.md']:
    for link in re.findall(r'\]\(([^)]+)\)',path.read_text()):
        if re.match(r'^[a-zA-Z]+:',link):continue
        exists=(path.parent/link.split('#')[0]).exists()
        assert exists,(str(path),link)
        links.append(dict(document=str(path.relative_to(ROOT)),target=link,exists=True))
write('validation.json',dict(at=datetime.now(timezone.utc).isoformat(),model_calls=0,
    existing_completed_indices=preservation,existing_index_entries_verified=1715,
    global_records=278,global_export_sha256=expected,
    exact_ablation_restoration=dict(variants=9,process_units=3,original_summary_preserved=True),
    synthetic_calculations_verified=True,tool_error_outcome_counts=cross['counts'],
    local_links=links,python_syntax='ok',
    limitations=['Design-only data; no new experimental effect has been observed.',
        'The external review raw-file and test totals were not rechecked here.',
        'The older workspace-review live-file index is not asserted unchanged.']))
paths=[NOTE]+sorted(p for p in OUT.rglob('*') if p.is_file() and p.name!='artifact-index.json'
                    and '__pycache__' not in p.parts)
write('artifact-index.json',dict(created_at=datetime.now(timezone.utc).isoformat(),model_calls=0,
    files=[dict(path=str(p.relative_to(ROOT)),sha256=sha(p),bytes=p.stat().st_size) for p in paths],
    interpretation='Intervention design note, source-grounded draft examples and explicitly synthetic calculations; not executed experimental results.'))
print(json.dumps(dict(prior_index_entries=1715,global_records=278,variants=9,
    successful_error_trials=14,successful_trials=15,indexed_files=len(paths),new_model_calls=0)))
