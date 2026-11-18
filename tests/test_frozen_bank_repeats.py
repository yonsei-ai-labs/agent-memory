"""Check the writer-by-solver crossing, including the actual read-only preload and controls."""
from collections import Counter
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.models.openrouter import OpenRouterClient
from agent_memory_eval.recording.journal import read_experiment
from scripts.run_frozen_bank_repeats import PROFILE, prepare, run_shard, schedule, shard_name
from test_forms_solve import NativeHTTP


class FrozenBankRepeatTests(unittest.TestCase):
    def test_each_writer_and_seed_share_a_fresh_pair_of_controls(self):
        cfg = read_json((ROOT / PROFILE).read_bytes())
        for task in cfg['tasks']:
            cells = Counter()
            for wave in (1, 2):
                jobs = schedule(cfg, task, wave)
                self.assertEqual([r['index'] for r in jobs], list(range(1, 13)))
                cells.update((r['repetition'], r['solver_seed'], r['arm']) for r in jobs)
            self.assertEqual(cells, {(w, s, a): 1 for w in (1, 2) for s in (4, 5)
                                     for a in cfg['forms'] + cfg['controls']})

    def test_same_sealed_bank_reaches_two_independent_native_seeds(self):
        workspace = Path(os.environ.get('FIXED_BANK_QA_WORKSPACE', ROOT))
        if not (workspace / 'runs/tau2-two-pass-forms-luna-deepseek-01/completion.json').exists():
            self.skipTest('Requires the archived first-pass writer journals')
        manifest, state = prepare(workspace)
        parent = read_json((state['parent'] / 'control.json').read_bytes())
        models = parent['model_catalog_entries']
        original = OpenRouterClient.__init__
        http = NativeHTTP()
        def transport(request, timeout):
            wire = json.loads(request.data)
            if wire['model'].startswith('deepseek/') and len(wire['messages']) == 2:
                http.by_role['user_simulator'].clear()
            return http(request, timeout)
        def inject(client, *args, **kwargs):
            original(client, *args, **kwargs, opener=transport)
        banks = {(b['writer'], b['arm']): b for b in manifest['banks'] if b['task'] == '3'}
        snapshots = set()
        with TemporaryDirectory() as tmp, patch.object(OpenRouterClient, '__init__', inject), \
             patch.dict(os.environ, OPENROUTER_API_KEY='local-test'), patch('socket.socket.connect', side_effect=AssertionError):
            for wave in (1, 2):
                args = (Path(tmp), manifest, state, {'implementation_commit':'local-test'}, models, '3', wave)
                seal = run_shard(*args)
                root = Path(tmp) / shard_name('3', wave)
                rows = read_json((root / 'allocations.json').read_bytes())
                self.assertEqual(len(rows), 12)
                self.assertTrue(all(r['result']['grading_status'] == 'graded' for r in rows))
                for row, wire in zip(rows, http.by_role['worker'][-12:], strict=True):
                    _, journal, _ = read_experiment(root / row['directory'])
                    assignment = journal['assignments'][row['run_id']]
                    self.assertEqual(wire['seed'], row['solver_seed'])
                    arm = row['arm']
                    if arm == 'no_memory':
                        self.assertIsNone(assignment['input_items_sha256'])
                        continue
                    snapshots.add(row['result']['start_snapshot_id'])
                    items = json.loads(wire['messages'][1]['content'])['items']
                    self.assertEqual(digest(canonical(items)), assignment['input_items_sha256'])
                    if arm == 'empty_preload':
                        self.assertEqual(items, [])
                    else:
                        bank = banks[row['repetition'], arm]
                        self.assertEqual(assignment['source_writer_run_id'], bank['run'])
                        self.assertEqual(assignment['source_writer_snapshot_id'], bank['snapshot'])
                        self.assertEqual(assignment['source_writer_bank_sha256'], bank['bank_sha256'])
                calls = len(http.wires)
                self.assertEqual(run_shard(*args), seal)
                self.assertEqual(len(http.wires), calls)
            self.assertEqual(len(snapshots), 20)
