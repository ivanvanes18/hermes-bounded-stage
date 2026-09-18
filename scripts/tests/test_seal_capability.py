"""Causal controller-runtime regressions, not a binary-size-only substitute.

Each case starts a fresh Python process and removes ALL listed optional fcntl
exports. Successful cases use real memfd/fcntl/exec syscalls. Failure cases make
real seal syscalls fail, or inject incomplete readback, before any child launch.
Numeric test oracles are independent Linux UAPI literals, not production imports.
"""
import json
from pathlib import Path
import subprocess
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
# The code is run by -I -S, so no site/PYTHONPATH injection can mask the case.
PROBE = r'''
import errno, fcntl, hashlib, json, os, pathlib, sys, tempfile
from unittest.mock import patch
scripts, case = sys.argv[1:]
sys.path[:0] = [scripts, str(pathlib.Path(scripts) / 'tests')]
symbols = ('F_ADD_SEALS', 'F_GET_SEALS', 'F_SEAL_SEAL', 'F_SEAL_SHRINK',
           'F_SEAL_GROW', 'F_SEAL_WRITE', 'F_SEAL_FUTURE_WRITE', 'F_SEAL_EXEC')
if case == 'late_absence':
    import harness_adapter as H
for name in symbols:
    fcntl.__dict__.pop(name, None)
import harness_adapter as H
import pinned_runtime as P
import routing_fixtures as F
from stage_contracts import ContractError
assert all(not hasattr(fcntl, name) for name in symbols)
real_fcntl, real_memfd, real_popen = fcntl.fcntl, os.memfd_create, H.subprocess.Popen
# A control probe independently proves the host kernel can seal without symbols.
control_fd = real_memfd('synthetic-control', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
try:
    os.write(control_fd, b'synthetic')
    control_add = real_fcntl(control_fd, 1033, 15)
    control_get = real_fcntl(control_fd, 1034)
finally:
    os.close(control_fd)
assert control_add == 0 and control_get & 15 == 15
created, calls, launches = [], [], []
pipe_read, pipe_write = os.pipe()
with tempfile.TemporaryDirectory() as directory:
    root = pathlib.Path(directory)
    sentinel = root / 'ORIGINAL_CHILD_SIDE_EFFECT'
    script = root / 'adapter.py'
    script.write_text('from pathlib import Path\nPath(' + repr(str(sentinel)) +
                      ').write_text("original")\nprint("ORIGINAL")\n')
    source = executable = pathlib.Path(sys.executable).resolve()
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    source_hash = sha(source)
    fixture = exact_size_control = None
    exact_size_destination = root / 'must-not-be-created'
    if case in ('large_absence', 'layout_negative_control'):
        from runtime_layout_fixture import prepare_large_runtime
        target_bytes = 31_362_304 if case == 'large_absence' else source.stat().st_size + 1
        fixture = prepare_large_runtime(source, root / 'runtime-fixture', target_bytes=target_bytes)
        executable = pathlib.Path(fixture['executable'])
        # Re-select the actually executed target-size object without a new copy.
        exact_size_control = prepare_large_runtime(executable, exact_size_destination,
                                                   target_bytes=target_bytes)
    spec = {'argv': [str(executable), '-I', '-S', str(script)],
            'pins': {str(executable): sha(executable), str(script): sha(script)},
            'timeout_seconds': 10}
    def create(name, flags):
        if case == 'add_denied' or (case == 'script_add_denied' and created):
            # A real memfd without MFD_ALLOW_SEALING starts with F_SEAL_SEAL;
            # the actual F_ADD_SEALS syscall MUST fail with EPERM.
            flags &= ~os.MFD_ALLOW_SEALING
        fd = real_memfd(name, flags)
        created.append(fd)
        return fd
    def call(fd, command, arg=0):
        entry = {'command': command, 'arg': arg}
        calls.append(entry)
        try:
            # Pipe is genuinely not a sealable file; kernel GET returns EINVAL.
            target = pipe_read if case == 'get_denied' and command == 1034 else fd
            value = real_fcntl(target, command, arg)
        except OSError as error:
            entry['errno'] = error.errno
            raise
        entry['kernel_return'] = value
        if case.startswith('incomplete_') and command == 1034:
            value &= ~int(case.split('_')[1])
            entry['injected_return'] = value
        return value
    def launch(*args, **kwargs):
        launches.append(True)
        # Real positive execution must use sealed executable AND script objects.
        fds = kwargs.get('pass_fds', ())
        assert len(fds) == 2, 'both objects must be descriptor bound'
        assert kwargs.get('executable', '').startswith('/proc/self/fd/')
        for fd in fds:
            assert real_fcntl(fd, 1034) & 15 == 15
            try:
                os.pwrite(fd, b'changed', 0)
            except OSError as error:
                assert error.errno == errno.EPERM
            else:
                raise AssertionError('snapshot was mutable at launch')
        return real_popen(*args, **kwargs)
    if case == 'non_linux':
        P.sys.platform = 'darwin'
    if case == 'missing_memfd':
        del os.memfd_create
    outcome = {}
    try:
        with patch.object(H.subprocess, 'Popen', launch), patch.object(fcntl, 'fcntl', call):
            if case == 'missing_memfd':
                try:
                    H.run_command(spec, cwd=root)
                except ContractError as error:
                    outcome = {'error': str(error)}
            else:
                with patch.object(os, 'memfd_create', create):
                    try:
                        if case == 'readiness':
                            stage, registry = F.stage(root), F.registry()
                            F.attach_adapter(root, registry)
                            outcome = {'readiness': H.readiness(stage, registry, evidence_mode='synthetic')['fixture_worker']}
                        else:
                            result = H.run_command(spec, cwd=root)
                            outcome = {'exit_code': result['exit_code'], 'stdout': result['stdout'].decode()}
                    except ContractError as error:
                        outcome = {'error': str(error)}
    finally:
        os.close(pipe_read); os.close(pipe_write)
    closed = []
    for fd in created:
        try:
            os.fstat(fd)
        except OSError as error:
            closed.append(error.errno == errno.EBADF)
        else:
            closed.append(False)
    print(json.dumps({'case': case, 'missing_symbols': [n for n in symbols if not hasattr(fcntl, n)],
                      'kernel_control': {'add': control_add, 'get': control_get},
                      'executable_bytes': executable.stat().st_size, 'outcome': outcome,
                      'controller_executable': str(source), 'controller_bytes': source.stat().st_size,
                      'fixture': fixture, 'source_unchanged': sha(source) == source_hash,
                      'exact_size_control': exact_size_control,
                      'exact_size_control_destination_created': exact_size_destination.exists(),
                      'fixture_layout_present': bool(fixture and fixture['layout_root'] and
                                                      pathlib.Path(fixture['layout_root']).is_dir()),
                      'seal_calls': calls, 'child_launches': len(launches),
                      'child_sentinel': sentinel.exists(), 'created_descriptors': len(created),
                      'all_descriptors_closed': all(closed)}, sort_keys=True))
'''


