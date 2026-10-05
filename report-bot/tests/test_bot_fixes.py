import asyncio
import json
import os
import sys

import httpx
import pytest
from bson import ObjectId
from telethon import errors
from telethon.tl import types

HERE = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import mocktg

ADMIN = 123456789
USER = 5151
SESS = "sess1"

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


@pytest.fixture(scope="session")
def bot():
    return mocktg.load_bot(BOT_DIR)


@pytest.fixture(scope="session")
def run(bot):
    loop = asyncio.new_event_loop()
    yield loop.run_until_complete
    loop.close()


@pytest.fixture(autouse=True)
def clean(bot, run, monkeypatch):
    monkeypatch.setattr(bot, "REPORT_DELAY_MIN", 0.0)
    monkeypatch.setattr(bot, "REPORT_DELAY_MAX", 0.01)
    run(bot.redis.flushall())
    bot.db["accounts"].delete_many({})
    bot.SUBS_COL.delete_many({})
    bot.SETTINGS_COL.delete_many({"key": "report_notify_channel"})
    bot._LIVE_REPORTS.clear()
    bot.PENDING_CONVERSATIONS.clear()
    yield


class FakeMsg:
    def __init__(self, sink):
        self.sink = sink

    async def edit(self, *a, **kw):
        self.sink.append(("edit", a, kw))

    async def reply(self, *a, **kw):
        self.sink.append(("reply", a, kw))
        return FakeMsg(self.sink)


class FakeEvent:
    def __init__(self, data=b"", uid=USER, text=""):
        self.sender_id = uid
        self.data = data
        self.raw_text = text
        self.text = text
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


def texts(event):
    return [a[0] for kind, a, kw in event.out if a and isinstance(a[0], str)]


def add_accounts(bot, n, owner=ADMIN):
    ids = []
    for i in range(n):
        oid = bot.db["accounts"].insert_one(
            {"phone": f"+1000{i}", "admin_id": owner, "api_id": 1, "api_hash": "x", "StringSession": ""}
        ).inserted_id
        ids.append(str(oid))
    return ids


async def start_live_report(bot, owner, sessions):
    for s in sessions:
        await bot.lock_account(s, owner)
    await bot.redis.set(f"report_active:{owner}", "1")
    await bot.redis.set(f"report_hb:{owner}", str(bot._now_ts()))


def give_subscription(bot, uid):
    bot.SUBS_COL.insert_one({"user_id": uid, "expires_at": bot._now_ts() + 86400 * 5})


def test_accounts_list_shows_every_account_while_a_report_runs(bot, run):
    sessions = add_accounts(bot, 127)
    give_subscription(bot, USER)
    run(start_live_report(bot, 999, sessions[:126]))
    assert len(run(bot.list_session_files(USER, include_locked=True))) == 127
    free, busy = run(bot.report_pool_with_busy(USER))
    assert len(free) == 1 and busy == 126
    title = run(bot.accounts_list_title(USER, sessions))
    assert "127" in title and "126" in title


def test_all_busy_returns_no_account_instead_of_reusing_busy_sessions(bot, run):
    sessions = add_accounts(bot, 3)
    run(start_live_report(bot, 999, sessions))
    assert run(bot._filter_unlocked_sessions(sessions, USER)) == []
    ev = FakeEvent()
    run(bot.respond_no_free_accounts(ev, USER, 3))
    assert texts(ev) == [bot.txt(USER, "accounts_all_busy", busy=3)]


def test_stale_locks_are_released(bot, run):
    sessions = add_accounts(bot, 3)
    for s in sessions:
        run(bot.lock_account(s, 999))
    assert run(bot._filter_unlocked_sessions(sessions, USER)) == sessions
    assert run(bot.redis.get(f"lock:{sessions[0]}")) is None


def test_admin_pool_choice_skips_busy_accounts(bot, run, monkeypatch):
    sessions = add_accounts(bot, 4)
    run(start_live_report(bot, 999, sessions[:3]))
    run(bot.ctx_set(ADMIN, {"mode": "msg", "target": "@x", "path": []}))
    seen = {}

    async def ask(event, uid, pool, busy=0):
        seen["pool"], seen["busy"] = list(pool), busy
        return None

    monkeypatch.setattr(bot, "ask_num_accounts", ask)
    run(bot.on_admin_pool_choice(FakeEvent(b"pool_all", uid=ADMIN)))
    assert seen == {"pool": [sessions[3]], "busy": 3}


