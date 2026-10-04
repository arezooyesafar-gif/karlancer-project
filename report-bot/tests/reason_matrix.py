"""Print, per report reason, what the bot sends to Telegram and the result.

Runs the real bot code against tests/mocktg.FakeTelegram (offline).

    python tests/reason_matrix.py
"""
import asyncio
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mocktg  # noqa: E402
from telethon import errors  # noqa: E402

BOT_DIR = os.path.dirname(HERE)
UID = 4242
SESS = "sess1"


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
        "comment_source": "text",
        "comments": ["matrix test"],
    }
    ctx.update(extra)
    return ctx


def snap(ctx):
    return {k: v for k, v in ctx.items() if k not in ("sample", "pool")}


async def post_route(bot, code):
    server = mocktg.FakeTelegram()
    mocktg.wire(bot, server)
    mode = {"scam": "scam", "fake": "fake"}.get(code, "msg")
    ctx = base_ctx(mode)
    await bot.ctx_set(UID, ctx)
    status, payload = await bot._auto_walk_reason(UID, ctx, [SESS], code, allow_user_choice=False)
    if status not in ("comment", "done"):
        return f"❌ {payload}", "-", "-"
    keys = [bytes(s["option"]).decode() for s in ctx["path"]]
    texts = [s["raw_text"] for s in ctx["path"]]
    server.calls.clear()
    _sid, ok, info = await bot.run_path_with_client(
        UID, SESS, snap(ctx), True, cli=mocktg.FakeUserClient(server)
    )
    peers = [c[2] for c in server.calls if c[0] == "account.reportPeer"]
    path = " › ".join(f"{k} {t}" for k, t in zip(keys, texts))
    return ("✅ works" if ok else f"❌ {info}"), path, ", ".join(peers) or "-"


async def peer_route(bot, code):
    server = mocktg.FakeTelegram()
    mocktg.wire(bot, server, peer=mocktg.USER)
    ctx = base_ctx("profile", entity_kind="user")
    opt = f"pr:{code}".encode()
    ctx["path"] = [bot._path_step_from_option(UID, opt, options_list=bot._peer_report_options())]
    res = await bot.run_dialog_with_client(
        UID, SESS, snap(ctx), True, rpt=1, cli=mocktg.FakeUserClient(server)
    )
    sent = [c for c in server.calls if c[0] == "account.reportPeer"]
    reason = sent[0][2] if sent else "-"
    _reason, note = bot._peer_reason_for_code(code)
    if res[1] != 1:
        return f"❌ {res[3]}", reason
    if note:
        return "⚠️ API has no Scam reason → sent as Other + scam text (not Fake)", reason
    return "✅ works", reason


async def error_cases(bot):
    rows = []
    path = lambda: [bot._path_step_from_option(UID, b"7"), bot._path_step_from_option(UID, b"74")]
    cases = [
        ("OPTION_INVALID", {"reject_options": {"74"}}, {}, mocktg.CHANNEL, None),
        ("MESSAGE_ID_INVALID", {}, {"msg_ids": [999]}, mocktg.CHANNEL, None),
        ("PEER_ID_INVALID", {}, {}, mocktg.BAD_PEER, None),
        (
            "MESSAGE_ID_REQUIRED (reportPeer)",
            {"peer_report_errors": {"InputReportReasonOther": errors.RPCError(request=None, message="MESSAGE_ID_REQUIRED", code=400)}},
            {},
            mocktg.CHANNEL,
            None,
        ),
    ]
    logger = bot.logger
    for name, skw, ckw, peer, _ in cases:
        server = mocktg.FakeTelegram(**skw)
        mocktg.wire(bot, server, peer=peer)
        ctx = base_ctx("scam", **ckw)
        ctx["path"] = path()
        lines = []

        class H(logging.Handler):
            def emit(self, record):
                msg = record.getMessage()
                if "report_rpc_error" in msg:
                    lines.append(msg)

        h = H()
        logger.addHandler(h)
        try:
            _sid, ok, info = await bot.run_path_with_client(
                UID, SESS, snap(ctx), True, cli=mocktg.FakeUserClient(server)
            )
        finally:
            logger.removeHandler(h)
        rows.append((name, ok, info, lines[0] if lines else "-"))
    return rows


async def main():
    logging.disable(logging.NOTSET)
    bot = mocktg.load_bot(BOT_DIR)
    bot.REPORT_DELAY_MIN = 0.0
    bot.REPORT_DELAY_MAX = 0.01
    logging.getLogger("reportbot").handlers.clear()
    print("| Reason | Post/channel route (messages.report) | Option path sent | Channel reportPeer | Profile route (account.reportPeer) | Reason sent |")
    print("|---|---|---|---|---|---|")
    for code in bot.REPORT_REASON_CODES:
        p_status, p_path, p_peer = await post_route(bot, code)
        r_status, r_reason = await peer_route(bot, code)
        print(f"| {bot._reason_label(code)} | {p_status} | {p_path} | {p_peer} | {r_status} | {r_reason} |")
    print()
    print("| Error from Telegram | Report ok? | Result string | Log line |")
    print("|---|---|---|---|")
    for name, ok, info, line in await error_cases(bot):
        print(f"| {name} | {ok} | `{info}` | `{line}` |")


if __name__ == "__main__":
    asyncio.run(main())
