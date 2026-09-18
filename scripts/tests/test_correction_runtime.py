"""Causal I-1/I-3 regressions. All mutations target disposable local fixtures."""
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1];sys.path.insert(0,str(SCRIPTS))
import harness_adapter as H
import stage_contracts as C
import routing_fixtures as F

TARGET_BYTES=31_362_304

def sha(path):
    with open(path,'rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def python_fixture(path,large=False):
    source=Path('/usr/bin/python3').resolve()
    shutil.copyfile(source,path);path.chmod(0o700)
    if large:
        with path.open('ab') as stream:stream.truncate(TARGET_BYTES)
        assert path.stat().st_size==TARGET_BYTES
    return path

class RuntimeCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.script=self.root/'adapter.py'
        self.script.write_text('print("ORIGINAL")\n')
        self.exe=python_fixture(self.root/'python-runtime')
    def spec(self):
        return {'argv':[str(self.exe),'-I','-S',str(self.script)],
                'pins':{str(self.exe):sha(self.exe),str(self.script):sha(self.script)},'timeout_seconds':10}
    def test_large_executable_runs_exact_31362304_bytes(self):
        python_fixture(self.exe,large=True)
        try:got=H.run_command(self.spec(),cwd=self.root)
        except C.ContractError as exc:self.fail('valid 31,362,304-byte executable rejected: '+str(exc))
        self.assertEqual(got['exit_code'],0);self.assertEqual(got['stdout'],b'ORIGINAL\n')
    def test_large_document_still_refused(self):
        python_fixture(self.exe,large=True)
        with self.assertRaises(C.ContractError):C.read_file(self.exe)
        with self.assertRaises(C.ContractError):C.file_hash(self.exe)
    def test_compact_executable_runs(self):
        got=H.run_command(self.spec(),cwd=self.root)
        self.assertEqual(got['exit_code'],0);self.assertEqual(got['stdout'],b'ORIGINAL\n')
    def race(self,which,*,in_place=False,probe=False):
        sentinel=self.root/'SUBSTITUTED_CODE_RAN';hit=[];errors=[];real=H.subprocess.Popen
        if probe:
            s=F.stage(self.root);reg=F.registry();adapter=F.attach_adapter(self.root,reg)
            self.script=Path(adapter['argv'][-1]);adapter['argv'][0]=str(self.exe)
            adapter['pins']={str(self.exe):sha(self.exe),str(self.script):sha(self.script)}
        spec=self.spec()
        def substitute(args,*a,**kw):
            hit.append(True)
            victim=self.exe if which=='executable' else self.script
            content=(('#!/usr/bin/python3\n' if which=='executable' else '')+
                     'from pathlib import Path\nPath('+repr(str(sentinel))+').write_text("substituted")\n')
            if in_place:victim.write_text(content)
            else:
                replacement=self.root/'replacement';replacement.write_text(content);replacement.chmod(0o700)
                os.replace(replacement,victim)
            return real(args,*a,**kw)
        with patch.object(H.subprocess,'Popen',substitute):
            if probe:
                result=H.readiness(s,reg,evidence_mode='synthetic');errors.append(not result['fixture_worker']['ready'])
            else:
                try:H.run_command(spec,cwd=self.root)
                except C.ContractError as exc:errors.append(str(exc))
        self.assertEqual(len(hit),1,'race did not fire exactly at process creation')
        self.assertFalse(sentinel.exists(),'substituted code executed before post-check')
        self.assertTrue(errors,'drift was not fail-closed')
        if not probe:self.assertEqual(errors,['command_pin_changed'])
    def test_script_replacement_has_no_side_effect(self):self.race('script')
    def test_executable_replacement_has_no_side_effect(self):self.race('executable')
    def test_script_in_place_change_has_no_side_effect(self):self.race('script',in_place=True)
    def test_executable_in_place_change_has_no_side_effect(self):self.race('executable',in_place=True)
    def test_readiness_uses_same_safe_binding(self):self.race('script',probe=True)
    def test_symlink_refused(self):
        spec=self.spec();target=self.root/'real.py';self.script.rename(target);self.script.symlink_to(target)
        with self.assertRaises(C.ContractError):H.run_command(spec,cwd=self.root)
    def test_directory_and_fifo_refused(self):
        for kind in ('directory','fifo'):
            with self.subTest(kind=kind):
                p=self.root/kind
                if kind=='directory':p.mkdir()
                else:os.mkfifo(p)
                spec=self.spec();spec['argv'][-1]=str(p);spec['pins'].pop(str(self.script));spec['pins'][str(p)]='0'*64
                with self.assertRaises(C.ContractError):H.run_command(spec,cwd=self.root)
    def test_bad_hash_refused_without_process(self):
        spec=self.spec();spec['pins'][str(self.exe)]='0'*64
        with patch.object(H.subprocess,'Popen') as launch:
            with self.assertRaises(C.ContractError):H.run_command(spec,cwd=self.root)
            launch.assert_not_called()

    def test_snapshot_is_kernel_sealed_not_merely_an_open_source(self):
        import errno
        import fcntl
        real=H.subprocess.Popen;observed=[]
        def inspect_snapshots(args,*a,**kw):
            fds=kw.get('pass_fds',())
            self.assertEqual(len(fds),2)
            for fd in fds:
                # Independent Linux UAPI oracle; target builds may omit fcntl exports.
                required=0x0001|0x0002|0x0004|0x0008
                self.assertEqual(fcntl.fcntl(fd,1034)&required,required)
                with self.assertRaises(OSError) as caught:os.pwrite(fd,b'SUBSTITUTED',0)
                self.assertEqual(caught.exception.errno,errno.EPERM);observed.append(fd)
            return real(args,*a,**kw)
        with patch.object(H.subprocess,'Popen',inspect_snapshots):got=H.run_command(self.spec(),cwd=self.root)
        self.assertEqual(len(observed),2);self.assertEqual(got['stdout'],b'ORIGINAL\n')
    def test_executable_limit_is_explicit_and_finite(self):
        from pinned_runtime import MAX_EXECUTABLE
        with self.exe.open('ab') as stream:stream.truncate(MAX_EXECUTABLE+1)
        spec=self.spec();spec['pins'][str(self.exe)]='0'*64
        with patch.object(H.subprocess,'Popen') as launch:
            with self.assertRaises(C.ContractError):H.run_command(spec,cwd=self.root)
            launch.assert_not_called()
    def test_unsafe_path_does_not_launch(self):
        spec=self.spec();bad=str(self.root)+'/../'+self.root.name+'/adapter.py'
        spec['argv'][-1]=bad;spec['pins'][bad]=spec['pins'].pop(str(self.script))
        with patch.object(H.subprocess,'Popen') as launch:
            with self.assertRaises(C.ContractError):H.run_command(spec,cwd=self.root)
            launch.assert_not_called()
    def test_child_environment_remains_allowlisted(self):
        import json
        self.script.write_text('import os,json\nprint(json.dumps(dict(os.environ)))\n')
        with patch.dict(os.environ,{'TEST_ONLY_CREDENTIAL':'not-a-real-credential','PYTHONPATH':'/untrusted'}):
            got=H.run_command(self.spec(),cwd=self.root)
        env=json.loads(got['stdout'])
        self.assertNotIn('TEST_ONLY_CREDENTIAL',env);self.assertNotIn('PYTHONPATH',env)
        self.assertLessEqual(set(env),{'PATH','LANG','PYTHONIOENCODING','PYTHONDONTWRITEBYTECODE','LC_CTYPE'})

if __name__=='__main__':unittest.main()
