"""Small AST interpreter for generated API wrappers; never exec generated text.

This is an experiment language, not a general Python sandbox. Unsupported
syntax is reported separately from wrong answers. Fixture/operation setup is
trusted, authored code from api_transfer_runtime; generated statements are
interpreted only through the operations below.
"""
from __future__ import annotations
import ast
import copy
import operator
import re


class UnsupportedCode(ValueError):pass


class Returned(Exception):
    def __init__(self,value):self.value=value


ALLOWED_NODES=(ast.Module,ast.FunctionDef,ast.arguments,ast.arg,ast.Assign,ast.AnnAssign,ast.AugAssign,
    ast.Expr,ast.If,ast.Return,ast.Pass,ast.Import,ast.ImportFrom,ast.alias,ast.Name,ast.Load,ast.Store,
    ast.Constant,ast.List,ast.Tuple,ast.Dict,ast.Set,ast.Call,ast.keyword,ast.Attribute,ast.Subscript,ast.Slice,
    ast.Compare,ast.Eq,ast.NotEq,ast.Is,ast.IsNot,ast.In,ast.NotIn,ast.Lt,ast.LtE,ast.Gt,ast.GtE,
    ast.BoolOp,ast.And,ast.Or,ast.UnaryOp,ast.Not,ast.USub,ast.UAdd,ast.BinOp,ast.Add,ast.Sub,
    ast.Mult,ast.Div,ast.BitOr,ast.IfExp)


def parse(text):
    fences=re.findall(r'```(?:python|py)?\s*\n(.*?)```',text,re.S)
    code=(fences[0] if fences else text).strip()
    if len(code)>16000:raise UnsupportedCode('Code length limit')
    try:tree=ast.parse(code)
    except SyntaxError as error:raise UnsupportedCode('Syntax error: '+str(error)) from error
    nodes=list(ast.walk(tree))
    if len(nodes)>500:raise UnsupportedCode('AST size limit')
    for node in nodes:
        if not isinstance(node,ALLOWED_NODES):raise UnsupportedCode('Unsupported syntax: '+type(node).__name__)
        if isinstance(node,ast.Name) and node.id.startswith('_'):raise UnsupportedCode('Private name')
        if isinstance(node,ast.Attribute) and node.attr.startswith('_'):raise UnsupportedCode('Private attribute')
        if isinstance(node,ast.Constant) and isinstance(node.value,(int,float)) and abs(node.value)>1024:
            raise UnsupportedCode('Numeric literal limit')
    functions=[n for n in tree.body if isinstance(n,ast.FunctionDef)]
    if len(functions)!=1 or functions[0].name!='solve':raise UnsupportedCode('One solve function required')
    function=functions[0];args=function.args
    if (len(args.args)!=2 or args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg or
        args.defaults or function.decorator_list):raise UnsupportedCode('solve requires exactly two plain arguments')
    if any(not isinstance(n,(ast.FunctionDef,ast.Import,ast.ImportFrom)) for n in tree.body):
        raise UnsupportedCode('Unsupported module-level statement')
    if sum(isinstance(n,ast.FunctionDef) for n in nodes)!=1:raise UnsupportedCode('Nested function')
    return tree,function,code


