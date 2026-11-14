"""Post-hoc check of every explicit setting in configured-state fixtures.

Keeps the frozen primary grader and scores intact. Replays the 20 configured
state/arm programs and adds snapshots of the six settings supplied by prelude.
"""
import argparse
import json
from pathlib import Path

from data_pipeline import evaluate_memory_unit_pilot as grading


def audit(data,predictions,judgments,output):
    if output.exists():raise FileExistsError(output)
    states=grading.states_at(data)
    generated={r['job_id']:r for r in map(json.loads,predictions.read_text().splitlines())}
    original_rows=list(map(json.loads,judgments.read_text().splitlines()))
    original_driver=grading.driver
    def with_snapshots(state,code):
        text=original_driver(state,code)
        before="    _result['before']=_before\n"
        assert text.count(before)==1
        text=text.replace(before,before+"    _audit_keys=['SECRET_KEY','INSTALLED_APPS','USE_I18N','DEBUG','MEMORY_PILOT_MARKER','TEMPLATES']\n    _audit_before=_json.dumps({k:getattr(_settings,k) for k in _audit_keys},sort_keys=True)\n")
        end="_result['startup_probe_seconds']=_time.perf_counter()-_start\n"
        assert text.count(end)==1
        text=text.replace(end,end+"try:\n    _audit_after=_json.dumps({k:getattr(_settings,k) for k in _audit_keys},sort_keys=True)\n    _old=_json.loads(_audit_before)\n    _new=_json.loads(_audit_after)\n    _result['explicit_settings_changed']=[k for k in _audit_keys if _old[k]!=_new[k]]\nexcept BaseException as _audit_error:\n    _result['settings_audit_error']=str(_audit_error)\n")
        return text
    rows=[];grading.driver=with_snapshots
    try:
        for row in original_rows:
            state=states[row['state_id']]
            if row['repeat_of'] is not None or not state['preserve_configured_settings']:continue
            result=grading.run_one(state,generated[row['job_id']]['text'])
            if result['passed']!=row['result']['passed']:raise ValueError('Primary result changed during audit: '+row['job_id'])
            rows.append({'job_id':row['job_id'],'primary_passed':result['passed'],
                'explicit_settings_changed':result.get('explicit_settings_changed'),
                'settings_audit_error':result.get('settings_audit_error'),'result':result})
    finally:grading.driver=original_driver
    result={'status':'passed','kind':'posthoc_additional_setting_audit_not_new_primary_grader',
        'programs':len(rows),'settings_checked':['SECRET_KEY','INSTALLED_APPS','USE_I18N','DEBUG','MEMORY_PILOT_MARKER','TEMPLATES'],
        'primary_results_unchanged':True,
        'previously_passed_with_setting_changes':[r['job_id'] for r in rows if r['primary_passed'] and r['explicit_settings_changed']],
        'all_setting_changes':[{'job_id':r['job_id'],'keys':r['explicit_settings_changed']} for r in rows if r['explicit_settings_changed']],
        'audit_errors':[r['job_id'] for r in rows if r['settings_audit_error']],
        'original_grader_sha256':grading.sha(Path(grading.__file__)),
        'audit_code_sha256':grading.sha(Path(__file__)),'rows':rows}
    grading.write(output,result)
    return {k:v for k,v in result.items() if k!='rows'}


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for name in ['data','predictions','judgments','output']:p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();print(json.dumps(audit(a.data,a.predictions,a.judgments,a.output),ensure_ascii=False))