def test_probe_reports_reasons_and_marks_logged_out_accounts(bot, run, monkeypatch):
    sessions = add_accounts(bot, 2)
    calls = []

    async def fake_connect(sess, *, use_proxy, force_proxy=None):
        calls.append((sess, use_proxy))
        return False, "NOT_AUTHORIZED"

    monkeypatch.setattr(bot, "_try_connect_authorized", fake_connect)
    monkeypatch.setattr(bot, "list_enabled_proxies", lambda: [{"id": "p"}])
    sess, err = run(bot.get_first_authorized_client(sessions))
    assert sess is None and err == "NOT_AUTHORIZED×2"
    assert calls == [(sessions[0], True), (sessions[1], True)]
    docs = list(bot.db["accounts"].find({}))
    assert all(d.get("health_status") == "unauthorized" for d in docs)
    fresh = add_accounts(bot, 1)
    assert bot._probe_order(sessions + fresh) == fresh + sessions


def test_probe_failure_message_explains_the_reason(bot, run, monkeypatch):
    sessions = add_accounts(bot, 1)
    give_subscription(bot, USER)

    async def fake_connect(sess, *, use_proxy, force_proxy=None):
        return False, "NOT_AUTHORIZED"

    monkeypatch.setattr(bot, "_try_connect_authorized", fake_connect)
    ev = FakeEvent()
    assert run(bot.resolve_probe_session(ev, USER, sessions)) is None
    msg = texts(ev)[-1]
    assert "NOT_AUTHORIZED×1" in msg
    assert bot.txt(USER, "probe_hint_unauthorized") in msg


@pytest.mark.parametrize("code", sorted(EXPECTED_PEER_REASON))
def test_profile_button_reaches_its_own_reason(bot, run, monkeypatch, code):
    server = mocktg.FakeTelegram()
    mocktg.wire(bot, server, peer=mocktg.USER)

    async def yes(*a, **kw):
        return True

    async def can_resolve(target):
        return True, "ok"

    monkeypatch.setattr(bot, "ensure_report_channel_member", yes)
    monkeypatch.setattr(bot, "_bot_can_resolve_target", can_resolve)
    ctx = {
        "mode": "profile",
        "target": "@someone",
        "entity_kind": "user",
        "path": [],
        "pool": [SESS],
        "sample": {"sess": SESS},
        "skip_join": True,
        "comment_source": "text",
        "comments": ["Report text."],
        "report_run_mode": "fixed",
        "reports_per_account": 1,
    }
    opts = bot._peer_report_options()
    bot._remember_report_options(ctx, opts)
    run(bot.ctx_set(USER, ctx))
    rows = bot.build_dialog_keyboard(USER, opts)
    datas = [getattr(getattr(b, "button", b), "type", None).data for row in rows for b in row]
    assert all(len(d) <= 64 for d in datas)
    data = next(d for d in datas if bot._decode_opt_callback(d, "db:") == f"pr:{code}".encode())
    ev = FakeEvent(data)
    run(bot.on_dialog_choose(ev))
    sent = [c[2] for c in server.calls if c[0] == "account.reportPeer"]
    assert len(sent) >= 2, (sent, ev.out)
    assert set(sent) == {EXPECTED_PEER_REASON[code]}
    assert not any(c[0] == "messages.report" for c in server.calls)


