"""Synthetic fixtures only. No provider credentials, no real model claims."""
from pathlib import Path
import hashlib
import json
import sys

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def stage(root):
    root=Path(root);work=root/'work';work.mkdir(exist_ok=True)
    (work/'.staged-routing-workspace.json').write_text('{"purpose":"hermes-routing-staged","schema_version":1}')
    (work/'sample.py').write_text('def add(a, b):\n    return a - b\n')
    owner=root/'shaw.md';owner.write_text('---\nname: shaw\nversion: 1.1.0\n---\nImplement one stage. Parent owns acceptance.\n')
    domain=root/'domain.md';domain.write_text('---\nname: synthetic-domain\nversion: 1.0.0\n---\nPreserve the function signature.\nDo not change unrelated code.\n')
    check=root/'check.py';check.write_text('import pathlib,sys\np=pathlib.Path(sys.argv[1])/"sample.py"\nns={}\nexec(compile(p.read_text(),str(p),"exec"),ns)\nassert ns["add"](2,3)==5\nassert ns["add"](-2,3)==1\n')
    return dict(schema_version=1,stage_id='synthetic-stage',objective='Repair addition in sample.py; preserve its signature.',
      methodology={'domain_skills':[{'name':'synthetic-domain','version':'1.0.0','source_path':str(domain),'sha256':sha(domain)}],
       'process_owner':{'name':'shaw','version':'1.1.0','stage':'implementation','source_path':str(owner),'sha256':sha(owner)},
       'domain_rules':[{'skill':'synthetic-domain','rule_id':'preserve-signature','text':'Preserve the function signature.','source_sha256':sha(domain)}]},
      workspace=str(work),inputs=[{'id':'source','path':'sample.py','sha256':sha(work/'sample.py')}],
      allowed_paths=['sample.py'],forbidden=['No other files may change.'],mode='bounded_write',role='implement',
      risk={'level':'low','production':False,'secrets':False,'irreversible':False,'money':False,'publication':False,'architecture_change':False},
      checks=[{'id':'addition','argv':[str(Path(sys.executable).resolve()),'-I','-S',str(check),'{workspace}'],
               'pins':{str(Path(sys.executable).resolve()):sha(Path(sys.executable).resolve()),str(check):sha(check)},'timeout_seconds':10,'expected_exit':0}],
      output_contract={'required_paths':['sample.py'],'allow_delete':False,'max_changed_files':1},
      stop_conditions=['Any unapproved file change.'],executor_candidates=['fixture_worker','owner'],
      limits={'max_jev_calls':8,'max_worker_calls':2,'max_seconds':120,'max_context_chars':16000},
      features={'context_rerank':False,'semantic_cascade':False,'stage_transition':False},
      context={'candidates':[],'mandatory_ids':[],'top_k':3},semantic_flags=[],
      data_policy={'classification':'synthetic','external_allowed':False})


def registry():
    base=Path(__file__).resolve().parents[2]/'assets/executor-routes.json'
    x=json.loads(base.read_text())
    x['routes'].append({'route_id':'fixture_worker','kind':'executor','skill':'synthetic-executor','harness':'fixture',
        'model':'fixture-model-1','effort':'medium','mode':'bounded_write','max_risk':'low','max_complexity':'medium',
        'roles':['implement','verify'],'required_runtime_proof':['harness','model','effort','session_id','permissions','packet_hash','nonce','exit_code'],
        'fallback':'owner','status':'approved-for-pilot','approval_sha256':'1'*64,'write_benchmark_sha256':'2'*64,'adapter':None})
    return x


def ready(reg,stage_hash):
    from schema_validation import digest
    r=next(x for x in reg['routes'] if x['route_id']=='fixture_worker')
    return {'fixture_worker':{'ready':True,'route_hash':digest(r),'registry_hash':digest(reg),'stage_hash':stage_hash,
        'evidence_kind':'synthetic','harness':'fixture','model':'fixture-model-1','effort':'medium',
        'supported_modes':['bounded_write'],'adapter_digest':'3'*64,'reason':'ready','expires_at':4_000_000_000.0}}


def routing_reply(request,choice='fixture_worker',**values):
    from typed_jev import MODEL
    answers={}
    for name,q in request['questions'].items():
        if q['type']=='choice':
            selected=choice if name=='route' else 'medium'
            answers[name]={'type':'choice','choice':selected,'confidence':.99,
                'probabilities':{x:1.0 if x==selected else 0.0 for x in q['criteria']}}
        else:answers[name]={'type':'noul','noul':values.get(name,.01 if name=='risk' else .99)}
    return {'model':MODEL,'answers':answers,'usage':{'input_tokens':10,'output_tokens':5}}

ADAPTER = r'''import hashlib,json,pathlib,sys
p=json.loads(sys.stdin.read());canonical=lambda x:json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode()
identity={"harness":"fixture","model":"fixture-model-1","effort":"medium"}
if p["operation"]=="probe":
    result={"protocol":"hermes-executor-v1","operation":"probe","ready":True,"identity":identity,
            "supported_modes":["bounded_write"],"evidence_kind":"synthetic"}
else:
    packet=p["packet"];root=pathlib.Path(p["workspace"])
    target=root/"sample.py"
    assert packet["allowed_paths"]==["sample.py"] and packet["mode"]=="bounded_write"
    target.write_text("def add(a, b):\n    return a + b\n")
    result={"protocol":"hermes-executor-v1","operation":"run","evidence_kind":"synthetic",
        "proof":{**identity,"session_id":"synthetic-"+packet["nonce"],
          "permissions":{"mode":"bounded_write","allowed_paths":["sample.py"],"network":False},
          "packet_hash":hashlib.sha256(canonical(packet)).hexdigest(),"nonce":packet["nonce"],"exit_code":0},
        "usage":{"input_tokens":0,"output_tokens":0}}
print(json.dumps(result,sort_keys=True))
'''

def attach_adapter(root,reg,variant='good'):
    root=Path(root);body=ADAPTER
    if variant=='wrong_model':body=body.replace('"model":"fixture-model-1"','"model":"wrong-model"')
    if variant=='wrong_runtime_model':body=body.replace('"proof":{**identity,','"proof":{**identity,"model":"wrong-model",')
    if variant=='bad_result':body=body.replace('return a + b','return a - b')
    if variant=='scope_violation':body=body.replace('result={"protocol":"hermes-executor-v1","operation":"run"',
        '(root/"unrelated.txt").write_text("unapproved")\n    result={"protocol":"hermes-executor-v1","operation":"run"')
    if variant=='wrong_nonce':body=body.replace('"nonce":packet["nonce"]','"nonce":"0"*32')
    if variant=='wrong_permissions':body=body.replace('"network":False','"network":True')
    if variant=='timeout':body=body.replace('packet=p["packet"]','__import__("time").sleep(3);packet=p["packet"]')
    if variant=='large_stdout':body=body.replace('packet=p["packet"]','print("x"*300000);packet=p["packet"]')
    p=root/('adapter-'+variant+'.py');p.write_text(body)
    spec={'protocol':'hermes-executor-v1','argv':[str(Path(sys.executable).resolve()),'-I','-S',str(p)],
          'pins':{str(Path(sys.executable).resolve()):sha(Path(sys.executable).resolve()),str(p):sha(p)},
          'timeout_seconds':1 if variant=='timeout' else 10,'evidence_kind':'synthetic'}
    next(r for r in reg['routes'] if r['route_id']=='fixture_worker')['adapter']=spec
    return spec
