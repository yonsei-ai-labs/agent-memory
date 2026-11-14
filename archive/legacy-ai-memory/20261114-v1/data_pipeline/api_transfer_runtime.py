"""Curated executable API return/mutation contracts, not scraped task labels.

All code strings below are authored fixtures using public package APIs. No code
from downloaded trajectories is executed. Each candidate is run on fresh inputs.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import time
import warnings

import numpy as np
import pandas as pd
from PIL import Image
import scipy.sparse as sp
import networkx as nx
from sklearn.preprocessing import StandardScaler, Normalizer

RECIPES={
    'returned':'result = operation(data)\nreturn result',
    'receiver':'operation(data)\nreturn data',
    'copied_returned':'work = copy.deepcopy(data)\nresult = operation(work)\nreturn result',
    'copied_receiver':'work = copy.deepcopy(data)\noperation(work)\nreturn work',
}


def plain(value):
    """Detached, finite JSON snapshots: mutation must never alter the 'before'."""
    if isinstance(value,dict):return {k:plain(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [plain(v) for v in value]
    if isinstance(value,float) and not math.isfinite(value):
        return {'nonfinite':'nan' if math.isnan(value) else 'inf' if value>0 else '-inf'}
    return copy.deepcopy(value)


def serialize(x):
    if x is None:return {'type':'None'}
    if isinstance(x,np.ndarray):return {'type':'ndarray','dtype':str(x.dtype),'values':plain(x.tolist())}
    if isinstance(x,pd.DataFrame):return {'type':'DataFrame','columns':list(x.columns),'index':list(x.index),'values':x.astype(object).where(pd.notna(x),None).values.tolist()}
    if isinstance(x,Image.Image):return {'type':'Image','mode':x.mode,'size':list(x.size),'pixels':np.asarray(x).reshape(-1,3).tolist()}
    if sp.issparse(x):return {'type':'csr_matrix','dtype':str(x.dtype),'values':x.toarray().tolist(),'indices':x.indices.tolist(),'indptr':x.indptr.tolist(),'sorted_indices':bool(x.has_sorted_indices)}
    if isinstance(x,nx.Graph):return {'type':'DiGraph' if x.is_directed() else 'Graph','nodes':[[n,dict(sorted(d.items()))] for n,d in sorted(x.nodes(data=True))], 'edges':sorted([list(e) for e in x.edges()])}
    if isinstance(x,list):return {'type':'list','values':plain(x)}
    if isinstance(x,dict):return {'type':'dict','values':plain(dict(sorted(x.items())))}
    raise TypeError('Unreviewed output type: '+str(type(x)))


def equal(a,b):
    if type(a) is not type(b):return False
    if isinstance(a,dict):return a.keys()==b.keys() and all(equal(a[k],b[k]) for k in a)
    if isinstance(a,list):return len(a)==len(b) and all(equal(x,y) for x,y in zip(a,b))
    if isinstance(a,float):return bool(np.isclose(a,b,rtol=1e-7,atol=1e-8,equal_nan=True))
    return a==b


def case(family,name,setup,operation,expected,description,mask_group=None,api=None):
    return {'family':family,'case_id':family+'-'+name,'setup':setup,'operation':operation,
            'expected':expected,'description':description,'mask_group':mask_group,'api':api}


def catalog():
    rows=[]
    array='data = np.array([[3., 1.], [2., 0.]])'
    sorted_array='np.array([[1., 3.], [0., 2.]])'
    rows += [case('numpy','sort-return',array,'return np.sort(data, axis=-1)',sorted_array,'Sort each row in ascending order.','sort','numpy.sort'),
             case('numpy','sort-receiver',array,'return data.sort(axis=-1)',sorted_array,'Sort each row in ascending order.','sort','numpy.ndarray.sort')]
    for flag in [False,True]:
        rows.append(case('numpy','replace-nan-'+str(flag).lower(),'data = np.array([np.nan, 2., -1.])',
                         f'return np.nan_to_num(data, copy={flag}, nan=0.)','np.array([0., 2., -1.])',
                         'Replace NaN with zero, preserving the other values.','replace-nan','numpy.nan_to_num'))
    rows += [case('numpy','round-return','data = np.array([1.2, 2.8])','return np.round(data, decimals=0)','np.array([1., 3.])','Round values to the nearest integer.','round','numpy.round'),
             case('numpy','round-out','data = np.array([1.2, 2.8])','return np.round(data, decimals=0, out=data)','np.array([1., 3.])','Round values to the nearest integer.','round','numpy.round')]
    frame="data = pd.DataFrame({'value': [3, 1, 2], 'extra': [9, 8, 7]})"
    for flag in [False,True]:
        rows.append(case('pandas','sort-'+str(flag).lower(),frame,f"return data.sort_values('value', inplace={flag})",
                         "pd.DataFrame({'value': [1, 2, 3], 'extra': [8, 7, 9]}, index=[1, 2, 0])",'Order rows by value ascending.','sort','pandas.DataFrame.sort_values'))
        rows.append(case('pandas','drop-'+str(flag).lower(),frame,f"return data.drop(columns=['extra'], inplace={flag})",
                         "pd.DataFrame({'value': [3, 1, 2]})",'Remove the extra column, retaining row order.','drop','pandas.DataFrame.drop'))
        rows.append(case('pandas','fill-'+str(flag).lower(),"data = pd.DataFrame({'value': [1., np.nan, 3.]})",f'return data.fillna(0., inplace={flag})',
                         "pd.DataFrame({'value': [1., 0., 3.]})",'Fill missing numeric values with zero.','fill','pandas.DataFrame.fillna'))
    picture="data = Image.new('RGB', (4, 4), color=(3, 4, 5))"
    rows += [case('pillow','resize',picture,'return data.resize((2, 2), resample=Image.Resampling.NEAREST)',"Image.new('RGB', (2, 2), color=(3, 4, 5))",'Produce a two by two image with the same solid color.','size','PIL.Image.Image.resize'),
             case('pillow','thumbnail',picture,'return data.thumbnail((2, 2), resample=Image.Resampling.NEAREST)',"Image.new('RGB', (2, 2), color=(3, 4, 5))",'Produce a two by two image with the same solid color.','size','PIL.Image.Image.thumbnail'),
             case('pillow','crop',picture,'return data.crop((0, 0, 2, 2))',"Image.new('RGB', (2, 2), color=(3, 4, 5))",'Produce the upper-left two by two crop.',None,'PIL.Image.Image.crop'),
             case('pillow','paste',picture,'return data.paste((9, 8, 7), (0, 0, 4, 4))',"Image.new('RGB', (4, 4), color=(9, 8, 7))",'Replace all pixels with the specified new color.',None,'PIL.Image.Image.paste')]
    sparse='data = sp.csr_matrix((np.array([3., 1., 2.]), np.array([2, 0, 1]), np.array([0, 3])), shape=(1, 3))'
    sr='sp.csr_matrix(np.array([[1., 2., 3.]]))'
    rows += [case('scipy','sorted-indices',sparse,'return data.sorted_indices()',sr,'Sort sparse column indices without changing matrix entries.','sort','scipy.sparse.csr_matrix.sorted_indices'),
             case('scipy','sort-indices',sparse,'return data.sort_indices()',sr,'Sort sparse column indices without changing matrix entries.','sort','scipy.sparse.csr_matrix.sort_indices'),
             case('scipy','multiply','data = sp.csr_matrix(np.array([[1., 2.]]))','return data.multiply(2).tocsr()','sp.csr_matrix(np.array([[2., 4.]]))','Double every matrix value.',None,'scipy.sparse.csr_matrix.multiply'),
             case('scipy','setdiag','data = sp.csr_matrix(np.array([[1., 2.], [3., 4.]]))','return data.setdiag([5., 6.])','sp.csr_matrix(np.array([[5., 2.], [3., 6.]]))','Replace diagonal entries with five and six.',None,'scipy.sparse.csr_matrix.setdiag')]
    graph='data = nx.DiGraph([(0, 1), (1, 2)])'
    for flag in [False,True]:
        rows.append(case('networkx','relabel-'+str(flag).lower(),graph,f'return nx.relabel_nodes(data, {{0: 10}}, copy={flag})',
                         'nx.DiGraph([(10, 1), (1, 2)])','Rename node zero to ten, retaining directed edges.','relabel','networkx.relabel_nodes'))
    rows += [case('networkx','reverse',graph,'return data.reverse(copy=True)','nx.DiGraph([(1, 0), (2, 1)])','Reverse all directed edges.',None,'networkx.DiGraph.reverse'),
             case('networkx','add-edge',graph,'return data.add_edge(0, 2)','nx.DiGraph([(0, 1), (1, 2), (0, 2)])','Add the directed edge zero to two.',None,'networkx.DiGraph.add_edge')]
    for dtype in ['float','int']:
        for flag in [False,True]:
            rows.append(case('sklearn','scale-'+dtype+'-'+str(flag).lower(),f'data = np.array([[3, 6], [5, 10]], dtype={dtype})\nmodel = StandardScaler().fit(np.array([[0., 0.], [2., 4.]]))',
                             f'return model.transform(data, copy={flag})','np.array([[2., 2.], [4., 4.]])',
                             'Apply the already fitted scaler; return a floating transformed array.','scale-'+dtype,'sklearn.preprocessing.StandardScaler.transform'))
    for flag in [False,True]:
        rows.append(case('sklearn','normalize-'+str(flag).lower(),'data = np.array([[2., 2.], [1., 3.]])\nmodel = Normalizer(norm="l1").fit(np.array([[1., 1.]]))',
                         f'return model.transform(data, copy={flag})','np.array([[.5, .5], [.25, .75]])','Normalize each row to unit L1 norm.','normalize','sklearn.preprocessing.Normalizer.transform'))
    rows += [case('stdlib','sorted','data = [3, 1, 2]','return sorted(data)','[1, 2, 3]','Sort the numbers ascending.','sort','builtins.sorted'),
             case('stdlib','sort','data = [3, 1, 2]','return data.sort()','[1, 2, 3]','Sort the numbers ascending.','sort','list.sort'),
             case('stdlib','dict-union',"data = {'a': 1}","return data | {'b': 2}","{'a': 1, 'b': 2}",'Add the b mapping while preserving the a mapping.','mapping','dict.__or__'),
             case('stdlib','dict-update',"data = {'a': 1}","return data.update({'b': 2})","{'a': 1, 'b': 2}",'Add the b mapping while preserving the a mapping.','mapping','dict.update')]
    return rows


def second_fixture(spec):
    """Manually specified confirmation inputs; not independent API experiences."""
    family=spec['family'];name=spec['case_id'].removeprefix(family+'-')
    setup=spec['setup'];expected=spec['expected']
    if family=='numpy':
        if name.startswith('sort'):
            setup='data = np.array([[9., -2.], [7., 3.]])';expected='np.array([[-2., 9.], [3., 7.]])'
        elif name.startswith('replace'):
            setup='data = np.array([np.nan, -4., 8., np.nan])';expected='np.array([0., -4., 8., 0.])'
        else:setup='data = np.array([-1.2, 3.7])';expected='np.array([-1., 4.])'
    elif family=='pandas':
        setup="data = pd.DataFrame({'value': [4, 2, 3], 'extra': [-2, -3, -4]})"
        if name.startswith('sort'):expected="pd.DataFrame({'value': [2, 3, 4], 'extra': [-3, -4, -2]}, index=[1, 2, 0])"
        elif name.startswith('drop'):expected="pd.DataFrame({'value': [4, 2, 3]})"
        else:setup="data = pd.DataFrame({'value': [-1., np.nan, 7.]})";expected="pd.DataFrame({'value': [-1., 0., 7.]})"
    elif family=='pillow':
        setup="data = Image.new('RGB', (4, 4), color=(6, 2, 1))"
        if name!='paste':expected="Image.new('RGB', (2, 2), color=(6, 2, 1))"
    elif family=='scipy':
        if 'indices' in name:
            setup='data = sp.csr_matrix((np.array([8., -2., 5.]), np.array([2, 0, 1]), np.array([0, 3])), shape=(1, 3))';expected='sp.csr_matrix(np.array([[-2., 5., 8.]]))'
        elif name=='multiply':setup='data = sp.csr_matrix(np.array([[-1., 4.]]))';expected='sp.csr_matrix(np.array([[-2., 8.]]))'
        else:setup='data = sp.csr_matrix(np.array([[8., -2.], [7., 9.]]))';expected='sp.csr_matrix(np.array([[5., -2.], [7., 6.]]))'
    elif family=='networkx':
        setup='data = nx.DiGraph([(0, 2), (2, 3)])'
        if name.startswith('relabel'):expected='nx.DiGraph([(10, 2), (2, 3)])'
        elif name=='reverse':expected='nx.DiGraph([(2, 0), (3, 2)])'
        else:
            # The prescribed edge must not already exist: preserve a real change.
            setup='data = nx.DiGraph([(0, 1), (1, 3)])';expected='nx.DiGraph([(0, 1), (1, 3), (0, 2)])'
    elif family=='sklearn':
        if name.startswith('scale'):
            dtype='int' if '-int-' in spec['case_id'] else 'float'
            setup=f'data = np.array([[-1, -2], [7, 14]], dtype={dtype})\nmodel = StandardScaler().fit(np.array([[0., 0.], [2., 4.]]))';expected='np.array([[-2., -2.], [6., 6.]])'
        else:
            setup='data = np.array([[3., 1.], [2., 6.]])\nmodel = Normalizer(norm="l1").fit(np.array([[1., 1.]]))';expected='np.array([[.75, .25], [.25, .75]])'
    elif family=='stdlib':
        if name.startswith('sort'):setup='data = [8, -2, 4]';expected='[-2, 4, 8]'
        else:setup="data = {'a': 7, 'c': -1}";expected="{'a': 7, 'b': 2, 'c': -1}"
    else:raise ValueError('Unreviewed API family')
    return spec|{'setup':setup,'expected':expected}


def run_case(spec,recipe,goal,fixture_id=0):
    namespace={'np':np,'pd':pd,'Image':Image,'sp':sp,'nx':nx,'StandardScaler':StandardScaler,'Normalizer':Normalizer,'copy':copy}
    exec(spec['setup'],namespace)
    data=namespace['data'];before=serialize(data);expected=serialize(eval(spec['expected'],namespace));calls=[]
    exec('def underlying(data):\n    '+spec['operation'],namespace)
    def operation(value):
        calls.append(serialize(value));return namespace['underlying'](value)
    namespace['operation']=operation
    exec('def recipe(data):\n    '+RECIPES[recipe].replace('\n','\n    '),namespace)
    start=time.perf_counter();exception=None;output=None
    with warnings.catch_warnings(record=True) as observed_warnings:
        warnings.simplefilter('always')
        try:output=serialize(namespace['recipe'](data))
        except Exception as e:exception={'type':type(e).__name__,'message':str(e)}
    elapsed=time.perf_counter()-start;after=serialize(data)
    output_pass=exception is None and equal(output,expected)
    effect_pass=equal(after,before) if goal=='preserve' else equal(after,expected)
    return {'case_id':spec['case_id'],'fixture_id':fixture_id,'recipe':recipe,'goal':goal,'before':before,'expected':expected,'output':output,'after':after,
            'exception':exception,'operation_calls':len(calls),'output_pass':output_pass,'effect_pass':effect_pass,
            'passed':output_pass and effect_pass and len(calls)==1,'elapsed_seconds':elapsed,
            'warnings':[str(w.message) for w in observed_warnings]}


def execute(output):
    if output.exists():raise FileExistsError(output)
    output.mkdir(parents=True)
    specs=catalog();results=[]
    for spec in specs:
        for fixture_id,fixture in enumerate([spec,second_fixture(spec)]):
            for goal in ['preserve','mutate']:
                for recipe in RECIPES:results.append(run_case(fixture,recipe,goal,fixture_id))
    # Ensure the reference describes a transformation, rather than a no-op task.
    for r in results:
        if equal(r['before'],r['expected']):raise ValueError('Uninformative identity task: '+r['case_id'])
    environment={'python':platform.python_version(),'packages':{name:importlib.metadata.version(name) for name in ['numpy','pandas','Pillow','scipy','scikit-learn','networkx']},
                 'runtime_code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                 'gpu_used':False,'model_api_calls':0,'origin':'Authored executable fixtures using real installed APIs; not natural SWE target trajectories.'}
    for name,value in [('catalog.json',specs),('confirmation-fixtures.json',[second_fixture(s) for s in specs]),('executions.json',results),('environment.json',environment),('recipes.json',RECIPES)]:
        (output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'cases':len(specs),'families':len({s['family'] for s in specs}),'candidate_executions':len(results),
                      'passed':sum(r['passed'] for r in results),'exceptions':sum(r['exception'] is not None for r in results)}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();execute(a.output)
