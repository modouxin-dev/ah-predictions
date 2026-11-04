"""scripts/safe_commit.sh：在临时 git 仓库 + bare remote 里验证加锁、只提交指定路径、rebase 重试、测试失败中止。"""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "safe_commit.sh"
pytestmark = pytest.mark.skipif(not shutil.which("flock") or not shutil.which("git"), reason="需要 git 和 flock")


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check).stdout.strip()


def env(tmp_path, **kw):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GITHUB_AH_TOKEN"))}
    e.update({"AH_SECRETS_FILE": str(tmp_path / "no-secrets.env"), "AH_BACKOFF_BASE": "0", "HOME": str(tmp_path),
              "GIT_CONFIG_NOSYSTEM": "1", "AH_LOCK_FILE": str(tmp_path / "test.lock")})
    e.update(kw)
    return e


@pytest.fixture()
def repos(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    a, b = tmp_path / "a", tmp_path / "b"
    git(tmp_path, "clone", "-q", str(remote), str(a))
    for r in (a,):
        git(r, "config", "user.name", "t"); git(r, "config", "user.email", "t@t")
    (a / "f.txt").write_text("1\n")
    (a / "other.txt").write_text("x\n")
    git(a, "add", "."); git(a, "commit", "-q", "-m", "init"); git(a, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", str(remote), str(b))
    git(b, "config", "user.name", "t"); git(b, "config", "user.email", "t@t")
    return tmp_path, remote, a, b


def run(cwd, tmp_path, *args, **envkw):
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True,
                          env=env(tmp_path, **envkw), timeout=60)


def test_help_and_usage_errors(repos):
    tmp, _, a, _ = repos
    r = run(a, tmp, "--help")
    assert r.returncode == 0 and "退出码" in r.stdout
    assert run(a, tmp, "f.txt").returncode == 2                 # 缺 -m
    assert run(a, tmp, "-m", "x").returncode == 2               # 缺路径
    assert run(a, tmp, "-m", "x", "--bogus", "f.txt").returncode == 2


def test_commits_only_given_paths_and_pushes_as_bot(repos):
    tmp, remote, a, _ = repos
    (a / "f.txt").write_text("2\n")
    (a / "other.txt").write_text("someone else's wip\n")
    git(a, "add", "other.txt")                                   # 别人已暂存的改动也不能被带上
    (a / "new.txt").write_text("n\n")
    r = run(a, tmp, "-m", "update f", "--skip-tests", "f.txt", "new.txt")
    assert r.returncode == 0, r.stderr
    head = git(remote, "log", "-1", "--format=%an <%ae>|%s", "main")
    assert head == "ah-predictions-bot <modouxin-dev@users.noreply.github.com>|update f"
    files = git(remote, "show", "--name-only", "--format=", "main").split()
    assert sorted(files) == ["f.txt", "new.txt"]
    assert "other.txt" in git(a, "diff", "--cached", "--name-only")   # 仍然是别人的暂存改动
    assert r.stdout.strip() == git(a, "rev-parse", "HEAD")


def test_nothing_to_commit_is_ok(repos):
    tmp, _, a, _ = repos
    r = run(a, tmp, "-m", "noop", "--skip-tests", "f.txt")
    assert r.returncode == 0 and "没有改动" in r.stderr


def test_dry_run_changes_nothing(repos):
    tmp, remote, a, _ = repos
    before = git(remote, "rev-parse", "main")
    (a / "f.txt").write_text("dry\n")
    r = run(a, tmp, "--dry-run", "-m", "x", "f.txt")
    assert r.returncode == 0 and "dry-run" in r.stderr and "f.txt" in r.stderr
    assert git(remote, "rev-parse", "main") == before and git(a, "status", "--porcelain") == "M f.txt"


def test_lock_timeout(repos):
    tmp, _, a, _ = repos
    lock = tmp / "test.lock"
    holder = subprocess.Popen(["flock", str(lock), "sleep", "5"])
    try:
        time.sleep(0.3)
        (a / "f.txt").write_text("3\n")
        t = time.time()
        r = run(a, tmp, "-m", "x", "--skip-tests", "--lock-timeout", "1", "f.txt")
        assert r.returncode == 3 and "等锁超时" in r.stderr and time.time() - t < 4
        assert git(a, "status", "--porcelain") == "M f.txt"      # 未提交
    finally:
        holder.kill(); holder.wait()


def test_rebases_onto_concurrent_commit(repos):
    tmp, remote, a, b = repos
    (b / "b.txt").write_text("b\n"); git(b, "add", "b.txt"); git(b, "commit", "-q", "-m", "from b"); git(b, "push", "-q")
    (a / "f.txt").write_text("4\n")
    r = run(a, tmp, "-m", "a after b", "--skip-tests", "f.txt")
    assert r.returncode == 0, r.stderr
    assert git(remote, "log", "--format=%s", "main").splitlines()[:2] == ["a after b", "from b"]


def test_push_race_retries_with_rebase(repos):
    """pre-push 钩子在第一次推送前让 b 抢先推送，制造真实竞争；脚本应 rebase 后重试成功。"""
    tmp, remote, a, b = repos
    hook = a / ".git" / "hooks" / "pre-push"
    hook.write_text(f"""#!/bin/sh
[ -f "{tmp}/raced" ] && exit 0
touch "{tmp}/raced"
cd "{b}" && echo race > race.txt && git add race.txt && git commit -q -m race && git push -q origin main
exit 0
""")
    hook.chmod(0o755)
    (a / "f.txt").write_text("5\n")
    r = run(a, tmp, "-m", "after race", "--test-cmd", "true", "--retries", "3", "f.txt")
    assert r.returncode == 0, r.stderr
    assert "重试" in r.stderr
    assert git(remote, "log", "--format=%s", "main").splitlines()[:2] == ["after race", "race"]


def test_push_gives_up_after_retries(repos):
    tmp, remote, a, _ = repos
    hook = a / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nexit 1\n"); hook.chmod(0o755)
    before = git(remote, "rev-parse", "main")
    (a / "f.txt").write_text("6\n")
    r = run(a, tmp, "-m", "never", "--skip-tests", "--retries", "2", "f.txt")
    assert r.returncode == 6 and "2 次" in r.stderr
    assert git(remote, "rev-parse", "main") == before
    assert git(a, "log", "-1", "--format=%s") == "never"        # 本地提交保留，下次可再推


def test_failing_tests_abort_and_undo_commit(repos):
    tmp, remote, a, _ = repos
    before_remote, before_local = git(remote, "rev-parse", "main"), git(a, "rev-parse", "HEAD")
    (a / "f.txt").write_text("7\n")
    r = run(a, tmp, "-m", "broken", "--test-cmd", "exit 1", "f.txt")
    assert r.returncode == 5 and "测试失败" in r.stderr
    assert git(remote, "rev-parse", "main") == before_remote
    assert git(a, "rev-parse", "HEAD") == before_local
    assert git(a, "status", "--porcelain") == "M f.txt" and (a / "f.txt").read_text() == "7\n"


def test_tests_run_in_clean_worktree(repos):
    """别人未提交的文件不应出现在测试环境里；待提交的内容应该在。"""
    tmp, _, a, _ = repos
    (a / "wip_untracked.txt").write_text("someone's wip\n")
    (a / "f.txt").write_text("8\n")
    r = run(a, tmp, "-m", "clean", "--test-cmd", "test ! -e wip_untracked.txt && grep -q 8 f.txt", "f.txt")
    assert r.returncode == 0, r.stderr
    assert "ah-safe-commit" not in git(a, "worktree", "list")   # 临时 worktree 已清理


def test_rebase_conflict_aborts(repos):
    tmp, remote, a, b = repos
    (b / "f.txt").write_text("theirs\n"); git(b, "commit", "-qam", "theirs"); git(b, "push", "-q")
    before_local = git(a, "rev-parse", "HEAD")
    (a / "f.txt").write_text("ours\n")
    r = run(a, tmp, "-m", "conflict", "--skip-tests", "f.txt")
    assert r.returncode == 4
    assert git(a, "rev-parse", "HEAD") == before_local
    assert not (a / ".git" / "rebase-merge").exists() and (a / "f.txt").read_text() == "ours\n"