class Interpreter:
    def __init__(self,modules,data,operation):
        self.modules=modules;self.env=dict(modules);self.steps=0;self.calls=0
        self.env.update({'len':len,'isinstance':isinstance,'list':list,'dict':dict,'tuple':tuple,
                         'set':set,'int':int,'float':float,'bool':bool,'type':type,'sorted':sorted})
        self.functions=[len,isinstance,list,dict,tuple,set,int,float,bool,type,sorted,copy.deepcopy,copy.copy]
        self.original=data
        def invoke(value):
            self.calls+=1
            if self.calls>1:raise ValueError('operation must be called exactly once')
            return operation(value)
        self.operation=invoke;self.functions.append(invoke)
        self.protected=set(self.env)

    def tick(self):
        self.steps+=1
        if self.steps>2000:raise UnsupportedCode('Interpreter step limit')

    def attribute(self,value,name):
        self.tick();np=self.modules['np'];pd=self.modules['pd'];sp=self.modules['sp'];nx=self.modules['nx'];Image=self.modules['Image']
        allowed=set()
        if value is copy:allowed={'deepcopy','copy'}
        elif value is np:allowed={'array','asarray','copy','copyto','ndarray','float64','int64','nan'}
        elif value is pd:allowed={'DataFrame'}
        elif value is sp:allowed={'csr_matrix','issparse'}
        elif value is nx:allowed={'Graph','DiGraph','relabel_nodes'}
        elif value is Image:allowed={'Image','Resampling'}
        elif value is Image.Resampling:allowed={'NEAREST'}
        elif isinstance(value,np.ndarray):allowed={'copy','astype','resize','fill','sort','dtype','shape','ndim','size','T'}
        elif isinstance(value,pd.DataFrame):allowed={'copy','sort_values','drop','fillna','update','to_numpy','index','columns','iloc','loc','shape','dtypes','values'}
        elif isinstance(value,pd.Index):allowed={'copy','difference','tolist'}
        elif isinstance(value,Image.Image):allowed={'copy','resize','thumbnail','crop','paste','size','mode'}
        elif sp.issparse(value):allowed={'copy','tocsr','toarray','multiply','sort_indices','sorted_indices','eliminate_zeros','setdiag','resize','data','indices','indptr','shape','dtype'}
        elif isinstance(value,nx.Graph):allowed={'copy','clear','update','add_nodes_from','add_edges_from','nodes','edges','reverse','add_edge','is_directed'}
        elif isinstance(value,dict):allowed={'copy','clear','update','items','keys','values','get'}
        elif isinstance(value,list):allowed={'copy','clear','extend','append','sort'}
        if name not in allowed:raise UnsupportedCode('Unsupported attribute '+type(value).__name__+'.'+name)
        result=getattr(value,name)
        if callable(result) and not isinstance(result,type):self.functions.append(result)
        return result

    def bounded_shape(self,value):
        if isinstance(value,int):value=[value]
        if not isinstance(value,(list,tuple)) or len(value)>4:raise UnsupportedCode('Shape limit')
        product=1
        for v in value:
            if not isinstance(v,int) or v<0 or v>4096:raise UnsupportedCode('Shape dimension limit')
            product*=v
        if product>4096:raise UnsupportedCode('Shape allocation limit')

    def bounded_dtype(self,value):
        if self.modules['np'].dtype(value).itemsize>32:raise UnsupportedCode('Dtype allocation limit')

    def checked_call(self,function,args,kwargs):
        name=getattr(function,'__name__','')
        if name in ['resize','thumbnail']:
            shape=args[0] if len(args)==1 or (args and isinstance(args[0],(list,tuple))) else args
            self.bounded_shape(kwargs.get('size',kwargs.get('new_shape',shape)))
        if name=='astype':self.bounded_dtype(kwargs.get('dtype',args[0] if args else None))
        if name in ['array','asarray'] and ('dtype' in kwargs or len(args)>1):
            self.bounded_dtype(kwargs.get('dtype',args[1] if len(args)>1 else None))
        return function(*args,**kwargs)

    def expression(self,node):
        self.tick()
        if isinstance(node,ast.Constant):return node.value
        if isinstance(node,ast.Name):
            if node.id not in self.env:raise UnsupportedCode('Unknown name: '+node.id)
            return self.env[node.id]
        if isinstance(node,(ast.List,ast.Tuple,ast.Set)):
            values=[self.expression(x) for x in node.elts]
            return tuple(values) if isinstance(node,ast.Tuple) else set(values) if isinstance(node,ast.Set) else values
        if isinstance(node,ast.Dict):return {self.expression(k):self.expression(v) for k,v in zip(node.keys,node.values)}
        if isinstance(node,ast.Attribute):return self.attribute(self.expression(node.value),node.attr)
        if isinstance(node,ast.Subscript):return self.expression(node.value)[self.expression(node.slice)]
        if isinstance(node,ast.Slice):return slice(*(self.expression(v) if v is not None else None for v in [node.lower,node.upper,node.step]))
        if isinstance(node,ast.Call):
            function=self.expression(node.func)
            approved=any(function is f for f in self.functions)
            if not approved:raise UnsupportedCode('Unapproved callable')
            if any(k.arg is None for k in node.keywords):raise UnsupportedCode('Expanded keyword arguments')
            args=[self.expression(a) for a in node.args];kwargs={k.arg:self.expression(k.value) for k in node.keywords}
            return self.checked_call(function,args,kwargs)
        if isinstance(node,ast.IfExp):return self.expression(node.body if self.expression(node.test) else node.orelse)
        if isinstance(node,ast.UnaryOp):
            return {ast.Not:operator.not_,ast.USub:operator.neg,ast.UAdd:operator.pos}[type(node.op)](self.expression(node.operand))
        if isinstance(node,ast.BinOp):
            a,b=self.expression(node.left),self.expression(node.right)
            if isinstance(node.op,ast.Mult) and isinstance(b,(list,tuple,str)):a,b=b,a
            if isinstance(a,(list,tuple,str)) and isinstance(node.op,ast.Mult) and (not isinstance(b,int) or len(a)*b>1024):
                raise UnsupportedCode('Container expansion limit')
            return {ast.Add:operator.add,ast.Sub:operator.sub,ast.Mult:operator.mul,ast.Div:operator.truediv,ast.BitOr:operator.or_}[type(node.op)](a,b)
        if isinstance(node,ast.BoolOp):
            value=self.expression(node.values[0])
            for v in node.values[1:]:
                if (isinstance(node.op,ast.And) and not value) or (isinstance(node.op,ast.Or) and value):return value
                value=self.expression(v)
            return value
        if isinstance(node,ast.Compare):
            left=self.expression(node.left)
            for op,right_node in zip(node.ops,node.comparators):
                right=self.expression(right_node)
                funcs={ast.Eq:operator.eq,ast.NotEq:operator.ne,ast.Is:operator.is_,ast.IsNot:operator.is_not,
                       ast.In:lambda a,b:a in b,ast.NotIn:lambda a,b:a not in b,ast.Lt:operator.lt,ast.LtE:operator.le,ast.Gt:operator.gt,ast.GtE:operator.ge}
                if not funcs[type(op)](left,right):return False
                left=right
            return True
        raise UnsupportedCode('Unsupported expression '+type(node).__name__)

    def assign(self,target,value):
        self.tick()
        if isinstance(target,ast.Name):
            if target.id in self.protected:raise UnsupportedCode('Reassignment of protected binding')
            self.env[target.id]=value
        elif isinstance(target,(ast.Tuple,ast.List)):
            if len(target.elts)!=len(value):raise ValueError('Unpack length mismatch')
            for t,v in zip(target.elts,value):self.assign(t,v)
        elif isinstance(target,ast.Subscript):self.expression(target.value)[self.expression(target.slice)]=value
        elif isinstance(target,ast.Attribute):
            obj=self.expression(target.value);name=target.attr
            np=self.modules['np'];pd=self.modules['pd'];sp=self.modules['sp']
            permitted=(isinstance(obj,np.ndarray) and name in ['dtype','shape']) or (isinstance(obj,pd.DataFrame) and name in ['index','columns']) or (sp.issparse(obj) and name in ['data','indices','indptr'])
            if not permitted:raise UnsupportedCode('Unsupported attribute assignment')
            if name=='shape':self.bounded_shape(value)
            if name=='dtype':self.bounded_dtype(value)
            setattr(obj,name,value)
        else:raise UnsupportedCode('Unsupported assignment target')

    def import_node(self,node):
        mapping={'copy':copy,'numpy':self.modules['np'],'pandas':self.modules['pd'],
                 'networkx':self.modules['nx'],'scipy.sparse':self.modules['sp'],'PIL.Image':self.modules['Image']}
        if isinstance(node,ast.Import):
            for alias in node.names:
                if alias.name not in mapping:raise UnsupportedCode('Unsupported import '+alias.name)
                name=alias.asname or alias.name
                if '.' in name:raise UnsupportedCode('Dotted imports require an alias')
                self.env[name]=mapping[alias.name];self.protected.add(name)
        else:
            if node.module=='PIL' and all(a.name=='Image' for a in node.names):
                for a in node.names:self.env[a.asname or a.name]=self.modules['Image']
            elif node.module=='copy' and all(a.name in ['copy','deepcopy'] for a in node.names):
                for a in node.names:self.env[a.asname or a.name]=getattr(copy,a.name)
            else:raise UnsupportedCode('Unsupported from import')
            self.protected.update(a.asname or a.name for a in node.names)

    def block(self,statements):
        for node in statements:
            self.tick()
            if isinstance(node,(ast.Import,ast.ImportFrom)):self.import_node(node)
            elif isinstance(node,ast.Assign):
                value=self.expression(node.value)
                for target in node.targets:self.assign(target,value)
            elif isinstance(node,ast.AnnAssign):self.assign(node.target,self.expression(node.value))
            elif isinstance(node,ast.AugAssign):
                current=self.expression(node.target);other=self.expression(node.value)
                if isinstance(current,(list,tuple,str)) and isinstance(node.op,ast.Mult) and (not isinstance(other,int) or len(current)*other>1024):
                    raise UnsupportedCode('Container expansion limit')
                value={ast.Add:operator.add,ast.Sub:operator.sub,ast.Mult:operator.mul,ast.Div:operator.truediv,ast.BitOr:operator.or_}[type(node.op)](current,other)
                self.assign(node.target,value)
            elif isinstance(node,ast.If):self.block(node.body if self.expression(node.test) else node.orelse)
            elif isinstance(node,ast.Return):raise Returned(self.expression(node.value) if node.value else None)
            elif isinstance(node,ast.Expr):self.expression(node.value)
            elif isinstance(node,ast.Pass):pass
            else:raise UnsupportedCode('Unsupported statement '+type(node).__name__)

    def run(self,text):
        tree,function,_=parse(text)
        for node in tree.body:
            if isinstance(node,(ast.Import,ast.ImportFrom)):self.import_node(node)
        names=[a.arg for a in function.args.args]
        if any(name in self.protected for name in names):raise UnsupportedCode('Reserved argument name')
        self.env[names[0]]=self.original;self.env[names[1]]=self.operation;self.protected.add(names[1])
        try:self.block(function.body)
        except Returned as result:return result.value
        return None
