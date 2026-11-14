"""Recheck the eight public evidence candidates before promoting any to training.

No trajectory commands are executed. Raw call/response links, excerpt offsets,
and hashes are checked. Readiness judgments remain explicitly human/assistant
annotations, not observed memory utility or independent human review.
"""
from __future__ import annotations
import argparse
import csv
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PACKET=ROOT/'docs/design-data/memory-prerequisite-sources-20261108'
RAW_INITIAL=ROOT/'training/memory_selector/outputs/daytime-prerequisite-source-screen-20261108-v1/initial-review'
RAW_DIVERSITY=ROOT/'training/memory_selector/outputs/daytime-prerequisite-diversity-review-20261108-v1'

# Each entry was checked against the command and linked output, not just a tag.
NOTES={
 'pydantic-jsonschema':{
    'unit':'conftest의 외부 jsonschema import와 검사 의존성 설치',
    'observation':'jsonschema import 실패 뒤 설치하고 같은 pytest 명령이 내부 FieldValidatorModes import 오류까지 진행했다.',
    'prescription_message_indices':[12],
    'boundary':'앞선 editable 설치가 의존성을 바꾸었다. 진행은 전체 성공이 아니다. dev dependency group을 dev extra로 취급한 경고도 보존한다.',
    'missing':'실제 판단 전 환경 snapshot과 설치 단계별 대조, 별도 현재 과제 및 제공/보류 실행',
    'cheap_baseline':'현재 import 오류와 선언된 의존성 확인',
 },
 'pydantic-benchmark-plugin':{
    'unit':'pytest 옵션을 등록하는 plugin과 실패했던 대안',
    'observation':'PYTHONPATH 변경과 검사 범위 축소에도 benchmark 옵션 거부가 반복됐고 plugin 설치 뒤 다음 수집 오류로 진행했다.',
    'prescription_message_indices':[28],
    'boundary':'옵션 파싱의 복구와 대상 코드의 통과는 다르다. 이후 dirty_equals 오류를 지우지 않는다.',
    'missing':'plugin 유무·이미 충족·다른 옵션 원인을 가진 새로운 현재 상태와 paired worker 결과',
    'cheap_baseline':'pytest plugin 목록과 프로젝트 addopts 확인',
 },
 'pydantic-dirty-equals':{
    'unit':'검사 모듈 수집 의존성과 관측된 검사 범위',
    'observation':'dirty_equals 수집 실패 뒤 설치하고 같은 test_edge_cases 명령이 186 passed, 5 skipped였다.',
    'prescription_message_indices':[32],
    'boundary':'한 파일의 검사 성공이다. repository 전체 해결이나 기억 제공의 이득이 아니다.',
    'missing':'현재 대상의 독립 출처, 판단 시점 환경, 추가 설치 불필요/부적합 대조',
    'cheap_baseline':'오류에 명시된 외부 의존성 확인',
 },
 'pydantic-collection-dependencies':{
    'unit':'pytz와 pytest-examples를 함께 설치한 과정',
    'observation':'수집 오류 뒤 두 의존성을 함께 설치하자 6,247개 수집과 실행 시작이 관측됐다.',
    'prescription_message_indices':[38],
    'boundary':'두 설치의 효과를 분리할 수 없다. 잘린 후속 로그를 전체 성공으로 바꾸지 않는다.',
    'missing':'패키지별 상태 snapshot, 완전한 검사 출력, 별도 현재 과제 및 실제 효용 비교',
    'cheap_baseline':'수집 오류와 개발 의존성 목록 확인',
 },
 'pydantic-faker-fixture':{
    'unit':'fixture 내부의 faker import 실패와 특정 여덟 검사',
    'observation':'faker import로 여덟 fixture 오류가 있었고 설치 뒤 전체 검사 출력에 해당 여덟 항목의 통과가 보였다.',
    'prescription_message_indices':[56],
    'boundary':'이름 있는 항목의 통과만 말할 수 있다. 전체 실행의 최종 성공은 불명확하다.',
    'missing':'같은 범위의 완전한 실행과 새 현재 상태, 기억 제공/보류 결과',
    'cheap_baseline':'fixture traceback과 현재 의존성 확인',
 },
 'pipdeptree-editable-build':{
    'unit':'내부 version module의 부재와 editable 설치',
    'observation':'version import 실패 뒤 editable 설치를 하자 동일 스크립트가 metadata TypeError까지 진행했다.',
    'prescription_message_indices':[26],
    'boundary':'build가 어떤 파일을 바꿨는지 분리되지 않았다. 모든 내부 import 오류에 설치를 권할 근거가 아니다.',
    'missing':'해당 revision의 build 산출물 전후, 실패하던 정확한 파일 상태와 독립 현재 상태',
    'cheap_baseline':'build-generated module 여부와 현재 설치 메타데이터 확인',
 },
 'dask-test-path-and-system-executable':{
    'unit':'잘못된 검사 경로 교정과 남은 시스템 실행 파일 전제',
    'observation':'test_highlevelgraph.py가 없어서 test_highgraph.py를 실행했고 18 pass와 dot 부재에 의한 1 fail이 관측됐다.',
    'prescription_message_indices':[16],
    'boundary':'경로 교정은 기록돼 있지만 Graphviz 설치로 해결했다는 근거는 없다. 검사 범위의 충분성도 별도다.',
    'missing':'새 현재 트리의 경로/범위 관계와 실제 worker 비교; Graphviz 수리는 관측되지 않음',
    'cheap_baseline':'현재 파일 목록과 실행 파일 존재 확인',
 },
 'gtts-six-dependency':{
    'unit':'six import와 재현 스크립트의 실행 진행',
    'observation':'six import 실패 뒤 six/requests 설치 명령을 실행했고 같은 스크립트가 여러 분기를 출력했다.',
    'prescription_message_indices':[8],
    'boundary':'requests는 이미 설치돼 있었고 출력 문구만으로 실제 assertion 검증을 보장하지 못한다.',
    'missing':'재현 스크립트의 검사 계약과 독립 현재 상태, 제공/보류 효용 측정',
    'cheap_baseline':'의존성 확인과 검사 스크립트의 assertion 검토',
 },
}


