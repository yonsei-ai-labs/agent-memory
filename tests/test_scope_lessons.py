import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.models.openrouter import OpenRouterClient
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from scripts.run_frozen_bank_repeats import run_shard, shard_name
from scripts.run_scope_lessons import bind_banks, prepare, write_banks
from test_forms_solve import NativeHTTP
from test_forms_write import store


class ScopeLessonTests(unittest.TestCase):
    def test_prompt_only_change_and_correct_target_and_preload_on_native_wires(self):
        workspace = Path(os.environ.get('FIXED_BANK_QA_WORKSPACE', ROOT))
        if not (workspace/'runs/tau2-two-pass-forms-luna-deepseek-01/completion.json').is_file():
            self.skipTest('Requires the archived first-pass cohort and native checkout')
        manifest,state = prepare(workspace)
        self.assertEqual((len(manifest['writer_jobs']), sum(map(len,manifest['scheduled'].values()))), (20,80))
        manifest['writer_jobs'] = [r for r in manifest['writer_jobs'] if r['task_id']=='49' and r['repetition']==1]
        name = shard_name('6',1)
        manifest['scheduled'] = {name:[r for r in manifest['scheduled'][name] if r['repetition']==1]}
        models = read_json((workspace / state['cfg']['endpoint_ref']['path']).read_bytes())['models']
        def respond(n, wire):
            if wire.get('tools') and wire['tools'][0]['function']['name']=='memory_store':
                if len(wire['messages']) == 3 and wire['messages'][0]['content'].endswith(state['cfg']['append_instruction']):
                    return store('scoped-test')['choices'][0]['message']
                return 'Finished.'
            return 'Please let me know how I can help.'
        http = NativeHTTP(worker=respond)
        def transport(request, timeout):
            wire = json.loads(request.data)
            if wire['model'].startswith('deepseek/') and len(wire['messages']) == 2:
                http.by_role['user_simulator'].clear()
            return http(request,timeout)
        orig = OpenRouterClient.__init__
        def inject(client,*args,**kwargs):
            orig(client,*args,opener=transport,**kwargs)
        with TemporaryDirectory() as tmp, patch.dict(os.environ,OPENROUTER_API_KEY='local-fixture'), \
                patch.object(OpenRouterClient,'__init__',inject), patch('socket.socket.connect',side_effect=AssertionError):
            state['parent'] = Path(tmp)/'writers'
            identity = {'implementation_commit':'offline-fixture'}
            write_banks(manifest,state,identity,models)
            manifest,state = bind_banks(manifest,state,identity)
            self.assertEqual({b['arm']:b['item_count'] for b in manifest['banks']}, {'lesson_original':0,'lesson_scoped':1})
            firsts = [w for w in http.by_role['worker'] if len(w['messages']) == 3]
            self.assertEqual(len(firsts),2)
            for w in firsts:
                self.assertEqual(w['messages'][1]['content'],state['sources']['49'].public_context)
                self.assertEqual(json.loads(w['messages'][2]['content']),{'kind':'evaluation_feedback','level':'none'})
                w['messages'][0]['content'] = w['messages'][0]['content'].removesuffix(' '+state['cfg']['append_instruction'])
            self.assertEqual(firsts[0],firsts[1])
            run_shard(Path(tmp),manifest,state,identity,models,'6',1)
            for row in read_json((Path(tmp)/name/'allocations.json').read_bytes()):
                self.assertEqual(row['result']['grading_status'],'graded')
                folder = Path(tmp)/name/row['directory']
                scan,journal,_ = read_experiment(folder)
                refs = [(e['payload']['actor_role'],e['payload']['request_ref']) for e in scan['events'].values()
                        if e['type']=='model.request_started']
                blobs = BlobStore(folder)
                w = next(blobs.get(ref) for role,ref in refs if role=='worker')
                u = next(blobs.get(ref) for role,ref in refs if role=='user_simulator')
                self.assertIn('sophia_taylor_9065',u['messages'][0]['content'])
                self.assertNotIn('Authored scoped-test',canonical(u).decode())
                assignment = journal['assignments'][row['run_id']]
                if row['arm'] in manifest['forms']:
                    b = next(b for b in manifest['banks'] if b['arm']==row['arm'])
                    self.assertEqual(assignment['source_task_id'],'49')
                    self.assertEqual(digest(canonical(json.loads(w['messages'][1]['content'])['items'])),b['items_sha256'])
                else:
                    self.assertIsNone(assignment['source_writer_run_id'])
                    if row['arm']=='empty_preload':
                        self.assertEqual(json.loads(w['messages'][1]['content'])['items'],[])
                    else:
                        self.assertNotIn('start_snapshot',canonical(w['messages']).decode())