def run_case(case):
    command = [sys.executable, '-I', '-S', '-B', '-c', PROBE, str(SCRIPTS), case]
    process = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if process.returncode:
        raise AssertionError(f'probe {case}: exit={process.returncode}\n{process.stdout}\n{process.stderr}')
    return json.loads(process.stdout)


class SealCapabilityTests(unittest.TestCase):
    def probe(self, case):
        result = run_case(case)
        self.assertEqual(len(result['missing_symbols']), 8)
        self.assertEqual(result['kernel_control'], {'add': 0, 'get': 15})
        self.assertTrue(result['all_descriptors_closed'])
        return result

    def assert_success(self, case):
        result = self.probe(case)
        self.assertEqual(result['outcome'], {'exit_code': 0, 'stdout': 'ORIGINAL\n'})
        self.assertEqual(result['child_launches'], 1)
        self.assertTrue(result['child_sentinel'])
        self.assertEqual(result['created_descriptors'], 2)
        self.assertTrue(any(c['command'] == 1033 and c['arg'] == 15 for c in result['seal_calls']))
        self.assertTrue(any(c['command'] == 1034 and c['kernel_return'] & 15 == 15 for c in result['seal_calls']))
        return result

    def assert_unavailable(self, case):
        result = self.probe(case)
        self.assertEqual(result['outcome'], {'error': 'sealed_execution_unavailable'})
        self.assertEqual(result['child_launches'], 0)
        self.assertFalse(result['child_sentinel'])
        return result

    def test_all_optional_exports_absent_full_pinned_execution(self):
        self.assert_success('absence')

    def test_large_executable_and_missing_controller_exports_together(self):
        # Extend the inherited method: neither remove tests nor disguise skips.
        result = self.assert_success('large_absence')
        self.assertEqual(result['executable_bytes'], 31_362_304)
        self.assertIn('fixture', result, 'the executed fixture mode must be recorded')
        fixture = result['fixture']
        expected_mode = ('actual_runtime_already_large' if fixture['source_bytes'] == 31_362_304
                         else 'synthetic_padded_runtime_layout')
        self.assertEqual(fixture['mode'], expected_mode)
        self.assertEqual(fixture['target_bytes'], 31_362_304)
        self.assertEqual(fixture['copied_binary'], expected_mode != 'actual_runtime_already_large')
        self.assertEqual(fixture['executable'] == fixture['source_executable'],
                         expected_mode == 'actual_runtime_already_large')
        self.assertTrue(result['source_unchanged'])
        # Re-select the actually executed target-sized object: no second copy,
        # not even a destination directory. This is a branch control, not a
        # statement that a synthetic object's provenance became a real host.
        direct = result['exact_size_control']
        self.assertEqual(direct['mode'], 'actual_runtime_already_large')
        self.assertEqual(direct['executable'], fixture['executable'])
        self.assertFalse(direct['copied_binary'])
        self.assertFalse(result['exact_size_control_destination_created'])
        # One byte above this controller's real size MUST use the other branch,
        # preserve layout, and execute. Its size is NOT target-size evidence.
        negative = self.assert_success('layout_negative_control')
        self.assertTrue(negative['source_unchanged'])
        control = negative['fixture']
        self.assertEqual(control['mode'], 'synthetic_padded_runtime_layout')
        self.assertTrue(control['copied_binary'])
        self.assertNotEqual(control['executable'], control['source_executable'])
        self.assertEqual(control['executable_bytes'], control['source_bytes'] + 1)
        self.assertEqual(control['target_bytes'], control['source_bytes'] + 1)
        self.assertGreater(control['linked_sibling_count'], 0)
        self.assertTrue(negative['fixture_layout_present'])

    def test_exports_removed_after_import_still_executes(self):
        self.assert_success('late_absence')

    def test_readiness_uses_the_same_symbol_independent_binding(self):
        result = self.probe('readiness')
        self.assertNotIn('error', result['outcome'])
        self.assertTrue(result['outcome']['readiness']['ready'])
        self.assertEqual(result['child_launches'], 1)

    def test_real_add_seals_eperm_is_unavailable_before_any_child(self):
        result = self.assert_unavailable('add_denied')
        self.assertTrue(any(c['command'] == 1033 and c.get('errno') == 1 for c in result['seal_calls']))

    def test_real_get_seals_einval_is_unavailable_before_any_child(self):
        result = self.assert_unavailable('get_denied')
        self.assertTrue(any(c['command'] == 1034 and c.get('errno') == 22 for c in result['seal_calls']))

    def test_second_object_sealing_failure_closes_both_and_never_launches(self):
        result = self.assert_unavailable('script_add_denied')
        self.assertEqual(result['created_descriptors'], 2)
        self.assertTrue(any(c['command'] == 1033 and c.get('errno') == 1 for c in result['seal_calls']))

    def test_each_missing_required_seal_bit_is_unavailable(self):
        for bit in (1, 2, 4, 8):
            with self.subTest(bit=bit):
                result = self.assert_unavailable('incomplete_' + str(bit))
                self.assertTrue(any(c.get('injected_return') == 15 & ~bit for c in result['seal_calls']))

    def test_non_linux_never_attempts_sealing_or_launch(self):
        result = self.assert_unavailable('non_linux')
        self.assertEqual(result['created_descriptors'], 0)
        self.assertEqual(result['seal_calls'], [])

    def test_missing_memfd_fails_closed(self):
        result = self.assert_unavailable('missing_memfd')
        self.assertEqual(result['created_descriptors'], 0)
        self.assertEqual(result['seal_calls'], [])


if __name__ == '__main__':
    unittest.main()
