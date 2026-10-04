"""Report reasons: each reason is sent through its own Telegram route.

Runs the real bot module (main-v6.py) against tests/mocktg.FakeTelegram and
checks the exact requests that reach "Telegram".

    pip install -r requirements.txt -r tests/requirements-test.txt
    python -m pytest tests -q
"""
import asyncio
import logging
import os
import re
import sys

import pytest
from telethon import errors
from telethon.tl import types

HERE = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import mocktg  # noqa: E402

UID = 4242
SESS = "sess1"

# Expected messages.report option path per reason (keys as Telegram sends them).
EXPECTED_PATH = {
    "spam": ["9", "92"],
    "violence": ["3", "32"],
    "porn": ["5", "57"],
    "child": ["2", "21"],
    "fake": ["7", "71"],
    "scam": ["7", "74"],
    "drugs": ["4", "42", "422"],
    "personal": ["6", "65"],
    "copyright": ["8"],
    "other": ["a", "a2"],
}
# account.reportPeer constructor per reason. Scam has none in the API.
EXPECTED_PEER_REASON = {
    "spam": "InputReportReasonSpam",
    "violence": "InputReportReasonViolence",
    "porn": "InputReportReasonPornography",
    "child": "InputReportReasonChildAbuse",
    "fake": "InputReportReasonFake",
    "scam": "InputReportReasonOther",
    "drugs": "InputReportReasonIllegalDrugs",
    "personal": "InputReportReasonPersonalDetails",
    "copyright": "InputReportReasonCopyright",
    "other": "InputReportReasonOther",
}
# Expected reason code for every leaf of Telegram's menu.
LEAF_CODE = {
    "1": "other", "21": "child", "22": "child",
    **{k: "violence" for k in ("31", "32", "33", "34", "35", "36", "37", "38")},
    **{k: "other" for k in ("411", "412", "413", "414", "43", "44", "45", "46", "47")},
    **{k: "drugs" for k in ("421", "422", "423")},
    "56": "child", **{k: "porn" for k in ("52", "53", "54", "55", "57")},
    **{k: "personal" for k in ("61", "62", "63", "64", "65")},
    "71": "fake", "72": "scam", "73": "scam", "74": "scam",
    "8": "copyright", "91": "spam", "92": "spam", "93": "spam",
    "a1": "other", "a2": "other", "a3": "other", "a4": "porn", "a5": "other",
    "b": "other",
}


@pytest.fixture(scope="session")
def bot():
    mod = mocktg.load_bot(BOT_DIR)
    yield mod


@pytest.fixture(scope="session")
def run(bot):
    # One loop for the whole run: the bot's Redis client binds to it.
    loop = asyncio.new_event_loop()
    yield loop.run_until_complete
    loop.close()


@pytest.fixture(autouse=True)
def fast(bot, monkeypatch):
    monkeypatch.setattr(bot, "REPORT_DELAY_MIN", 0.0)
    monkeypatch.setattr(bot, "REPORT_DELAY_MAX", 0.01)


def leaf_paths(tree=mocktg.TREE):
    out = []

    def walk(node, acc):
        for k, t in tree[node][1]:
            if k in tree:
                walk(k, acc + [(k, t)])
            else:
                out.append(acc + [(k, t)])

    walk("", [])
    return out


def base_ctx(mode, **extra):
    ctx = {
        "mode": mode,
        "target": "@target_channel",
        "entity_kind": "channel",
        "msg_ids": [10],
        "path": [],
        "pool": [SESS],
        "sample": {"sess": SESS},
        "skip_join": True,
        "comment": "",
        "comment_source": "text",
        "comments": ["Report text from test."],
    }
    ctx.update(extra)
    return ctx


def snapshot(ctx):
    return {k: v for k, v in ctx.items() if k not in ("sample", "pool")}


def path_keys(path):
    return [bytes(s["option"]).decode() for s in path]


def walks(server):
    """Split messages.report calls into walks (each starts with option b'')."""
    out = []
    for c in server.calls:
        if c[0] != "messages.report":
            continue
        if c[3] == b"":
            out.append([])
        out[-1].append(c)
    return out


