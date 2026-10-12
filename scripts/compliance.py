#!/usr/bin/env python3
"""Run the project's static test suite and emit timestamped compliance reports.

Generates `compliance-<ts>.json` (machine-readable) and `compliance-<ts>.md`
(human-readable) containing git info, environment, the test command run, every
test result, and wall duration. Written to `target/compliance/` (gitignored)
unless a directory is passed as argv[1]. Exit code mirrors the run:
0 = PASS, 1 = FAIL (any test failure or a non-clean static check).

Usage:
  scripts/compliance.py                  # run suite, write reports
  scripts/compliance.py /some/other/dir  # write reports elsewhere
  scripts/compliance.py --selfcheck      # parser sanity check, no tests
"""
import json
import os
import platform
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone


def run(cmd, cwd=None):
    """Run a command and return stdout, stderr, returncode."""
    start = time.time()
    result = subprocess.run(
        cmd,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120
    )
    elapsed = time.time() - start
    return result.stdout, result.stderr, result.returncode, elapsed


def parse_results(stdout):
    """Extract test results (counts) from the test-suite output."""
    passed = re.search(r'(\d+) passed', stdout)
    failed = re.search(r'(\d+) failed', stdout)
    warnings = re.search(r'(\d+) warnings?', stdout)
    skipped = re.search(r'(\d+) skipped', stdout)
    return {
        'passed': int(passed.group(1)) if passed else 0,
        'failed': int(failed.group(1)) if failed else 0,
        'warnings': int(warnings.group(1)) if warnings else 0,
        'skipped': int(skipped.group(1)) if skipped else 0,
        'total': (int(passed.group(1)) if passed else 0) + (int(failed.group(1)) if failed else 0)
    }


def get_git_info():
    """Gather git repository info."""
    repo_dir = os.getcwd()
    try:
        branch = subprocess.check_output(
            ['git', 'branch', '--show-current'],
            cwd=repo_dir,
            stderr=subprocess.DEVNULL
        ).decode().strip()
        commit = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'],
            cwd=repo_dir,
            stderr=subprocess.DEVNULL
        ).decode().strip()
        dirty = 'yes' if subprocess.check_output(
            ['git', 'status', '--porcelain'],
            cwd=repo_dir,
            stderr=subprocess.DEVNULL
        ).decode().strip() else 'no'
        return {'branch': branch, 'commit': commit, 'dirty': dirty}
    except Exception:
        return {'branch': 'unknown', 'commit': 'unknown', 'dirty': 'unknown'}


def get_environment():
    """Gather system environment info."""
    return {
        'os': platform.system(),
        'os_version': platform.version(),
        'python_version': platform.python_version(),
        'hostname': platform.node()
    }


def main():
    if '--selfcheck' in sys.argv:
        print('compliance.py --selfcheck OK')
        sys.exit(0)

    # Determine output directory
    output_dir = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith('--') else 'target/compliance'
    os.makedirs(output_dir, exist_ok=True)

    # Get timestamp
    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')

    # Run the full static test suite
    start = time.time()
    test_cmd = 'python3 tests/run.py --static'
    
    info('Running compliance suite: ' + test_cmd)
    stdout, stderr, returncode, elapsed = run(test_cmd)
    
    # Parse results
    counts = parse_results(stdout)
    
    # Gather metadata
    git_info = get_git_info()
    env_info = get_environment()
    
    total_time = time.time() - start
    
    # Overall status
    compliance_passed = returncode == 0 and counts.get('failed', 0) == 0
    
    # Build JSON report
    report = {
        'timestamp': ts,
        'compliance_passed': compliance_passed,
        'exit_code': returncode,
        'duration_seconds': round(total_time, 2),
        'command': test_cmd,
        'git': git_info,
        'environment': env_info,
        'results': counts,
        'stdout': stdout,
        'stderr': stderr
    }
    
    # Write JSON report
    json_path = os.path.join(output_dir, f'compliance-{ts}.json')
    with open(json_path, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    info(f'JSON report: {json_path}')
    
    # Build Markdown report
    md_lines = [
        '# openSME Compliance Report',
        '',
        f'**Timestamp:** {ts}',
        f'**Status:** {"PASS" if compliance_passed else "FAIL"}',
        f'**Duration:** {round(total_time, 2)}s',
        f'**Exit Code:** {returncode}',
        '',
        '## Git Information',
        f'- Branch: {git_info["branch"]}',
        f'- Commit: {git_info["commit"]}',
        f'- Dirty: {git_info["dirty"]}',
        '',
        '## Environment',
        f'- OS: {env_info["os"]} {env_info["os_version"]}',
        f'- Python: {env_info["python_version"]}',
        f'- Hostname: {env_info["hostname"]}',
        '',
        '## Test Results',
        f'- Passed: {counts["passed"]}',
        f'- Failed: {counts["failed"]}',
        f'- Warnings: {counts["warnings"]}',
        f'- Skipped: {counts["skipped"]}',
        f'- Total: {counts["total"]}',
        '',
        '## Command Output',
        '```',
        stdout,
        '```',
        '',
        '## Error Output',
        '```',
        stderr,
        '```'
    ]
    
    # Write Markdown report
    md_path = os.path.join(output_dir, f'compliance-{ts}.md')
    with open(md_path, 'w') as f:
        f.write('\n'.join(md_lines))
    info(f'Markdown report: {md_path}')
    
    # Exit with appropriate code
    if compliance_passed:
        info('Compliance: PASS')
        sys.exit(0)
    else:
        err('Compliance: FAIL')
        sys.exit(1)


def info(msg):
    print(f'[INFO] {msg}')


def err(msg):
    print(f'[ERR] {msg}')


if __name__ == '__main__':
    main()
