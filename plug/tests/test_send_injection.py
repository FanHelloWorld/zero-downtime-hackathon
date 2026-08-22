"""Message bodies reach AppleScript as arguments, never as source code.

If they were interpolated into the script text, a message containing a quote
followed by `do shell script` would execute arbitrary commands on this machine.
These tests run osascript for real, with a harmless echo script, and assert the
payload comes back as inert data.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from supervisor_agent.send import SCRIPT_DIR

pytestmark = pytest.mark.skipif(shutil.which("osascript") is None, reason="macOS only")

HOSTILE = 'hi" & (do shell script "touch /tmp/plug_injection_canary") & "'


@pytest.fixture()
def echo_script(tmp_path: Path) -> Path:
    script = tmp_path / "echo.applescript"
    script.write_text("on run argv\n\treturn item 1 of argv\nend run\n")
    return script


def test_hostile_payload_is_returned_verbatim(echo_script, tmp_path):
    canary = Path("/tmp/plug_injection_canary")
    canary.unlink(missing_ok=True)

    proc = subprocess.run(
        ["osascript", str(echo_script), HOSTILE],
        capture_output=True, text=True, timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == HOSTILE, "payload was altered — it was not treated as data"
    assert not canary.exists(), "injection executed: the payload ran as AppleScript"


def test_send_scripts_take_arguments_rather_than_interpolating():
    for script in SCRIPT_DIR.glob("*.applescript"):
        source = script.read_text()
        assert "on run argv" in source, f"{script.name} must accept argv"
        assert "item 1 of argv" in source, f"{script.name} must read its text from argv"


def test_send_module_never_builds_script_source_from_message_text():
    source = (SCRIPT_DIR.parent / "send.py").read_text()
    # A string-formatted `osascript -e` call is exactly the vulnerable shape.
    assert '"-e"' not in source, "send.py must not pass inline script source"
    assert "shell=True" not in source, "send.py must not invoke a shell"
