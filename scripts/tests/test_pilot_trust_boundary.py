"""Correction wave: the out-of-tree pilot trust boundary. No model call anywhere.

Four properties are proven here, each against the real `tools/` code:

  1. the Codex CLI version is *probed* from the held ELF and bound EXACTLY -- not as a
     minimum -- into the adapter spec argv, hence into `adapter_digest` and the approval;
  2. every private publication -- the registry, the preflight observation and the pinned
     probe anchor alike -- resolves its parents with an `O_NOFOLLOW|O_DIRECTORY` dirfd
     walk from `/`, refuses a swapped ancestor, a symlinked or existing target and a
     non-private parent, never creates a parent, completes short writes, and reads the
     published bytes back through a fresh nofollow descriptor at exactly the mode asked
     for -- one of `{0400, 0600}` and nothing else;
  3. the Codex executable is HELD by descriptor: it is measured and executed as the same
     inode, so replacing the pathname after the open cannot change what runs;
  4. registry creation is gated on a hash-bound evidence bundle read from a held
     descriptor, and every approval field is compared value by value before any write.

Every refusal is asserted to leave the filesystem byte-identical outside the sandbox
directory the test itself created.
"""
import contextlib
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest

TESTS=Path(__file__).resolve().parent
REPO=TESTS.parents[1]
for extra in (REPO/'scripts',REPO/'tools'):
    if str(extra) not in sys.path:sys.path.insert(0,str(extra))
from schema_validation import canonical,digest                          # noqa: E402

try:
    import pilot_io
except ImportError:pilot_io=None
try:
    import pilot_registry
except ImportError:pilot_registry=None
try:
    import pilot_preflight
except ImportError:pilot_preflight=None

SHIPPED=REPO/'assets/executor-routes.json'
ADAPTER_SOURCE=REPO/'scripts/luna_codex_adapter.py'
CLI_VERSION='codex-cli 0.153.4'
ROUTE_ID='luna_max_read_only'
DROP=object()      # sentinel: remove this key from the approval record entirely


def fake_cli_elf(message):
    """A 177-byte static x86-64 ELF that writes `message` to stdout and exits 0.

    Built rather than compiled: the suite is stdlib-only and must not acquire a
    toolchain. It ignores its argv entirely, so it stands in for `codex --version`
    without being able to serve anything else. x86-64 Linux only, like the pilot.
    """
    payload=message if isinstance(message,bytes) else message.encode('utf-8')
    base=0x400000;code_offset=120;data_offset=code_offset+39
    code=(b'\xba'+struct.pack('<I',len(payload))+
          b'\x48\xbe'+struct.pack('<Q',base+data_offset)+
          b'\xbf\x01\x00\x00\x00'+b'\xb8\x01\x00\x00\x00'+b'\x0f\x05'+
          b'\xbf\x00\x00\x00\x00'+b'\xb8\x3c\x00\x00\x00'+b'\x0f\x05')
    assert len(code)==39,len(code)
    total=data_offset+len(payload)
    header=b'\x7fELF\x02\x01\x01\x00'+b'\x00'*8+struct.pack(
        '<HHIQQQIHHHHHH',2,0x3e,1,base+code_offset,64,0,0,64,56,1,64,0,0)
    segment=struct.pack('<IIQQQQQQ',1,5,0,base,base,total,total,0x1000)
    return header+segment+code+payload


def tree(root):
    """Every regular file under `root`, by relative path, content digest and mode."""
    root=Path(root);out={}
    for path in sorted(root.rglob('*')):
        info=path.lstat()
        if stat.S_ISREG(info.st_mode):
            out[str(path.relative_to(root))]=(hashlib.sha256(path.read_bytes()).hexdigest(),
                                              stat.S_IMODE(info.st_mode))
        else:
            out[str(path.relative_to(root))]=(stat.S_IFMT(info.st_mode),None)
    return out


