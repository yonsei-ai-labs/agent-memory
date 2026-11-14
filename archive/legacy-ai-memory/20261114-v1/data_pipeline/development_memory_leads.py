"""Prepare source-grounded diagnostic-memory leads for different real issues.

This is supervised diagnostic relevance, NOT observed memory utility or proof
that a historical patch can be applied directly. Source commands are never run.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re

REVISION='35455389ab51bf5e2306bfd436ef72d0f98bf882'
DATASET='nebius/SWE-rebench-openhands-trajectories'
LABELS=['shared_diagnostic_mechanism','different_mechanism','insufficient_connection']
QUESTION=('Using only the current issue excerpt and this past experience, judge whether the memory supplies a concrete diagnostic lead. '
          'Shared mechanism means a specific inspection or failure mechanism transfers; different mechanism means the proposed lead addresses a different cause; '
          'insufficient connection means a plausible connection needs a missing premise. This does not assert patch correctness or measured benefit.')

# Summaries describe observed editing attempts; no final task success is asserted.
MEMORIES=[
 ('roundtrip_fields',342,[112,118],'Serialization and round-trip metadata',
  'Font metadata disappeared when converting from UFO to Glyphs and back. The observed edits copied the meta table into an intermediate userData field and restored it on the reverse conversion.',
  'When data disappears across representation changes, inspect both conversion directions and the fields carried by the intermediate object. Exact field names and schemas require checking.'),
 ('output_dtype',1050,[52,124],'Requested dtype and output container type',
  'An intensity transform converted results back to the original image dtype despite a dtype argument. The trace revised the output conversion several times, distinguishing dtype from tensor or metadata container type.',
  'For an unexpected output type, inspect the final conversion and caller-requested dtype, while preserving supported container types. This does not fix every numerical error.'),
 ('callback_arguments',2645,[68],'Callback argument binding',
  'A registered callback accepted one message argument but the caller passed channel, method, properties, and body. The observed edit accepted those arguments and assembled a message object inside the callback.',
  'Compare the caller arguments and registered function signature when dispatch raises argument errors. Check positional and keyword routing; do not copy the old signature without checking the new caller.'),
 ('numeric_validation',247,[62,66],'Validate numerical inputs before computation',
  'A transport solver checked its matrix and right-hand side but omitted the initial vector. The edit checked the provided vector for finite values and moved initialization before validation.',
  'When invalid numerical inputs reach computation, inspect all inputs and the order of default construction and validation. Finiteness, accepted types, and valid ranges are different contracts.'),
 ('specific_precedence',477,[32],'Specific values versus global defaults',
  'A metrics reporter updated per-datapoint tags with global tags, overwriting the specific values. The observed edit built the merged mapping so per-datapoint tags took precedence.',
  'For unexpectedly overwritten settings or metadata, inspect merge order and where caller-specific keys are stored. A shared name or dictionary alone does not establish an overwrite problem.'),
 ('stale_client',478,[76,80],'Release stale connection state',
  'An Android snippet client retained an event client after stopping, so a later use could reuse a broken connection. The trace added event-client cleanup to stop paths.',
  'For reuse of invalid connection state, inspect lifecycle cleanup and lazy recreation together. Ownership matters: releasing an external shared resource can be wrong.'),
 ('literal_escaping',894,[54],'Escape literal data in generated syntax',
  'An AST code generator inserted literal braces into an f-string without escaping them. The observed edit doubled braces in literal segments while retaining formatted-expression handling.',
  'When generated syntax changes the meaning of literal text, distinguish literal and syntax regions and inspect escaping at that boundary. More escaping is not universally correct for URLs, regexes, or other formats.'),
 ('container_wrappers',3680,[90,92],'Native container defaults versus framework wrappers',
  'A schema library expected wrapped List and Dict values while users supplied native list and dict defaults. Several observed edits wrapped default factories so framework-specific container behavior remained available.',
  'When native containers and framework wrappers behave differently, inspect construction and conversion paths, including empty defaults. The existence of a list does not by itself require a wrapper.'),
 ('multivalue_mapping',91,[54,56,60],'Expand sequence values at the encoding boundary',
  'Query parameter types allowed scalar values but a request supplied sequences. The observed work extended the accepted value types and expanded multiple values into repeated key-value entries.',
  'For scalar-versus-sequence mismatches, inspect both the accepted type and the representation of each element. Query-value expansion does not determine which URL characters should be escaped.'),
 ('introspection_version',2259,[116],'Runtime changes to introspection metadata',
  'Python 3.13 changed docstring trimming behavior. The trace revised deprecation docstring processing to avoid assumptions about old leading or trailing whitespace.',
  'If introspection breaks across runtime versions, inspect metadata returned by that runtime and assumptions in lookup or formatting code. A version difference alone does not establish this cause.'),
 ('nested_blocks',1536,[74,78],'Nested syntax blocks and traversal',
  'A test-style checker did not discover action blocks inside context managers. The observed edit traversed nested syntax nodes instead of scanning only the top-level function body.',
  'When syntactically nested statements are skipped or classified incorrectly, inspect traversal depth and the role assigned to each block. Finding more nodes does not alone decide their semantic role.'),
 ('empty_input',243,[52],'Guard empty or exhausted input before indexing',
  'An empty pre-commit configuration caused an IndexError. The observed migration edit checked the line index against the line count before inspecting the next header line.',
  'For failures on empty or omitted inputs, inspect assumptions about element existence and default initialization. An unrelated exception or missing package is a different mechanism.'),
 ('identifier_scope',415,[60,70],'Replacement scope inside identifiers',
  'A default Class-to-Type substitution also changed Classifier inside EClassifier. An anchored-regex attempt was later reverted in the trace, so it is not a confirmed final repair.',
  'Inspect full-token versus substring matching when identifier rewriting changes unintended portions. Keep the failed or reverted attempt as a boundary, not a reusable successful patch.'),
 ('socket_options',3057,[110,114],'Transport-specific operations',
  'HTTP over Unix sockets hit an unsupported TCP socket option. The trace guarded socket-option operations and considered the reported OS error instead of assuming every socket was TCP.',
  'When a shared networking interface covers distinct transports, check capability assumptions at the operation that fails. An argument mismatch or ownership bug needs a different investigation.'),
 ('connection_ownership',2300,[112],'Factory forwarding and connection ownership',
  'Redis.from_url passed kwargs to the connection pool while a client-level auto-close option controlled resource lifetime. The observed edit separated that option from pool construction and set it on the client.',
  'For convenience constructors, inspect which layer consumes each option and who owns cleanup. This differs from unconditionally closing or resetting every client.'),
 ('element_constraints',1282,[78],'Container constraints versus element constraints',
  'A JSON Schema generator put a string pattern on an array-valued slot rather than its items. The observed edit routed value constraints to the element schema for multivalued slots.',
  'For nested containers, check whether a rule applies to the collection or to each element. Correct nesting and element types must be observed before copying a schema edit.'),
]

def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def text_sha(value): return hashlib.sha256(value.encode()).hexdigest()
def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def tokenize(text): return re.findall(r'[a-z][a-z0-9_]{2,}',text.lower())

def issue_excerpt(record,limit=2200):
    message=next(m for m in record['trajectory'] if m['role']=='user')
    text=message['content'];start=text.index('<issue_description>')+len('<issue_description>')
    end=text.index('</issue_description>',start)
    issue=text[start:end];clipped=len(issue)>limit
    if clipped:
        cut=issue.rfind('\n',0,limit)
        if cut<limit//2:cut=limit
        issue=issue[:cut]
    return issue,{'message_sha256':text_sha(text),'start':start,'end':start+len(issue),'clipped':clipped}

def prepare(intake,output):
    if output.exists():raise FileExistsError(output)
    catalog=json.loads((intake/'catalog.json').read_text());lookup={r['row_index']:r for r in catalog}
    output.mkdir(parents=True);bank=[];source_evidence=[]
    for mid,idx,indices,title,observed,lesson in MEMORIES:
        record=json.loads(Path(lookup[idx]['path']).read_text());events=[]
        for mi in indices:
            calls=record['trajectory'][mi].get('tool_calls') or []
            if len(calls)!=1:raise ValueError(('ambiguous source call',idx,mi))
            call=calls[0];args=json.loads(call['function']['arguments'])
            responses=[(i,m) for i,m in enumerate(record['trajectory']) if i>mi and m.get('tool_call_id')==call['id']]
            if len(responses)!=1 or 'has been edited' not in responses[0][1]['content']:raise ValueError(('unconfirmed edit',idx,mi))
            events.append({'message_index':mi,'call':call,'response_index':responses[0][0],'response':responses[0][1]})
        text='Historical experience: '+title+'\nObserved attempt: '+observed+'\nDiagnostic use and boundary: '+lesson+'\nOverall source success is not asserted.'
        bank.append({'memory_id':mid,'source_row':idx,'source_instance':record['instance_id'],'source_repository':record['repo'],'text':text})
        source_evidence.append({'memory_id':mid,'dataset':DATASET,'revision':REVISION,'row_index':idx,'trajectory_id':record['trajectory_id'],
            'raw_record_sha256':lookup[idx]['sha256'],'events':events,'summary_status':'single_assistant_reviewed_observed_attempt_not_success_claim'})
    source_rows={m['source_row'] for m in bank}
    candidates=[r for r in sorted(catalog,key=lambda r:r['row_index']) if r['row_index'] not in source_rows][:40]
    repos=sorted({r['repo'] for r in candidates},key=lambda r:text_sha('lead-split-20261108:'+r))
    n=len(repos);split_by_repo={r:('test' if i<n//5 else 'dev' if i<2*(n//5) else 'train') for i,r in enumerate(repos)}
    docs=[set(tokenize(m['text'])) for m in bank];df=Counter(t for ts in docs for t in ts)
    idf={t:math.log((1+len(bank))/(1+n))+1 for t,n in df.items()}
    packets=[]
    for entry in candidates:
        record=json.loads(Path(entry['path']).read_text());excerpt,span=issue_excerpt(record)
        q=set(tokenize(excerpt));scores=[]
        for m,tokens in zip(bank,docs):
            score=sum(idf[t] for t in tokens&q)/math.sqrt(max(1,len(tokens))*max(1,len(q)))
            scores.append((score,m['memory_id']))
        selected=sorted([m['memory_id'] for m in bank],key=lambda mid:text_sha('order:'+str(entry['row_index'])+':'+mid))
        packets.append({'query_id':'rebench-'+str(entry['row_index']),'row_index':entry['row_index'],'instance_id':record['instance_id'],
            'repository':record['repo'],'split':split_by_repo[record['repo']],'current_excerpt':excerpt,'excerpt_provenance':span,
            'raw_record_sha256':entry['sha256'],'candidate_memory_ids':selected,'lexical_scores':dict((mid,s) for s,mid in scores)})
    write(output/'memory-bank.json',bank);write(output/'source-evidence.json',source_evidence);write(output/'review-packet.json',packets)
    write(output/'design.json',{'task':'diagnostic_lead_relation_not_utility','labels':LABELS,'question':QUESTION,'dataset':DATASET,'hub_revision':REVISION,
        'source_bank_frozen_before_target_training':True,'source_current_same_issue_allowed':False,'current_input':'Issue excerpt only, before any target agent action',
        'candidate_selection':'All sixteen fixed source memories for every issue; lexical retrieval is a baseline, not an inclusion filter',
        'split_unit':'current repository; shared source memory bank is intentionally known across splits',
        'unseen_memory_generalization':False,'chronological_historical_order_verified':False,'source_bank_role':'offline prior-experience bank, not a claim about historical dates',
        'split_repositories':split_by_repo,'source_issues':len(bank),'current_issues':len(packets),'new_observed_utility_labels':0,
        'annotator':'one assistant; not independent human consensus','utility_evaluation':False,'source_commands_executed':0})
    print(json.dumps({'memories':len(bank),'current_issues':len(packets),'current_repositories':n,'pairs':sum(len(r['candidate_memory_ids']) for r in packets),
                      'split_queries':dict(Counter(r['split'] for r in packets))}))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--intake',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();prepare(a.intake,a.output)
