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
from support import load_fixture as load

from hush import cli, report
from hush.parsers.base import Outcome

# The run id every log name ends with, written out here so the tests do not use the pattern in cli.py.
RUN_ID = r"\d{8}T\d{6}-[0-9a-f]{4}"


class Run:
    def __init__(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self.tmp_path = tmp_path
        self._capsys = capsys
        self._logs: list[Path] = []

    def __call__(self, *args: str, child: str | list[str]) -> tuple[int, str]:
        command = ["--", sys.executable, "-c", child] if isinstance(child, str) else ["--", *child]
        before = self._listing()
        code = cli.main(["--tool", "demo", "--log-dir", str(self.log_dir), *args, *command])
        self._logs += sorted(self._listing() - before)
        return code, self._capsys.readouterr().out

    def _listing(self) -> set[Path]:
        return set(self.log_dir.glob("*.log")) if self.log_dir.is_dir() else set()

    @property
    def log_dir(self) -> Path:
        return self.tmp_path / "logs"

    def log(self, slug: str = "demo-smoke") -> str:
        """The text of the log that the latest run of this slug wrote."""
        paths = [path for path in self._logs if re.fullmatch(f"{re.escape(slug)}-{RUN_ID}\\.log", path.name)]
        return paths[-1].read_text()


def _only_log(log_dir: Path) -> Path:
    """The one log in a directory, for tests that run hush once."""
    (path,) = log_dir.glob("*.log")
    assert re.fullmatch(rf"demo-smoke-{RUN_ID}\.log", path.name)
    return path


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


def test_success_shows_skipped_xfailed_and_warning_counts_from_a_real_pytest_run(run: Run):
    _, out = run("--tool", "pytest", "--name", "Smoke", child=f"print({load('pytest_pass_skip_xfail_warn')!r})")
    assert re.fullmatch(
        r"  ✔ PASS \(pytest\) Smoke passed \(2 tests, 1 skipped, 1 xfailed, 1 warning\); \d+\.\ds\n", out
    )


def test_success_with_only_skipped_tests_shows_the_skips_alone(run: Run):
    _, out = run("--tool", "pytest", "--name", "Smoke", child=f"print({load('pytest_all_skipped')!r})")
    assert re.fullmatch(r"  ✔ PASS \(pytest\) Smoke passed \(3 skipped\); \d+\.\ds\n", out)


def test_success_line_lists_extra_counts_in_a_fixed_order_and_pluralises_warnings():
    counts = Outcome(test_count=1, skipped=2, xfailed=3, xpassed=4, todo=5, warnings=6)
    assert report.success_line("(x)", counts, 0.04) == (
        "  ✔ PASS (x) passed (1 test, 2 skipped, 3 xfailed, 4 xpassed, 5 todo, 6 warnings); 0.0s"
    )
    assert report.success_line("(x)", Outcome(warnings=1), 0.04) == "  ✔ PASS (x) passed (1 warning); 0.0s"
    assert report.success_line("(x)", Outcome(test_count=2), 0.04) == "  ✔ PASS (x) passed (2 tests); 0.0s"
    assert report.success_line("(x)", Outcome(), 0.04) == "  ✔ PASS (x) passed; 0.0s"


def test_color_always_marks_a_pass_green_and_a_failure_red(run: Run):
    _, passed = run("--color", "always", child="print('ok')")
    assert "\x1b[32m✔ PASS\x1b[0m (demo)" in passed
    (run.tmp_path / "app.py").write_text("x = 1\n")
    _, failed = run(
        "--color", "always",
        child="import sys; print('app.py:1: something broke here'); sys.exit(3)",
    )  # fmt: skip
    assert "\x1b[31m✗ FAIL\x1b[0m (demo)" in failed
    assert "\x1b[" not in run.log("demo")


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
    details = re.search(rf"Details: (logs/demo-smoke-{RUN_ID}\.log)\n", out)
    assert details
    assert (run.tmp_path / details.group(1)).is_file()


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


@pytest.mark.parametrize(
    ("docker_args", "recorded"),
    [
        (["compose", "exec", "-T", "app", "pytest", "tests"], "compose exec -T app pytest -x tests "),
        (["exec", "-i", "web", "pytest", "tests"], "exec -i web pytest -x tests "),
    ],
)
def test_docker_exec_gets_the_inner_tool_label_counts_and_failfast(
    docker_args: list[str], recorded: str, run: Run, capsys: pytest.CaptureFixture[str]
):
    # A stand-in `docker` that records its arguments and prints real captured pytest output.
    (run.tmp_path / "pytest_output.txt").write_text(load("pytest_pass_skip_xfail_warn"))
    docker = run.tmp_path / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s " "$@" > args.txt\ncat pytest_output.txt\n')
    docker.chmod(0o755)
    code = cli.main(["--log-dir", str(run.log_dir), "--failfast", "--", str(docker), *docker_args])
    out = capsys.readouterr().out
    assert code == 0
    assert re.fullmatch(r"  ✔ PASS \(pytest\) passed \(2 tests, 1 skipped, 1 xfailed, 1 warning\); \d+\.\ds\n", out)
    assert (run.tmp_path / "args.txt").read_text() == recorded


def test_docker_exec_env_secrets_are_masked_in_the_log_and_the_output(run: Run):
    docker = run.tmp_path / "docker"
    docker.write_text("#!/bin/sh\necho 'connecting with longsecret99 and short7'\nexit 1\n")
    docker.chmod(0o755)
    command = [str(docker), "exec", "-e", "DB_PASSWORD=short7", "-e", "API_KEY=longsecret99", "-e", "DEBUG=1"]
    _, out = run("--name", "Smoke", child=[*command, "web", "pytest"])
    log = run.log()
    # Short values are hidden in the command line. Values of 8 characters or more are hidden everywhere.
    assert (
        log.splitlines()[0] == f"$ {docker} exec -e DB_PASSWORD=[REDACTED] -e API_KEY=[REDACTED] -e DEBUG=1 web pytest"
    )
    assert "longsecret99" not in log + out
    assert "connecting with [REDACTED] and short7" in log


def _failing_docker(run: Run) -> str:
    """A stand-in `docker` that fails with a path relative to the container's working directory."""
    docker = run.tmp_path / "docker"
    docker.write_text("#!/bin/sh\necho 'tests/test_a.py:3: AssertionError: the total is wrong'\nexit 1\n")
    docker.chmod(0o755)
    return str(docker)


@pytest.mark.parametrize(
    ("hush_options", "host_files", "workdir", "expected"),
    [
        # The container folder is a subfolder of the project, so the location gets that folder in front.
        ([], ["backend/tests/test_a.py"], "/workspace/backend", "backend/tests/test_a.py:3"),
        # The longest matching tail wins, even when a shorter one is also a folder.
        (
            [],
            ["workspace/backend/tests/test_a.py", "backend/other.py"],
            "/workspace/backend",
            "workspace/backend/tests/test_a.py:3",
        ),
        # Combined with `--cwd`, the folder is looked for under it and shown after it.
        (["--cwd", "proj"], ["proj/backend/tests/test_a.py"], "/w/backend", "proj/backend/tests/test_a.py:3"),
        # No folder of that name on the host: nothing to map, the path is looked up as before.
        ([], ["tests/test_a.py"], "/workspace/backend", "tests/test_a.py:3"),
    ],
)
def test_docker_exec_workdir_is_mapped_to_a_host_folder(
    hush_options: list[str], host_files: list[str], workdir: str, expected: str, run: Run
):
    for name in host_files:
        (run.tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (run.tmp_path / name).write_text("")
    _, out = run("--name", "Smoke", *hush_options, child=[_failing_docker(run), "exec", "-w", workdir, "web", "pytest"])
    assert f"\n    {expected}\n    Reason:\n" in out


@pytest.mark.parametrize("docker_args", [["exec", "-w", "/workspace/frontend"], ["exec"]])
def test_docker_exec_reports_no_location_when_no_host_folder_fits(docker_args: list[str], run: Run):
    # The file exists only under `backend`, which nothing in the command points to.
    (run.tmp_path / "backend/tests").mkdir(parents=True)
    (run.tmp_path / "backend/tests/test_a.py").write_text("")
    _, out = run("--name", "Smoke", child=[_failing_docker(run), *docker_args, "web", "pytest"])
    assert "\n    Reason:\n      tests/test_a.py:3: AssertionError: the total is wrong\n" in out


def test_cwd_runs_the_command_there_and_reports_paths_from_the_caller(run: Run):
    module = run.tmp_path / "module-a"
    module.mkdir()
    (module / "app.py").write_text("")
    child = "import os, sys; print('app.py:1: broke in', os.getcwd()); sys.exit(1)"
    _, out = run("--name", "Smoke", "--cwd", "module-a", child=child)
    assert "module-a/app.py:1" in out
    details = re.search(rf"Details: (logs/module-a-demo-smoke-{RUN_ID}\.log)\n", out)
    assert details
    assert (run.tmp_path / details.group(1)).is_file()


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
    # 100 characters of slug, then "-", the 20-character run id and ".log".
    assert len(names[0]) == 125
    assert re.fullmatch(rf"demo-n{{86}}-[0-9a-f]{{8}}-{RUN_ID}\.log", names[0])


def test_log_is_private_and_a_planted_symlink_is_replaced_not_followed(run: Run, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli, "_run_id", lambda: "20260101T000000-abcd")
    run.log_dir.mkdir()
    victim = run.tmp_path / "victim.txt"
    victim.write_text("ORIGINAL")
    (run.log_dir / "demo-smoke-20260101T000000-abcd.log").symlink_to(victim)
    run("--name", "Smoke", child="raise SystemExit(1)")
    log = run.log_dir / "demo-smoke-20260101T000000-abcd.log"
    assert victim.read_text() == "ORIGINAL"
    assert not log.is_symlink()
    assert (log.stat().st_mode & 0o777) == 0o600
    assert list(run.log_dir.glob(".hush-*")) == []


def test_each_run_writes_its_own_log(run: Run):
    _, first = run("--name", "Smoke", child="raise SystemExit(1)")
    _, second = run("--name", "Smoke", child="raise SystemExit(1)")
    names = sorted(path.name for path in run.log_dir.iterdir())
    assert len(names) == 2
    assert all(re.fullmatch(rf"demo-smoke-{RUN_ID}\.log", name) for name in names)
    # Each report names the file its own run wrote, and the two runs wrote different files.
    reported = [re.search(r"Details: (\S+)\n", out) for out in (first, second)]
    assert all(reported)
    assert sorted(Path(m.group(1)).name for m in reported if m) == names
    assert all((run.tmp_path / m.group(1)).is_file() for m in reported if m)


def _make_logs(log_dir: Path, mtimes: dict[str, int]) -> None:
    log_dir.mkdir(exist_ok=True)
    for name, mtime in mtimes.items():
        (log_dir / name).write_text("old")
        os.utime(log_dir / name, (mtime, mtime))


def test_pruning_keeps_the_newest_logs_across_all_steps_and_leaves_other_files_alone(run: Run):
    old_a = "demo-smoke-20250101T000000-aaaa.log"
    old_b = "other-step-20250101T000001-bbbb.log"
    mid = "demo-smoke-20250101T000002-cccc.log"
    new = "other-step-20250101T000003-dddd.log"
    # By file name the order is old_a, old_b, mid, new. By mtime old_b is the oldest, so only mtime can decide.
    _make_logs(run.log_dir, {old_a: 1_000_000_300, old_b: 1_000_000_100, mid: 1_000_000_400, new: 1_000_000_500})
    others = {"notes.log": 1, "pytest.log": 1, ".hush-x.tmp": 1}
    _make_logs(run.log_dir, others)
    (run.log_dir / "subdir-20250101T000000-eeee.log").mkdir()
    code, _ = run("--name", "Smoke", "--keep-logs", "3", child="pass")
    assert code == 0
    # The newest three are the run's own log, `new` and `mid`.
    (own,) = run._logs
    assert {path.name for path in run.log_dir.iterdir()} == {
        own.name,
        new,
        mid,
        *others,
        "subdir-20250101T000000-eeee.log",
    }


def test_the_log_just_written_survives_keep_logs_1_even_when_another_log_is_newer(run: Run):
    future = "demo-smoke-29990101T000000-ffff.log"
    _make_logs(run.log_dir, {future: 32_000_000_000})
    run("--name", "Smoke", "--keep-logs", "1", child="pass")
    (own,) = run._logs
    assert [path.name for path in run.log_dir.iterdir()] == [own.name]


def test_pruning_does_not_touch_a_symlink_that_has_a_log_name(run: Run):
    target = run.tmp_path / "precious.txt"
    target.write_text("keep me")
    run.log_dir.mkdir()
    link = run.log_dir / "demo-smoke-20250101T000000-aaaa.log"
    link.symlink_to(target)
    run("--name", "Smoke", "--keep-logs", "1", child="pass")
    assert link.is_symlink()
    assert target.read_text() == "keep me"


def _without_timing(out: str) -> str:
    return re.sub(r"\d+\.\ds", "Ns", out)


def test_a_failing_prune_does_not_change_the_output_or_exit_code(run: Run, monkeypatch: pytest.MonkeyPatch):
    child = "import sys; print('app.py:1: something broke here'); sys.exit(3)"
    (run.tmp_path / "app.py").write_text("")
    old = ["demo-smoke-20250101T000000-aaaa.log", "demo-smoke-20250101T000001-bbbb.log"]
    _make_logs(run.log_dir, {old[0]: 1, old[1]: 2})
    # A fixed run id makes the reported log path the same in both runs.
    monkeypatch.setattr(cli, "_run_id", lambda: "20260101T000000-abcd")
    baseline_code, baseline_out = run("--name", "Smoke", "--keep-logs", "100", child=child)
    assert baseline_code == 3
    assert "something broke here" in baseline_out

    def refuse(path: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(os, "unlink", refuse)
    code, out = run("--name", "Smoke", "--keep-logs", "1", child=child)
    assert code == baseline_code
    assert _without_timing(out) == _without_timing(baseline_out)
    # Pruning did try to delete, and could not.
    assert all((run.log_dir / name).exists() for name in old)


def test_a_log_that_vanishes_during_pruning_is_ignored(run: Run, monkeypatch: pytest.MonkeyPatch):
    _make_logs(run.log_dir, {"demo-smoke-20250101T000000-aaaa.log": 1})

    def vanished(path: object) -> None:
        raise FileNotFoundError(path)

    monkeypatch.setattr(os, "unlink", vanished)
    code, out = run("--name", "Smoke", "--keep-logs", "1", child="pass")
    assert code == 0
    assert "✔ PASS (demo) Smoke passed" in out


def test_no_pruning_when_the_log_could_not_be_written(run: Run, monkeypatch: pytest.MonkeyPatch):
    def refuse(path: object, content: str) -> None:
        raise OSError(13, "Permission denied")

    _make_logs(run.log_dir, {"demo-smoke-20250101T000000-aaaa.log": 1, "demo-smoke-20250101T000001-bbbb.log": 2})
    monkeypatch.setattr(cli, "_write_log", refuse)
    code, out = run("--name", "Smoke", "--keep-logs", "1", child="raise SystemExit(1)")
    assert code == 1
    assert "Details: not written" in out
    assert len(list(run.log_dir.iterdir())) == 2


def test_a_failed_run_prunes_too(run: Run):
    _make_logs(run.log_dir, {"demo-smoke-20250101T000000-aaaa.log": 1, "demo-smoke-20250101T000001-bbbb.log": 2})
    code, _ = run("--name", "Smoke", "--keep-logs", "2", child="raise SystemExit(1)")
    assert code == 1
    (own,) = run._logs
    assert {path.name for path in run.log_dir.iterdir()} == {"demo-smoke-20250101T000001-bbbb.log", own.name}


@pytest.mark.parametrize("value", ["0", "-3", "x"])
def test_keep_logs_must_be_a_whole_number_of_at_least_1(run: Run, capsys: pytest.CaptureFixture[str], value: str):
    with pytest.raises(SystemExit) as exit_info:
        run("--keep-logs", value, child="pass")
    assert exit_info.value.code == 2
    # Names the option, so an unknown-option error does not pass for this.
    assert "argument --keep-logs: " in capsys.readouterr().err


def test_the_default_keeps_the_newest_40_logs(run: Run):
    _make_logs(run.log_dir, {f"demo-smoke-20250101T0000{i:02d}-aaaa.log": 1000 + i for i in range(45)})
    run("--name", "Smoke", child="pass")
    (own,) = run._logs
    names = {path.name for path in run.log_dir.iterdir()}
    assert len(names) == 40
    # Of the 45 old logs, the 39 newest stay (indexes 6 to 44) next to the run's own.
    assert names == {own.name, *(f"demo-smoke-20250101T0000{i:02d}-aaaa.log" for i in range(6, 45))}


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
        "print('MIIE' + 'owIBAAKCAQEAfirstbodyline')\n"
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
    assert "packages/module-a/src/x.ts:2" in run.log("module-a-demo-smoke")


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
    log = _only_log(tmp_path / "logs").read_text()
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
    assert "interrupted by signal" in _only_log(tmp_path / "logs").read_text()
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
    assert _only_log(tmp_path / "logs").is_file()
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
    assert "exit code: 3" in _only_log(tmp_path / "l").read_text()


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
    log = _only_log(run.log_dir).read_text()
    outputs = (log, captured.out, captured.err)
    assert code == 1
    assert _leaked_slices(long_secret, *outputs) == []
    assert _leaked_slices(straddling_secret, *outputs) == []
    assert "[REDACTED]" in log


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


def _classify(run: Run, *command: str, options: tuple[str, ...] = ()) -> tuple[int, str]:
    code = cli.main(["--classify", "--log-dir", str(run.log_dir), *options, "--", *command])
    return code, run._capsys.readouterr().out


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (["uv", "run", "pytest", "tests/x.py"], "pytest"),
        (["pnpm", "exec", "vitest", "run"], "vitest"),
        (["python", "-m", "pytest"], "pytest"),
        (["./node_modules/.bin/tsc", "--noEmit"], "tsc"),
    ],
)
def test_classify_prints_the_recognised_tool_and_exits_0(run: Run, command: list[str], expected: str):
    assert _classify(run, *command) == (0, f"{expected}\n")


@pytest.mark.parametrize(
    "command",
    [["pnpm", "run", "test"], ["git", "status"], ["make"]],
)
def test_classify_prints_nothing_and_exits_1_when_no_tool_is_recognised(run: Run, command: list[str]):
    assert _classify(run, *command) == (1, "")


def test_classify_does_not_run_the_command_or_write_a_log(run: Run):
    marker = run.tmp_path / "ran.txt"
    child = f"import pathlib, sys; pathlib.Path({str(marker)!r}).write_text('x'); sys.exit(5)"
    assert _classify(run, sys.executable, "-c", child) == (1, "")
    assert not marker.exists()
    assert not run.log_dir.exists()


def test_classify_does_not_run_a_recognised_tool_either(run: Run):
    marker = run.tmp_path / "ran.txt"
    script = run.tmp_path / "pytest"
    script.write_text(f"#!/bin/sh\ntouch {marker}\nexit 5\n")
    script.chmod(0o755)
    assert _classify(run, str(script), "-q") == (0, "pytest\n")
    assert not marker.exists()
    assert not run.log_dir.exists()


def test_classify_ignores_tool_and_cwd(run: Run):
    assert _classify(run, "git", "status", options=("--tool", "ruff")) == (1, "")
    assert _classify(run, "uv", "run", "pytest", options=("--tool", "ruff", "--cwd", "missing-dir")) == (0, "pytest\n")


def test_classify_still_requires_a_command(run: Run):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--classify"])
    assert exit_info.value.code == 2