class PilotPrivateIoTests(unittest.TestCase):
    """Finding 2 and the held-executable half of finding 3: `tools/pilot_io.py`."""

    def setUp(self):
        self.assertIsNotNone(pilot_io,'tools/pilot_io.py missing')
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.root=Path(self.t.name).resolve()
        self.private=self.root/'private';self.private.mkdir(mode=0o700)
        self.target=self.private/'out.json'

    def assertNoWrites(self,before,*,allow=()):
        after=tree(self.root)
        for name in allow:after.pop(name,None);before.pop(name,None)
        self.assertEqual(after,before,'a refusal must write nothing')

    def refuses(self,reason,call,*args,**kw):
        before=tree(self.root)
        with self.assertRaises(pilot_io.PilotIoError) as caught:call(*args,**kw)
        self.assertEqual(str(caught.exception),reason)
        self.assertNoWrites(before)
        return caught.exception

    # ---------- publication ----------

    def test_publish_writes_private_bytes_and_returns_digest(self):
        body=canonical({'hello':'world'})
        got=pilot_io.publish(self.target,body)
        self.assertEqual(got,hashlib.sha256(body).hexdigest())
        info=self.target.lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode),0o600)
        self.assertEqual(info.st_uid,os.getuid())
        self.assertEqual(self.target.read_bytes(),body)

    def test_publish_refuses_relative_and_dotted_paths(self):
        self.refuses('path_not_absolute',pilot_io.publish,Path('out.json'),b'{}')
        self.refuses('path_component_invalid',pilot_io.publish,
                     Path(str(self.private)+'/../private/out.json'),b'{}')

    def test_publish_refuses_swapped_ancestor(self):
        """Ancestor swap: a parent component replaced by a symlink is never traversed."""
        chain=self.private/'a'/'b';chain.mkdir(parents=True,mode=0o700)
        elsewhere=self.root/'elsewhere';elsewhere.mkdir(mode=0o700)
        os.rename(self.private/'a',self.root/'moved')
        os.symlink(elsewhere,self.private/'a')
        self.refuses('path_component_not_directory',pilot_io.publish,
                     self.private/'a'/'b'/'out.json',b'{}')

    def test_publish_refuses_symlinked_target(self):
        os.symlink(self.root/'decoy',self.target)
        (self.root/'decoy').write_bytes(b'original')
        self.refuses('target_symlink',pilot_io.publish,self.target,b'{}')
        self.assertEqual((self.root/'decoy').read_bytes(),b'original')

    def test_publish_refuses_existing_target(self):
        self.target.write_bytes(b'original');os.chmod(self.target,0o600)
        self.refuses('target_exists',pilot_io.publish,self.target,b'replacement')
        self.assertEqual(self.target.read_bytes(),b'original')

    def test_publish_refuses_special_target(self):
        os.mkfifo(self.target,0o600)
        self.refuses('target_not_regular',pilot_io.publish,self.target,b'{}')

    def test_publish_refuses_non_private_parent(self):
        for mode in (0o750,0o701,0o755):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.private,mode)
                self.refuses('parent_not_private',pilot_io.publish,self.target,b'{}')
        os.chmod(self.private,0o700)

    def test_publish_accepts_only_the_two_restricted_modes(self):
        """The mode is an explicit argument, and the set of legal values is closed."""
        body=canonical({'hello':'world'})
        anchor=self.private/'anchor.json'
        self.assertEqual(pilot_io.publish(anchor,body,mode=0o400),
                         hashlib.sha256(body).hexdigest())
        self.assertEqual(stat.S_IMODE(anchor.lstat().st_mode),0o400)
        self.assertEqual(anchor.read_bytes(),body)
        for mode in (0o644,0o700,0o660,0o444,0o000):
            with self.subTest(mode=oct(mode)):
                self.refuses('mode_not_restricted',pilot_io.publish,
                             self.private/('m%o.json'%mode),body,mode=mode)

    def test_verify_published_requires_the_exact_expected_mode(self):
        """A 0400 publication is not a 0600 one: the readback compares the mode exactly."""
        body=canonical({'hello':'world'})
        pilot_io.publish(self.target,body,mode=0o400)
        parent_fd,name=pilot_io.open_parent(self.target)
        try:
            with self.assertRaises(pilot_io.PilotIoError) as caught:
                pilot_io.verify_published(parent_fd,name,body)
            self.assertEqual(str(caught.exception),'published_mode')
            self.assertEqual(pilot_io.verify_published(parent_fd,name,body,mode=0o400),
                             hashlib.sha256(body).hexdigest())
        finally:os.close(parent_fd)

    def test_publish_completes_short_writes(self):
        body=b'MARKER'+canonical({'padding':'x'*900})
        real=os.write;calls=[]
        def chunked(fd,data):
            calls.append(len(data));return real(fd,bytes(data)[:7])
        os.write=chunked
        try:pilot_io.publish(self.target,body)
        finally:os.write=real
        self.assertGreater(len(calls),1,'the write loop must iterate')
        self.assertEqual(self.target.read_bytes(),body)

    def test_publish_refuses_and_unlinks_on_stalled_write(self):
        body=b'MARKER'+canonical({'padding':'x'*64})
        before=tree(self.root)
        real=os.write
        def stalled(fd,data):
            return 0 if bytes(data).startswith(b'MARKER') else real(fd,data)
        os.write=stalled
        try:
            with self.assertRaises(pilot_io.PilotIoError) as caught:
                pilot_io.publish(self.target,body)
        finally:os.write=real
        self.assertEqual(str(caught.exception),'short_write')
        self.assertFalse(self.target.exists(),'a stalled publication leaves no target')
        self.assertNoWrites(before)

    def test_readback_detects_a_rebound_target(self):
        """Final rebind: the name is re-pointed at another inode before the readback."""
        body=canonical({'hello':'world'})
        pilot_io.publish(self.target,body)
        held=os.stat(self.target)
        other=self.private/'other.json';other.write_bytes(body);os.chmod(other,0o600)
        os.rename(other,self.target)
        parent_fd,name=pilot_io.open_parent(self.target)
        try:
            with self.assertRaises(pilot_io.PilotIoError) as caught:
                pilot_io.verify_published(parent_fd,name,body,inode=(held.st_dev,held.st_ino))
        finally:os.close(parent_fd)
        self.assertEqual(str(caught.exception),'published_rebound')

    def test_readback_detects_content_drift_on_the_same_inode(self):
        body=canonical({'hello':'world'})
        pilot_io.publish(self.target,body)
        os.chmod(self.target,0o600)
        with open(self.target,'r+b') as handle:handle.write(b'X')
        parent_fd,name=pilot_io.open_parent(self.target)
        try:
            with self.assertRaises(pilot_io.PilotIoError) as caught:
                pilot_io.verify_published(parent_fd,name,body)
        finally:os.close(parent_fd)
        self.assertEqual(str(caught.exception),'published_bytes')

    # ---------- private reads ----------

    def test_read_private_file_requires_a_private_regular_file(self):
        bundle=self.private/'evidence.json';bundle.write_bytes(b'{}');os.chmod(bundle,0o600)
        body,got=pilot_io.read_private_file(bundle)
        self.assertEqual(body,b'{}')
        self.assertEqual(got,hashlib.sha256(b'{}').hexdigest())
        os.chmod(bundle,0o644)
        self.refuses('source_not_private',pilot_io.read_private_file,bundle)
        os.chmod(bundle,0o600)
        link=self.private/'link.json';os.symlink(bundle,link)
        self.refuses('source_symlink',pilot_io.read_private_file,link)

    def test_read_private_file_can_require_an_exact_mode(self):
        """`expect_mode` is the read-side half of the restricted-mode contract."""
        stub=self.private/'anchor.json';stub.write_bytes(b'{}');os.chmod(stub,0o400)
        body,got=pilot_io.read_private_file(stub,expect_mode=0o400)
        self.assertEqual(body,b'{}')
        self.assertEqual(got,hashlib.sha256(b'{}').hexdigest())
        self.refuses('source_mode',pilot_io.read_private_file,stub,expect_mode=0o600)
        self.refuses('mode_not_restricted',pilot_io.read_private_file,stub,expect_mode=0o444)

    # ---------- held executable ----------

    def test_hold_executable_measures_the_opened_inode(self):
        path=self.private/'codex';path.write_bytes(fake_cli_elf(CLI_VERSION+'\n'))
        os.chmod(path,0o700)
        fd,measured,info=pilot_io.hold_executable(path)
        try:
            self.assertEqual(measured,hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(info.st_ino,path.stat().st_ino)
            self.assertEqual(pilot_io.alias(fd),'/proc/self/fd/%d'%fd)
        finally:os.close(fd)

    def test_hold_executable_rejects_symlink_special_and_non_elf(self):
        good=self.private/'codex';good.write_bytes(fake_cli_elf('x'));os.chmod(good,0o700)
        link=self.private/'link';os.symlink(good,link)
        self.refuses('codex_symlink',pilot_io.hold_executable,link)
        fifo=self.private/'fifo';os.mkfifo(fifo,0o700)
        self.refuses('codex_not_regular',pilot_io.hold_executable,fifo)
        script=self.private/'script';script.write_bytes(b'#!/bin/sh\nexit 0\n')
        os.chmod(script,0o700)
        self.refuses('codex_not_elf',pilot_io.hold_executable,script)
        plain=self.private/'plain';plain.write_bytes(fake_cli_elf('x'));os.chmod(plain,0o600)
        self.refuses('codex_not_executable',pilot_io.hold_executable,plain)
        setuid=self.private/'setuid';setuid.write_bytes(fake_cli_elf('x'))
        os.chmod(setuid,0o4700)
        self.refuses('codex_setid',pilot_io.hold_executable,setuid)

    def test_held_bytes_execute_after_the_pathname_is_replaced(self):
        """Finding 3, causally: the measured inode is the one that runs."""
        path=self.private/'codex';path.write_bytes(fake_cli_elf(CLI_VERSION+'\n'))
        os.chmod(path,0o700)
        fd,measured,_=pilot_io.hold_executable(path)
        try:
            impostor=self.private/'impostor'
            impostor.write_bytes(fake_cli_elf('codex-cli 9.9.9\n'));os.chmod(impostor,0o700)
            os.rename(impostor,path)
            self.assertNotEqual(hashlib.sha256(path.read_bytes()).hexdigest(),measured)
            # By pathname the impostor now answers; through the held descriptor it cannot.
            byname=subprocess.run([str(path),'--version'],capture_output=True,timeout=30)
            self.assertEqual(byname.stdout.decode().strip(),'codex-cli 9.9.9')
            self.assertEqual(pilot_io.probe_cli_version(pilot_io.alias(fd),pass_fds=(fd,)),
                             CLI_VERSION)
        finally:os.close(fd)

    def test_probe_cli_version_refuses_unparseable_output(self):
        path=self.private/'codex';path.write_bytes(fake_cli_elf('Python 3.13.5\n'))
        os.chmod(path,0o700)
        fd,_,_=pilot_io.hold_executable(path)
        try:
            with self.assertRaises(pilot_io.PilotIoError) as caught:
                pilot_io.probe_cli_version(pilot_io.alias(fd),pass_fds=(fd,))
            self.assertEqual(str(caught.exception),'cli_version_unreadable')
        finally:os.close(fd)


class PilotAnchorTests(unittest.TestCase):
    """`prepare_anchor` is inside the same boundary as the registry it helps publish.

    The anchor is the fourth pin and the final `argv` item, so the bytes that end up in
    `pins` must be proven the same way the registry is: a nofollow dirfd walk to a parent
    that ALREADY exists privately, a no-clobber descriptor-relative publication at exactly
    0400, and a held read of an existing anchor rather than a pathname `read_bytes`.
    """

    def setUp(self):
        self.assertIsNotNone(pilot_registry,'tools/pilot_registry.py missing')
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.root=Path(self.t.name).resolve()
        self.private=self.root/'pilot';self.private.mkdir(mode=0o700)
        self.parent=self.private/'probe-anchor';self.parent.mkdir(mode=0o700)
        self.anchor=self.parent/'anchor.json'
        self.stub=canonical(pilot_registry.PROBE_ANCHOR_STUB)

    def refuses(self,reason,path=None):
        before=tree(self.root)
        with self.assertRaises((pilot_registry.Refusal,pilot_io.PilotIoError)) as caught:
            pilot_registry.prepare_anchor(path or self.anchor)
        self.assertEqual(str(caught.exception),reason)
        self.assertEqual(tree(self.root),before,'a refusal must write nothing')

    def test_publishes_the_stub_at_0400_into_an_existing_private_parent(self):
        got=pilot_registry.prepare_anchor(self.anchor)
        self.assertEqual(got,self.anchor)
        info=self.anchor.lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode),0o400)
        self.assertEqual(info.st_uid,os.getuid())
        self.assertEqual(self.anchor.read_bytes(),self.stub)
        self.assertEqual(pilot_registry.sha256_file(self.anchor),
                         hashlib.sha256(self.stub).hexdigest())

    def test_accepts_an_existing_exact_stub_without_rewriting_it(self):
        pilot_registry.prepare_anchor(self.anchor)
        before=self.anchor.lstat()
        self.assertEqual(pilot_registry.prepare_anchor(self.anchor),self.anchor)
        after=self.anchor.lstat()
        self.assertEqual((after.st_ino,after.st_ctime_ns,stat.S_IMODE(after.st_mode)),
                         (before.st_ino,before.st_ctime_ns,0o400),
                         'an already-correct anchor is read, never rewritten or re-chmodded')
        self.assertEqual(self.anchor.read_bytes(),self.stub)

    def test_refuses_an_existing_stub_at_the_wrong_mode(self):
        self.anchor.write_bytes(self.stub);os.chmod(self.anchor,0o600)
        self.refuses('source_mode')
        os.chmod(self.anchor,0o604)
        self.refuses('source_not_private')

    def test_refuses_a_non_stub_oversize_or_special_anchor(self):
        self.anchor.write_bytes(canonical({'purpose':'something-else'}))
        os.chmod(self.anchor,0o400)
        self.refuses('probe_anchor_not_stub')
        os.chmod(self.anchor,0o600);self.anchor.write_bytes(b'{'+b' '*8192)
        os.chmod(self.anchor,0o400)
        self.refuses('source_too_large')
        os.chmod(self.anchor,0o600);self.anchor.unlink()
        os.mkfifo(self.anchor,0o400)
        self.refuses('source_not_regular')

    def test_refuses_a_symlinked_anchor_at_both_layers(self):
        decoy=self.private/'decoy.json';decoy.write_bytes(self.stub);os.chmod(decoy,0o400)
        os.symlink(decoy,self.anchor)
        # A symlinked name is not the canonical path it claims to be.
        self.refuses('probe_anchor_not_canonical')
        self.anchor.unlink()
        # A self-referential link IS its own canonical path, so path resolution cannot
        # see it. The nofollow open is what refuses -- which is the point of holding a
        # descriptor instead of trusting a pathname.
        os.symlink(self.anchor.name,self.anchor)
        self.refuses('source_symlink')

    def test_refuses_a_missing_parent_and_never_creates_one(self):
        self.parent.rmdir()
        self.refuses('path_component_missing')
        self.assertFalse(self.parent.exists(),'the parent is never created recursively')
        deeper=self.private/'a'/'b'/'anchor.json'
        self.refuses('path_component_missing',deeper)
        self.assertFalse((self.private/'a').exists())

    def test_refuses_a_non_private_parent(self):
        for mode in (0o755,0o750,0o701):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.parent,mode)
                self.refuses('parent_not_private')
        os.chmod(self.parent,0o700)

    def test_refuses_an_ancestor_swapped_after_resolution(self):
        """TOCTOU, causally: the swap lands between path resolution and the dirfd walk."""
        chain=self.private/'a'/'b';chain.mkdir(parents=True,mode=0o700)
        elsewhere=self.root/'elsewhere';elsewhere.mkdir(mode=0o700)
        anchor=chain/'anchor.json'
        real=pilot_registry.outside_repo
        def swap(path,label):
            if not (self.private/'a').is_symlink():
                os.rename(self.private/'a',self.root/'moved')
                os.symlink(elsewhere,self.private/'a')
            return real(path,label)
        pilot_registry.outside_repo=swap
        try:
            # The helper's snapshot cannot be used here: the swap itself changes the tree.
            with self.assertRaises(pilot_io.PilotIoError) as caught:
                pilot_registry.prepare_anchor(anchor)
        finally:pilot_registry.outside_repo=real
        self.assertEqual(str(caught.exception),'path_component_not_directory')
        self.assertTrue((self.private/'a').is_symlink())
        self.assertEqual(sorted(p.name for p in elsewhere.iterdir()),[],
                         'nothing is written into the swapped-in tree')
        self.assertFalse((self.root/'moved'/'b'/'anchor.json').exists(),
                         'nothing is written into the real tree either')

    def test_refuses_an_anchor_inside_the_repo_or_a_codex_package(self):
        self.refuses('probe_anchor_inside_repo',REPO/'anchor.json')
        self.assertFalse((REPO/'anchor.json').exists(),'the repo is never written to')
        self.refuses('probe_anchor_inside_codex_package',
                     self.private/'node_modules'/'@openai'/'anchor.json')
        self.assertFalse((self.private/'node_modules').exists())


