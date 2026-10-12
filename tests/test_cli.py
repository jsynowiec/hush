import os
import random
import re
import signal
import string
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hush import cli


class Run:
    def __init__(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self.tmp_path = tmp_path
        self._capsys = capsys

    def __call__(self, *args: str, child: str | list[str]) -> tuple[int, str]:
        command = ["--", sys.executable, "-c", child] if isinstance(child, str) else ["--", *child]
        code = cli.main(["--tool", "demo", "--log-dir", str(self.log_dir), *args, *command])
        return code, self._capsys.readouterr().out

    @property
    def log_dir(self) -> Path:
        return self.tmp_path / "logs"

    def log(self, name: str = "demo-smoke.log") -> str:
        return (self.log_dir / name).read_text()


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Run:
    monkeypatch.chdir(tmp_path)
    return Run(tmp_path, capsys)


def test_success_prints_one_line_and_hides_child_output(run: Run):
    code, out = run("--name", "Smoke", child="print('noisy build chatter\\n' * 100)")
    assert code == 0
    assert re.fullmatch(r"  ✔ PASS \(demo\) Smoke passed; \d+\.\ds\n", out)


def test_success_reports_the_passed_count(run: Run):
    _, out = run("--name", "Smoke", child="print('=== 12 passed in 1.00s ===')")
    assert "passed (12 tests);" in out


def test_color_always_marks_a_pass_green_and_a_failure_red(run: Run):
    _, passed = run("--color", "always", child="print('ok')")
    assert "\x1b[32m✔ PASS\x1b[0m (demo)" in passed
    (run.tmp_path / "app.py").write_text("x = 1\n")
    _, failed = run(
        "--color", "always",
        child="import sys; print('app.py:1: something broke here'); sys.exit(3)",
    )  # fmt: skip
    assert "\x1b[31m✗ FAIL\x1b[0m (demo)" in failed
    assert "\x1b[" not in run.log("demo.log")


def test_colour_is_off_when_not_a_terminal_unless_asked_for(run: Run):
    _, auto = run(child="print('ok')")
    _, never = run("--color", "never", child="print('ok')")
    assert "\x1b[" not in auto
    assert "\x1b[" not in never


def test_failure_reports_location_reason_and_log_path(run: Run):
    (run.tmp_path / "app.py").write_text("x = 1\n")
    code, out = run(
        "--name", "Smoke",
        child="import sys; print('app.py:1: something broke here'); sys.exit(3)",
    )  # fmt: skip
    assert code == 3
    assert "✗ FAIL (demo) Smoke failed;" in out
    assert "app.py:1" in out
    assert "something broke here" in out
    assert "Details: logs/demo-smoke.log" in out


def test_failure_without_references_shows_the_output_tail(run: Run):
    code, out = run(
        "--name", "Smoke",
        child="import sys; print('first'); print('the real cause'); sys.exit(1)",
    )  # fmt: skip
    assert code == 1
    assert "the real cause" in out


def test_log_keeps_the_full_output_without_colour(run: Run):
    run(
        "--name",
        "Smoke",
        child="import sys; print('\\x1b[31mred\\x1b[0m line'); sys.exit(1)",
    )
    log = run.log()
    assert "red line" in log
    assert "\x1b" not in log
    assert "exit code: 1" in log


def test_secret_env_values_are_redacted_in_report_and_log(run: Run, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEMO_API_TOKEN", "s3cr3t-value-12345")
    child = "import os, sys; print('leaked', os.environ['DEMO_API_TOKEN']); sys.exit(1)"
    _, out = run("--name", "Smoke", child=child)
    assert "s3cr3t-value-12345" not in out
    assert "s3cr3t-value-12345" not in run.log()
    assert "[REDACTED]" in run.log()


def test_the_command_line_in_the_log_is_redacted_too(run: Run, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEMO_PASSWORD", "hunter2-hunter2")
    run(
        "--name",
        "Smoke",
        child="import sys; print('x', 'hunter2-hunter2'); sys.exit(1)",
    )
    assert "hunter2-hunter2" not in run.log()


def test_timeout_kills_the_command_and_exits_124(run: Run):
    started = time.monotonic()
    code, out = run("--name", "Smoke", "--timeout", "0.5", child="import time; time.sleep(30)")
    assert code == 124
    assert "timed out" in out
    assert time.monotonic() - started < 10


def test_timeout_kills_grandchildren_too(run: Run):
    pid_file = run.tmp_path / "grandchild.pid"
    grandchild = (
        f"import os, pathlib, time; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(60)"
    )
    child = f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {grandchild!r}])\ntime.sleep(60)"
    code, _ = run("--name", "Smoke", "--timeout", "1", child=child)
    assert code == 124
    assert not _is_alive(int(pid_file.read_text()))


def test_missing_binary_exits_127_with_a_readable_message(run: Run):
    code, out = run("--name", "Smoke", child=["definitely-not-a-real-binary-xyz", "--flag"])
    assert code == 127
    assert "definitely-not-a-real-binary-xyz: No such file or directory" in out
    assert "\n\n\n" not in out


def test_stdin_is_closed_so_prompts_cannot_hang(run: Run):
    code, _ = run(
        "--name",
        "Smoke",
        "--timeout",
        "10",
        child="import sys; sys.exit(sys.stdin.read() != '')",
    )
    assert code == 0


def test_child_runs_without_colour_and_without_the_ci_flag(run: Run, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CI", raising=False)
    child = (
        "import os, sys\n"
        "seen = (os.environ['NO_COLOR'], os.environ['FORCE_COLOR'], 'CI' in os.environ)\n"
        "sys.exit(0 if seen == ('1', '0', False) else 1)"
    )
    code, _ = run("--name", "Smoke", child=child)
    assert code == 0


def test_long_output_keeps_the_start_and_the_end_and_says_so(run: Run):
    (run.tmp_path / "app.py").write_text("")
    child = (
        "import sys\n"
        "print('first line')\n"
        "for i in range(40000): print('progress line', i, 'x' * 40)\n"
        "print('app.py:1: the real failure is here')\n"
        "sys.exit(5)"
    )
    code, out = run("--name", "Smoke", "--max-capture-mb", "1", child=child)
    log = run.log()
    assert code == 5
    assert "app.py:1" in out
    assert "the real failure is here" in out
    assert "The output was very long" in out
    assert "first line" in log
    assert "bytes omitted" in log
    assert len(log.encode()) < 1.3 * 1024 * 1024


def test_failures_beyond_the_limit_are_counted_not_listed(run: Run):
    for i in range(1, 13):
        (run.tmp_path / f"f{i}.py").write_text("")
    child = "import sys\nfor i in range(1, 13): print(f'f{i}.py:1: problem number {i} here')\nsys.exit(1)"
    _, out = run("--name", "Smoke", "--max-failures", "10", child=child)
    assert "f10.py:1" in out
    assert "f11.py:1" not in out
    assert "... and 2 more (see log)" in out


def test_verbose_streams_raw_output_to_stderr_but_stdout_stays_curated(run: Run, capsys: pytest.CaptureFixture[str]):
    code = cli.main(
        [
            "--tool",
            "demo",
            "--verbose",
            "--log-dir",
            str(run.log_dir),
            "--",
            sys.executable,
            "-c",
            "print('raw chatter')",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "raw chatter" in captured.err
    assert "raw chatter" not in captured.out


def test_absolute_paths_under_the_working_directory_are_shortened(run: Run):
    (run.tmp_path / "app.py").write_text("")
    child = f"import sys; print('{run.tmp_path.resolve()}/app.py:1: broke in a file'); sys.exit(1)"
    _, out = run("--name", "Smoke", child=child)
    assert "app.py:1" in out
    assert str(run.tmp_path.resolve()) not in out


TWO_FAILING_TESTS = """
def test_first():
    assert 1 == 2, "first broke"

def test_second():
    assert 3 == 4, "second broke"
"""


def test_failfast_stops_pytest_at_the_first_failure(run: Run):
    (run.tmp_path / "test_two.py").write_text(TWO_FAILING_TESTS)
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"]
    _, with_failfast = run("--name", "Unit tests", "--failfast", child=command)
    _, without = run("--name", "Unit tests", child=command)
    assert "first broke" in with_failfast
    assert "second broke" not in with_failfast
    assert "first broke" in without
    assert "second broke" in without


def test_cwd_runs_the_command_there_and_reports_paths_from_the_caller(run: Run):
    module = run.tmp_path / "module-a"
    module.mkdir()
    (module / "app.py").write_text("")
    child = "import os, sys; print('app.py:1: broke in', os.getcwd()); sys.exit(1)"
    _, out = run("--name", "Smoke", "--cwd", "module-a", child=child)
    assert "module-a/app.py:1" in out
    assert "Details: logs/module-a-demo-smoke.log" in out
    assert (run.log_dir / "module-a-demo-smoke.log").is_file()


def test_cwd_must_be_a_directory(run: Run):
    with pytest.raises(SystemExit) as exit_info:
        run("--cwd", "missing-dir", child="pass")
    assert exit_info.value.code == 2


def test_files_are_only_reported_when_they_exist_in_the_cwd(run: Run):
    (run.tmp_path / "module-a").mkdir()
    (run.tmp_path / "app.py").write_text("")
    child = "import sys; print('app.py:1: exists only in the parent dir'); sys.exit(1)"
    _, out = run("--name", "Smoke", "--cwd", "module-a", child=child)
    assert "module-a/app.py" not in out
    assert "exists only in the parent dir" in out


def test_failure_with_no_output_says_so(run: Run):
    code, out = run("--name", "Smoke", child="raise SystemExit(3)")
    assert code == 3
    assert "The command printed nothing (exit code 3)." in out


def test_unwritable_log_dir_is_reported_not_fatal(run: Run):
    (run.tmp_path / "blocker").write_text("")
    code = cli.main(
        ["--tool", "demo", "--log-dir", str(run.tmp_path / "blocker/logs"), "--", sys.executable]
        + ["-c", "raise SystemExit(1)"]
    )
    assert code == 1
    assert "Details: not written" in run._capsys.readouterr().out


@pytest.mark.parametrize(
    "option",
    [
        ["--timeout", "0"],
        ["--max-capture-mb", "0"],
        ["--max-capture-mb", "-1"],
        ["--max-failures", "0"],
    ],
)
def test_nonsensical_numbers_are_rejected(run: Run, option: list[str]):
    with pytest.raises(SystemExit) as exit_info:
        run(*option, child="pass")
    assert exit_info.value.code == 2


def test_a_very_long_name_still_gets_a_valid_log_file(run: Run):
    code, _ = run("--name", "n" * 300, child="raise SystemExit(1)")
    names = [path.name for path in run.log_dir.iterdir()]
    assert code == 1
    assert len(names) == 1
    assert len(names[0]) < 120
    assert names[0].endswith(".log")


def test_log_is_private_and_a_planted_symlink_is_replaced_not_followed(run: Run):
    run.log_dir.mkdir()
    victim = run.tmp_path / "victim.txt"
    victim.write_text("ORIGINAL")
    (run.log_dir / "demo-smoke.log").symlink_to(victim)
    run("--name", "Smoke", child="raise SystemExit(1)")
    log = run.log_dir / "demo-smoke.log"
    assert victim.read_text() == "ORIGINAL"
    assert not log.is_symlink()
    assert (log.stat().st_mode & 0o777) == 0o600
    assert list(run.log_dir.glob(".hush-*")) == []


def test_a_secret_that_is_also_part_of_a_file_name_does_not_hide_the_location(
    run: Run, monkeypatch: pytest.MonkeyPatch
):
    (run.tmp_path / "tests").mkdir()
    (run.tmp_path / "tests/test_postgres.py").write_text("")
    monkeypatch.setenv("POSTGRES_PASSWORD", "postgres")
    child = "print('tests/test_postgres.py:3: the password is postgres'); raise SystemExit(1)"
    _, out = run("--name", "Smoke", child=child)
    assert "tests/test_postgres.py:3" in out
    assert "the password is [REDACTED]" in out
    assert "is postgres" not in out


def test_a_multi_line_secret_env_value_is_masked_in_the_report(run: Run, monkeypatch: pytest.MonkeyPatch):
    (run.tmp_path / "app.py").write_text("")
    monkeypatch.setenv("MY_PRIVATE_KEY", "firstsecretline1\nsecondsecretline2")
    child = "import os, sys\nprint('app.py:1')\nprint(os.environ['MY_PRIVATE_KEY'])\nsys.exit(1)"
    _, out = run("--name", "Smoke", child=child)
    assert "app.py:1" in out
    assert "[REDACTED]" in out
    assert "firstsecretline1" not in out
    assert "secondsecretline2" not in out
    assert "secretline" not in run.log()


def test_a_private_key_block_after_a_short_location_line_is_masked_in_the_report(run: Run):
    (run.tmp_path / "app.py").write_text("")
    child = (
        "import sys\n"
        "print('app.py:1')\n"
        "print('bad key: -----BEGIN RSA PRIVATE KEY-----')\n"
        "print('MIIEowIBAAKCAQEAfirstbodyline')\n"
        "print('qrstuvwxyz0123456789secondbodyline')\n"
        "print('-----END RSA PRIVATE KEY-----')\n"
        "sys.exit(1)"
    )
    _, out = run("--name", "Smoke", child=child)
    assert "app.py:1" in out
    assert "bad key: [REDACTED]" in out
    assert "firstbodyline" not in out
    assert "secondbodyline" not in out
    assert "PRIVATE KEY" not in out


def test_an_unterminated_key_header_does_not_hide_the_output_after_it(run: Run):
    (run.tmp_path / "app.py").write_text("")
    child = (
        "import sys\n"
        "print('-----BEGIN PRIVATE KEY-----')\n"
        "print('app.py:1: the real failure after the header')\n"
        "sys.exit(1)"
    )
    _, out = run("--name", "Smoke", child=child)
    assert "app.py:1" in out
    assert "the real failure after the header" in out
    assert "the real failure after the header" in run.log()
    assert "BEGIN PRIVATE KEY" not in run.log()


def test_secrets_in_a_test_id_are_masked_in_the_location_but_the_file_path_stays(
    run: Run, monkeypatch: pytest.MonkeyPatch
):
    (run.tmp_path / "tests").mkdir()
    (run.tmp_path / "tests/test_auth.py").write_text(
        "import os\n"
        "import pytest\n"
        "\n"
        "@pytest.mark.parametrize('token', ['ghp_' + 'a' * 36, os.environ['APP_PASSWORD']])\n"
        "def test_login(token):\n"
        "    assert False, 'login rejected'\n"
    )
    monkeypatch.setenv("APP_PASSWORD", "plain-env-secret-value")
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "tests"]
    _, out = run("--name", "Smoke", "--tool", "pytest", child=command)
    assert "tests/test_auth.py::test_login[[REDACTED]]" in out
    assert "ghp_" not in out
    assert "plain-env-secret-value" not in out


def test_verbose_output_is_cleaned_and_redacted_too(
    run: Run, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("DEMO_API_TOKEN", "s3cr3t-value-12345")
    child = "import os; print('\\x1b]0;evil\\x07token', os.environ['DEMO_API_TOKEN'])"
    code = cli.main(
        ["--tool", "demo", "--verbose", "--log-dir", str(run.log_dir), "--"] + [sys.executable, "-c", child]
    )
    err = capsys.readouterr().err
    assert code == 0
    assert "token [REDACTED]" in err
    assert "\x1b" not in err
    assert "s3cr3t" not in err


def test_only_the_working_directory_prefix_is_removed_from_absolute_paths(
    run: Run,
):
    module = run.tmp_path / "module-a"
    (module / "src").mkdir(parents=True)
    (module / "src/x.ts").write_text("")
    inside = f"{module.resolve()}/src/x.ts:1: bad here now"
    other = "packages/module-a/src/x.ts:2: other file here"
    child = f"print('{inside}'); print('{other}'); raise SystemExit(1)"
    _, out = run("--name", "Smoke", "--cwd", "module-a", child=child)
    assert "module-a/src/x.ts:1" in out
    assert "packages/module-a/src/x.ts:2" in run.log("module-a-demo-smoke.log")


def test_paths_outside_the_working_directory_are_not_reported_as_locations(run: Run):
    outside = run.tmp_path / "outside.ts"
    outside.write_text("")
    (run.tmp_path / "sub").mkdir()
    child = f"print('{outside}(1,1): error TS2322: outside'); raise SystemExit(1)"
    code = cli.main(
        ["--tool", "tsc", "--cwd", "sub", "--log-dir", str(run.log_dir), "--"] + [sys.executable, "-c", child]
    )
    out = run._capsys.readouterr().out
    assert code == 1
    assert "outside.ts:1" not in out.split("Reason:")[0]


def test_a_path_that_escapes_the_repository_is_not_a_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "inside.py").write_text("")
    (tmp_path / "outside.py").write_text("")
    monkeypatch.chdir(repo)
    exists = cli._exists_in(repo)
    assert exists("inside.py")
    assert not exists("../outside.py")
    assert not exists("sub/../../outside.py")


def test_a_sibling_module_is_reachable_from_cwd_because_the_limit_is_the_start_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "module-a").mkdir()
    (tmp_path / "module-b").mkdir()
    (tmp_path / "module-b/x.ts").write_text("")
    monkeypatch.chdir(tmp_path)
    assert cli._exists_in(Path("module-a"))("../module-b/x.ts")


def test_a_symlink_inside_the_repository_that_points_outside_is_not_a_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "secret.txt").write_text("")
    (repo / "link.txt").symlink_to(tmp_path / "secret.txt")
    monkeypatch.chdir(repo)
    assert not cli._exists_in(repo)("link.txt")


def test_a_parent_directory_path_is_not_reported_even_after_the_working_directory_is_stripped(
    run: Run, monkeypatch: pytest.MonkeyPatch
):
    repo = run.tmp_path.resolve() / "repo"
    repo.mkdir()
    (run.tmp_path / "outside.py").write_text("")
    monkeypatch.chdir(repo)
    child = f"print('{repo}/../outside.py:3: oops outside'); raise SystemExit(1)"
    code, out = run("--name", "Smoke", child=child)
    assert code == 1
    assert "    ../outside.py:3\n" not in out
    assert "../outside.py:3: oops outside" in out


def _start_hush(tmp_path: Path, child: str, *extra: str) -> subprocess.Popen[str]:
    env = {**os.environ, "DEMO_API_TOKEN": "supersecretvalue12345"}
    command = [sys.executable, "-m", "hush", "--tool", "demo"]
    command += ["--log-dir", str(tmp_path / "logs"), *extra, "--", sys.executable, "-c", child]
    return subprocess.Popen(
        command,
        cwd=tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for(path: Path, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.05)


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize(("sig", "exit_code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_interrupting_hush_stops_the_command_and_leaves_only_a_redacted_log(
    tmp_path: Path, sig: signal.Signals, exit_code: int
):
    child = (
        "import os, time\n"
        "print('token', os.environ['DEMO_API_TOKEN'], flush=True)\n"
        "open('child.pid', 'w').write(str(os.getpid()))\n"
        "time.sleep(60)"
    )
    proc = _start_hush(tmp_path, child, "--name", "Smoke", "--timeout", "120")
    _wait_for(tmp_path / "child.pid")
    child_pid = int((tmp_path / "child.pid").read_text())
    started = time.monotonic()
    proc.send_signal(sig)
    out, err = proc.communicate(timeout=15)
    deadline = time.monotonic() + 5
    while _is_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    log = (tmp_path / "logs/demo-smoke.log").read_text()
    assert proc.returncode == exit_code
    assert time.monotonic() - started < 8
    assert "interrupted" in out
    assert "Traceback" not in err
    assert not _is_alive(child_pid)
    assert "supersecretvalue12345" not in log
    assert "token [REDACTED]" in log


# Writes a file in one step, so a test never reads a half-written PID.
PUBLISH = """
import os
def publish(name, text):
    with open(name + '.tmp', 'w') as f:
        f.write(text)
    os.replace(name + '.tmp', name)
"""
STUBBORN_CHILD = (
    PUBLISH
    + """
import signal, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, lambda *_: publish('term-received', ''))
publish('child.pid', str(os.getpid()))
time.sleep(30)
"""
)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (signal.SIGINT, signal.SIGINT),
        (signal.SIGINT, signal.SIGTERM),
        (signal.SIGTERM, signal.SIGINT),
    ],
)
def test_a_second_signal_kills_a_command_that_ignores_the_first_and_still_reports(
    tmp_path: Path, first: signal.Signals, second: signal.Signals
):
    proc = _start_hush(tmp_path, STUBBORN_CHILD, "--name", "Smoke", "--timeout", "120")
    _wait_for(tmp_path / "child.pid")
    child_pid = int((tmp_path / "child.pid").read_text())
    proc.send_signal(first)
    _wait_for(tmp_path / "term-received")
    second_sent = time.monotonic()
    proc.send_signal(second)
    out, err = proc.communicate(timeout=15)
    assert proc.returncode == 128 + first
    assert time.monotonic() - second_sent < 1.5
    assert "interrupted" in out
    assert "Traceback" not in err
    assert "interrupted by signal" in (tmp_path / "logs/demo-smoke.log").read_text()
    assert not _is_alive(child_pid)


def test_a_signal_while_waiting_for_output_to_drain_kills_the_group_and_still_reports(
    tmp_path: Path,
):
    child = (
        PUBLISH
        + """
import subprocess, sys
grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
publish('grandchild.pid', str(grandchild.pid))
publish('child.pid', str(os.getpid()))
"""
    )
    # A long drain wait, so that the signal reliably lands inside it.
    code = "import sys\nfrom hush import cli, runner\nrunner.DRAIN_GRACE_SECONDS = 30\nsys.exit(cli.main(sys.argv[1:]))"
    command = [sys.executable, "-c", code, "--tool", "demo", "--log-dir", str(tmp_path / "logs")]
    command += ["--name", "Smoke", "--", sys.executable, "-c", child]
    proc = subprocess.Popen(command, cwd=tmp_path, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _wait_for(tmp_path / "child.pid")
    child_pid = int((tmp_path / "child.pid").read_text())
    grandchild_pid = int((tmp_path / "grandchild.pid").read_text())
    deadline = time.monotonic() + 10
    while _is_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    proc.send_signal(signal.SIGINT)
    out, err = proc.communicate(timeout=15)
    assert proc.returncode == 130
    assert "interrupted" in out
    assert "Traceback" not in err
    assert (tmp_path / "logs/demo-smoke.log").is_file()
    assert not _is_alive(grandchild_pid)


def test_a_background_process_holding_the_output_does_not_delay_a_finished_command(tmp_path: Path):
    child = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import os, time; os.setsid(); time.sleep(5)'])\n"
        "print('done')"
    )
    started = time.monotonic()
    proc = _start_hush(tmp_path, child, "--name", "Smoke", "--timeout", "60")
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0
    assert "passed" in out
    assert time.monotonic() - started < 4


def test_a_timeout_does_not_crash_when_the_process_group_is_already_gone(tmp_path: Path):
    proc = _start_hush(tmp_path, "import time; time.sleep(30)", "--timeout", "0.5")
    out, err = proc.communicate(timeout=15)
    assert proc.returncode == 124
    assert "timed out" in out
    assert "Traceback" not in err


def test_a_longer_path_that_merely_contains_the_directory_is_left_alone(tmp_path: Path):
    directory = tmp_path.resolve()
    text = f"/mnt{directory}/src/a.ts and {directory}/src/b.ts"
    assert cli._relativize(text, directory) == f"/mnt{directory}/src/a.ts and src/b.ts"


def test_both_spellings_of_a_symlinked_directory_are_shortened_the_same_way(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    text = f"{link}/a.ts:1 and {real.resolve()}/b.ts:2"
    assert cli._relativize(text, link) == "a.ts:1 and b.ts:2"


def test_a_relative_directory_only_strips_its_absolute_form(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "module-a").mkdir()
    monkeypatch.chdir(tmp_path)
    text = f"{tmp_path.resolve()}/module-a/src/x.ts and packages/module-a/src/y.ts"
    assert cli._relativize(text, Path("module-a")) == "src/x.ts and packages/module-a/src/y.ts"


def test_a_failing_command_is_not_reported_as_passed_when_sigchld_is_ignored(tmp_path: Path):
    # On Linux an inherited SIG_IGN for SIGCHLD makes `Popen` report every exit status as 0.
    launcher = (
        "import os, signal, sys\n"
        "signal.signal(signal.SIGCHLD, signal.SIG_IGN)\n"
        "os.execv(sys.executable, [sys.executable, '-m', 'hush', *sys.argv[1:]])"
    )
    command = [sys.executable, "-c", launcher, "--tool", "demo", "--log-dir", str(tmp_path / "l")]
    command += ["--name", "Smoke", "--", sys.executable, "-c", "raise SystemExit(3)"]
    done = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, timeout=30)
    assert done.returncode == 3
    assert "✗ FAIL (demo) Smoke failed;" in done.stdout
    assert "passed" not in done.stdout
    assert "exit code: 3" in (tmp_path / "l/demo-smoke.log").read_text()


def test_a_path_component_over_255_characters_is_not_a_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    assert not cli._exists_in(tmp_path)("bundle." + "a" * 300 + ".js")


def test_a_path_inside_an_unreadable_directory_is_not_a_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if os.geteuid() == 0:
        pytest.skip("root can read a directory with mode 000")
    monkeypatch.chdir(tmp_path)
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "x.py").write_text("")
    locked.chmod(0)
    try:
        assert not cli._exists_in(tmp_path)("locked/x.py")
    finally:
        locked.chmod(0o755)