def peer_calls(server):
    return [c for c in server.calls if c[0] == "account.reportPeer"]


MODE_FOR = {"scam": "scam", "fake": "fake"}


# --------------------------------------------------------------------------
# API facts
# --------------------------------------------------------------------------

def test_telegram_api_has_no_scam_report_reason():
    names = {n for n in dir(types) if n.startswith("InputReportReason")}
    assert "InputReportReasonScam" not in names
    assert "InputReportReasonFake" in names
    assert "InputReportReasonCopyright" in names


def test_every_api_report_reason_is_registered(bot):
    api = {n for n in dir(types) if n.startswith("InputReportReason")}
    registered = {
        s.peer_reason.__name__ for s in bot.REPORT_REASON_SPECS.values() if s.peer_reason
    }
    assert api == registered


def test_scam_and_fake_have_distinct_specs(bot):
    scam, fake = bot.REPORT_REASON_SPECS["scam"], bot.REPORT_REASON_SPECS["fake"]
    assert scam.peer_reason is None
    assert fake.peer_reason is types.InputReportReasonFake
    assert "71" in scam.forbidden_keys
    assert {"72", "73", "74"} <= fake.forbidden_keys
    assert bot._peer_reason_for_code("scam")[1] == bot.REPORT_API_LIMIT_SCAM
    assert type(bot._peer_reason_for_code("scam")[0]).__name__ == "InputReportReasonOther"
    assert type(bot._peer_reason_for_code("fake")[0]).__name__ == "InputReportReasonFake"


# --------------------------------------------------------------------------
# One route per reason: probe walk + worker run against the fake Telegram
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code", sorted(EXPECTED_PATH))
def test_reason_route_end_to_end(bot, run, code):
    server = mocktg.FakeTelegram()
    mocktg.wire(bot, server)
    mode = MODE_FOR.get(code, "msg")
    ctx = base_ctx(mode)
    run(bot.ctx_set(UID, ctx))
    status, _payload = run(
        bot._auto_walk_reason(UID, ctx, [SESS], code, allow_user_choice=False)
    )
    assert status in ("comment", "done"), (code, status, _payload)
    assert path_keys(ctx["path"]) == EXPECTED_PATH[code]
    assert bot._reason_code_from_path(ctx["path"]) == code

    server.calls.clear()
    sid, ok, info = run(
        bot.run_path_with_client(UID, SESS, snapshot(ctx), True, cli=mocktg.FakeUserClient(server))
    )
    assert ok, (code, info)
    ws = walks(server)
    assert ws, code
    for w in ws:
        sent = [c[3].decode() for c in w]
        assert sent == [""] + EXPECTED_PATH[code] + [EXPECTED_PATH[code][-1]], (code, sent)
        assert w[-1][4].strip(), "final call must carry the comment"
    pcs = peer_calls(server)
    if code in ("scam", "fake"):
        assert [c[2] for c in pcs] == [EXPECTED_PEER_REASON[code]]
    else:
        assert pcs == []


def test_scam_never_uses_fake(bot, run):
    for sub in ("72", "73", "74"):
        server = mocktg.FakeTelegram()
        mocktg.wire(bot, server)
        ctx = base_ctx("scam")
        ctx["path"] = [
            bot._path_step_from_option(UID, b"7"),
            bot._path_step_from_option(UID, sub.encode()),
        ]
        sid, ok, info = run(
            bot.run_path_with_client(UID, SESS, snapshot(ctx), True, cli=mocktg.FakeUserClient(server))
        )
        assert ok and info == "REPORTED+CHANNEL", info
        sent_options = {c[3] for c in server.calls if c[0] == "messages.report"}
        assert b"71" not in sent_options
        pcs = peer_calls(server)
        assert [c[2] for c in pcs] == ["InputReportReasonOther"]
        assert pcs[0][3].startswith(bot._SCAM_PEER_PREFIX)
        assert all(c[2] != "InputReportReasonFake" for c in pcs)


