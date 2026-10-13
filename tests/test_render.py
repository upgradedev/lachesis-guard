"""The state renderer: the call first, budgeted history, tagged tool blocks."""

from lachesis.render import (
    CHARS_PER_TOKEN,
    DEFAULT_MAX_TOKENS,
    TOOL_TEXT_CHARS,
    render_state,
)


def test_call_renders_before_conversation():
    messages = [{"role": "user", "content": [{"text": "do the thing"}]}]
    state = render_state(messages, "exec", {"command": "rm -rf /"})
    assert state.index("--- PROPOSED TOOL CALL ---") < state.index("--- CONVERSATION ---")
    assert "tool: exec" in state
    assert "rm -rf /" in state
    assert "user: do the thing" in state


def test_empty_conversation_keeps_sentinel():
    assert "(no conversation yet)" in render_state([], "t", {})
    assert "(no conversation yet)" in render_state(None, "t", {})
    assert "(no conversation yet)" in render_state("junk", "t", {})


def test_long_history_keeps_the_call_and_counts_the_omitted():
    messages = [
        {"role": "user", "content": [{"text": f"turn {i}: " + "x" * 1000}]} for i in range(40)
    ]
    args = {"path": "/", "recursive": True}
    state = render_state(messages, "delete_file", args)
    assert '"path": "/"' in state
    assert '"recursive": true' in state
    assert "earlier turns omitted" in state
    assert "turn 39: " in state  # newest kept
    assert "turn 0: " not in state  # oldest cut
    assert len(state) <= DEFAULT_MAX_TOKENS * CHARS_PER_TOKEN + 1000


def test_small_window_omits_deterministically():
    messages = [
        {"role": "user", "content": [{"text": "first"}]},
        {"role": "user", "content": [{"text": "second"}]},
    ]
    state = render_state(messages, "t", {}, max_tokens=8)
    assert "[2 earlier turns omitted]" in state
    assert "tool: t" in state


def test_tool_use_renders_as_a_call():
    sdk_shape = [
        {"role": "assistant", "content": [{"toolUse": {"name": "read", "input": {"p": "a"}}}]}
    ]
    cli_shape = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "read", "input": {"p": "a"}}],
        }
    ]
    for messages in (sdk_shape, cli_shape):
        state = render_state(messages, "t", {})
        assert 'assistant called read({"p": "a"})' in state


def test_tool_result_renders_tagged_untrusted():
    messages = [
        {
            "role": "assistant",
            "content": [{"toolUse": {"toolUseId": "u1", "name": "fetch", "input": {}}}],
        },
        {
            "role": "user",
            "content": [{"toolResult": {"toolUseId": "u1", "content": [{"text": "page"}]}}],
        },
    ]
    state = render_state(messages, "t", {})
    assert "tool fetch returned [UNTRUSTED]: page" in state


def test_tool_result_without_a_name_or_text():
    messages = [{"role": "user", "content": [{"toolResult": {"content": []}}]}]
    state = render_state(messages, "t", {})
    assert "tool tool returned [UNTRUSTED]: (no text output)" in state


def test_tool_text_is_capped_with_a_note():
    big = "y" * (TOOL_TEXT_CHARS + 500)
    messages = [{"role": "user", "content": [{"toolResult": {"content": big}}]}]
    state = render_state(messages, "t", {})
    assert "[500 more characters]" in state
    assert big not in state


def test_unicode_stays_literal():
    messages = [{"role": "user", "content": [{"text": "διάβασε το αρχείο"}]}]
    state = render_state(messages, "read", {"path": "σημειώσεις.txt"})
    assert "διάβασε το αρχείο" in state
    assert "σημειώσεις.txt" in state
    assert "\\u" not in state


def test_unknown_blocks_are_skipped():
    messages = [{"role": "user", "content": [{"image": "bytes"}, "plain", {"text": "hi"}]}, "junk"]
    state = render_state(messages, "t", {})
    assert "user: plain" in state
    assert "user: hi" in state
    assert "bytes" not in state


def test_unserializable_args_fall_back_to_str():
    state = render_state([], "t", {"f": object()})
    assert "tool: t" in state


def test_invalid_window_falls_back_to_default():
    messages = [{"role": "user", "content": [{"text": "x" * 40000}]}]
    for bad in (0, -5):
        state = render_state(messages, "t", {"a": 1}, max_tokens=bad)
        assert "tool: t" in state
        assert "[1 earlier turns omitted]" in state