class PilotRegistryTests(unittest.TestCase):
    """Findings 1 and 4: exact version binding and evidence-bundle semantic binding."""

    def setUp(self):
        self.assertIsNotNone(pilot_registry,'tools/pilot_registry.py missing')
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.root=Path(self.t.name).resolve()
        self.private=self.root/'pilot';self.private.mkdir(mode=0o700)
        self.codex=self.private/'codex'
        self.codex.write_bytes(fake_cli_elf(CLI_VERSION+'\n'));os.chmod(self.codex,0o700)
        self.python=Path(sys.executable).resolve()
        # The anchor's parent is a precondition the tool refuses to create for itself.
        (self.private/'probe-anchor').mkdir(mode=0o700)
        self.anchor=self.private/'probe-anchor'/'anchor.json'
        self.bundle=self.private/'evidence.json'
        self.bundle.write_bytes(canonical({'thread_start_observation':'ok'}))
        os.chmod(self.bundle,0o600)
        self.out=self.private/'registry.json'
        self.shipped_before=SHIPPED.read_bytes()

    def tearDown(self):
        self.assertEqual(SHIPPED.read_bytes(),self.shipped_before,
                         'the shipped registry is read-only to this tool')

    def invoke(self,*argv):
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream):code=pilot_registry.main(list(argv))
        return code,json.loads(stream.getvalue())

    def base(self,**over):
        args=['--codex',str(self.codex),'--python',str(self.python),
              '--adapter',str(ADAPTER_SOURCE),'--probe-anchor',str(self.anchor),
              '--evidence-bundle',str(over.pop('bundle',self.bundle))]
        for key,value in over.items():args+=['--'+key.replace('_','-'),str(value)]
        return args

    def bindings(self,**over):
        code,body=self.invoke(*self.base(**over),'--bindings-only')
        self.assertEqual(code,0,body)
        return body

    def approval(self,bindings=None,**over):
        bindings=bindings or self.bindings()
        now=datetime.datetime.now(datetime.timezone.utc)
        stamp=lambda delta:(now+delta).isoformat().replace('+00:00','Z')
        record={'schema_version':1,'route_id':ROUTE_ID,
                'purpose':'one bounded read-only pilot turn, no artifact production',
                'harness':'luna','model':'gpt-5.6-luna','effort':'max','mode':'read_only',
                'evidence_kind':'live','scope':'single-run',
                'data_classification':'public-redacted','cli_version':bindings['cli_version'],
                'python_sha256':bindings['python_sha256'],
                'adapter_sha256':bindings['adapter_sha256'],
                'codex_sha256':bindings['codex_sha256'],
                'probe_anchor_path':bindings['probe_anchor_path'],
                'probe_anchor_sha256':bindings['probe_anchor_sha256'],
                'evidence_bundle_sha256':bindings['evidence_bundle_sha256'],
                'adapter_digest':bindings['adapter_digest'],'approved_by':'parent',
                'approved_at':stamp(-datetime.timedelta(minutes=5)),
                'expires_at':stamp(datetime.timedelta(hours=2))}
        for key in [k for k in over if over[k] is DROP]:record.pop(key);over.pop(key)
        record.update(over)
        path=self.private/'approval.json';path.write_bytes(canonical(record))
        os.chmod(path,0o600)
        return path,record

    def write(self,**over):
        path,record=self.approval(**over)
        return self.invoke(*self.base(),'--approval',str(path),'--out',str(self.out)),record

    # ---------- finding 1: exact CLI version binding ----------

    def test_bindings_report_the_probed_version_and_bind_it_into_argv(self):
        body=self.bindings()
        self.assertIs(body['written'],False)
        self.assertEqual(body['cli_version'],CLI_VERSION)
        argv=body['argv']
        self.assertEqual(argv[-4:],[pilot_registry.EXPECTED_VERSION_FLAG,CLI_VERSION,
                                    pilot_registry.PROBE_ANCHOR_FLAG,body['probe_anchor_path']])
        self.assertEqual(body['codex_sha256'],
                         hashlib.sha256(self.codex.read_bytes()).hexdigest())

    def test_adapter_digest_changes_with_the_probed_version(self):
        first=self.bindings()
        self.codex.chmod(0o700)
        self.codex.write_bytes(fake_cli_elf('codex-cli 0.153.5\n'));os.chmod(self.codex,0o700)
        second=self.bindings()
        self.assertEqual(second['cli_version'],'codex-cli 0.153.5')
        self.assertNotEqual(second['adapter_digest'],first['adapter_digest'],
                            'the exact version must be bound into adapter_digest')

    def test_refuses_an_unreadable_cli_version(self):
        self.bindings()      # creates the §4 anchor stub, so the snapshot below is stable
        before=tree(self.private)
        self.codex.write_bytes(fake_cli_elf('Python 3.13.5\n'));os.chmod(self.codex,0o700)
        before['codex']=(hashlib.sha256(self.codex.read_bytes()).hexdigest(),0o700)
        code,body=self.invoke(*self.base(),'--bindings-only')
        self.assertEqual(code,2)
        self.assertEqual(body['reason'],'cli_version_unreadable')
        self.assertEqual(tree(self.private),before)

    def test_refuses_an_approval_whose_cli_version_differs(self):
        before=tree(self.private)
        (code,body),_=self.write(cli_version='codex-cli 0.153.3')
        self.assertEqual(code,2)
        self.assertIn('cli_version',body['reason'])
        self.assertIs(body['written'],False)
        self.assertFalse(self.out.exists())
        self.assertEqual({k:v for k,v in tree(self.private).items() if k.startswith('registry')},
                         {k:v for k,v in before.items() if k.startswith('registry')})

    def test_refuses_an_approval_pinned_to_a_stale_codex_digest(self):
        _,record=self.approval()
        self.codex.write_bytes(fake_cli_elf(CLI_VERSION+'\n')+b'\n');os.chmod(self.codex,0o700)
        path=self.private/'approval.json'
        code,body=self.invoke(*self.base(),'--approval',str(path),'--out',str(self.out))
        self.assertEqual(code,2)
        self.assertIn('codex_sha256',body['reason'])
        self.assertFalse(self.out.exists())

    # ---------- finding 4: evidence bundle semantic binding ----------

    def test_bindings_report_the_evidence_bundle_digest(self):
        body=self.bindings()
        self.assertEqual(body['evidence_bundle_sha256'],
                         hashlib.sha256(self.bundle.read_bytes()).hexdigest())

    def test_evidence_bundle_is_required(self):
        with self.assertRaises(SystemExit):
            self.invoke('--codex',str(self.codex),'--python',str(self.python),
                        '--adapter',str(ADAPTER_SOURCE),'--probe-anchor',str(self.anchor),
                        '--bindings-only')

    def test_refuses_a_stale_evidence_bundle(self):
        path,record=self.approval()
        os.chmod(self.bundle,0o600)
        self.bundle.write_bytes(canonical({'thread_start_observation':'stale'}))
        os.chmod(self.bundle,0o600)
        code,body=self.invoke(*self.base(),'--approval',str(path),'--out',str(self.out))
        self.assertEqual(code,2)
        self.assertIn('evidence_bundle_sha256',body['reason'])
        self.assertFalse(self.out.exists())

    def test_refuses_an_approval_missing_the_evidence_field(self):
        path,_=self.approval(evidence_bundle_sha256=DROP)
        code,body=self.invoke(*self.base(),'--approval',str(path),'--out',str(self.out))
        self.assertEqual(code,2)
        self.assertTrue(body['reason'].startswith('approval_key_set:'),body['reason'])
        self.assertIn('evidence_bundle_sha256',body['reason'])
        self.assertFalse(self.out.exists())

    def test_refuses_a_world_readable_evidence_bundle(self):
        os.chmod(self.bundle,0o644)
        code,body=self.invoke(*self.base(),'--bindings-only')
        self.assertEqual(code,2)
        self.assertEqual(body['reason'],'source_not_private')

    def test_refuses_a_symlinked_evidence_bundle(self):
        link=self.private/'evidence-link.json';os.symlink(self.bundle,link)
        code,body=self.invoke(*self.base(bundle=link),'--bindings-only')
        self.assertEqual(code,2)
        self.assertEqual(body['reason'],'source_symlink')

    # ---------- the written registry ----------

    def test_writes_a_private_registry_bound_to_every_measured_value(self):
        (code,body),record=self.write()
        self.assertEqual(code,0,body)
        self.assertEqual(body['path'],str(self.out))
        self.assertEqual(body['approval_sha256'],digest(record))
        self.assertEqual(body['cli_version'],CLI_VERSION)
        self.assertEqual(body['evidence_bundle_sha256'],record['evidence_bundle_sha256'])
        info=self.out.lstat()
        self.assertEqual(stat.S_IMODE(info.st_mode),0o600)
        self.assertEqual(info.st_uid,os.getuid())
        written=json.loads(self.out.read_bytes())
        route=next(r for r in written['routes'] if r['route_id']==ROUTE_ID)
        argv=route['adapter']['argv']
        self.assertEqual(argv[-4:],[pilot_registry.EXPECTED_VERSION_FLAG,CLI_VERSION,
                                    pilot_registry.PROBE_ANCHOR_FLAG,str(self.anchor)])
        self.assertEqual(len(route['adapter']['pins']),4)
        self.assertEqual(route['adapter']['native_pins'],[str(self.codex)])
        self.assertEqual(route['approval_sha256'],digest(record))
        self.assertEqual(hashlib.sha256(self.out.read_bytes()).hexdigest(),body['sha256'])

    def test_refuses_a_second_write_to_the_same_target(self):
        (code,_),_=self.write()
        self.assertEqual(code,0)
        original=self.out.read_bytes()
        (code,body),_=self.write()
        self.assertEqual(code,2)
        self.assertEqual(body['reason'],'target_exists')
        self.assertEqual(self.out.read_bytes(),original)

    def test_refuses_a_symlinked_out_target(self):
        decoy=self.private/'decoy.json';decoy.write_bytes(b'original');os.chmod(decoy,0o600)
        os.symlink(decoy,self.out)
        path,_=self.approval()
        code,body=self.invoke(*self.base(),'--approval',str(path),'--out',str(self.out))
        self.assertEqual(code,2)
        self.assertEqual(body['reason'],'target_symlink')
        self.assertEqual(decoy.read_bytes(),b'original')

    def test_refuses_a_non_private_out_parent(self):
        loose=self.root/'loose';loose.mkdir(mode=0o755)
        path,_=self.approval()
        code,body=self.invoke(*self.base(),'--approval',str(path),
                              '--out',str(loose/'registry.json'))
        self.assertEqual(code,2)
        self.assertEqual(body['reason'],'parent_not_private')
        self.assertFalse((loose/'registry.json').exists())

    def test_refuses_every_approval_field_mismatch_without_writing(self):
        cases={'route_id':'other_route','approved_by':'self','purpose':'   ',
               'harness':'codex','model':'gpt-5.5','effort':'high','mode':'bounded_write',
               'evidence_kind':'synthetic','scope':'multi-run',
               'data_classification':'private','schema_version':2,
               'python_sha256':'0'*64,'adapter_sha256':'0'*64,
               'probe_anchor_sha256':'0'*64,'adapter_digest':'0'*64,
               'evidence_bundle_sha256':'0'*64,'cli_version':'codex-cli 0.1.0'}
        for field,value in cases.items():
            with self.subTest(field=field):
                out=self.private/('registry-%s.json'%field)
                path,_=self.approval(**{field:value})
                code,body=self.invoke(*self.base(),'--approval',str(path),'--out',str(out))
                self.assertEqual(code,2,body)
                self.assertIn(field,body['reason'])
                self.assertIs(body['written'],False)
                self.assertFalse(out.exists(),'a mismatched field must write nothing')

    def test_refuses_an_expired_or_future_approval(self):
        now=datetime.datetime.now(datetime.timezone.utc)
        stamp=lambda d:(now+d).isoformat().replace('+00:00','Z')
        for field,value in (('expires_at',stamp(-datetime.timedelta(minutes=1))),
                            ('approved_at',stamp(datetime.timedelta(hours=1)))):
            with self.subTest(field=field):
                out=self.private/('registry-%s.json'%field)
                path,_=self.approval(**{field:value})
                code,body=self.invoke(*self.base(),'--approval',str(path),'--out',str(out))
                self.assertEqual(code,2)
                self.assertIn(field,body['reason'])
                self.assertFalse(out.exists())

    def test_refuses_an_unknown_or_null_approval_key(self):
        out=self.private/'registry-extra.json'
        path,_=self.approval(surprise='yes')
        code,body=self.invoke(*self.base(),'--approval',str(path),'--out',str(out))
        self.assertEqual(code,2)
        self.assertIn('surprise',body['reason'])
        path,_=self.approval(purpose=None)
        code,body=self.invoke(*self.base(),'--approval',str(path),'--out',str(out))
        self.assertEqual(code,2)
        self.assertIn('purpose',body['reason'])
        self.assertFalse(out.exists())