def digest(payload):return hashlib.sha256(payload).hexdigest()
def content(message):
    value=message.get('content') or ''
    return value if isinstance(value,str) else '\n'.join(x.get('text','') for x in value)


def review(output):
    if output.exists():raise FileExistsError(output)
    initial=json.loads((PACKET/'initial-review.json').read_text())['evidence_cards']
    diverse=[r for r in json.loads((PACKET/'diversity-review.json').read_text())['rows'] if r['decision']=='keep_evidence']
    records=[];excerpts=0
    for card in initial+diverse:
        folder=RAW_INITIAL if card in initial else RAW_DIVERSITY
        path=folder/(card['candidate_id']+'.json');original=json.loads(path.read_text())
        assert digest(path.read_bytes())==card['source_record_sha256']
        calls={original['message_index']:(original['tool_call'],original['observation_index'],original['observation'])}
        calls.update({r['message_index']:(r['tool_call'],r['observation_index'],r['observation']) for r in original['subsequent_tool_calls']})
        evidence=[]
        for item in card['evidence']:
            call,oi,observation=calls[item['message_index']]
            text=content(observation)
            assert call==item['tool_call'] and oi==item['observation_index'] and call['id'] in observation['tool_call_ids']
            assert digest(text.encode())==item['original_observation_sha256']
            assert text[item['excerpt_start']:item['excerpt_end']]==item['exact_excerpt']
            evidence.append(item);excerpts+=1
        annotation=NOTES[card['episode']]
        prescriptions=[]
        for mi in annotation['prescription_message_indices']:
            call,_,_=calls[mi]
            prescriptions.append({'message_index':mi,'command':json.loads(call['function']['arguments'])['command']})
        records.append({'episode':card['episode'],'source_family':card['connected_family'],'candidate_id':card['candidate_id'],
            'source_record_sha256':card['source_record_sha256'],'dataset':card['dataset'],'hub_revision':card['hub_revision'],
            'instance_id':card['instance_id'],'source_status':'source_evidence_verified_only','annotation':annotation,
            'observed_prescription':prescriptions,'evidence':evidence,'target_predecision_state':None,
            'relation_label':None,'memory_utility_label':None,'ready_for_utility_training':False,
            'independent_human_review':False})
    assert {r['episode'] for r in records}==set(NOTES)
    output.mkdir(parents=True)
    (output/'candidates.json').write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n')
    with (output/'candidate-readiness.csv').open('w') as f:
        fields=['episode','source_family','unit','observation','boundary','missing','cheap_baseline','ready_for_utility_training']
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        for r in records:writer.writerow({k:r[k] if k in r else r['annotation'][k] for k in fields})
    result={'created_at':datetime.now(timezone.utc).isoformat(),'status':'passed','candidates':len(records),
        'independent_connected_families':len({r['source_family'] for r in records}),'raw_excerpts_verified':excerpts,
        'ready_for_utility_training':0,'new_memory_effect_labels':0,'commands_executed':0,
        'reviewer':'same assistant, not an independent annotator',
        'interpretation':'Source evidence is genuine; target states, executable contracts, and paired memory interventions remain missing. Five Pydantic episodes must stay in one family.',
        'source_files_sha256':{n:digest((PACKET/n).read_bytes()) for n in ['initial-review.json','diversity-review.json']}}
    (output/'audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    (output/'artifact-index.json').write_text(json.dumps({'sha256':{p.name:digest(p.read_bytes()) for p in sorted(output.iterdir())}},indent=2)+'\n')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    print(json.dumps(review(a.output),ensure_ascii=False))