def test_fake_never_uses_scam_options(bot, run):
    server = mocktg.FakeTelegram()
    mocktg.wire(bot, server)
    ctx = base_ctx("fake")
    run(bot.ctx_set(UID, ctx))
    status, _ = run(bot._auto_walk_reason(UID, ctx, [SESS], "fake"))
    assert status == "comment"
    sent = {c[3] for c in server.calls if c[0] == "messages.report"}
    assert sent <= {b"", b"7", b"71"}


@pytest.mark.parametrize("mode,path,expect", [
    ("scam", ["7", "71"], "SCAM_PATH_INVALID:fake"),
    ("fake", ["7", "74"], "FAKE_PATH_INVALID:scam"),
    ("scam", ["9", "92"], "SCAM_PATH_INVALID:spam"),
])
def test_handlers_refuse_a_path_of_another_reason(bot, run, mode, path, expect):
    server = mocktg.FakeTelegram()
    mocktg.wire(bot, server)
    ctx = base_ctx(mode)
    ctx["path"] = [bot._path_step_from_option(UID, k.encode()) for k in path]
    sid, ok, info = run(
        bot.run_path_with_client(UID, SESS, snapshot(ctx), True, cli=mocktg.FakeUserClient(server))
    )
    assert not ok and info == expect
    assert [c for c in server.calls if c[0] in ("messages.report", "account.reportPeer")] == []


def test_scam_wizard_offers_only_scam_subtypes(bot, run):
    server = mocktg.FakeTelegram()
    mocktg.wire(bot, server)
    ctx = base_ctx("scam")
    run(bot.ctx_set(UID, ctx))
    status, payload = run(bot._auto_walk_reason(UID, ctx, [SESS], "scam", choose_at_level=1))
    assert status == "choose"
    assert sorted(bot._option_key(o) for o in payload) == ["72", "73", "74"]
    assert path_keys(ctx["path"]) == ["7"]


@pytest.mark.parametrize("code,tree_change,expect", [
    ("scam", {"drop_root": "7"}, "SCAM_OPTION_UNAVAILABLE"),
    ("fake", {"drop_child": ("7", "71")}, "FAKE_OPTION_UNAVAILABLE"),
    ("copyright", {"drop_root": "8"}, "COPYRIGHT_OPTION_UNAVAILABLE"),
])
def test_missing_telegram_option_is_an_error_not_a_substitute(bot, run, code, tree_change, expect):
    tree = {k: (t, list(kids)) for k, (t, kids) in mocktg.TREE.items()}
    if "drop_root" in tree_change:
        tree[""] = (tree[""][0], [kv for kv in tree[""][1] if kv[0] != tree_change["drop_root"]])
    if "drop_child" in tree_change:
        parent, child = tree_change["drop_child"]
        tree[parent] = (tree[parent][0], [kv for kv in tree[parent][1] if kv[0] != child])
    server = mocktg.FakeTelegram(tree=tree)
    mocktg.wire(bot, server)
    ctx = base_ctx(MODE_FOR.get(code, "msg"))
    run(bot.ctx_set(UID, ctx))
    status, payload = run(bot._auto_walk_reason(UID, ctx, [SESS], code, allow_user_choice=False))
    assert (status, payload) == ("error", expect)
    for c in server.calls:
        if c[0] == "messages.report":
            assert c[3] in (b"", b"7")


