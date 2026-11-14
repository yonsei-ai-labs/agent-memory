"""Record exact class definitions needed by the inheritance comparison."""
import concurrent.futures
import json
from pathlib import Path
import sys

from collect_public_contexts import collect_one

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'experiments/swe_memory/scripts'))
from records import now, write_json

HERE = Path(__file__).resolve().parent


def collect():
    jobs = []
    paths = ['sklearn/mixture/gaussian_mixture.py', 'sklearn/mixture/_gaussian_mixture.py',
        'sklearn/mixture/bayesian_mixture.py', 'sklearn/mixture/_bayesian_mixture.py',
        'sklearn/cluster/affinity_propagation_.py', 'sklearn/cluster/_affinity_propagation.py']
    for iid in ['scikit-learn__scikit-learn-12471', 'scikit-learn__scikit-learn-13142', 'scikit-learn__scikit-learn-15512']:
        current = json.loads((HERE / 'evidence/public-code' / (iid + '.json')).read_text())
        jobs.append(dict(task=current['public_task'], image_id=current['image_id'], paths=paths,
            prior_actor_target=current['prior_actor_target'], output_subdirectory='public-extra-code',
            provenance=dict(reason='Public class definitions for the source BaseMixture inheritance requirement. No target patch or test expectations.')))
    path = HERE / 'extra-code-plan.json'
    if path.exists():
        assert json.loads(path.read_text())['jobs'] == jobs
    else:
        write_json(path, dict(at=now(), jobs=jobs, model_api_calls=0))
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(collect_one, jobs))
    write_json(HERE / 'extra-code-collection.json', dict(at=now(), rows=rows, model_api_calls=0))
    print('Recorded', len(rows), 'public class-definition contexts.')


if __name__ == '__main__':
    collect()
