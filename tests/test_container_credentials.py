import copy
import json
import os
import subprocess

import pytest

from ops.container import activate_credentials as setup


@pytest.fixture
def document():
    return {
        "version": 1,
        "database": "antiscam_v2",
        "passwords": {role: chr(65 + i) * 64 for i, role in enumerate(setup.ROLES)},
    }


@pytest.mark.parametrize(
    "payload",
    ["x' ; DROP ROLE postgres; --", "A" * 63, "A" * 64 + "\n", "\\password postgres"],
)
def test_activation_refuses_sql_or_psql_in_password(document, payload):
    document["passwords"][setup.ROLES[0]] = payload
    with pytest.raises(setup.SetupError):
        setup.activation_sql(document["passwords"])


def test_activation_refuses_foreign_roles_and_reused_passwords(document):
    wrong = copy.deepcopy(document)
    wrong["passwords"]["postgres"] = "Z" * 64
    with pytest.raises(setup.SetupError):
        setup.validate_credentials(wrong)
    document["passwords"][setup.ROLES[1]] = document["passwords"][setup.ROLES[0]]
    with pytest.raises(setup.SetupError):
        setup.validate_credentials(document)


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.name != "posix", reason="Linux file permissions"
)
def test_credentials_reject_unprotected_files_and_symlinks(document, tmp_path):
    if os.geteuid() != 0:
        pytest.skip("Root-owned credentials check is exercised in the container")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "credentials.json"
    path.write_text(json.dumps(document), encoding="ascii")
    path.chmod(0o600)
    assert setup.read_credentials(path) == document["passwords"]
    path.chmod(0o644)
    with pytest.raises(setup.SetupError):
        setup.read_credentials(path)
    path.chmod(0o600)
    link = private / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        setup.read_credentials(link)
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(private, target_is_directory=True)
    with pytest.raises(setup.SetupError):
        setup.read_credentials(parent_link / path.name)
    os.chown(path, 10001, 10001)
    with pytest.raises(setup.SetupError):
        setup.read_credentials(path)


def test_command_error_does_not_expose_subprocess_secrets(monkeypatch, document):
    secret = document["passwords"][setup.ROLES[0]]
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, secret, secret),
    )
    with pytest.raises(setup.SetupError) as caught:
        setup.execute(["psql"], sql=secret)
    assert secret not in str(caught.value)


def test_wrong_container_is_rejected_before_password_sql(monkeypatch, document):
    calls = []

    def execute(args, sql=None):
        calls.append((args, sql))
        return "different-project/db/true/healthy"

    monkeypatch.setattr(setup, "execute", execute)
    with pytest.raises(setup.SetupError):
        setup.activate(document["passwords"])
    assert len(calls) == 1 and calls[0][1] is None


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Linux entry point")
def test_main_hides_unexpected_exception_text(monkeypatch, capsys, document):
    secret = document["passwords"][setup.ROLES[0]]
    monkeypatch.setattr(setup.sys, "platform", "linux")
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)

    def fail(path):
        raise ValueError(secret)

    monkeypatch.setattr(setup, "read_credentials", fail)
    assert setup.main() == 1
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert json.loads(output.out)["error_type"] == "ValueError"