@pytest.mark.parametrize("lang", ["en", "fa", "ar"])
def test_matching_uses_meaning_not_option_bytes(bot, run, lang):
    """Telegram localises the menu and its option bytes are opaque: shuffle
    the bytes and translate the texts, every reason must still land on the
    same menu entries."""
    keys = sorted({k for _t, kids in mocktg.TREE.values() for k, _x in kids})
    key_map = {k: f"x{i}" for i, k in enumerate(keys)}
    tree = (
        mocktg.translated_tree(lang, bot.REPORT_OPT_TRANSLATIONS, key_map=key_map)
        if lang != "en"
        else {
            key_map.get(p, p): (t, [(key_map[k], x) for k, x in kids])
            for p, (t, kids) in mocktg.TREE.items()
        }
    )
    inv = {v: k for k, v in key_map.items()}
    for code, expected in EXPECTED_PATH.items():
        server = mocktg.FakeTelegram(tree=tree)
        mocktg.wire(bot, server)
        ctx = base_ctx(MODE_FOR.get(code, "msg"))
        run(bot.ctx_set(UID, ctx))
        status, payload = run(
            bot._auto_walk_reason(UID, ctx, [SESS], code, allow_user_choice=False)
        )
        assert status in ("comment", "done"), (lang, code, status, payload)
        got = [inv[k] for k in path_keys(ctx["path"])]
        if code == "scam":
            assert got[0] == "7" and got[1] in ("72", "73", "74"), (lang, got)
        elif code == "spam":
            assert got[0] == "9", (lang, got)
        elif code == "violence":
            assert got[0] == "3", (lang, got)
        else:
            assert got == expected, (lang, code, got)


# --------------------------------------------------------------------------
# Reason classification of a recorded path (used for account.reportPeer)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("variant", ["en", "fa", "ar", "keys_only", "text_only"])
def test_every_telegram_leaf_maps_to_the_right_reason(bot, variant):
    trans = bot.REPORT_OPT_TRANSLATIONS
    wrong = []
    for leaf in leaf_paths():
        path = []
        for k, en in leaf:
            local = (trans.get(k) or {}).get(variant, en) if variant in ("fa", "ar") else en
            step = {"text": local, "raw_text": en, "option": k.encode(), "key": k}
            if variant == "keys_only":
                step.update(text="?", raw_text="")
            if variant == "text_only":
                step.update(option=None, key="")
            if variant in ("fa", "ar"):
                step["raw_text"] = ""
            path.append(step)
        code = bot._reason_code_from_path(path)
        want = LEAF_CODE[leaf[-1][0]]
        if code != want:
            wrong.append((variant, [k for k, _ in leaf], code, want))
    assert wrong == []


def test_peer_menu_lists_every_reason_including_scam_and_copyright(bot):
    opts = {bytes(o.option).decode(): o.text for o in bot._peer_report_options()}
    assert list(opts) == [f"pr:{c}" for c in bot.REPORT_REASON_CODES]
    for code in bot.REPORT_REASON_CODES:
        assert f"pr:{code}" in bot.REPORT_OPT_TRANSLATIONS


@pytest.mark.parametrize("code", sorted(EXPECTED_PEER_REASON))
def test_profile_report_sends_registered_reason(bot, run, code):
    server = mocktg.FakeTelegram()
    mocktg.wire(bot, server, peer=mocktg.USER)
    opt = f"pr:{code}".encode()
    status, payload = run(
        bot.dialog_probe(mocktg.FakeUserClient(server), mocktg.USER, opt, SESS, prefer_peer=True)
    )
    assert status == "done", payload
    ctx = base_ctx("profile", entity_kind="user")
    ctx["path"] = [bot._path_step_from_option(UID, opt, options_list=bot._peer_report_options())]
    res = run(bot.run_dialog_with_client(UID, SESS, snapshot(ctx), True, rpt=1, cli=mocktg.FakeUserClient(server)))
    assert res[1] == 1, res
    pcs = peer_calls(server)
    assert [c[2] for c in pcs] == [EXPECTED_PEER_REASON[code]] * 2
    if code == "scam":
        assert all(c[3].startswith(bot._SCAM_PEER_PREFIX) for c in pcs)
    else:
        assert all(not c[3].startswith(bot._SCAM_PEER_PREFIX) for c in pcs)


