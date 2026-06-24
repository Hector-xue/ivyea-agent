"""Interactive arrow-key selector (tui.select) + permission integration."""
from __future__ import annotations

from ivyea_agent import permission, tui

OPTS = [("approve", "批准本次"), ("session", "本会话同类都批准"), ("deny", "拒绝"), ("abort", "全部停止")]


def _drive(keys: str):
    """Run the interactive selector, feeding `keys` via a prompt_toolkit pipe input."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.output import DummyOutput
    try:
        from prompt_toolkit.input.defaults import create_pipe_input
    except ImportError:  # older ptk
        from prompt_toolkit.input import create_pipe_input
    cm = create_pipe_input()
    inp = cm.__enter__() if hasattr(cm, "__enter__") else cm
    try:
        with create_app_session(input=inp, output=DummyOutput()):
            inp.send_text(keys)
            return tui._interactive("需要确认", "run ls", OPTS, "warn")
    finally:
        if hasattr(cm, "__exit__"):
            cm.__exit__(None, None, None)
        elif hasattr(inp, "close"):
            inp.close()


def test_enter_picks_highlighted_top():
    assert _drive("\r") == "approve"


def test_arrow_down_moves_selection():
    assert _drive("\x1b[B\r") == "session"
    assert _drive("\x1b[B\x1b[B\r") == "deny"


def test_digit_shortcut_selects_directly():
    assert _drive("3") == "deny"


def test_ctrl_c_falls_to_abort():
    assert _drive("\x03") == "abort"


def test_fallback_numbered_input():
    assert tui._fallback("t", "b", OPTS, "warn", lambda p: "2") == "session"
    assert tui._fallback("t", "b", OPTS, "warn", lambda p: "") == "abort"   # EOF → last (abort)
    assert tui._fallback("t", "b", OPTS, "warn", lambda p: "3") == "deny"


def test_permission_request_intent_maps_choices():
    st = permission.PermissionState()
    d = permission.request_intent({"op_type": "run_command"}, "run ls", st, input_fn=lambda p: "2")
    assert d == permission.APPROVE and "run_command" in st.session_allow
    # same op_type now auto-approves (no prompt)
    assert permission.request_intent({"op_type": "run_command"}, "x", st, input_fn=lambda p: "3") == permission.APPROVE
    assert permission.request_intent({"op_type": "z"}, "x", permission.PermissionState(),
                                     input_fn=lambda p: "3") == permission.DENY
    assert permission.request_intent({"op_type": "z"}, "x", permission.PermissionState(),
                                     input_fn=lambda p: "4") == permission.ABORT