class PilotPreflightTests(unittest.TestCase):
    """Finding 3 at the tool boundary: the Codex path is measured, not trusted."""

    def setUp(self):
        self.assertIsNotNone(pilot_preflight,'tools/pilot_preflight.py missing')
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.root=Path(self.t.name).resolve()
        self.private=self.root/'pilot';self.private.mkdir(mode=0o700)
        self.codex=self.private/'codex'
        self.codex.write_bytes(fake_cli_elf(CLI_VERSION+'\n'));os.chmod(self.codex,0o700)
        self.digest=hashlib.sha256(self.codex.read_bytes()).hexdigest()

    def invoke(self,*argv):
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream):code=pilot_preflight.main(list(argv))
        return code,json.loads(stream.getvalue())

    def test_refuses_a_symlinked_special_or_non_elf_codex(self):
        link=self.private/'link';os.symlink(self.codex,link)
        script=self.private/'script';script.write_bytes(b'#!/bin/sh\nexit 0\n')
        os.chmod(script,0o700)
        fifo=self.private/'fifo';os.mkfifo(fifo,0o700)
        for path,reason in ((link,'codex_symlink'),(script,'codex_not_elf'),
                            (fifo,'codex_not_regular')):
            with self.subTest(path=path.name):
                code,body=self.invoke('--codex',str(path))
                self.assertEqual(code,2)
                self.assertEqual(body['reason'],reason)

    def test_refuses_a_codex_digest_that_does_not_match_the_expectation(self):
        code,body=self.invoke('--codex',str(self.codex),'--expected-codex-sha256','0'*64)
        self.assertEqual(code,2)
        self.assertEqual(body['reason'],'codex_digest_mismatch')
        self.assertEqual(body['codex_sha256'],self.digest)

    def test_reports_the_measured_digest_and_the_held_cli_version(self):
        """The version is observed through `/proc/self/fd/<n>`, not through the path."""
        code,body=self.invoke('--codex',str(self.codex),'--expected-codex-sha256',self.digest)
        self.assertEqual(body['codex_sha256'],self.digest)
        self.assertEqual(body['codex_cli_version'],CLI_VERSION)
        # The stub ELF cannot speak JSON-RPC, so the session itself refuses -- which is
        # exactly the point: the measurement and the version already happened.
        self.assertEqual(code,2)
        self.assertEqual(body['status'],'refused')

    def test_validates_the_out_target_before_holding_or_spawning_anything(self):
        existing=self.private/'observation.json'
        existing.write_bytes(b'original');os.chmod(existing,0o600)
        code,body=self.invoke('--codex',str(self.codex),'--out',str(existing))
        self.assertEqual(code,2)
        self.assertEqual(body['reason'],'target_exists')
        self.assertEqual(existing.read_bytes(),b'original')
        loose=self.root/'loose';loose.mkdir(mode=0o755)
        code,body=self.invoke('--codex',str(self.codex),'--out',str(loose/'observation.json'))
        self.assertEqual(code,2)
        self.assertEqual(body['reason'],'parent_not_private')


if __name__=='__main__':unittest.main()
