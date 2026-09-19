"""Synthetic loop fixtures only. No credentials, no live claims, no real models.

Built on `routing_fixtures`: stage A is the existing pinned-adapter stage, stage B
is a pinned deterministic transform. Stage B deliberately pins a **stable** input
(`spec.md`) that no earlier stage may write, and a disjoint write scope
(`notes.md`), because `executor_runtime.initialize` re-validates every stage with
`verify_files=True` at the moment its iteration starts.
"""
from pathlib import Path
import copy
import json
import sys
import routing_fixtures as F

EXE=str(Path(sys.executable).resolve())
SPEC='Transform the checked source into release notes.\nOne line per change.\n'
# Attempt-dependent worker: wrong on attempt 1, correct on the single bounded
# correction. Raw strings keep the adapter's literal backslash-n sequences.
RETRY_BODY=F.ADAPTER.replace(
    r'target.write_text("def add(a, b):\n    return a + b\n")',
    r'target.write_text("def add(a, b):\n    return a + b\n" if packet["execution"]["attempt"]>1'
    r' else "def add(a, b):\n    return a * b\n")')


def sha(path):return F.sha(path)


def _command(identity,script,*,timeout=10):
    return {'id':identity,'argv':[EXE,'-I','-S',str(script),'{workspace}'],
            'pins':{EXE:sha(EXE),str(script):sha(script)},'timeout_seconds':timeout,'expected_exit':0}


def stage_b(root,stage_a):
    """A pinned deterministic transform over a stable input and a disjoint scope."""
    root=Path(root);work=Path(stage_a['workspace'])
    (work/'spec.md').write_text(SPEC)
    action=root/'action.py'
    action.write_text('import pathlib,sys\n'
                      'work=pathlib.Path(sys.argv[1])\n'
                      'first=(work/"spec.md").read_text().splitlines()[0]\n'
                      '(work/"notes.md").write_text("notes: "+first+"\\n")\n')
    check=root/'check_b.py'
    check.write_text('import pathlib,sys\n'
                     'work=pathlib.Path(sys.argv[1])\n'
                     'text=(work/"notes.md").read_text()\n'
                     'assert text.startswith("notes: ")\n'
                     'assert (work/"spec.md").read_text().splitlines()[0] in text\n')
    methodology=copy.deepcopy(stage_a['methodology'])
    methodology['process_owner']['stage']='transform'
    return dict(schema_version=1,stage_id='synthetic-stage-b',
      objective='Write release notes from the stable spec; touch nothing else.',
      methodology=methodology,workspace=str(work),
      inputs=[{'id':'spec','path':'spec.md','sha256':sha(work/'spec.md')}],
      allowed_paths=['notes.md'],forbidden=['No other files may change.'],
      mode='bounded_write',role='transform',
      risk={'level':'low','production':False,'secrets':False,'irreversible':False,'money':False,
            'publication':False,'architecture_change':False},
      checks=[_command('notes-present',check)],
      output_contract={'required_paths':['notes.md'],'allow_delete':False,'max_changed_files':1},
      stop_conditions=['Any unapproved file change.'],executor_candidates=['deterministic','owner'],
      limits={'max_jev_calls':8,'max_worker_calls':2,'max_seconds':120,'max_context_chars':16000},
      features={'context_rerank':False,'semantic_cascade':False,'stage_transition':False},
      context={'candidates':[],'mandatory_ids':[],'top_k':3},semantic_flags=[],
      data_policy={'classification':'synthetic','external_allowed':False},
      deterministic_action=_command('fixed-transform',action))


def envelope(root,*,max_iterations=2,max_seconds=600,
             goal='Repair addition, then write the release notes.'):
    """A 2-stage pre-admitted loop envelope over one synthetic workspace."""
    root=Path(root);first=F.stage(root)
    return {'schema_version':1,'loop_id':'synthetic-loop','goal':goal,
            'workspace':first['workspace'],'max_iterations':max_iterations,'max_seconds':max_seconds,
            'parent_owned':True,'stages':[first,stage_b(root,first)]}


def envelope_2(root,base=None,*,max_iterations=2,max_seconds=600):
    """The continuation envelope: same workspace, new goal, new stage menu.

    Its only stage pins `spec.md`, which no stage of envelope 1 may write, so the
    envelope stays valid after iteration 1 has rewritten `sample.py`. Pass an
    already-built `base` to build it without touching the workspace again.
    """
    if base is None:base=envelope(root,max_iterations=max_iterations,max_seconds=max_seconds)
    return {**base,'loop_id':'synthetic-loop-2',
            'goal':'Write the release notes for the accepted repair.',
            'stages':[base['stages'][1]]}


def registry(root,variant='good'):
    reg=F.registry();F.attach_adapter(root,reg,variant);return reg


def attach_retry_adapter(root,reg):
    """A pinned adapter that only succeeds on the one bounded correction."""
    root=Path(root);path=root/'adapter-retry.py';path.write_text(RETRY_BODY)
    spec={'protocol':'hermes-executor-v1','argv':[EXE,'-I','-S',str(path)],
          'pins':{EXE:sha(EXE),str(path):sha(path)},'timeout_seconds':10,'evidence_kind':'synthetic'}
    next(r for r in reg['routes'] if r['route_id']=='fixture_worker')['adapter']=spec
    return spec


class Judge:
    """Counting stub provider; presence proves a call happened, never quality."""
    def __init__(self):self.calls=0
    def evaluate(self,state,questions):
        self.calls+=1
        return {'model':'jev-1.13.0','answers':{k:{'type':'noul','noul':.01} for k in questions},
                'usage':{'input_tokens':1,'output_tokens':1}}


def walkthrough(root):
    """Write a persistent fixture tree for the documented CLI walkthrough.

    Nothing here is a temporary directory: the CLI runs as separate processes.
    """
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    first=envelope(root);second=envelope_2(root,first);reg=registry(root)
    paths={'workspace':first['workspace'],
           'envelope':str(root/'loop-envelope.json'),
           'envelope_2':str(root/'loop-envelope-2.json'),
           'registry':str(root/'loop-registry.json')}
    for key,value in (('envelope',first),('envelope_2',second),('registry',reg)):
        Path(paths[key]).write_text(json.dumps(value,ensure_ascii=False,sort_keys=True))
    return paths
