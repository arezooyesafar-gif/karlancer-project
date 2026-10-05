import importlib.util
import os
import shutil
import sys
import tempfile

from telethon import errors
from telethon.tl import functions, types

TREE = {
    "": ("Report", [
        ("1", "I don't like it"),
        ("2", "Child abuse"),
        ("3", "Violence"),
        ("4", "Illegal goods and services"),
        ("5", "Illegal adult content"),
        ("6", "Personal data"),
        ("7", "Scam or fraud"),
        ("8", "Copyright"),
        ("9", "Spam"),
        ("a", "Other"),
        ("b", "It's not illegal, but must be taken down"),
    ]),
    "2": ("Child abuse", [("21", "Child sexual abuse"), ("22", "Child physical abuse")]),
    "3": ("Violence", [
        ("31", "Insults or false information"),
        ("32", "Graphic or disturbing content"),
        ("33", "Extreme violence, dismemberment"),
        ("34", "Hate speech or symbols"),
        ("35", "Calling for violence"),
        ("36", "Organized crime"),
        ("37", "Terrorism"),
        ("38", "Animal abuse"),
    ]),
    "4": ("Illegal goods and services", [
        ("41", "Weapons"),
        ("42", "Drugs"),
        ("43", "Fake documents"),
        ("44", "Counterfeit money"),
        ("45", "Hacking tools and malware"),
        ("46", "Counterfeit merchandise"),
        ("47", "Other goods and services"),
    ]),
    "41": ("Weapons", [
        ("411", "Firearms and accessories"),
        ("412", "Melee weapons"),
        ("413", "Non-lethal weapons"),
        ("414", "Other weapons"),
    ]),
    "42": ("Drugs", [
        ("421", "Nicotine products"),
        ("422", "Illegal drugs"),
        ("423", "Other substances"),
    ]),
    "5": ("Illegal adult content", [
        ("56", "Child abuse"),
        ("52", "Illegal sexual services"),
        ("55", "Animal abuse"),
        ("53", "Non-consensual sexual imagery"),
        ("57", "Pornography"),
        ("54", "Other illegal sexual content"),
    ]),
    "6": ("Personal data", [
        ("61", "Private images"),
        ("62", "Phone number"),
        ("63", "Address"),
        ("64", "Stolen data or credentials"),
        ("65", "Other personal information"),
    ]),
    "7": ("Scam or fraud", [
        ("71", "Impersonation"),
        ("72", "Deceptive or unrealistic financial claims"),
        ("73", "Malware, phishing"),
        ("74", "Fraudulent seller, product or service"),
    ]),
    "9": ("Spam", [
        ("93", "Insults or false information"),
        ("91", "Promoting illegal content"),
        ("92", "Promoting other content"),
    ]),
    "a": ("Other", [
        ("a3", "I don't like it"),
        ("a1", "False information or defamation"),
        ("a4", "Illegal adult content"),
        ("a5", "Illegal goods and services"),
        ("a2", "Something else"),
    ]),
}
LEAVES = set()
for _k, (_t, _kids) in TREE.items():
    for _ok, _ot in _kids:
        if _ok not in TREE:
            LEAVES.add(_ok)

ALL_REPORT_REASONS = (
    types.InputReportReasonSpam,
    types.InputReportReasonViolence,
    types.InputReportReasonPornography,
    types.InputReportReasonChildAbuse,
    types.InputReportReasonOther,
    types.InputReportReasonCopyright,
    types.InputReportReasonGeoIrrelevant,
    types.InputReportReasonFake,
    types.InputReportReasonIllegalDrugs,
    types.InputReportReasonPersonalDetails,
)

CHANNEL = types.InputPeerChannel(channel_id=1234567, access_hash=99)
USER = types.InputPeerUser(user_id=7654321, access_hash=77)
BAD_PEER = types.InputPeerChannel(channel_id=1, access_hash=1)
VALID_MSG_IDS = {10, 11, 12}


def translated_tree(lang: str, translations: dict, *, key_map=None):
    key_map = key_map or {}

    def k(x):
        return key_map.get(x, x)

    out = {}
    for parent, (title, kids) in TREE.items():
        out[k(parent)] = (
            title,
            [(k(ok), (translations.get(ok) or {}).get(lang, ot)) for ok, ot in kids],
        )
    return out


