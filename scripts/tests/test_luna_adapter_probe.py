"""Slice C: the offline probe, through the real `harness_adapter.readiness()`.

The probe proves identity and CLI capability against a fake Codex executable. It issues
`initialize` plus the mandatory `initialized` notification and nothing else -- no
`thread/start`, no `turn/start`, no model call anywhere in this file.

Probe identity is the adapter's own pinned constants plus a proven CLI capability check:
the honest bound. The RUN (slice B) is what proves the model actually served the turn.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS=Path(__file__).resolve().parents[1];sys.path.insert(0,str(SCRIPTS))
import executor_routes
import harness_adapter
import luna_fixtures as L
from schema_validation import canonical,digest,loads
try:
    import luna_codex_adapter as A
except ImportError:A=None

SHIPPED=SCRIPTS.parent/'assets/executor-routes.json'
PROBE_KEYS={'protocol','operation','ready','identity','supported_modes','evidence_kind'}
PROBE_NEGATIVE=('old_cli_version','unparseable_version','bad_initialize','wrong_codex_home')


class LunaAdapterProbeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(A,'luna codex adapter missing')
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.root=Path(self.t.name).resolve()
        self.stage=L.stage(self.root)
        self.workspace=Path(self.stage['workspace'])

    def build(self,scenario='good',*,status='approved-for-pilot',**kw):
        spec=L.adapter_spec(self.root,scenario,**kw)
        return spec,L.registry(spec,status=status)

    def records(self,registry,*,evidence_mode='live'):
        return harness_adapter.readiness(self.stage,registry,evidence_mode=evidence_mode)

    def probe_body(self,spec):
        got=harness_adapter.run_command(spec,cwd=Path(spec['argv'][-1]).parent,
                                        input_data=canonical(L.probe_request()),
                                        worker_root=self.workspace)
        self.assertEqual(got['exit_code'],0,'probe must exit zero')
        return loads(got['stdout'])

    # ---------- C1-C2: the proven probe envelope ----------

    def test_probe_ready_identity(self):
        spec,registry=self.build()
        body=self.probe_body(spec)
        self.assertIs(body['ready'],True)
        self.assertEqual(body['identity'],{'harness':'luna','model':'gpt-5.6-luna','effort':'max'})
        self.assertEqual(body['supported_modes'],['read_only'])
        self.assertEqual(body['evidence_kind'],'live')
        record=self.records(registry)[L.ROUTE_ID]
        self.assertIs(record['ready'],True)
        self.assertEqual(record['reason'],'ready')
        self.assertEqual(record['supported_modes'],['read_only'])
        self.assertEqual(record['evidence_kind'],'live')
        self.assertEqual(record['adapter_digest'],digest(spec))

    def test_probe_envelope_key_set_exact(self):
        spec,_=self.build()
        self.assertEqual(set(self.probe_body(spec)),PROBE_KEYS)

    # ---------- C3-C6: CLI capability and handshake failures ----------

    def test_probe_rejects_old_cli_version(self):
        self.assertUnready('old_cli_version','probe_failed_or_mismatched')

    def test_probe_rejects_unparseable_version(self):
        self.assertUnready('unparseable_version','probe_failed_or_mismatched')

    def test_probe_rejects_bad_initialize_response(self):
        self.assertUnready('bad_initialize','probe_failed_or_mismatched')

    def test_probe_rejects_wrong_codex_home(self):
        self.assertUnready('wrong_codex_home','probe_failed_or_mismatched')

    def test_probe_requires_the_exact_bound_cli_version(self):
        """Exact, not minimum.

        The fake prints `codex-cli 0.153.4`. A spec that bound `0.152.9` would pass a
        `>= MIN_CLI_VERSION` check and must still refuse; a spec that bound a version
        the CLI does not report must refuse in the other direction too.
        """
        for bound in ('codex-cli 0.152.9','codex-cli 0.153.5','codex-cli 0.154.0'):
            with self.subTest(expected_version=bound):
                self.assertUnready('good','probe_failed_or_mismatched',expected_version=bound)
                self.assertIsNone(L.sidecar(self.root),'no app-server may be started')

    def test_probe_requires_the_version_pair_in_argv(self):
        spec,registry=self.build('good',version_pair=False)
        record=self.records(registry)[L.ROUTE_ID]
        self.assertIs(record['ready'],False)
        self.assertEqual(record['reason'],'probe_failed_or_mismatched')
        self.assertIsNone(L.sidecar(self.root),'the layout refusal precedes any spawn')

    def assertUnready(self,scenario,reason,**kw):
        spec,registry=self.build(scenario,**kw)
        record=self.records(registry)[L.ROUTE_ID]
        self.assertIs(record['ready'],False)
        self.assertEqual(record['reason'],reason)
        return spec,registry,record

    # ---------- C7-C8: identity and evidence-kind gates ----------

    def test_probe_identity_mismatch_when_route_model_differs(self):
        spec,registry=self.build()
        # The adapter still probes honestly; the controller refuses the disagreement.
        self.assertEqual(self.probe_body(spec)['identity']['model'],'gpt-5.6-luna')
        route=next(r for r in registry['routes'] if r['route_id']==L.ROUTE_ID)
        route['model']='gpt-5.6-sol'
        record=self.records(registry)[L.ROUTE_ID]
        self.assertIs(record['ready'],False)
        self.assertEqual(record['reason'],'probe_failed_or_mismatched')

    def test_probe_evidence_kind_mismatch(self):
        _,registry=self.build()
        record=self.records(registry,evidence_mode='synthetic')[L.ROUTE_ID]
        self.assertIs(record['ready'],False)
        self.assertEqual(record['reason'],'adapter_evidence_mismatch')
        self.assertIsNone(L.sidecar(self.root),'a mismatched adapter is never launched')

    # ---------- C9-C10: where the probe runs, and what it is allowed to say ----------

    def test_probe_cwd_is_the_probe_anchor_directory(self):
        spec,registry=self.build()
        self.records(registry)
        recorded=L.sidecar(self.root)
        anchor_directory=Path(spec['argv'][-1]).parent
        self.assertEqual(Path(recorded['cwd']).resolve(),anchor_directory.resolve())
        self.assertEqual(anchor_directory.name,'probe-anchor')
        self.assertFalse(anchor_directory.resolve().is_relative_to(self.workspace.resolve()))
        # Not the directory holding the Codex executable, which is what the final argv
        # item would otherwise have been.
        self.assertNotEqual(anchor_directory.resolve(),
                            Path(L.fake_native(self.root)).parent.resolve())
        self.assertEqual(sorted(p.name for p in anchor_directory.iterdir()),['anchor.json'])

    def test_probe_method_list_is_handshake_only(self):
        """C10: the probe-path half of B31. No thread/start, no turn/start, no model call."""
        _,registry=self.build()
        self.records(registry)
        self.assertEqual(L.sidecar(self.root)['methods'],['initialize','initialized'])

    # ---------- C11-C13: the probe never escapes, and is never reached unadmitted ----------

    def test_probe_never_raises_out_of_readiness(self):
        cases=[(scenario,{}) for scenario in PROBE_NEGATIVE]
        cases.append(('good',{'anchor_pair':False}))
        for variant in ('wrong_json','garbage','oversize'):
            cases.append(('good',{'anchor':variant}))
        for scenario,kw in cases:
            with self.subTest(scenario=scenario,**kw):
                _,registry=self.build(scenario,**kw)
                record=self.records(registry)[L.ROUTE_ID]
                self.assertIs(record['ready'],False)
                self.assertEqual(record['reason'],'probe_failed_or_mismatched')

    def test_probe_anchor_invalid_degrades_to_not_ready(self):
        """C12: the adapter refuses on argv/anchor shape BEFORE spawning Codex."""
        for kw in ({'anchor':'garbage'},{'anchor_pair':False}):
            with self.subTest(**kw):
                inner=tempfile.TemporaryDirectory();self.addCleanup(inner.cleanup)
                root=Path(inner.name).resolve()
                spec=L.adapter_spec(root,'good',**kw)
                registry=L.registry(spec)
                record=harness_adapter.readiness(self.stage,registry,evidence_mode='live')[L.ROUTE_ID]
                self.assertIs(record['ready'],False)
                self.assertIsNone(L.sidecar(root),'no Codex process may be launched')

    def test_readiness_gate_requires_approved_for_pilot(self):
        _,registry=self.build(status='unverified')
        record=self.records(registry)[L.ROUTE_ID]
        self.assertIs(record['ready'],False)
        self.assertEqual(record['reason'],'disabled_or_unverified')
        self.assertIsNone(L.sidecar(self.root),'an unverified route is never probed')

    # ---------- C14: the shipped registry is untouched ----------

    def test_shipped_registry_still_has_no_ready_luna_route(self):
        shipped=json.loads(SHIPPED.read_text())
        for route in shipped['routes']:
            if route['kind']=='executor':
                self.assertEqual(route['status'],'unverified',route['route_id'])
                self.assertIsNone(route['adapter'],route['route_id'])
                self.assertIsNone(route['model'],route['route_id'])
        stage=dict(self.stage);stage['executor_candidates']=['luna_max','owner']
        self.assertEqual(executor_routes.available(stage,shipped,{}),['owner'])
        stage['executor_candidates']=['luna_low','luna_medium','luna_max','owner']
        self.assertEqual(executor_routes.available(stage,shipped,{}),['owner'])


if __name__=='__main__':unittest.main()
