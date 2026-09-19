"""Slice B: the Luna Codex adapter against a strict synthetic fake app-server.

Every case goes through the real `harness_adapter.run_command` / `harness_adapter.execute`,
so pinning, sealing, the child-environment allowlist, the stdio budgets and process-group
cleanup are exercised for real against a real JSON-RPC peer process.

The controller discards adapter stderr at the OS level (`harness_adapter.py:32`), so the
controller path can prove *that* a clause refused but never *which* one. Each negative
therefore also runs the identical adapter file through the identical argv layout with
stderr captured, and asserts the documented reason code. Both observations are required.

No model call happens anywhere in this file.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

SCRIPTS=Path(__file__).resolve().parents[1];sys.path.insert(0,str(SCRIPTS))
import harness_adapter
import luna_fixtures as L
from schema_validation import ContractError,canonical,digest
from stage_contracts import PROOF_FIELDS
try:
    import luna_codex_adapter as A
except ImportError:A=None

# The controller's own child environment (`harness_adapter.py:26`), reused verbatim so a
# direct run starts from exactly what the adapter really gets. HOME is deliberately absent.
CONTROLLER_ENV={'PATH':os.defpath,'LANG':'C.UTF-8','PYTHONIOENCODING':'utf-8','PYTHONDONTWRITEBYTECODE':'1'}
SECRETS=('OPENAI_API_KEY','CODEX_API_KEY','ANTHROPIC_API_KEY','TYPESAFE_API_KEY','GITHUB_TOKEN',
         'GH_TOKEN','HERMES_GATEWAY_URL','AWS_SECRET_ACCESS_KEY','HTTP_PROXY','PYTHONPATH',
         'NODE_OPTIONS','SSH_AUTH_SOCK')
ALLOWED_CHILD_KEYS={'PATH','HOME','CODEX_HOME','LANG','LC_ALL','TERM','TMPDIR','LC_CTYPE'}


def tree(root):
    root=Path(root);out={}
    for path in sorted(root.rglob('*')):
        if path.is_file():out[str(path.relative_to(root))]=hashlib.sha256(path.read_bytes()).hexdigest()
    return out


class LunaAdapterProtocolTests(unittest.TestCase):
    maxDiff=None

    def setUp(self):
        self.assertIsNotNone(A,'luna codex adapter missing')
        self.assertTrue(L.ADAPTER_SOURCE.is_file(),'luna codex adapter missing')
        import tempfile
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.root=Path(self.t.name).resolve()
        self.stage=L.stage(self.root)
        self.workspace=Path(self.stage['workspace'])

    # ---------- shared drivers ----------

    def build(self,scenario,**kw):
        spec=L.adapter_spec(self.root,scenario,**kw)
        registry=L.registry(spec)
        packet=L.packet(self.stage,registry)
        return spec,registry,packet,L.run_request(self.stage,packet)

    def controller_run(self,spec,request,**kw):
        return harness_adapter.run_command(spec,cwd=self.workspace,input_data=canonical(request),
                                           worker_root=self.workspace,**kw)

    def controller_execute(self,registry,packet):
        route=next(r for r in registry['routes'] if r['route_id']==L.ROUTE_ID)
        return harness_adapter.execute(route,packet,self.workspace,evidence_mode='live',timeout_seconds=60)

    def direct(self,spec,request,*,cwd=None,env=None,timeout=120):
        """Identical file, identical argv layout, stderr captured."""
        return subprocess.run(L.direct_argv(spec),input=canonical(request),capture_output=True,
                              cwd=str(cwd or self.workspace),env=dict(env or CONTROLLER_ENV),timeout=timeout)

    def refuse(self,scenario,reason,**kw):
        """Refuse through the controller, then name the clause through a captured-stderr run."""
        spec,registry,packet,request=self.build(scenario,**kw)
        got=self.controller_run(spec,request)
        self.assertNotEqual(got['exit_code'],0,'adapter must exit non-zero')
        self.assertEqual(got['stdout'],b'','no partial or optimistic envelope may be written')
        recorded=L.sidecar(self.root)
        proc=self.direct(spec,request)
        self.assertEqual(proc.stdout,b'')
        self.assertNotEqual(proc.returncode,0)
        self.assertEqual(proc.stderr.decode().strip(),reason)
        return got,recorded

    # ---------- B1-B11: the ThreadStartResponse verification table ----------

    def test_model_mismatch_fails_closed(self):
        self.refuse('wrong_model','model_mismatch')

    def test_effort_mismatch_fails_closed(self):
        self.refuse('wrong_effort','effort_not_proven')

    def test_null_reasoning_effort_fails_closed(self):
        self.refuse('null_effort','effort_not_proven')

    def test_thread_source_mismatch_fails_closed(self):
        self.refuse('wrong_source','thread_source_mismatch')

    def test_sandbox_workspace_write_fails_closed(self):
        self.refuse('workspace_write_sandbox','sandbox_mismatch')

    def test_sandbox_shape_mismatch_fails_closed(self):
        """B6: wrong `sandbox.type` while `networkAccess` is correctly false."""
        _,recorded=self.refuse('wrong_sandbox','sandbox_mismatch')
        self.assertIsNotNone(recorded)

    def test_missing_network_access_fails_closed(self):
        """B7: `{"type":"readOnly"}` with the key absent -- a wire shape the schema permits.

        The adapter must not fall back to the schema `"default": false`, must not infer
        from `sandbox.type`, and must never emit `permissions.network` from an unobserved
        field. Distinct from B6 by construction: same refusal, different clause.
        """
        got,_=self.refuse('missing_network_access','sandbox_network_not_proven')
        self.assertEqual(got['stdout'],b'')
        # The two sandbox failures are genuinely different observations, not one code twice.
        spec,_,_,request=self.build('wrong_sandbox')
        other=self.direct(spec,request).stderr.decode().strip()
        self.assertNotEqual(other,'sandbox_network_not_proven')
        self.assertEqual(other,'sandbox_mismatch')

    def test_cwd_mismatch_fails_closed(self):
        self.refuse('wrong_cwd','cwd_mismatch')

    def test_approval_policy_echo_mismatch_fails_closed(self):
        self.refuse('approval_policy_on_request','approval_policy_mismatch')

    def test_reviewer_mismatch_fails_closed(self):
        self.refuse('wrong_reviewer','reviewer_mismatch')

    def test_missing_session_id_fails_closed(self):
        self.refuse('no_session_id','session_binding_missing')

    # ---------- B12-B14: scoping and usage evidence ----------

    def test_foreign_thread_notifications_ignored(self):
        for scenario in ('foreign_thread_events','foreign_turn_events'):
            with self.subTest(scenario=scenario):
                self.refuse(scenario,'turn_not_completed')

    def test_foreign_turn_usage_not_adopted(self):
        self.refuse('usage_wrong_turn','usage_evidence_missing')

    def test_missing_usage_fails_closed(self):
        got,_=self.refuse('no_usage','usage_evidence_missing')
        self.assertNotIn(b'input_tokens',got['stdout'])
        self.assertEqual(got['stdout'],b'')

    # ---------- B15-B20: server requests, terminal status, scoped errors ----------

    def test_approval_request_declined_and_fails_closed(self):
        _,recorded=self.refuse('approval_request','server_request_declined')
        self.assertIn('execCommandApproval',recorded['server_requests'])
        self.assertEqual(len(recorded['declined']),1)
        self.assertEqual(recorded['declined'][0]['error']['message'],'approval_declined')
        self.assertEqual(recorded['declined'][0]['error']['code'],-32001)
        self.assertIn('turn/interrupt',recorded['methods'])
        self.assertLess(recorded['methods'].index('response:9001'),
                        recorded['methods'].index('turn/interrupt'))

    def test_tool_call_request_declined_and_fails_closed(self):
        _,recorded=self.refuse('tool_call_request','server_request_declined')
        self.assertIn('item/tool/call',recorded['server_requests'])
        self.assertEqual(recorded['declined'][0]['error']['code'],-32001)
        self.assertIn('turn/interrupt',recorded['methods'])

    def test_turn_failed_status_fails_closed(self):
        self.refuse('turn_failed','turn_not_completed')

    def test_turn_interrupted_status_fails_closed(self):
        self.refuse('turn_interrupted','turn_not_completed')

    def test_no_terminal_notification_fails_closed(self):
        self.refuse('no_terminal','turn_not_completed')

    def test_server_error_notification_fails_closed(self):
        self.refuse('server_error_notification','server_error_notification')

    # ---------- B21-B25: the three separately observed budgets ----------

    def test_timeout_kills_child(self):
        spec,_,_,request=self.build('hang',timeout=1)
        with self.assertRaises(ContractError) as caught:self.controller_run(spec,request)
        self.assertEqual(str(caught.exception),'adapter_timeout')

    def test_rx_line_budget_enforced(self):
        """B22: an oversized JSON-RPC frame is an ADAPTER-side refusal."""
        spec,registry,packet,request=self.build('oversized_rpc_line')
        got=self.controller_run(spec,request)
        self.assertNotEqual(got['exit_code'],0);self.assertEqual(got['stdout'],b'')
        self.assertEqual(self.direct(spec,request).stderr.decode().strip(),'rx_budget')
        with self.assertRaises(ContractError) as caught:self.controller_execute(registry,packet)
        self.assertEqual(str(caught.exception),'worker_exit_failure')

    def test_rx_notification_flood_enforced(self):
        """B23: MAX_NOTIFICATIONS ends the run bounded rather than draining forever."""
        spec,registry,packet,request=self.build('notification_flood')
        started=time.monotonic()
        got=self.controller_run(spec,request)
        self.assertLess(time.monotonic()-started,spec['timeout_seconds'])
        self.assertNotEqual(got['exit_code'],0);self.assertEqual(got['stdout'],b'')
        self.assertEqual(self.direct(spec,request).stderr.decode().strip(),'rx_budget')
        with self.assertRaises(ContractError) as caught:self.controller_execute(registry,packet)
        self.assertEqual(str(caught.exception),'worker_exit_failure')

    def test_harness_output_budget_enforced(self):
        """B24: the CONTROLLER stdout budget, from a standalone stub. No fake app-server."""
        spec=L.oversized_stdout_spec(self.root)
        with self.assertRaises(ContractError) as caught:
            harness_adapter.run_command(spec,cwd=self.workspace,input_data=b'{}',worker_root=self.workspace)
        self.assertEqual(str(caught.exception),'adapter_output_budget')

    def test_harness_input_budget_enforced(self):
        spec,_,_,_=self.build('good')
        with self.assertRaises(ContractError) as caught:
            harness_adapter.run_command(spec,cwd=self.workspace,input_data=b'x'*(128*1024+1),
                                        worker_root=self.workspace)
        self.assertEqual(str(caught.exception),'adapter_input_budget')

    # ---------- B26-B29: malformed frames and the probe anchor ----------

    def test_duplicate_json_keys_rejected(self):
        self.refuse('duplicate_json_keys','rpc_frame')

    def test_garbage_line_rejected(self):
        self.refuse('garbage_line','rpc_frame')

    def test_missing_probe_anchor_pair_refused(self):
        """B28: a stray trailing argument is never tolerable."""
        self.refuse('good','argv_shape',anchor_pair=False)

    def test_invalid_probe_anchor_refused(self):
        for variant in ('wrong_json','garbage','oversize'):
            with self.subTest(anchor=variant):
                self.refuse('good','probe_anchor_invalid',anchor=variant)

    # ---------- exact CLI version binding on the RUN path ----------

    def test_missing_expected_version_pair_refused(self):
        """The version pair is mandatory layout, exactly like the anchor pair."""
        self.refuse('good','argv_shape',version_pair=False)

    def test_run_refuses_a_version_the_spec_did_not_bind(self):
        """`old_cli_version` prints 0.152.9 while the spec binds 0.153.4."""
        self.refuse('old_cli_version','cli_version_mismatch')

    def test_run_refuses_a_newer_version_than_the_spec_bound(self):
        """Exact, not minimum: the fake prints 0.153.4 and the spec bound 0.152.9.

        A `>= MIN_CLI_VERSION` check would accept this. The run must not.
        """
        self.refuse('good','cli_version_mismatch',expected_version='codex-cli 0.152.9')

    def test_run_refuses_an_unparseable_version(self):
        self.refuse('unparseable_version','cli_version_unreadable')

    # ---------- B30-B34: the proven envelope ----------

    def test_happy_path_run_envelope(self):
        spec,registry,packet,_=self.build('good')
        result=self.controller_execute(registry,packet)
        proof,usage=result['proof'],result['usage']
        self.assertEqual(set(proof),set(PROOF_FIELDS))
        self.assertEqual(proof['harness'],'luna')
        self.assertEqual(proof['model'],L.MODEL)
        self.assertEqual(proof['effort'],L.EFFORT)
        self.assertEqual(proof['packet_hash'],digest(packet))
        self.assertEqual(proof['nonce'],packet['nonce'])
        self.assertEqual(proof['permissions'],{'mode':'read_only','allowed_paths':[],'network':False})
        self.assertEqual(proof['exit_code'],0)
        self.assertEqual(usage,{'input_tokens':1234,'output_tokens':56})
        self.assertEqual(result['evidence_kind'],'live')
        self.assertEqual(result['adapter_digest'],digest(spec))

    def test_initialized_handshake_precedes_thread_start(self):
        """B31: causal -- the fake refuses `thread/start` until the notification arrives."""
        spec,registry,packet,_=self.build('good')
        self.controller_execute(registry,packet)
        recorded=L.sidecar(self.root)
        self.assertEqual(recorded['methods'][:4],['initialize','initialized','thread/start','turn/start'])
        self.assertIs(recorded['initialized_seen_at_thread_start'],True)

    def test_usage_before_completion_drains_both(self):
        _,registry,packet,_=self.build('usage_before_completion')
        self.assertEqual(self.controller_execute(registry,packet)['usage'],
                         {'input_tokens':1234,'output_tokens':56})

    def test_completion_before_usage_drains_both(self):
        """B33: the drain does NOT stop at the terminal notification."""
        _,registry,packet,_=self.build('completion_before_usage')
        self.assertEqual(self.controller_execute(registry,packet)['usage'],
                         {'input_tokens':1234,'output_tokens':56})

    def test_session_id_binds_thread_session_and_turn(self):
        _,registry,packet,_=self.build('good')
        session=self.controller_execute(registry,packet)['proof']['session_id']
        self.assertTrue(1<=len(session)<=128)
        self.assertTrue(session.startswith('codex:'))
        for part in ('01930000-0000-7000-8000-0000000a7e1d','01930000-0000-7000-8000-0000005e5510',
                     '01930000-0000-7000-8000-0000000c0de0'):
            self.assertIn(part,session)

    # ---------- B35-B37: canonical JSON and the packet-only prompt ----------

    def test_canonical_matches_schema_validation(self):
        import schema_validation
        samples=[{'a':1,'b':[1,2,{'c':None}]},{'ключ':'значение','z':True},
                 {'nested':{'deep':{'list':[{'x':'ünïcødé'},'—']}}},
                 L.PROBE_ANCHOR_STUB,{'empty':{},'zero':0,'neg':-1,'float':1.5}]
        for sample in samples:
            with self.subTest(sample=str(sample)[:40]):
                self.assertEqual(A.canonical(sample),schema_validation.canonical(sample))
                self.assertEqual(A.digest(sample),schema_validation.digest(sample))

    def test_prompt_derived_only_from_packet(self):
        _,registry,packet,_=self.build('good')
        self.controller_execute(registry,packet)
        prompt=L.sidecar(self.root)['params']['turn/start']['input'][0]['text']
        self.assertIn(packet['objective'],prompt)
        self.assertIn(packet['forbidden'][0],prompt)
        self.assertIn(packet['stop_conditions'][0],prompt)
        anchor=str(L.probe_anchor(self.root))
        for secret in (packet['nonce'],digest(packet),packet['route_receipt_hash'],
                       packet['stage_hash'],packet['methodology_hash'],L.ROUTE_ID,
                       str(self.workspace),anchor):
            self.assertNotIn(secret,prompt)

    def test_prompt_budget_enforced(self):
        """The adapter enforces its own bound; it does not trust an upstream budget."""
        spec,registry,packet,request=self.build('good')
        packet=dict(packet)
        packet['selected_context']=[{'id':'oversize','source_sha256':'a'*64,'text':'y'*70000}]
        request=L.run_request(self.stage,packet)
        self.assertLess(len(canonical(request)),128*1024)
        got=self.controller_run(spec,request)
        self.assertNotEqual(got['exit_code'],0);self.assertEqual(got['stdout'],b'')
        self.assertEqual(self.direct(spec,request).stderr.decode().strip(),'prompt_budget')

    # ---------- B38-B43: environment, source discipline, descriptors, cleanup ----------

    def test_child_environment_is_strict(self):
        _,registry,packet,_=self.build('good')
        self.controller_execute(registry,packet)
        env=L.sidecar(self.root)['env']
        self.assertLessEqual(set(env),ALLOWED_CHILD_KEYS)
        import pwd
        home=pwd.getpwuid(os.getuid()).pw_dir
        self.assertEqual(env['HOME'],home)
        self.assertEqual(env['CODEX_HOME'],os.path.join(home,'.codex'))
        self.assertEqual(env['PATH'],os.defpath)
        self.assertEqual(env['TERM'],'dumb')
        self.assertTrue(env['TMPDIR'])

    def test_no_secret_reaches_child(self):
        """B39: causal -- the adapter process itself is polluted, the grandchild is not."""
        spec,_,_,request=self.build('good')
        polluted=dict(CONTROLLER_ENV)
        for name in SECRETS:polluted[name]='TEST_ONLY_SECRET_DO_NOT_FORWARD'
        proc=self.direct(spec,request,env=polluted)
        self.assertEqual(proc.returncode,0,proc.stderr[:400])
        env=L.sidecar(self.root)['env']
        for name in SECRETS:self.assertNotIn(name,env)
        self.assertNotIn('TEST_ONLY_SECRET_DO_NOT_FORWARD',json.dumps(env))

    def test_environ_copy_absent_from_source(self):
        text=L.ADAPTER_SOURCE.read_text()
        self.assertNotIn('os.environ.copy',text)
        self.assertNotIn('environ.copy()',text)

    def test_no_start_new_session_in_source(self):
        self.assertNotIn('start_new_session',L.ADAPTER_SOURCE.read_text())

    def test_fd_alias_passed_through_to_grandchild(self):
        """B42: the Codex alias really reaches exec; the anchor fd deliberately does not."""
        _,registry,packet,_=self.build('good')
        self.controller_execute(registry,packet)
        recorded=L.sidecar(self.root)
        self.assertIn('memfd:hermes-pinned',recorded['exe'])
        digests={entry['sha256'] for entry in recorded['fds'] if entry['sha256']}
        self.assertIn(L.sha(L.fake_server(self.root)),digests)
        self.assertNotIn(L.sha(L.probe_anchor(self.root)),digests)
        self.assertNotIn(hashlib.sha256(canonical(L.PROBE_ANCHOR_STUB)).hexdigest(),digests)

    def test_process_group_cleanup(self):
        spec,_,_,request=self.build('orphan_grandchild')
        self.controller_run(spec,request)
        pids=L.pidfile_pids(self.root)
        self.assertEqual(len(pids),2,'fake must record its own pid and a long-lived grandchild')
        deadline=time.monotonic()+10
        alive=list(pids)
        while alive and time.monotonic()<deadline:
            alive=[pid for pid in alive if self.running(pid)]
        self.assertEqual(alive,[],'process-group cleanup left a descendant running')

    @staticmethod
    def running(pid):
        try:os.kill(pid,0)
        except ProcessLookupError:return False
        except PermissionError:return True
        return Path('/proc/%d/stat'%pid).exists() and 'Z' not in _state(pid)

    # ---------- B44-B46: output discipline and artifact integrity ----------

    def test_stdout_is_exactly_one_json_object(self):
        spec,_,_,request=self.build('good')
        got=self.controller_run(spec,request)
        self.assertEqual(got['exit_code'],0)
        self.assertLessEqual(got['stdout'].count(b'\n'),1)
        body=json.loads(got['stdout'])
        self.assertEqual(set(body),{'protocol','operation','evidence_kind','proof','usage'})
        self.assertEqual(canonical(body)+b'\n',got['stdout'])

    def test_failure_emits_no_stdout(self):
        """B45: every negative scenario, plus both anchor-failure specs."""
        cases=[(scenario,{}) for scenario in L.RUN_NEGATIVE]
        cases.append(('good',{'anchor_pair':False}))
        for variant in ('wrong_json','garbage','oversize'):
            cases.append(('good',{'anchor':variant}))
        for scenario,kw in cases:
            with self.subTest(scenario=scenario,**kw):
                spec,_,_,request=self.build(scenario,**kw)
                proc=self.direct(spec,request)
                self.assertEqual(proc.stdout,b'')
                self.assertNotEqual(proc.returncode,0)
                self.assertTrue(proc.stderr.decode().strip())

    def test_run_declines_before_any_write(self):
        for scenario in ('orphan_grandchild','approval_request'):
            with self.subTest(scenario=scenario):
                spec,_,_,request=self.build(scenario)
                before=tree(self.workspace)
                self.controller_run(spec,request)
                self.assertEqual(tree(self.workspace),before)


def _state(pid):
    try:return Path('/proc/%d/stat'%pid).read_text().rsplit(')',1)[1].split()[0]
    except OSError:return ''


if __name__=='__main__':unittest.main()