class FakeTelegram:

    def __init__(self, *, tree=None, valid_ids=None, peer_report_errors=None, reject_options=(), members=None):
        self.tree = tree or TREE
        self.valid_ids = set(valid_ids or VALID_MSG_IDS)
        self.calls = []
        self.peer_report_errors = dict(peer_report_errors or {})
        self.reject_options = set(reject_options)
        self.members = members

    def _option_step(self, option: bytes, message: str):
        key = bytes(option or b"").decode("utf-8", "ignore")
        if key in self.reject_options:
            raise errors.OptionInvalidError(request=None)
        if key in self.tree:
            title, kids = self.tree[key]
            return types.ReportResultChooseOption(
                title=title,
                options=[types.MessageReportOption(text=t, option=k.encode()) for k, t in kids],
            )
        known = {k for _t, kids in self.tree.values() for k, _x in kids}
        if key not in known:
            raise errors.OptionInvalidError(request=None)
        if not message:
            return types.ReportResultAddComment(option=key.encode(), optional=True)
        return types.ReportResultReported()

    async def handle(self, req):
        if isinstance(req, functions.messages.ReportRequest):
            self.calls.append(("messages.report", req.peer, list(req.id), bytes(req.option), req.message))
            if req.peer == BAD_PEER:
                raise errors.PeerIdInvalidError(request=None)
            if not req.id or any(i not in self.valid_ids for i in req.id):
                raise errors.MessageIdInvalidError(request=None)
            return self._option_step(req.option, req.message)
        if isinstance(req, functions.stories.ReportRequest):
            self.calls.append(("stories.report", req.peer, list(req.id), bytes(req.option), req.message))
            return self._option_step(req.option, req.message)
        if isinstance(req, functions.account.ReportPeerRequest):
            self.calls.append(("account.reportPeer", req.peer, type(req.reason).__name__, req.message))
            if req.peer == BAD_PEER:
                raise errors.PeerIdInvalidError(request=None)
            if not isinstance(req.reason, ALL_REPORT_REASONS):
                raise errors.RPCError(request=None, message="REASON_INVALID", code=400)
            err = self.peer_report_errors.get(type(req.reason).__name__)
            if err is not None:
                raise err
            return True
        if isinstance(req, functions.channels.LeaveChannelRequest):
            self.calls.append(("channels.leave", req.channel))
            if self.members is not None and not self.members:
                raise errors.UserNotParticipantError(request=None)
            return None
        if isinstance(req, functions.account.UpdateStatusRequest):
            return True
        if isinstance(req, functions.messages.GetMessagesViewsRequest):
            return None
        if isinstance(req, functions.messages.StartBotRequest):
            return None
        raise AssertionError(f"unexpected request {type(req).__name__}")


class _Msg:
    def __init__(self, mid, text="post"):
        self.id = mid
        self.message = text


class FakeUserClient:
    def __init__(self, server: FakeTelegram, *, history_ids=(10,)):
        self.server = server
        self.history_ids = list(history_ids)
        self._connected = True

    async def __call__(self, req, *a, **kw):
        return await self.server.handle(req)

    def is_connected(self):
        return self._connected

    async def connect(self):
        self._connected = True

    async def disconnect(self):
        self._connected = False

    async def get_messages(self, peer, ids=None, limit=None, **kw):
        if ids is not None:
            return [_Msg(i) for i in ids]
        return [_Msg(i) for i in self.history_ids]

    async def get_entity(self, peer):
        return peer

    async def get_input_entity(self, peer):
        return peer

    async def send_message(self, *a, **kw):
        return None

    async def iter_dialogs(self, *a, **kw):
        if False:
            yield None


def load_bot(src_dir: str, main_file: str = "main-v6.py", mod_name: str = "botmain"):
    import mongomock
    import pymongo
    import redis.asyncio as redis_asyncio
    import fakeredis
    import telethon

    work = tempfile.mkdtemp(prefix="botrun_")
    for name in ("translations.json", "report_options_translations.json", "emoji_patch.py"):
        shutil.copy(os.path.join(src_dir, name), work)
    shutil.copy(os.path.join(src_dir, "config.example.py"), os.path.join(work, "config.py"))
    shutil.copy(os.path.join(src_dir, main_file), os.path.join(work, mod_name + ".py"))

    class _Mongo(mongomock.MongoClient):
        def __init__(self, *a, **kw):
            super().__init__()

        @property
        def admin(self):
            class _A:
                def command(self, *a, **kw):
                    return {"ok": 1}
            return _A()

    pymongo.MongoClient = _Mongo

    class _Redis:
        @staticmethod
        def from_url(*a, **kw):
            return fakeredis.aioredis.FakeRedis(decode_responses=True)

    redis_asyncio.Redis = _Redis
    telethon.TelegramClient.start = lambda self, *a, **kw: self

    os.chdir(work)
    sys.path.insert(0, work)
    for m in ("config", "emoji_patch", mod_name):
        sys.modules.pop(m, None)
    spec = importlib.util.spec_from_file_location(mod_name, os.path.join(work, mod_name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def wire(mod, server: FakeTelegram, *, peer=CHANNEL, history_ids=(10,)):
    cli = FakeUserClient(server, history_ids=history_ids)

    async def connect_session_client(session_id):
        return cli, None

    async def join_group(client, target, sess_key=None, skip_join=False, send_join_request=False):
        return peer

    async def resolve_profile(client, target, sess_key=None):
        return peer

    async def resolve_story(cli_, target, sess_key, skip_join=False):
        return peer

    async def drop_account(*a, **kw):
        return None

    async def no_pause(*a, **kw):
        return None

    mod.connect_session_client = connect_session_client
    mod.join_group = join_group
    mod._resolve_profile_peer = resolve_profile
    mod._resolve_story_peer = resolve_story
    mod.drop_account = drop_account
    mod._report_step_pause = no_pause
    mod._global_report_gate = no_pause
    mod.sess_phone = lambda s: f"+{s}"
    mod.retern_client = lambda *a, **kw: cli
    return cli