def _ai_client(handler):
    real = httpx.AsyncClient

    def make(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    return make


AI_SETTINGS = {"api_key": "k", "base_url": "https://ai.test/v1", "model": "gpt-4o-mini"}


def _ok(text="hello"):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def test_ai_retries_with_max_completion_tokens(bot, run, monkeypatch):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead."}})
        if "temperature" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported value: 'temperature' does not support 0.2 with this model."}})
        return _ok("[]")

    monkeypatch.setattr(bot.httpx, "AsyncClient", _ai_client(handler))
    out = run(bot._openai_chat_raw(AI_SETTINGS, "s", "u", max_tokens=100))
    assert out == "[]"
    assert "max_completion_tokens" in bodies[-1] and "temperature" not in bodies[-1]


def test_ai_reasoning_models_use_completion_tokens_directly(bot, run, monkeypatch):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _ok()

    monkeypatch.setattr(bot.httpx, "AsyncClient", _ai_client(handler))
    run(bot._openai_chat_raw(dict(AI_SETTINGS, model="o4-mini"), "s", "u"))
    assert len(bodies) == 1
    assert "max_completion_tokens" in bodies[0] and "max_tokens" not in bodies[0]


def test_ai_retries_rate_limit_then_succeeds(bot, run, monkeypatch):
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0.01"}, json={"error": {"message": "Rate limit"}})
        return _ok("done")

    monkeypatch.setattr(bot.httpx, "AsyncClient", _ai_client(handler))
    assert run(bot._openai_chat_raw(AI_SETTINGS, "s", "u")) == "done"
    assert state["n"] == 2


@pytest.mark.parametrize(
    "status,message,hint",
    [
        (401, "Incorrect API key provided", "ai_err_401"),
        (403, "Country, region, or territory not supported", "ai_err_region"),
        (404, "The model `gpt-x` does not exist", "ai_err_404"),
        (429, "You exceeded your current quota | insufficient_quota", "ai_err_quota"),
    ],
)
def test_ai_errors_are_explained(bot, run, monkeypatch, status, message, hint):
    def handler(request):
        return httpx.Response(status, json={"error": {"message": message}})

    monkeypatch.setattr(bot.httpx, "AsyncClient", _ai_client(handler))
    with pytest.raises(bot.AIRequestError) as exc:
        run(bot._openai_chat_raw(AI_SETTINGS, "s", "u"))
    text = bot.ai_error_text(USER, exc.value)
    assert f"HTTP {status}" in text
    assert bot.txt(USER, hint) in text


def test_report_join_prompt_has_a_join_link(bot, run):
    bot.set_report_notify_channel(
        {"chat_id": "-1003936575731", "username": "reportchan", "title": "-1003936575731", "url": ""}
    )
    text, rows = run(bot.build_report_join_prompt(USER, bot._report_channel_ref()))
    assert "https://t.me/reportchan" in text
    assert "-1003936575731" not in text
    urls = [getattr(getattr(getattr(b, "button", b), "type", None), "url", None) for row in rows for b in row]
    assert "https://t.me/reportchan" in urls


def test_report_join_prompt_uses_exported_invite_for_private_channel(bot, run, monkeypatch):
    bot.set_report_notify_channel({"chat_id": "-1003936575731", "title": "-1003936575731"})

    async def get_entity(ref):
        return types.Channel(id=3936575731, title="Reports", photo=types.ChatPhotoEmpty(), date=None, access_hash=1)

    async def call(self, req, *a, **kw):
        if isinstance(req, bot.functions.channels.GetFullChannelRequest):
            raise errors.RPCError(request=None, message="CHAT_ADMIN_REQUIRED", code=400)
        if isinstance(req, bot.functions.messages.ExportChatInviteRequest):
            return types.ChatInviteExported(link="https://t.me/+abcDEF", admin_id=1, date=None, permanent=True)
        raise AssertionError(req)

    monkeypatch.setattr(bot.bot, "get_entity", get_entity)
    monkeypatch.setattr(type(bot.bot), "__call__", call)
    text, rows = run(bot.build_report_join_prompt(USER, bot._report_channel_ref()))
    assert "https://t.me/+abcDEF" in text and "Reports" in text
    assert bot._report_channel_ref()["url"] == "https://t.me/+abcDEF"


def test_membership_check_fails_open_and_warns_admins_once(bot, run, monkeypatch):
    sent = []

    async def status(uid, ch):
        return "unknown", "CHAT_ADMIN_REQUIRED"

    async def send(chat, text, **kw):
        sent.append((chat, text))

    monkeypatch.setattr(bot, "force_join_status", status)
    monkeypatch.setattr(bot.bot, "send_message", send)
    ch = {"chat_id": "-100123", "title": "Reports"}
    assert run(bot.check_user_force_joined(USER, ch)) is True
    assert run(bot.check_user_force_joined(USER, ch)) is True
    assert [c for c, _ in sent] == list(bot.ADMINS)


def test_join_check_button_edits_in_place_when_still_missing(bot, run, monkeypatch):
    bot.set_report_notify_channel({"chat_id": "-100123", "username": "reportchan", "title": "Reports"})

    async def status(uid, ch):
        return "missing", ""

    monkeypatch.setattr(bot, "force_join_status", status)
    ev = FakeEvent(b"rnjoin:check")
    run(bot.on_report_join_check(ev))
    kinds = [k for k, a, kw in ev.out]
    assert "respond" not in kinds and "reply" not in kinds
    assert ev.out[0][0] == "answer" and ev.out[0][2].get("alert") is True


def test_second_conversation_is_refused_with_a_message(bot, run, monkeypatch):
    sent = []

    async def send(chat, text, **kw):
        sent.append(text)

    monkeypatch.setattr(bot.bot, "send_message", send)

    async def scenario():
        async with bot.standard_conversation(USER, timeout=5):
            with pytest.raises(bot.MenuInterrupt):
                async with bot.standard_conversation(USER, timeout=5):
                    pass

    run(scenario())
    assert sent == [bot.txt(USER, "conv_busy")]
    assert USER not in bot.PENDING_CONVERSATIONS


def test_duplicate_updates_are_processed_once(bot, run):
    assert run(bot._first_delivery("upd:cb:1")) is True
    assert run(bot._first_delivery("upd:cb:1")) is False


def test_restart_stops_reports_and_announces(bot, run, monkeypatch):
    sent = []

    async def send(chat, text, **kw):
        sent.append((chat, text))

    monkeypatch.setattr(bot.bot, "send_message", send)
    run(bot.redis.set("report_active:777", "1"))
    run(bot.redis.set("active_pool:777", json.dumps(["a"])))
    run(bot.lock_account("a", 777))
    assert run(bot.stop_all_reports_for_restart(wait_seconds=0)) == 0
    assert run(bot.redis.get("report_active:777")) is None
    assert run(bot.redis.get("lock:a")) is None
    run(bot.redis.set("bot_pending_restart", str(ADMIN)))
    run(bot.announce_restart())
    assert sent == [(ADMIN, bot.txt(ADMIN, "restart_done"))]
    assert run(bot.redis.get("bot_pending_restart")) is None


def test_instance_lock_blocks_a_second_instance(bot, run, monkeypatch):
    assert run(bot.acquire_instance_lock(rounds=1)) is True
    monkeypatch.setattr(bot, "INSTANCE_TOKEN", "other")

    async def no_sleep(*a, **kw):
        return None

    monkeypatch.setattr(bot.asyncio, "sleep", no_sleep)
    assert run(bot.acquire_instance_lock(rounds=2)) is False


def test_price_package_added_without_false_error(bot, run, monkeypatch):
    menus = []

    async def menu(chat, plan, edit_msg=None):
        menus.append(plan)

    monkeypatch.setattr(bot, "send_price_plan_menu", menu)
    monkeypatch.setattr(bot, "add_package", lambda *a: None)
    run(bot.ctx_set(ADMIN, {"mode": "add_package", "plan": "special", "step": "days"}))
    replies = []
    for value in ("30", "1000", "0"):
        ev = FakeEvent(uid=ADMIN, text=value)
        with pytest.raises(bot.events.StopPropagation):
            run(bot.price_add_conversation(ev))
        replies += texts(ev)
    assert bot.txt(ADMIN, "invalid_number") not in replies
    assert replies[-1] == bot.txt(ADMIN, "package_added")
    assert menus == ["special"]


def test_price_remove_reopens_the_right_plan(bot, run, monkeypatch):
    menus = []

    async def menu(chat, plan, edit_msg=None):
        menus.append(plan)

    monkeypatch.setattr(bot, "send_price_plan_menu", menu)
    monkeypatch.setattr(bot, "remove_package", lambda *a: None)
    run(bot.price_remove(FakeEvent(b"price_remove:normal:0", uid=ADMIN)))
    assert menus == ["normal"]


def test_txt_survives_a_broken_override(bot, monkeypatch):
    monkeypatch.setitem(bot.TEXT_OVERRIDES, "report_must_join", {"fa": "bad {channel", "en": "bad {0}"})
    out = bot.txt(USER, "report_must_join", channel="X")
    assert "X" in out


def test_menu_map_follows_text_overrides_and_legacy_buttons(bot, monkeypatch):
    monkeypatch.setitem(bot.TEXT_OVERRIDES, "menu_report_fake", {"fa": "🎭 گزارش جعل"})
    bot.rebuild_menu_actions_map()
    try:
        assert bot.MENU_ACTIONS_MAP["🎭 گزارش جعل"] == "report_fake"
        assert bot.MENU_ACTIONS_MAP["ریپورت اسکم | جعلی"] == "back"
        assert bot.MENU_ACTIONS_MAP[bot.TRANSLATIONS["menu_report_scam"]["fa"]] == "report_scam"
    finally:
        bot.TEXT_OVERRIDES.pop("menu_report_fake", None)
        bot.rebuild_menu_actions_map()


def test_obsolete_combined_scam_override_is_dropped(bot, monkeypatch):
    monkeypatch.setitem(bot.TEXT_OVERRIDES, "menu_report_scam", {"fa": "ریپورت اسکم | جعلی", "en": "Scam!"})
    bot._drop_obsolete_menu_overrides()
    assert bot.TEXT_OVERRIDES["menu_report_scam"] == {"en": "Scam!"}


def test_reportable_message_prefers_incoming_posts(bot):
    class M:
        def __init__(self, mid, out=False, action=None):
            self.id, self.out, self.action = mid, out, action

    msgs = [M(9, out=True), M(8, action=object()), M(7), M(6)]
    assert bot._reportable_ids_in_order(msgs) == [7, 6, 9]


def test_user_can_read_login_code_of_own_account(bot, run, monkeypatch):
    sid = add_accounts(bot, 1, owner=USER)[0]
    give_subscription(bot, USER)

    class Cli:
        async def connect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_messages(self, peer, limit=None):
            class Msg:
                message = "Login code: 54321. Do not give this code to anyone."
            return [Msg()]

        async def disconnect(self):
            return None

    monkeypatch.setattr(bot, "retern_client", lambda *a, **kw: Cli())
    monkeypatch.setattr(bot, "can_use_accounts", lambda uid: True)

    async def access(uid):
        return True

    monkeypatch.setattr(bot, "has_access", access)
    ev = FakeEvent(f"get_code={sid}".encode())
    with pytest.raises(bot.events.StopPropagation):
        run(bot.callback_accounts(ev))
    assert bot.txt(USER, "your_code", code="54321") in texts(ev)

    other = add_accounts(bot, 1, owner=ADMIN + 1)[0]
    bot.SUBS_COL.delete_many({})
    ev2 = FakeEvent(f"get_code={other}".encode())
    with pytest.raises(bot.events.StopPropagation):
        run(bot.callback_accounts(ev2))
    assert texts(ev2) == [bot.txt(USER, "account_not_found")]


def test_health_scan_skips_busy_accounts_and_keeps_connection_errors(bot, run, monkeypatch):
    sessions = add_accounts(bot, 3)
    run(start_live_report(bot, 999, sessions[:1]))
    probed = []

    async def probe(sid):
        probed.append(sid)
        status = "error" if sid == sessions[1] else "unauthorized"
        return {"sess": sid, "phone": "p", "status": status, "detail": ""}

    monkeypatch.setattr(bot, "_probe_account_health", probe)
    ev = FakeEvent(uid=ADMIN)
    run(bot.run_account_health_scan(ev, ADMIN))
    assert sessions[0] not in probed
    bad = json.loads(run(bot.redis.get(f"acc_health_bad:{ADMIN}")))
    assert [b["sess"] for b in bad] == [sessions[2]]
