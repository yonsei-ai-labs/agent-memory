"""Single-assistant semantic review of the frozen issue excerpts and 16 memories.

Every unlisted bank member was reviewed as a different diagnostic mechanism.
These are fallible expert annotations, not automatically measured utility.
"""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
from data_pipeline.development_memory_leads import LABELS,QUESTION,sha,write

# Per issue: identified shared mechanisms, plausible but unestablished links,
# and the specific reasoning used while reading the issue excerpt.
REVIEWS={
7:({'numeric_validation':'Zero coordinate increments reach a numerical plotting operation; validate the numerical input contract before computation.'},{},'Swapped coordinates and zero increments, not callback binding or syntax escaping.'),
24:({'literal_escaping':'Brackets become percent escapes at a URL encoding boundary; inspect literal versus syntax handling and the safe-character policy.'},{},'Repeated query values already exist; sequence expansion would not decide bracket escaping.'),
162:({'empty_input':'Empty data/files still produce a boundary; inspect the zero-element branch before emitting output.'},{},'An empty multipart body, not argument count, AST traversal, or array element validation.'),
197:({'literal_escaping':'The report distinguishes regex character meaning from escaped textual spelling.'},{'identifier_scope':'The warning matcher may need a more precise matching boundary, but its implementation is not shown.'},'Regex-warning recognition; no container, connection, or numeric input failure.'),
242:({},{},'Rewritten Git history invalidates a symmetric-difference range; none of the fixed memories addresses commit ancestry or missing revisions.'),
293:({},{'nested_blocks':'Mixed DTD content is skipped; traversal depth is plausible but parser internals are missing.','roundtrip_fields':'A class is lost in schema-to-code conversion, but it is unknown whether intermediate fields are dropped.','multivalue_mapping':'Mixed repeated content may need a sequence representation; the actual parser representation is missing.','element_constraints':'Mixed element semantics may matter, but no misplaced per-element constraint is demonstrated.'},'A missing generated mixed-content class; avoid assuming the cause from the word schema.'),
323:({'roundtrip_fields':'Client-port metadata is omitted from the explicit mapping into the WSGI environment; inspect propagated fields at the representation boundary.'},{'socket_options':'Client address capabilities may differ by transport, but the issue does not show a failing non-TCP address.'},'A missing environment field, not pool ownership, argument forwarding, or dictionary merge precedence.'),
406:({},{'introspection_version':'MRO/builtin-provider introspection could be implicated, but the short report does not establish runtime metadata behavior.'},'A short recursion/MRO report; no evidence for the other concrete mechanisms.'),
424:({'numeric_validation':'The request explicitly calls for client-side validation/coercion of numeric input before an API call.'},{},'Input type validation, not output dtype, tag merge order, or repeated query encoding.'),
434:({'roundtrip_fields':'The input contains an id:null field which disappears from serialized output; inspect field propagation/filtering.'},{},'Null preservation is different from array-level constraints, native wrapper factories, and dtype conversion.'),
469:({'multivalue_mapping':'The requested boundary converts scalar textual reports containing multiple numbers into arrays/elements.'},{'output_dtype':'Output representation is relevant, but no ignored dtype argument or cast-back behavior is established.'},'Returning structured output rather than text; no nested syntax traversal or missing object field is shown.'),
541:({'stale_client':'A recovery path uses connection state whose heartbeat attribute is unavailable; inspect initialization/cleanup and reuse together.'},{'connection_ownership':'Resource lifetime might contribute, but external pool ownership is not shown.'},'Heartbeat state during recovery; the callback runs and does not report an argument binding failure.'),
553:({},{'connection_ownership':'Begin/end inset scope raises lifecycle questions, but connection ownership is only a possible analogy.'},'A new plotting-scope API, not an observed parser, socket-option, or stale-client defect.'),
602:({},{'output_dtype':'Aggregation/explode may encounter a representation mismatch, but the excerpt ends before the actual error.','multivalue_mapping':'List-valued scoring may require element-wise representation, but the traceback is clipped before the failure detail.'},'The available excerpt is insufficient to identify the aggregation cause; do not fill in missing traceback text.'),
639:({},{},'Requested linter warning/error exit-code policy; none of the sixteen mechanisms addresses status aggregation.'),
644:({'numeric_validation':'The request explicitly changes which values the message-data type validator accepts.'},{},'Accepting bool input is not a scalar-to-sequence or output conversion problem.'),
647:({'callback_arguments':'The issue explicitly identifies required test-name/time arguments as an unnecessary calling restriction.','empty_input':'Generalizing the call beyond the existing context calls for checking omitted-input/default handling.'},{},'Required call parameters, not Android connection cleanup despite the common repository.'),
668:({'specific_precedence':'Multiple layer glyphs receive a mapping intended for the base glyph; inspect which object owns each shared codepoint key.'},{'nested_blocks':'Layer traversal and node roles might explain duplication, but the construction code is absent.'},'Ownership/scope of duplicate mappings; blindly preserving all fields would not be the requested behavior.'),
672:({'empty_input':'The issue explicitly requests handling params=None using decorator defaults.','callback_arguments':'The decorated and direct invocation paths must deliver parameters consistently to ParamHandler.'},{},'Missing call parameters and hidden exception propagation; not a transport-specific socket operation.'),
714:({},{'output_dtype':'Version-dependent identifiers may have changed representation, but the before/after identifier types are absent.','container_wrappers':'A container/identifier representation change is plausible but not demonstrated.'},'Time-series ids do not align; a library version change is not evidence of Python introspection metadata drift.'),
775:({},{'introspection_version':'Dependency-key construction may use generic/type metadata, but no runtime-version metadata change is shown.'},'A missing or invalidly scoped dependency factory, not connection pool auto-close behavior.'),
876:({'literal_escaping':'Generated code embeds forward references as live type expressions where delayed/string references are required; inspect data versus executable syntax.'},{'nested_blocks':'References inside nested choices metadata may be missed by traversal, but generator internals are not shown.'},'Recursive forward references, not native-list defaults or missing serialization fields.'),
896:({'empty_input':'The shown formatting expression emits an extra separator when the optional name is empty.'},{},'Trailing output space from an absent name, not runtime docstring trimming or f-string brace escaping.'),
912:({'stale_client':'Failure in the coroutine path leaves the connection open whereas the synchronous path closes it; inspect cleanup across lifecycle paths.'},{'connection_ownership':'Cleanup responsibility is related, but the excerpt does not establish externally owned resources.'},'Async exception cleanup, not callback arity or unsupported TCP socket options.'),
923:({'container_wrappers':'A framework marker wrapping a factory is rejected as the factory itself; inspect wrapper handling at construction/dispatch.'},{},'An adapter/marker representation boundary; no numeric output or identifier replacement is shown.'),
963:({'numeric_validation':'The request is explicitly about a configurable acceptance bound, here AE-title length, rather than always rejecting the extended value.'},{'socket_options':'The server supports a nonstandard capability, but this is not evidence of a transport socket-option error.'},'A protocol input-validation policy; the numeric validation memory transfers only as an inspection principle.'),
1001:({'roundtrip_fields':'The emitted program retains an internal placeholder instead of the original expression; inspect reconstruction from the intermediate representation.'},{'nested_blocks':'The WHERE expression is nested, but omitted traversal is not established.'},'Intermediate-expression restoration, not text escaping of literal braces.'),
1074:({'specific_precedence':'The generated value uses the containing metaclass identity instead of the requested instance/class context.'},{'roundtrip_fields':'A wrong mapping between representations is plausible, but the issue shows a context/default problem rather than a lost field.'},'Incorrect context for generated defaults; no evidence of runtime introspection version changes.'),
1092:({'nested_blocks':'The same checker must distinguish the role of a statement nested in pytest.raises from an Act block.'},{},'The transfer is block-role inspection; recursively collecting more nodes alone is not a correct patch.'),
1098:({'empty_input':'A dictionary lookup assumes the flags key exists and raises KeyError; inspect existence/default handling before access.'},{'stale_client':'Long-lived state may contribute, but stale connection state is not established.'},'Missing message field, not numeric input or dtype conversion.'),
1127:({'specific_precedence':'Several physics objects overwrite a shared exported value; inspect shared-state ownership and update scope.'},{},'The requested repair removes the export; reusing the historical merge patch verbatim is not implied.'),
1228:({'literal_escaping':'A space inside a device name is treated like a boundary in the resulting filename; inspect text quoting/tokenization.'},{'identifier_scope':'Token boundaries may be implicated, but substring rewriting is not shown.'},'Truncation at whitespace, not repeated sequence values or output dtype.'),
1266:({'introspection_version':'Python 3.8 decorator/code-object line metadata differs from parsed FunctionDef locations, producing a lookup mismatch.'},{},'Metadata alignment across representations/runtime behavior, not an empty-input guard or AST traversal depth.'),
1274:({},{'roundtrip_fields':'IO tests could cover round-trip field preservation, but no required behavior is specified.','output_dtype':'IO tests could cover output types, but no type contract is specified.','container_wrappers':'Wrapper handling might be part of IO, but no such requirement is shown.','element_constraints':'Container/element validation could be relevant, but the requested test contract is absent.'},'The entire available request only asks for tests for three IO classes; no concrete failure mechanism is given.'),
1420:({'container_wrappers':'A Literal wrapper is compared with a raw integer; inspect unwrapping/conversion at the operator boundary.'},{},'Wrapped versus native value interoperability, not numeric range rejection.'),
1470:({},{'numeric_validation':'Both involve numerical bounds, but this issue concerns output clipping rather than rejecting invalid inputs.'},'Missing lower clipping branch; do not equate every numerical issue with dtype or input validation.'),
1483:({},{},'Package discovery exports a tests namespace globally; none of the bank entries concerns build/package inclusion.'),
1504:({'literal_escaping':'Escaped regex fragments are incompletely unescaped when generating a concrete URL.'},{'identifier_scope':'Fragment-specific matching boundaries may matter, but identifier substitution is not demonstrated.'},'Representation-dependent escaping; the correct direction is unescaping, not copying brace doubling.'),
1514:({'container_wrappers':'The operation rejects a supported framework tensor wrapper despite accepting its ordinary base representations.','output_dtype':'The memory explicitly distinguishes dtype conversion from preserving MetaTensor/container type at transform output.'},{},'Type-wrapper interoperability; no evidence of multivalued query expansion or option precedence.'),
1627:({},{'output_dtype':'The title mentions PDU-to-primitive conversion but the excerpt stops before the exception and resulting type.','element_constraints':'Presentation-context constraints may be implicated, but the rejected/parsed structure and failure are not visible.'},'Clipped negotiation log; do not infer the missing exception or treat a DICOM context rejection as a TCP socket-option failure.'),
}

