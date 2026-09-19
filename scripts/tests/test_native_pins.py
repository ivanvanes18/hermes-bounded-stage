"""Slice A: optional `native_pins`, honoured at BOTH enforcement sites.

`stage_contracts.validate_pins` runs first and drives `pinned_hash(..., executable=)`;
`pinned_runtime.bind_command` runs second and drives the snapshot mode and the ELF
check. Changing only one site leaves a 246.7 MiB Codex ELF rejected as `source_size`
before the other site is ever entered, so both are exercised here.

The validation matrix is exactly two new reason codes -- `native_pin_shape` and
`native_pin_not_pinned` -- plus the pre-existing pin reasons they hand off to.
A13 asserts that closure instead of asserting a dead code path.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

SCRIPTS=Path(__file__).resolve().parents[1];sys.path.insert(0,str(SCRIPTS))
import routing_fixtures as F
from pinned_runtime import MAX_DOCUMENT,MAX_EXECUTABLE,bind_command
from schema_validation import ContractError
from stage_contracts import SCHEMAS,contract,validate_pins

REGISTRY_SCHEMA='executor-registry.schema.json'
ADAPTER_REQUIRED=['protocol','argv','pins','timeout_seconds','evidence_kind']
# Independent Linux UAPI literals, not production imports.
F_GET_SEALS=1034
SEAL_MASK=0x0001|0x0002|0x0004|0x0008


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class NativePinTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.root=Path(self.t.name).resolve()
        self.python=Path(sys.executable).resolve()
        self.script=self.root/'stub.py';self.script.write_text('print("stub")\n')

    def native(self,name='native.bin',*,mode=0o755,content=None,size=None):
        """A pinned object that the spec will declare in `native_pins`."""
        p=self.root/name
        if size is not None:
            p.touch();os.truncate(p,size)
        else:
            p.write_bytes(content if content is not None else self.python.read_bytes())
        os.chmod(p,mode)
        return p

    def spec(self,extra=(),native=None,pins=None,timeout=10):
        argv=[str(self.python),'-I','-S',str(self.script),*[str(x) for x in extra]]
        computed={str(self.python):sha(self.python),str(self.script):sha(self.script)}
        for x in extra:
            if Path(str(x)).is_absolute():computed[str(x)]=sha(x)
        out={'protocol':'hermes-executor-v1','argv':argv,'pins':pins if pins is not None else computed,
             'timeout_seconds':timeout,'evidence_kind':'live'}
        if native is not None:out['native_pins']=native
        return out

    def reason(self,command,*,bind=False):
        with self.assertRaises(ContractError) as caught:
            if bind:
                with bind_command(command,self.root):pass
            else:validate_pins(command)
        return str(caught.exception)

    # ---------- shape matrix: exactly two new reason codes ----------

    def test_native_pin_not_in_pins_refused(self):
        """A1: a native entry outside `pins` is refused before any hashing."""
        outsider=self.native()
        command=self.spec()                      # `outsider` is in neither argv nor pins
        command['native_pins']=[str(outsider)]
        self.assertEqual(self.reason(command),'native_pin_not_pinned')

    def test_native_pin_equal_argv0_refused(self):
        """A2: argv[0] is native by definition; naming it again is a shape error."""
        command=self.spec(native=[str(self.python)])
        self.assertEqual(self.reason(command),'native_pin_not_pinned')

    def test_native_pin_duplicate_refused(self):
        """A3."""
        binary=self.native()
        command=self.spec(extra=[binary],native=[str(binary),str(binary)])
        self.assertEqual(self.reason(command),'native_pin_shape')

    def test_native_pin_over_four_refused(self):
        """A4: at most four directly bound objects per command."""
        extra=[self.native('n%d.bin'%i) for i in range(5)]
        command=self.spec(extra=extra,native=[str(p) for p in extra])
        self.assertEqual(self.reason(command),'native_pin_shape')

    def test_native_pin_non_list_refused(self):
        """A5: a truthy non-list is a shape error; absence/None/falsy is the optional case."""
        binary=self.native()
        for value in ('/not/a/list',{'path':str(binary)}):
            with self.subTest(value=type(value).__name__):
                command=self.spec(extra=[binary],native=value)
                self.assertEqual(self.reason(command),'native_pin_shape')
        for value in (None,[],0,''):
            with self.subTest(optional=repr(value)):
                command=self.spec(extra=[binary],native=value)
                validate_pins(command)   # collapses to [] through `.get(...) or []`

    # ---------- hand-off to the pre-existing pin reasons ----------

    def test_native_pin_non_elf_refused(self):
        """A6: a native pin must be an ELF; a shebang script is refused at bind time."""
        binary=self.native(content=b'#!/bin/sh\necho unpinned interpreter\n')
        command=self.spec(extra=[binary],native=[str(binary)])
        validate_pins(command)
        self.assertEqual(self.reason(command,bind=True),'explicit_native_runtime_required')

    def test_native_pin_without_exec_bit_refused(self):
        """A7: the +x check now applies to native pins at the first enforcement site."""
        binary=self.native(mode=0o644)
        command=self.spec(extra=[binary],native=[str(binary)])
        self.assertEqual(self.reason(command),'executable_permissions')

    def test_native_pin_setuid_refused(self):
        """A8: setuid/setgid bits are refused on a native pin, as on argv[0]."""
        binary=self.native(mode=0o4755)
        self.assertTrue(binary.stat().st_mode&stat.S_ISUID,'host did not preserve the setuid bit')
        command=self.spec(extra=[binary],native=[str(binary)])
        self.assertEqual(self.reason(command),'executable_permissions')

    def test_native_pin_over_256mib_refused(self):
        """A9: the failure is the legible `executable_size`, not `source_size`."""
        binary=self.native(size=MAX_EXECUTABLE+1)
        command=self.spec(extra=[binary],pins={str(self.python):sha(self.python),
                                               str(self.script):sha(self.script),str(binary):'0'*64},
                          native=[str(binary)])
        self.assertEqual(self.reason(command),'executable_size')

    def test_native_pin_hash_drift_refused(self):
        """A10: control -- a native pin is still content-pinned."""
        binary=self.native()
        command=self.spec(extra=[binary],native=[str(binary)])
        command['pins'][str(binary)]='0'*64
        self.assertEqual(self.reason(command),'command_pin_changed')

    # ---------- the sealed, executable snapshot ----------

    def test_native_pin_sealed_and_executable(self):
        """A11: mode 0500, full seal mask, writes refused, and the alias actually execs."""
        binary=self.native()
        command=self.spec(extra=[binary],native=[str(binary)])
        validate_pins(command)
        with bind_command(command,self.root) as (args,executable,descriptors):
            alias=args[-1]
            self.assertRegex(alias,r'^/proc/self/fd/\d+$')
            fd=int(alias.rsplit('/',1)[1])
            self.assertIn(fd,descriptors)
            self.assertEqual(os.fstat(fd).st_mode&0o777,0o500)
            self.assertEqual(fcntl.fcntl(fd,F_GET_SEALS)&SEAL_MASK,SEAL_MASK)
            with self.assertRaises(OSError) as caught:os.pwrite(fd,b'x',0)
            self.assertEqual(caught.exception.errno,1)   # EPERM
            proc=subprocess.run([alias,'-I','-S','-c','print("native-exec-ok")'],
                                executable=alias,pass_fds=(fd,),capture_output=True,timeout=30)
            self.assertEqual(proc.returncode,0)
            self.assertEqual(proc.stdout.strip(),b'native-exec-ok')

    # ---------- the optional case is byte-identical for existing specs ----------

    def test_non_native_pin_still_document_limited(self):
        """A12: a non-native pin keeps the 16 MiB document cap, and pre-existing specs are untouched."""
        document=self.root/'big.json';document.touch();os.truncate(document,MAX_DOCUMENT+1)
        command=self.spec(extra=[document],pins={str(self.python):sha(self.python),
                                                 str(self.script):sha(self.script),str(document):'0'*64})
        self.assertNotIn('native_pins',command)
        self.assertEqual(self.reason(command),'source_size')
        # Every shipped synthetic adapter spec has no `native_pins` and must behave as before.
        registry=F.registry();shipped=F.attach_adapter(self.root,registry)
        self.assertNotIn('native_pins',shipped)
        validate_pins(shipped)
        with bind_command(shipped,self.root) as (args,executable,descriptors):
            self.assertEqual(args[1:3],['-I','-S'])
            self.assertEqual(os.fstat(int(args[3].rsplit('/',1)[1])).st_mode&0o777,0o400)
            self.assertEqual(os.fstat(int(executable.rsplit('/',1)[1])).st_mode&0o777,0o500)

    def test_native_pin_absent_from_argv_is_already_refused(self):
        """A13: the matrix is closed -- there is deliberately no `native_pin_not_in_argv`."""
        outsider=self.native()
        # (a) named in `pins` but not in argv: the pre-existing equality check fires first.
        pins={str(self.python):sha(self.python),str(self.script):sha(self.script),str(outsider):sha(outsider)}
        command=self.spec(pins=pins,native=[str(outsider)])
        self.assertEqual(self.reason(command),'incomplete_command_pins')
        # (b) dropped from `pins` instead: the native check fires, never a membership test.
        command=self.spec(native=[str(outsider)])
        self.assertEqual(self.reason(command),'native_pin_not_pinned')

    # ---------- registry layer ----------

    def test_registry_schema_accepts_and_rejects_native_pins(self):
        """A14: one optional array property; `required` stays at the existing five keys."""
        registry=F.registry();spec=F.attach_adapter(self.root,registry)
        binary=self.native()
        spec['argv'].append(str(binary));spec['pins'][str(binary)]=sha(binary)
        spec['native_pins']=[str(binary)]
        contract(registry,REGISTRY_SCHEMA)
        for bad in (str(binary),{'0':str(binary)},[str(binary)]*5):
            with self.subTest(bad=type(bad).__name__ if not isinstance(bad,list) else 'five'):
                spec['native_pins']=bad
                with self.assertRaises(ContractError):contract(registry,REGISTRY_SCHEMA)
        schema=json.loads((SCHEMAS/REGISTRY_SCHEMA).read_text())
        adapter=schema['properties']['routes']['items']['properties']['adapter']['anyOf'][0]
        self.assertEqual(adapter['required'],ADAPTER_REQUIRED)
        self.assertIn('native_pins',adapter['properties'])


if __name__=='__main__':unittest.main()