def test_dialog_peer_reason_path_is_not_walked_as_a_menu(bot, run):
    """Probe had no message (pr: menu) but the worker finds one: it must still
    send account.reportPeer, not fail with PATH_MISMATCH."""
    server = mocktg.FakeTelegram()
    mocktg.wire(bot, server)
    ctx = base_ctx("dialog", msg_ids=[])
    ctx["path"] = [bot._path_step_from_option(UID, b"pr:spam", options_list=bot._peer_report_options())]
    res = run(bot.run_dialog_with_client(UID, SESS, snapshot(ctx), True, rpt=1, cli=mocktg.FakeUserClient(server)))
    assert res[1] == 1 and res[3] == "REPORTED_PEER", res
    assert [c[2] for c in peer_calls(server)] == ["InputReportReasonSpam"]


def test_dialog_menu_path_sends_its_own_reason_when_no_message(bot, run):
    server = mocktg.FakeTelegram()
    mocktg.wire(bot, server)
    for sub, want in (("71", "InputReportReasonFake"), ("74", "InputReportReasonOther")):
        server.calls.clear()
        ctx = base_ctx("dialog", msg_ids=[])
        ctx["path"] = [bot._path_step_from_option(UID, b"7"), bot._path_step_from_option(UID, sub.encode())]
        ok, info = run(bot._report_peer_legacy(mocktg.FakeUserClient(server), mocktg.CHANNEL, ctx["path"], snapshot(ctx)))
        assert ok, info
        assert [c[2] for c in peer_calls(server)] == [want]


def test_story_report_uses_the_same_reason_path(bot, run):
    server = mocktg.FakeTelegram()
    mocktg.wire(bot, server)
    ctx = base_ctx("story", selected_story_ids={5})
    ctx["path"] = [bot._path_step_from_option(UID, b"7"), bot._path_step_from_option(UID, b"73")]
    sid, ok, info = run(bot.run_path_with_client(UID, SESS, snapshot(ctx), True, cli=mocktg.FakeUserClient(server)))
    assert ok, info
    sent = [c[3] for c in server.calls if c[0] == "stories.report"]
    assert sent == [b"", b"7", b"73", b"73"]


# --------------------------------------------------------------------------
# Wizard handlers (callback buttons) end to end
# --------------------------------------------------------------------------

class FakeMsg:
    def __init__(self, sink):
        self.sink = sink

    async def edit(self, *a, **kw):
        self.sink.append(("edit", a, kw))

    async def reply(self, *a, **kw):
        self.sink.append(("reply", a, kw))
        return FakeMsg(self.sink)


class FakeEvent:
    def __init__(self, data=b""):
        self.sender_id = UID
        self.data = data
        self.out = []

    async def answer(self, *a, **kw):
        self.out.append(("answer", a, kw))

    async def edit(self, *a, **kw):
        self.out.append(("edit", a, kw))

    async def respond(self, *a, **kw):
        self.out.append(("respond", a, kw))
        return FakeMsg(self.out)

    async def reply(self, *a, **kw):
        self.out.append(("reply", a, kw))
        return FakeMsg(self.out)


def _wire_wizard(bot, monkeypatch, server):
    mocktg.wire(bot, server)

    async def probe(event, uid, pool):
        return pool[0]

    async def yes(*a, **kw):
        return True

    async def can_resolve(target):
        return True, "ok"

    monkeypatch.setattr(bot, "resolve_probe_session", probe)
    monkeypatch.setattr(bot, "ensure_report_channel_member", yes)
    monkeypatch.setattr(bot, "_bot_can_resolve_target", can_resolve)


def _buttons_data(event):
    for kind, a, kw in reversed(event.out):
        if kw.get("buttons"):
            out = []
            for row in kw["buttons"]:
                for b in row:
                    b = getattr(b, "button", b)
                    out.append(getattr(getattr(b, "type", None), "data", None) or getattr(b, "data", None))
            return out
    return []


