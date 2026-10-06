"""manage_accounts.py, driven the way it is actually used: as a CLI.

These commands move real credential files around, and one of them (`detach`)
exists purely to keep `codex login` from revoking a live session, so the file
moves are worth pinning down.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_auth  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    accounts = tmp_path / "accounts"
    accounts.mkdir()
    return {
        "CODEX_HOME": str(codex_home),
        "CODEX_ACCOUNTS_DIR": str(accounts),
        "CODEX_ACCOUNTS_CONFIG": str(accounts / "pool.json"),
        "CODEX_ACCOUNTS_STATE": str(accounts / "state.json"),
        "SystemRoot": r"C:\Windows",
        "PATH": "",
    }


def run(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(APP_DIR / "manage_accounts.py"), *args],
        env=env, capture_output=True, text=True, cwd=str(APP_DIR),
    )


def pool_of(env: dict) -> dict:
    return json.loads(Path(env["CODEX_ACCOUNTS_CONFIG"]).read_text(encoding="utf-8"))


def cli_auth(env: dict) -> Path:
    return Path(env["CODEX_HOME"]) / "auth.json"


def test_detach_moves_the_cli_login_into_the_pool(env):
    fake_auth.write(cli_auth(env), "acctA", plan="pro")

    result = run(env, "detach", "--id", "the-pro")

    assert result.returncode == 0, result.stderr
    # The point of detach: nothing left for `codex login` to revoke.
    assert not cli_auth(env).exists()
    moved = Path(env["CODEX_ACCOUNTS_DIR"]) / "the-pro.json"
    assert moved.exists()
    entry = pool_of(env)["accounts"][0]
    assert entry["id"] == "the-pro"
    assert Path(entry["path"]) == moved
    assert entry["account_id"].startswith("acctA")


def test_detach_replaces_the_revoked_file_of_a_known_account(env):
    fake_auth.write(cli_auth(env), "acctA", plan="pro")
    run(env, "detach", "--id", "the-pro")
    stored = Path(env["CODEX_ACCOUNTS_DIR"]) / "the-pro.json"
    before = json.loads(stored.read_text(encoding="utf-8"))["tokens"]["refresh_token"]

    # same ChatGPT account, logged in again after its session was revoked
    fake_auth.write(cli_auth(env), "acctA", plan="pro")
    Path(cli_auth(env)).write_text(
        json.dumps({**json.loads(cli_auth(env).read_text(encoding="utf-8")),
                    "last_refresh": "2026-08-02T00:00:00.000000000Z"}), encoding="utf-8")

    result = run(env, "detach")

    assert result.returncode == 0, result.stderr
    assert len(pool_of(env)["accounts"]) == 1, "must not add a second entry for one account"
    assert json.loads(stored.read_text(encoding="utf-8"))["last_refresh"].startswith("2026-08-02")
    assert before == f"rt-acctA"  # sanity: the fixture is what we think it is


def test_detach_is_a_no_op_when_there_is_nothing_to_detach(env):
    result = run(env, "detach")
    assert result.returncode == 0
    assert "already absent" in result.stdout


def test_import_first_takes_priority_over_the_existing_account(env):
    fake_auth.write(cli_auth(env), "acctA", plan="pro")
    run(env, "detach", "--id", "the-pro")
    fake_auth.write(cli_auth(env), "acctB", plan="prolite")

    result = run(env, "import", "--id", "the-prolite", "--first")

    assert result.returncode == 0, result.stderr
    ids = [a["id"] for a in pool_of(env)["accounts"]]
    assert ids == ["the-prolite", "the-pro"]
    # import copies rather than moves, so the CLI login is left intact.
    assert cli_auth(env).exists()


def test_import_refuses_a_duplicate_account(env):
    fake_auth.write(cli_auth(env), "acctA")
    run(env, "import", "--id", "one")
    result = run(env, "import", "--id", "two")
    assert result.returncode == 2
    assert "already in the pool" in result.stdout
    assert len(pool_of(env)["accounts"]) == 1


def test_import_refuses_an_api_key_stub(env):
    stub = {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-test", "tokens": {}}
    cli_auth(env).write_text(json.dumps(stub), encoding="utf-8")
    result = run(env, "import", "--id", "stub")
    assert result.returncode == 2
    assert "not a ChatGPT login" in result.stdout


def test_attach_hands_a_pool_account_back_to_the_cli(env):
    fake_auth.write(cli_auth(env), "acctA", plan="pro")
    run(env, "detach", "--id", "the-pro")

    result = run(env, "attach", "the-pro")

    assert result.returncode == 0, result.stderr
    assert cli_auth(env).exists()
    assert not (Path(env["CODEX_ACCOUNTS_DIR"]) / "the-pro.json").exists()
    # One file per account, never a copy: the pool now points at the CLI's file.
    assert Path(pool_of(env)["accounts"][0]["path"]) == cli_auth(env)


def test_attach_refuses_to_overwrite_a_live_cli_login(env):
    fake_auth.write(cli_auth(env), "acctA", plan="pro")
    run(env, "detach", "--id", "the-pro")
    fake_auth.write(cli_auth(env), "acctB", plan="prolite")

    result = run(env, "attach", "the-pro")

    assert result.returncode == 2
    assert "run `detach` first" in result.stdout
    assert fake_auth.label_of(
        json.loads(cli_auth(env).read_text(encoding="utf-8"))["tokens"]["access_token"]
    ) == "acctB"


def test_backup_and_restore_round_trip(env):
    fake_auth.write(cli_auth(env), "acctA", plan="pro")

    assert run(env, "backup").returncode == 0
    backups = list((Path(env["CODEX_ACCOUNTS_DIR"]) / "backups").glob("auth-*.json"))
    assert len(backups) == 1

    # something clobbers the CLI login...
    fake_auth.write(cli_auth(env), "acctB", plan="prolite")
    result = run(env, "restore", str(backups[0]))

    assert result.returncode == 0, result.stderr
    restored = json.loads(cli_auth(env).read_text(encoding="utf-8"))
    assert fake_auth.label_of(restored["tokens"]["access_token"]) == "acctA"


def test_park_takes_an_account_out_of_service(env):
    fake_auth.write(cli_auth(env), "acctA", plan="pro")
    run(env, "import", "--id", "a")

    assert run(env, "park", "a", "--hours", "2").returncode == 0
    state = json.loads(Path(env["CODEX_ACCOUNTS_STATE"]).read_text(encoding="utf-8"))
    left = state["a"]["cooldown_until"] - state["a"]["since"]
    assert 7100 < left <= 7200
    assert run(env, "park", "nope").returncode == 2


def test_order_and_clear(env):
    fake_auth.write(cli_auth(env), "acctA", plan="pro")
    run(env, "import", "--id", "a")
    fake_auth.write(cli_auth(env), "acctB", plan="prolite")
    run(env, "import", "--id", "b")

    assert run(env, "order", "b", "a").returncode == 0
    assert [a["id"] for a in pool_of(env)["accounts"]] == ["b", "a"]

    Path(env["CODEX_ACCOUNTS_STATE"]).write_text(
        json.dumps({"b": {"cooldown_until": 4102444800, "reason": "usage_limit_reached"}}),
        encoding="utf-8")
    assert "parked" in run(env, "list").stdout
    assert run(env, "clear", "b").returncode == 0
    assert json.loads(Path(env["CODEX_ACCOUNTS_STATE"]).read_text(encoding="utf-8")) == {}