@pytest.mark.skipif(sys.version_info >= (3, 13), reason="Path.resolve only raises on a symlink loop in Python 3.12")
def test_a_path_through_a_symlink_loop_is_not_a_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "loop").symlink_to("loop")
    assert not cli._exists_in(tmp_path)("loop")


def test_a_path_with_a_nul_character_is_not_a_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    assert not cli._exists_in(tmp_path)("a\0b.py")


def test_an_over_long_file_name_in_the_output_keeps_the_exit_code_and_the_report(run: Run):
    child = "print('see bundle.' + 'a' * 300 + '.js:1:5 here'); raise SystemExit(9)"
    code, out = run("--name", "Smoke", child=child)
    assert code == 9
    assert "✗ FAIL (demo) Smoke failed;" in out
    assert "see bundle." in out


def test_a_location_in_an_unreadable_directory_keeps_the_exit_code_and_the_report(run: Run):
    if os.geteuid() == 0:
        pytest.skip("root can read a directory with mode 000")
    locked = run.tmp_path / "locked"
    locked.mkdir()
    (locked / "x.py").write_text("")
    locked.chmod(0)
    try:
        code, out = run("--name", "Smoke", child="print('locked/x.py:3: error here'); exit(7)")
    finally:
        locked.chmod(0o755)
    assert code == 7
    assert "✗ FAIL (demo) Smoke failed;" in out
    assert "error here" in out