def test_scam_wizard_end_to_end(bot, run, monkeypatch):
    server = mocktg.FakeTelegram()
    _wire_wizard(bot, monkeypatch, server)
    ctx = base_ctx("scam", report_run_mode="fixed", reports_per_account=1, comment_source="")
    ctx.pop("comments")
    run(bot.ctx_set(UID, ctx))
    ev = FakeEvent()
    run(bot.probe_scam_path(ev, UID, ctx, [SESS]))
    offered = [bot._decode_opt_callback(d, "mr:") for d in _buttons_data(ev) if d != b"mr:cancel"]
    assert sorted(offered) == [b"72", b"73", b"74"]

    # A forged / stale "Impersonation" button is refused in Scam mode.
    forged = FakeEvent(bot._encode_opt_callback("mr:", b"71"))
    server.calls.clear()
    run(bot.on_menu_choose(forged))
    assert server.calls == []
    assert run(bot.ctx_get(UID))["path"][-1]["key"] == "7"

    server.calls.clear()
    pick = FakeEvent(bot._encode_opt_callback("mr:", b"73"))
    run(bot.on_menu_choose(pick))
    full = [b"", b"7", b"73", b"73"]
    ws = [[c[3] for c in w] for w in walks(server)]
    assert full in ws, ws
    assert all(w == full[: len(w)] for w in ws), ws
    pcs = peer_calls(server)
    assert pcs and all(c[2] == "InputReportReasonOther" for c in pcs)
    assert all(c[3].startswith(bot._SCAM_PEER_PREFIX) for c in pcs)


def test_fake_wizard_end_to_end(bot, run, monkeypatch):
    server = mocktg.FakeTelegram()
    _wire_wizard(bot, monkeypatch, server)
    ctx = base_ctx("fake", report_run_mode="fixed", reports_per_account=1, comment_source="")
    ctx.pop("comments")
    run(bot.ctx_set(UID, ctx))
    ev = FakeEvent()
    run(bot.probe_fake_path(ev, UID, ctx, [SESS]))
    full = [b"", b"7", b"71", b"71"]
    ws = [[c[3] for c in w] for w in walks(server)]
    assert full in ws, ws
    assert all(w == full[: len(w)] for w in ws), ws
    pcs = peer_calls(server)
    assert pcs and all(c[2] == "InputReportReasonFake" for c in pcs)


# --------------------------------------------------------------------------
# Error reporting
# --------------------------------------------------------------------------

class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture
def logs(bot):
    h = _Capture()
    bot.logger.addHandler(h)
    yield h.lines
    bot.logger.removeHandler(h)


def test_error_names_are_telegram_names(bot):
    cases = {
        errors.OptionInvalidError(request=None): "OPTION_INVALID",
        errors.PeerIdInvalidError(request=None): "PEER_ID_INVALID",
        errors.MessageIdInvalidError(request=None): "MESSAGE_ID_INVALID",
        errors.ChannelPrivateError(request=None): "CHANNEL_PRIVATE",
        errors.FloodWaitError(request=None, capture=7): "FLOOD_WAIT",
        errors.RPCError(request=None, message="MESSAGE_ID_REQUIRED", code=400): "MESSAGE_ID_REQUIRED",
    }
    for exc, name in cases.items():
        assert bot._tg_error_name(exc) == name


@pytest.mark.parametrize("server_kw,ctx_kw,peer,expect", [
    ({"reject_options": {"74"}}, {}, mocktg.CHANNEL, "RPC_ERROR:OPTION_INVALID:400"),
    ({}, {"msg_ids": [999]}, mocktg.CHANNEL, "RPC_ERROR:MESSAGE_ID_INVALID:400"),
    ({}, {}, mocktg.BAD_PEER, "RPC_ERROR:PEER_ID_INVALID:400"),
])
def test_report_errors_are_named_and_logged(bot, run, logs, server_kw, ctx_kw, peer, expect):
    server = mocktg.FakeTelegram(**server_kw)
    mocktg.wire(bot, server, peer=peer)
    ctx = base_ctx("scam", **ctx_kw)
    ctx["path"] = [bot._path_step_from_option(UID, b"7"), bot._path_step_from_option(UID, b"74")]
    sid, ok, info = run(bot.run_path_with_client(UID, SESS, snapshot(ctx), True, cli=mocktg.FakeUserClient(server)))
    assert not ok and info == f"SCAM_{expect}", info
    name = expect.split(":")[1]
    hit = [l for l in logs if "report_rpc_error" in l and f"error={name}" in l]
    assert hit, logs
    assert "method=messages.report" in hit[0] and "reason=scam" in hit[0]

    # The probe account gets the same named error (it used to say JOIN_FAILED).
    run(bot.ctx_set(UID, ctx))
    status, payload = run(bot.sample_step(UID, ctx))
    assert (status, payload) == ("error", expect)
    assert name in bot._format_wizard_error(UID, payload) or "PRIVATE" in name


