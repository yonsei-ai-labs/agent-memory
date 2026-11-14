"""Small execution-grounded boundary/namespace/setup probes, not SWE agent trials."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import sys

from collect_public_contexts import run_container

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'experiments/swe_memory/scripts'))
from records import now, write_json

HERE = Path(__file__).resolve().parent
ACTOR_PYTHON = '/opt/miniconda3/envs/testbed/bin/python'


def specifications():
    jobs = []

    def add(identifier, task, kind, fixture, body):
        jobs.append(dict(probe_id=identifier, context_task=task, mechanism=kind, fixture=fixture,
                         interpreter=ACTOR_PYTHON, program=body))

    for mode in ['missing', 'settings_only', 'full']:
        setup = ''
        if mode == 'settings_only':
            setup = "from django.conf import settings\nsettings.configure(SECRET_KEY='test',USE_I18N=True)\n"
        elif mode == 'full':
            setup = "import os,sys\nsys.path.insert(0,'/testbed/tests')\nos.environ['DJANGO_SETTINGS_MODULE']='test_sqlite'\nimport django\ndjango.setup()\n"
        body = setup + "from django import forms\nclass Example(forms.Form):\n    value=forms.CharField()\nrendered=str(Example())\nresult={'rendered':bool(rendered)}\n"
        add('django-init-' + mode, 'django__django-16041', 'initialization', dict(mode=mode), body)
    for mode in ['fitted_empty', 'fitted_nonempty', 'unfitted_empty', 'invalid_shape', 'unseen_label']:
        setup = "from sklearn.preprocessing import LabelEncoder\nimport numpy as np\nencoder=LabelEncoder()\n"
        if mode != 'unfitted_empty':
            setup += "encoder.fit(['a','b'])\n"
        value = {'fitted_empty': '[]', 'fitted_nonempty': "['a']", 'unfitted_empty': '[]',
                 'invalid_shape': 'np.empty((1,0))', 'unseen_label': "['missing']"}[mode]
        body = setup + "values=" + value + "\noutput=encoder.transform(values)\nresult={'values':output.tolist(),'dtype':str(output.dtype),'size':int(output.size)}\n"
        add('label-encoder-' + mode, 'scikit-learn__scikit-learn-12471', 'validation_order', dict(mode=mode), body)
    for mode, value in [('zero', '0.0'), ('nan', "float('nan')"), ('positive', '800.0')]:
        body = ("from pvlib.irradiance import reindl\nimport numpy as np\nghi=" + value + "\n"
            "output=reindl(30,180,100,0,ghi,1360,30,180)\n"
            "result={'output_repr':repr(output),'is_finite':bool(np.isfinite(output)),'is_zero':bool(output==0),'ghi_is_exact_zero':bool(ghi==0)}\n")
        add('pvlib-zero-guard-' + mode, 'pvlib__pvlib-python-1707', 'numeric_boundary', dict(ghi=mode), body)
    for mode, a, b in [('different', True, False), ('same_true', True, True), ('same_false', False, False)]:
        body = ("a=" + repr(a) + "\nb=" + repr(b) + "\n"
            "left=eval('should_skip',{'should_skip':a})\nright=eval('should_skip',{'should_skip':b})\n"
            "result={'module_a_value':left,'module_b_value':right,'same_result':left==right}\n")
        add('pytest-namespace-' + mode, 'pytest-dev__pytest-7432', 'namespace_dependency',
            dict(module_a=dict(should_skip=a), module_b=dict(should_skip=b), expression='should_skip'), body)
    for mode in ['known', 'missing', 'qualified_known']:
        if mode == 'known':
            annotation, namespace = "'Alias'", "{'Alias':str}"
        elif mode == 'missing':
            annotation, namespace = "'Alias'", '{}'
        else:
            annotation, namespace = "'example.Alias'", "{'example':types.SimpleNamespace(Alias=str)}"
        body = ("import types\nfrom sphinx.util.typing import get_type_hints\nclass Example:pass\n"
            "Example.__annotations__={'value':" + annotation + "}\nnamespace=" + namespace + "\n"
            "annotations=get_type_hints(Example,globalns=namespace)\nvalue=annotations['value']\n"
            "result={'value_repr':repr(value),'resolved_to_str_type':value is str,'returned_unresolved_string':isinstance(value,str)}\n")
        add('sphinx-alias-' + mode, 'sphinx-doc__sphinx-8627', 'namespace_resolution',
            dict(annotation=mode, namespace_present=mode != 'missing'), body)
    for mode in ['none', 'description', 'signature']:
        body = ("import inspect\nfrom sphinx.util.inspect import stringify_signature\n"
            "def example(value:int)->str:return str(value)\nsetting=" + repr(mode) + "\n"
            "show=setting not in ('none','description')\noutput=stringify_signature(inspect.signature(example),show_annotation=show)\n"
            "result={'output':output,'suppression_guard_active':not show}\n")
        add('sphinx-config-' + mode, 'sphinx-doc__sphinx-8627', 'configuration_guard', dict(autodoc_typehints=mode), body)
    for mode, expression in [('ratio', 'units.second/(units.ohm*units.farad)'),
                             ('second', 'units.second'), ('velocity', 'units.meter/units.second')]:
        body = ("from sympy.physics import units\nfrom sympy.physics.units.systems.si import SI\n"
            "expr=" + expression + "\nfactor,dimension=SI._collect_factor_and_dimension(expr)\n"
            "result={'factor':str(factor),'dimension':str(dimension),'dimensionless':bool(SI.get_dimension_system().is_dimensionless(dimension))}\n")
        add('sympy-dimension-' + mode, 'sympy__sympy-24213', 'physical_dimension_guard', dict(expression=expression), body)
    for iid in ['scikit-learn__scikit-learn-15512', 'scikit-learn__scikit-learn-15535']:
        body = ("import inspect,importlib,os\nfrom sklearn.preprocessing import LabelEncoder\n"
            "legacy=importlib.import_module('sklearn.preprocessing.label')\n"
            "result={'canonical_module':LabelEncoder.__module__,'definition_file':inspect.getfile(LabelEncoder),"
            "'legacy_module_file':getattr(legacy,'__file__',None),'legacy_alias_same_class':legacy.LabelEncoder is LabelEncoder}\n")
        add('sklearn-alias-' + iid.rsplit('-', 1)[-1], iid, 'compatibility_alias', dict(module='sklearn.preprocessing.label'), body)
    add('pvlib-zero-guard-consistent_zero', 'pvlib__pvlib-python-1707', 'numeric_boundary',
        dict(ghi='zero', dhi=0, followup='Contrast with zeroGHI/nonzeroDHI after the first observed result.'),
        "from pvlib.irradiance import reindl\nimport numpy as np\nghi=0.0\noutput=reindl(30,180,0,0,ghi,1360,30,180)\nresult={'output_repr':repr(output),'is_finite':bool(np.isfinite(output)),'is_zero':bool(output==0),'ghi_is_exact_zero':True}\n")
    for mode, constructor in [('inferred_shape', 'ImmutableDenseMatrix([[block]])'),
                              ('explicit_shape', 'ImmutableDenseMatrix(1,1,[block])')]:
        add('sympy-block-' + mode, 'sympy__sympy-19007', 'container_representation', dict(constructor=constructor),
            "from sympy import Matrix,ImmutableDenseMatrix\nfrom sympy.matrices import MatrixBase\nblock=Matrix([[1,2],[3,4]])\ngrid="
            + constructor + "\ncell=grid[0,0]\nresult={'grid_shape':list(grid.shape),'cell_type':type(cell).__name__,'cell_is_matrix':isinstance(cell,MatrixBase),'cell_repr':repr(cell)}\n")
    return jobs


def collect_one(job):
    destination = HERE / 'evidence/runtime' / (job['probe_id'] + '.json')
    context = json.loads((HERE / 'evidence/public-code' / (job['context_task'] + '.json')).read_text())
    if destination.exists():
        previous = json.loads(destination.read_text()); assert previous['specification'] == job
        return dict(probe_id=job['probe_id'], reused=True, outcome=previous['observed'])
    body = "import json,traceback\ntry:\n" + ''.join('    ' + line + '\n' for line in job['program'].splitlines())
    body += ("    observed={'kind':'returned','result':result}\nexcept Exception as error:\n"
             "    observed={'kind':'exception','exception_type':type(error).__name__,'message':str(error),'traceback':traceback.format_exc()}\n"
             "print('MEMORY_RELATION_RESULT:'+json.dumps(observed,sort_keys=True,allow_nan=False))\n")
    result = run_container(context['image_id'], body, timeout=90, interpreter=job['interpreter'])
    lines = [line.removeprefix('MEMORY_RELATION_RESULT:') for line in result['stdout'].splitlines()
             if line.startswith('MEMORY_RELATION_RESULT:')]
    if result['returncode'] != 0 or len(lines) != 1:
        raise RuntimeError('Probe did not yield one recorded result: ' + job['probe_id'] + ' ' + result['stderr'][-600:])
    observed = json.loads(lines[0])
    write_json(destination, dict(at=now(), model_api_calls=0, specification=job,
        public_context_file=str((HERE / 'evidence/public-code' / (job['context_task'] + '.json')).relative_to(ROOT)),
        image_id=context['image_id'], executed_program_sha256=hashlib.sha256(body.encode()).hexdigest(),
        observed=observed, execution=result,
        interpretation='Controlled mechanism probe. Expected validation exceptions are observed outcomes, not infrastructure failures. '
            'Shell success is not treated as semantic success. No actor or memory policy was evaluated.'))
    return dict(probe_id=job['probe_id'], reused=False, outcome=observed)


def collect():
    path = HERE / 'runtime-plan-v2.json'; jobs = specifications()
    if path.exists():
        assert json.loads(path.read_text())['jobs'] == jobs
    else:
        write_json(path, dict(at=now(), model_api_calls=0, jobs=jobs,
            scope='First25 testbed probes preserved in runtime-plan.json. V2 adds a consistent zeroGHI boundary '
                  'after inspecting its contrasting case and two block-constructor checks. Exploratory additions, not independent confirmation.'))
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(collect_one, jobs))
    write_json(HERE / 'runtime-collection.json', dict(at=now(), model_api_calls=0, probes=len(results), rows=results))
    print(json.dumps(dict(probes=len(results), returned=sum(r['outcome']['kind'] == 'returned' for r in results),
                         exceptions=sum(r['outcome']['kind'] == 'exception' for r in results), model_api_calls=0)))


if __name__ == '__main__':
    collect()