def finalize(source):
    if (source/'artifact-index.json').exists():raise FileExistsError('Already frozen')
    bank={r['memory_id']:r for r in json.loads((source/'memory-bank.json').read_text())}
    queries=json.loads((source/'review-packet.json').read_text())
    if {r['row_index'] for r in queries}!=set(REVIEWS):raise ValueError('Review inventory mismatch')
    inputs=[];supervision=[];review=[]
    for q in queries:
        yes,uncertain,reason=REVIEWS[q['row_index']]
        if set(yes)&set(uncertain) or (set(yes)|set(uncertain))-set(bank):raise ValueError('Invalid manual labels')
        if set(q['candidate_memory_ids'])!=set(bank):raise ValueError('Missing reviewed candidate')
        for mid in q['candidate_memory_ids']:
            m=bank[mid];label=0 if mid in yes else 2 if mid in uncertain else 1
            if q['instance_id']==m['source_instance']:raise ValueError('Same issue memory leak')
            sid=q['query_id']+'--'+mid
            inputs.append({'sample_id':sid,'query_id':q['query_id'],'memory_id':mid,'repository':q['repository'],'split':q['split'],
                'input':{'state_text':'Current issue excerpt, before agent actions:\n'+q['current_excerpt'],
                         'candidate_text':m['text']}})
            supervision.append({'sample_id':sid,'label':label,'label_name':LABELS[label],
                'rationale':yes.get(mid,uncertain.get(mid,reason+' The candidate concerns '+m['text'].split('\n')[0].removeprefix('Historical experience: ')+'.')),
                'annotation_kind':'single_assistant_diagnostic_relation','observed_utility':None})
        review.append({'query_id':q['query_id'],'row_index':q['row_index'],'shared':yes,'uncertain':uncertain,
            'all_other_reviewed_memories':'different_mechanism','current_issue_reason':reason})
    for name,rows in [('inputs.jsonl',inputs),('supervision.jsonl',supervision)]:
        (source/name).write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
    write(source/'annotation-review.json',review)
    labels={r['sample_id']:r['label'] for r in supervision}
    summary={'rows':len(inputs),'queries':len(queries),'memories':len(bank),'current_repositories':len({r['repository'] for r in queries}),
        'splits':{s:{'queries':len({r['query_id'] for r in inputs if r['split']==s}),
                     'repositories':len({r['repository'] for r in inputs if r['split']==s}),
                     'label_counts':dict(Counter(LABELS[labels[r['sample_id']]] for r in inputs if r['split']==s))} for s in ['train','dev','test']},
        'actual_memory_utility_labels':0,'independent_human_annotation':False,'test_relation_labels_not_for_upload':True}
    write(source/'review-summary.json',summary)
    write(source/'artifact-index.json',{'status':'frozen_before_training','sha256':{p.name:sha(p) for p in sorted(source.iterdir()) if p.is_file()}})
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True)
    finalize(p.parse_args().source)