def test_channel_level_error_is_logged_and_does_not_hide_post_report(bot, run, logs):
    server = mocktg.FakeTelegram(
        peer_report_errors={
            "InputReportReasonFake": errors.RPCError(request=None, message="MESSAGE_ID_REQUIRED", code=400)
        }
    )
    mocktg.wire(bot, server)
    ctx = base_ctx("fake")
    ctx["path"] = [bot._path_step_from_option(UID, b"7"), bot._path_step_from_option(UID, b"71")]
    sid, ok, info = run(bot.run_path_with_client(UID, SESS, snapshot(ctx), True, cli=mocktg.FakeUserClient(server)))
    assert ok and info == "REPORTED;CHANNEL_RPC_ERROR:MESSAGE_ID_REQUIRED:400", info
    assert any("method=account.reportPeer" in l and "error=MESSAGE_ID_REQUIRED" in l for l in logs)


def test_wizard_error_texts(bot):
    assert "Scam or fraud" in bot._format_wizard_error(UID, "SCAM_OPTION_UNAVAILABLE")
    assert "Impersonation" in bot._format_wizard_error(UID, "FAKE_OPTION_UNAVAILABLE")
    assert "OPTION_INVALID" in bot._format_wizard_error(UID, "RPC_ERROR:OPTION_INVALID:400")
    assert "SOMETHING_NEW" in bot._format_wizard_error(UID, "RPC_ERROR:SOMETHING_NEW:400")


# --------------------------------------------------------------------------
# Menus, permissions, translations, AI
# --------------------------------------------------------------------------

def test_menu_has_separate_scam_and_fake_buttons(bot):
    for lang in ("fa", "ar", "en"):
        scam = bot.TRANSLATIONS["menu_report_scam"][lang]
        fake = bot.TRANSLATIONS["menu_report_fake"][lang]
        assert scam != fake
        assert bot.MENU_ACTIONS_MAP[scam.strip()] == "report_scam"
        assert bot.MENU_ACTIONS_MAP[fake.strip()] == "report_fake"
        assert bot.MENU_ACTIONS_MAP[bot.normalize_menu_text(scam)] == "report_scam"
        assert bot.MENU_ACTIONS_MAP[bot.normalize_menu_text(fake)] == "report_fake"
        assert "|" not in scam and "|" not in fake


def test_menu_keyboard_shows_both(bot, run, monkeypatch):
    async def access(uid):
        return True

    monkeypatch.setattr(bot, "has_access", access)
    rows = run(bot.kb_main(UID))
    texts = [getattr(b, "button", b).text for row in rows for b in row]
    assert any("Scam" in t or "اسکم" in t for t in texts)
    assert any("Fake" in t or "فیک" in t for t in texts)


def test_fake_permission_migrates_from_scam(bot):
    col = bot.SETTINGS_COL
    col.update_one(
        {"key": "panel_permissions"},
        {"$set": {"value": {"normal": ["report_msg", "report_scam", "report_profile"], "special": ["report_msg"]}}},
        upsert=True,
    )
    bot.init_panel_permissions()
    perms = bot.get_panel_permissions()
    assert perms["normal"][:3] == ["report_msg", "report_scam", "report_fake"]
    assert "report_fake" not in perms["special"]
    assert "report_fake" in bot.PANEL_PERMISSION_ACTIONS