def _random_secret(length: int, seed: int) -> str:
    return "".join(random.Random(seed).choices(string.ascii_letters + string.digits, k=length))


def _leaked_slices(secret: str, *outputs: str) -> list[str]:
    """The 16-character pieces of the secret that appear in any output."""
    pieces = {secret[i : i + 16] for i in range(len(secret) - 15)}
    return sorted(piece for piece in pieces if any(piece in text for text in outputs))


def test_a_secret_longer_than_a_clipped_line_does_not_leak_its_first_part(
    run: Run, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    long_secret = _random_secret(6000, seed=1)
    straddling_secret = _random_secret(200, seed=2)
    monkeypatch.setenv("SERVICE_ACCOUNT_KEY", long_secret)
    monkeypatch.setenv("OTHER_SECRET", straddling_secret)
    child = (
        "import os, sys\n"
        "print(os.environ['SERVICE_ACCOUNT_KEY'])\n"
        "print('x' * 3950 + os.environ['OTHER_SECRET'])\n"
        "sys.exit(1)"
    )
    code = cli.main(
        ["--tool", "demo", "--verbose", "--log-dir", str(run.log_dir), "--name", "Smoke", "--"]
        + [sys.executable, "-c", child]
    )
    captured = capsys.readouterr()
    outputs = (run.log(), captured.out, captured.err)
    assert code == 1
    assert _leaked_slices(long_secret, *outputs) == []
    assert _leaked_slices(straddling_secret, *outputs) == []
    assert "[REDACTED]" in run.log()


def test_a_normal_length_line_still_reports_its_location_next_to_a_long_secret_line(
    run: Run, monkeypatch: pytest.MonkeyPatch
):
    (run.tmp_path / "app.py").write_text("")
    monkeypatch.setenv("SERVICE_ACCOUNT_KEY", _random_secret(6000, seed=3))
    child = (
        "import os, sys\nprint(os.environ['SERVICE_ACCOUNT_KEY'])\nprint('app.py:1: the failure is here')\nsys.exit(1)"
    )
    _, out = run("--name", "Smoke", child=child)
    assert "app.py:1" in out
    assert "the failure is here" in out