def test_all_used_translation_keys_exist(bot):
    src = open(os.path.join(BOT_DIR, "main-v6.py"), encoding="utf-8").read()
    keys = set(re.findall(r'txt\(\s*\w+\s*,\s*"([a-z0-9_]+)"', src))
    keys |= {f"{m}_pick_subtype" for m in ("scam", "fake")}
    keys |= {"report_scam_intro", "report_fake_intro", "peer_reason_scam_note"}
    missing = [k for k in sorted(keys) if k not in bot.TRANSLATIONS]
    new_keys = {
        "menu_report_fake", "report_scam_intro", "report_fake_intro", "scam_pick_subtype",
        "fake_pick_subtype", "scam_option_unavailable", "fake_option_unavailable",
        "reason_option_unavailable", "peer_reason_scam_note", "report_err_no_option",
        "report_err_option_invalid", "report_err_message_id_invalid",
        "report_err_peer_id_invalid", "report_err_rpc",
    }
    assert not (new_keys & set(missing))
    for k in new_keys:
        assert set(bot.TRANSLATIONS[k]) == {"fa", "ar", "en"}, k


def test_ai_reasons_keep_their_own_route(bot, run, monkeypatch):
    async def fake_ai(settings, system, prompt, max_tokens=800):
        return (
            '[{"id":10,"reason":"fake","why":"a"},{"id":11,"reason":"scam","why":"b"},'
            '{"id":12,"reason":"child","why":"c"},{"id":13,"reason":"nonsense","why":"d"}]'
        )

    monkeypatch.setattr(bot, "_openai_chat_raw", fake_ai)
    monkeypatch.setattr(bot, "get_ai_settings", lambda: {"enabled": True, "api_key": "k"})
    out = run(bot._ai_analyze_channel("@x", [(10, "a"), (11, "b"), (12, "c"), (13, "d")]))
    assert [o["reason"] for o in out] == ["fake", "scam", "child", "other"]
    assert bot._AI_MODE_FOR_REASON == {"scam": "scam", "fake": "fake"}


# --------------------------------------------------------------------------
# /reportcheck: live menu check that never files a report
# --------------------------------------------------------------------------

class _Match:
    def __init__(self, arg):
        self.arg = arg

    def group(self, i):
        return self.arg


@pytest.mark.parametrize("drop_scam_root", [False, True])
def test_reportcheck_reads_menu_without_reporting(bot, run, monkeypatch, drop_scam_root):
    tree = {k: (t, list(kids)) for k, (t, kids) in mocktg.TREE.items()}
    if drop_scam_root:
        tree[""] = (tree[""][0], [kv for kv in tree[""][1] if kv[0] != "7"])
    server = mocktg.FakeTelegram(tree=tree)
    _wire_wizard(bot, monkeypatch, server)
    monkeypatch.setattr(bot, "ADMIN_IDS", set(bot.ADMIN_IDS) | {UID})

    async def pool(uid, include_locked=False):
        return [SESS]

    monkeypatch.setattr(bot, "list_session_files", pool)
    ev = FakeEvent()
    ev.pattern_match = _Match("https://t.me/target_channel/10")
    with pytest.raises(bot.events.StopPropagation):
        run(bot.report_check_command(ev))
    text = "\n".join(a[0] for kind, a, kw in ev.out if kind == "respond" and a)
    sent = {c[3] for c in server.calls if c[0] == "messages.report"}
    # Only the root menu and parent entries are requested; never a final option.
    leaves = {k.encode() for k in mocktg.LEAVES}
    assert not (sent & leaves), sent
    assert all(c[4] == "" for c in server.calls if c[0] == "messages.report")
    assert peer_calls(server) == []
    if drop_scam_root:
        assert "❌ Scam:" in text and "❌ Fake:" in text
        assert "✅ Spam:" in text
    else:
        for code in bot.REPORT_REASON_CODES:
            assert f"✅ {bot._reason_label(code)}:" in text, (code, text)
        assert "71 «Impersonation»" in text
        assert "scam options: 72" in text and "71 «Impersonation»" not in text.split("scam options:")[1].split("\n")[0]
        assert "API has no Scam reason" in text
