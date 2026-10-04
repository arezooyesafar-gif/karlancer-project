import html, json, logging, mimetypes, os, re, io
import httpx
from telethon import Button, TelegramClient, events, types
from telethon.tl.custom import Message

log = logging.getLogger('emoji_patch')
log.addHandler(logging.NullHandler())

_MARKER = re.compile(r'(\S[\uFE0F\uFE0E\u20E3\s]?[\uFE0F\uFE0E\u20E3]?)\\?\[(-?\d+)\\?\]')
_ANY_MARKER = re.compile(r'\\?\[-?\d+\\?\]')
_STRIP = re.compile(r'[\u200b\u200e\u200f\ufeff\r\t]')
_BTN_TEXT = {}
_BTN_COPY = {}
_PATCHED = set()

_IMAGE_EXTS={'.jpg','.jpeg','.png','.webp'}
_VIDEO_EXTS={'.mp4','.mov'}
_ANIM_EXTS={'.gif'}
_AUDIO_EXTS={'.mp3','.m4a','.ogg','.wav'}

_EXPECTED_NOISE = (
    "message can't be edited",
    'message is not modified',
    'fallback without premium/html',
    'Bot API failed; Telethon plain fallback',
)

def _warn(where, detail=None, exc=None):
    try:
        msg = f'emoji_patch: {where}' + (f' | {detail}' if detail else '')
        if detail and any(n in str(detail) for n in _EXPECTED_NOISE):
            log.debug(msg)
            return
        log.warning(msg, exc_info=exc if exc else None)
    except Exception:
        pass

def _uid(v):
    return str(int(v) & 0xFFFFFFFFFFFFFFFF)

def _clean(s):
    return _STRIP.sub('', str(s or ''))

def _has_marker(s):
    return isinstance(s, str) and bool(_ANY_MARKER.search(s))

def _to_html(text):
    text = html.escape(_clean(text), quote=False)
    text = re.sub(r'\*\*(.+?)\*\*', lambda m: f'<b>{m.group(1)}</b>', text, flags=re.S)
    text = re.sub(r'`([^`]+)`', lambda m: f'<code>{m.group(1)}</code>', text, flags=re.S)
    return _MARKER.sub(lambda m: f'<tg-emoji emoji-id="{_uid(m.group(2))}">{html.escape(m.group(1).strip(), quote=False)}</tg-emoji>', text)

def _plain(text):
    if text is None: return ''
    text = str(text)
    text = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', lambda m: m.group(1), text, flags=re.I|re.S)
    text = _MARKER.sub(lambda m: m.group(1).strip(), text)
    text = _ANY_MARKER.sub('', text)
    text = re.sub(r'<b>(.*?)</b>', r'**\1**', text, flags=re.I|re.S)
    text = re.sub(r'<code>(.*?)</code>', r'`\1`', text, flags=re.I|re.S)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text)

def _button_visible_text(raw, drop_premium=False):
    raw = _clean(raw)
    m = _MARKER.search(raw)
    if m:
        if drop_premium:
            return _plain(raw)
        txt = _MARKER.sub('', raw)
        txt = _ANY_MARKER.sub('', txt)
        txt = re.sub(r'\s+', ' ', txt).strip()
        if txt:
            return '\u00A0' + txt if m.start() == 0 else txt
        return '\u2060'
    return _plain(raw) or '\u2060'

def _iter_rows(buttons):
    if not buttons: return
    rows = buttons if isinstance(buttons, list) else [buttons]
    for row in rows:
        if hasattr(row, 'buttons'): yield list(row.buttons)
        elif isinstance(row, list): yield row
        else: yield [row]

def _unwrap_btn(btn):

    return getattr(btn, 'button', btn)

def _btn_type_obj(inner):

    return getattr(inner, 'type', None)

def _btn_type_name(inner):
    t = _btn_type_obj(inner)
    if t is not None:
        return type(t).__name__
    return type(inner).__name__

def _callback_data(inner):

    data = getattr(inner, 'data', None)
    if data is not None:
        return data

    t = _btn_type_obj(inner)
    return getattr(t, 'data', None) if t is not None else None

def _btn_url(inner):
    url = getattr(inner, 'url', None)
    if url is not None:
        return url
    t = _btn_type_obj(inner)
    return getattr(t, 'url', None) if t is not None else None

def _btn_switch_query(inner):
    q = getattr(inner, 'query', None)
    if q is not None:
        return q
    t = _btn_type_obj(inner)
    return getattr(t, 'query', None) if t is not None else None

def _is_inline_btn(btn):
    inner = _unwrap_btn(btn)
    name = type(inner).__name__

    if name == 'KeyboardInlineButton':
        return True
    tname = _btn_type_name(inner)
    if tname.startswith('InlineButtonType'):
        return True

    if name in {
        'KeyboardButtonCallback',
        'KeyboardButtonUrl',
        'KeyboardButtonSwitchInline',
        'KeyboardButtonUrlAuth',
        'KeyboardButtonUserProfile',
        'KeyboardButtonBuy',
        'KeyboardButtonGame',
        'KeyboardButtonWebView',
        'KeyboardButtonSimpleWebView',
        'KeyboardButtonCopy',
    }:
        return True
    if _callback_data(inner) is not None or _btn_url(inner) is not None:
        return True
    if getattr(btn, 'is_inline', None) is True:
        return True
    return False

def _is_reply_btn(btn):
    if _is_inline_btn(btn):
        return False
    inner = _unwrap_btn(btn)
    name = type(inner).__name__
    tname = _btn_type_name(inner)

    if name == 'KeyboardButton':
        return True
    if tname.startswith('ButtonType'):
        return True

    if name in {
        'KeyboardButton',
        'KeyboardButtonRequestPhone',
        'KeyboardButtonRequestGeoLocation',
        'KeyboardButtonRequestPoll',
        'KeyboardButtonRequestPeer',
    }:
        return True
    if getattr(btn, 'is_inline', None) is False:
        return True

    if type(btn).__name__ == 'Button' and hasattr(btn, 'button'):
        return True
    return False

def _btn_text(btn):
    if id(btn) in _BTN_TEXT:
        return _clean(_BTN_TEXT[id(btn)])
    inner = _unwrap_btn(btn)
    t = getattr(inner, 'text', getattr(btn, 'text', ''))
    return _clean('' if callable(t) else t)

def _has_reply_keyboard(buttons):
    return any(_is_reply_btn(b) for row in (_iter_rows(buttons) or []) for b in row)

def _buttons_have_marker(buttons):
    return any(_has_marker(_btn_text(b)) for row in (_iter_rows(buttons) or []) for b in row)

def _label(text):
    text = _clean(text)
    m = _MARKER.search(text)
    eid = _uid(m.group(2)) if m else None
    if m: text = (text[:m.start()] + m.group(1).strip() + text[m.end():]).strip()
    text = _ANY_MARKER.sub('', text)
    text = re.sub(r'(\*\*|__|~~|```|`)', '', text)
    text = re.sub(r'\[([^\[\]]*?)\]\((?:[\s\S]*?)\)', lambda x: x.group(1), text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text or '\u2060', eid

def _inline_kb(buttons, drop_premium=False):
    if not buttons or _has_reply_keyboard(buttons):
        return None
    out = []
    for row in _iter_rows(buttons) or []:
        r = []
        for btn in row:
            inner = _unwrap_btn(btn)
            raw_text = _btn_text(btn)
            text, eid = _label(raw_text)
            text = _button_visible_text(raw_text, drop_premium) if eid else text
            d = None
            tname = _btn_type_name(inner)
            name = type(inner).__name__

            if id(btn) in _BTN_COPY:
                d = {'text': text, 'copy_text': {'text': str(_BTN_COPY[id(btn)])}}
            elif tname in ('InlineButtonTypeCopy',) or name == 'KeyboardButtonCopy':

                d = {'text': text, 'callback_data': 'copy_placeholder'}
            elif tname in ('InlineButtonTypeUrl', 'InlineButtonTypeUrlAuth') or name in (
                'KeyboardButtonUrl',
                'KeyboardButtonUrlAuth',
            ):
                url = _btn_url(inner) or ''
                d = {'text': text, 'url': url}
            elif tname in ('InlineButtonTypeSwitchInline',) or name == 'KeyboardButtonSwitchInline':
                d = {'text': text, 'switch_inline_query': _btn_switch_query(inner) or ''}
            elif tname in ('InlineButtonTypeCallback',) or name == 'KeyboardButtonCallback' or _callback_data(inner) is not None:
                data = _callback_data(inner) or b''
                if isinstance(data, bytes):
                    data = data.decode('utf-8', 'ignore')
                d = {'text': text, 'callback_data': str(data)}
            elif tname in ('InlineButtonTypeWebView',) or name in (
                'KeyboardButtonWebView',
                'KeyboardButtonSimpleWebView',
            ):
                url = _btn_url(inner) or getattr(_btn_type_obj(inner), 'url', '') or ''
                d = {'text': text, 'web_app': {'url': url}}
            else:

                _warn('_inline_kb', f'skip unknown button {name}/{tname}')
                continue

            if eid and not drop_premium:
                d['icon_custom_emoji_id'] = eid
            r.append(d)
        if r:
            out.append(r)
    return out or None

def _reply_kb(buttons, drop_premium=False):
    if not buttons or not _has_reply_keyboard(buttons):
        return None
    keyboard = []
    resize = False
    one_time = False
    selective = False
    for row in _iter_rows(buttons) or []:
        r = []
        for btn in row:
            inner = _unwrap_btn(btn)
            raw = _btn_text(btn)
            text, eid = _label(raw)
            text = _button_visible_text(raw, drop_premium) if eid else (_plain(raw) or text or '\u2060')
            item = {'text': text}
            if eid and not drop_premium:
                item['icon_custom_emoji_id'] = eid
            tname = _btn_type_name(inner)
            name = type(inner).__name__
            if tname in ('ButtonTypeRequestPhone',) or name == 'KeyboardButtonRequestPhone':
                item = {'text': text, 'request_contact': True}
                if eid and not drop_premium:
                    item['icon_custom_emoji_id'] = eid
            elif tname in ('ButtonTypeRequestGeoLocation',) or name == 'KeyboardButtonRequestGeoLocation':
                item = {'text': text, 'request_location': True}
                if eid and not drop_premium:
                    item['icon_custom_emoji_id'] = eid
            r.append(item)
            resize = resize or bool(getattr(btn, 'resize', False))
            one_time = one_time or bool(getattr(btn, 'single_use', False))
            selective = selective or bool(getattr(btn, 'selective', False))
        if r:
            keyboard.append(r)
    if not keyboard:
        return None
    out = {'keyboard': keyboard, 'resize_keyboard': True if resize else True}
    if one_time:
        out['one_time_keyboard'] = True
    if selective:
        out['selective'] = True
    return out

def _should_api(text=None, buttons=None):

    return _has_marker(text) or _buttons_have_marker(buttons)

def _method(ext, force=False):
    ext=(ext or '').lower()
    if not force:
        if ext in _IMAGE_EXTS: return 'sendPhoto','photo'
        if ext in _VIDEO_EXTS: return 'sendVideo','video'
        if ext in _ANIM_EXTS: return 'sendAnimation','animation'
        if ext in _AUDIO_EXTS: return 'sendAudio','audio'
    return 'sendDocument','document'

def _resolve_file(file, force=False, attrs=None):
    name_override=None
    for a in attrs or []:
        if hasattr(a,'file_name'): name_override=a.file_name
    if isinstance(file,str):
        if file.startswith(('http://','https://')):
            m,f=_method(os.path.splitext(file.split('?')[0])[1], force); return f,file,True,m,None
        if os.path.isfile(file):
            m,f=_method(os.path.splitext(file)[1], force); h=open(file,'rb'); return f,(name_override or os.path.basename(file),h,mimetypes.guess_type(file)[0] or 'application/octet-stream'),False,m,h
    if isinstance(file, bytes):
        m,f=_method(os.path.splitext(name_override or 'file')[1], force); return f,(name_override or 'file',file,'application/octet-stream'),False,m,None
    if hasattr(file,'read'):
        name=name_override or getattr(file,'name','file'); m,f=_method(os.path.splitext(str(name))[1], force); return f,(os.path.basename(str(name)),file.read(),'application/octet-stream'),False,m,None
    return None,None,False,None,None

class BotAPIMessageProxy:
    def __init__(self, client, api, data, chat_id):
        self._client=client; self._api=api; self._data=data or {}; self.chat_id=chat_id; self.id=self._data.get('message_id',0); self.message=self._data.get('text') or self._data.get('caption') or ''; self.text=self.message; self.raw_text=self.message
    def __bool__(self): return True
    def __getattr__(self,n):
        if n in self._data: return self._data[n]
        raise AttributeError(n)
    async def edit(self, text='', buttons=None, **kw): return await self._api.edit_message(self.chat_id,self.id,text,buttons=buttons,**kw)
    async def respond(self, message='', buttons=None, **kw): return await self._api.send_message(self.chat_id,message,buttons=buttons,**kw)
    async def reply(self, message='', buttons=None, **kw): kw.setdefault('reply_to_message_id',self.id); return await self._api.send_message(self.chat_id,message,buttons=buttons,**kw)
    async def delete(self): return await self._client.delete_messages(self.chat_id,self.id)

class BotAPIFailureProxy:
    id=0; message=''; text=''; raw_text=''
    def __init__(self,data=None): self._data=data or {}
    def __bool__(self): return False
    def __getattr__(self,n): return None

class BotAPI:
    def __init__(self, token):
        self.base='https://api.telegram.org/bot'+re.sub(r'[}\s\r\n\t]','',str(token or ''))
        self.http=httpx.AsyncClient(timeout=120, trust_env=False)
    async def _post(self, method, payload=None, data=None, files=None):
        try:
            if files is None: r=await self.http.post(f'{self.base}/{method}', json=payload)
            else: r=await self.http.post(f'{self.base}/{method}', data=data, files=files)
            try: out=r.json()
            except Exception as e: _warn(method, f'HTTP {r.status_code} invalid json: {r.text[:250]}', e); return {'ok':False,'description':'invalid json'}
            if not out.get('ok'): _warn(method, out.get('description', out))
            return out
        except Exception as e:
            _warn(method, 'request failed', e); return {'ok':False,'description':str(e)}
    async def send_message(self, chat_id, text='', buttons=None, **kw):
        p={'chat_id':chat_id,'text':_to_html(text) or ' ','parse_mode':'HTML'}
        kb=_inline_kb(buttons)
        if kb: p['reply_markup']={'inline_keyboard':kb}
        else:
            rk=_reply_kb(buttons)
            if rk: p['reply_markup']=rk
        p.update({k:v for k,v in kw.items() if v is not None and k!='buttons'})
        res=await self._post('sendMessage', payload=p)
        if not res.get('ok'):
            p['text']=_plain(text) or ' '; p.pop('parse_mode',None); kb=_inline_kb(buttons, True)
            if kb: p['reply_markup']={'inline_keyboard':kb}
            else:
                rk=_reply_kb(buttons, True)
                if rk: p['reply_markup']=rk
            _warn('sendMessage','fallback without premium/html')
            res=await self._post('sendMessage', payload=p)
        return res
    async def edit_message(self, chat_id, msg_id, text='', buttons=None, **kw):
        p={'chat_id':chat_id,'message_id':msg_id,'text':_to_html(text) or ' ','parse_mode':'HTML'}
        kb=_inline_kb(buttons)
        if kb: p['reply_markup']={'inline_keyboard':kb}
        p.update({k:v for k,v in kw.items() if v is not None and k!='buttons'})
        res=await self._post('editMessageText', payload=p)
        if res.get('ok'):
            return res
        desc = (res.get('description') or '').lower()

        if any(x in desc for x in (
            'flood', 'too many', 'not modified', 'message is not modified',
            'document_invalid', 'can\'t parse',
        )):
            _warn('editMessageText', res.get('description', res))
            return res
        p['text']=_plain(text) or ' '; p.pop('parse_mode',None); kb=_inline_kb(buttons, True)
        if kb: p['reply_markup']={'inline_keyboard':kb}
        _warn('editMessageText','fallback without premium/html')
        res=await self._post('editMessageText', payload=p)
        return res
    async def send_media(self, chat_id, method, field, value, is_url, caption=None, buttons=None, **kw):
        if is_url:
            p={'chat_id':chat_id,field:value}
            if caption: p.update({'caption':_to_html(caption),'parse_mode':'HTML'})
            kb=_inline_kb(buttons)
            if kb: p['reply_markup']={'inline_keyboard':kb}
            p.update({k:v for k,v in kw.items() if v is not None})
            return await self._post(method, payload=p)
        d={'chat_id':str(chat_id)}
        if caption: d.update({'caption':_to_html(caption),'parse_mode':'HTML'})
        kb=_inline_kb(buttons)
        if kb: d['reply_markup']=json.dumps({'inline_keyboard':kb})
        d.update({k:str(v) for k,v in kw.items() if v is not None})
        return await self._post(method, data=d, files={field:value})

def _proxy(client, api, res, chat_id):
    if res and res.get('ok'):
        if isinstance(res.get('result'), bool): return res['result']
        return BotAPIMessageProxy(client, api, res.get('result') or {}, chat_id)
    return BotAPIFailureProxy(res)

def apply_patch(client: TelegramClient, token: str):
    if id(client) in _PATCHED: return client
    _PATCHED.add(id(client))
    api=BotAPI(token)

    otxt=Button.text
    def ptxt(text,*a,**kw):
        btn=otxt(_plain(text) if _has_marker(text) else text,*a,**kw)
        _BTN_TEXT[id(btn)]=text
        return btn
    Button.text=ptxt

    oi=Button.inline
    def pi(text,*a,**kw):
        btn=oi(_plain(text) if _has_marker(text) else text,*a,**kw); _BTN_TEXT[id(btn)]=text; return btn
    Button.inline=pi
    ou=Button.url
    def pu(text,url,*a,**kw):
        btn=ou(_plain(text) if _has_marker(text) else text,url,*a,**kw); _BTN_TEXT[id(btn)]=text; return btn
    Button.url=pu
    osw=Button.switch_inline
    def psw(text,*a,**kw):
        btn=osw(_plain(text) if _has_marker(text) else text,*a,**kw); _BTN_TEXT[id(btn)]=text; return btn
    Button.switch_inline=psw
    def pcopy(text,value,style=None):
        btn=Button.inline(_plain(text), data=b'copy_placeholder'); _BTN_TEXT[id(btn)]=text; _BTN_COPY[id(btn)]=value; return btn
    Button.copy=pcopy

    orig_send=client.send_message
    async def send(entity, message='', buttons=None, **kw):
        if _should_api(message, buttons):
            chat_id=await client.get_peer_id(entity); res=await api.send_message(chat_id,message,buttons=buttons,**kw)
            if res and res.get('ok'): return _proxy(client,api,res,chat_id)
            _warn('client.send_message','Bot API failed; Telethon plain fallback')
            return await orig_send(entity,_plain(message),buttons=buttons,**kw)
        return await orig_send(entity,message,buttons=buttons,**kw)
    client.send_message=send

    orig_edit=client.edit_message
    async def edit(entity, message=None, text=None, buttons=None, **kw):
        body=text if text is not None else '' ; mid=message.id if hasattr(message,'id') else message
        if _should_api(body, buttons):
            chat_id=await client.get_peer_id(entity); res=await api.edit_message(chat_id,mid,body,buttons=buttons,**kw)
            if res and res.get('ok'): return _proxy(client,api,res,chat_id)
            _warn('client.edit_message','Bot API failed; Telethon plain fallback')
            return await orig_edit(entity,message,text=_plain(body),buttons=buttons,**kw)
        return await orig_edit(entity,message,text=text,buttons=buttons,**kw)
    client.edit_message=edit

    orig_file=client.send_file
    async def send_file(entity, file, caption=None, buttons=None, **kw):
        if _should_api(caption, buttons) and isinstance(caption,(str,type(None))):
            field,value,is_url,method,h=_resolve_file(file, kw.get('force_document',False), kw.get('attributes'))
            if field:
                chat_id=await client.get_peer_id(entity)
                try:
                    res=await api.send_media(chat_id,method,field,value,is_url,caption,buttons,**kw)
                    if res and res.get('ok'): return _proxy(client,api,res,chat_id)
                    _warn('client.send_file','Bot API failed; Telethon plain fallback')
                finally:
                    if h: h.close()
            return await orig_file(entity,file,caption=_plain(caption),buttons=buttons,**kw)
        return await orig_file(entity,file,caption=caption,buttons=buttons,**kw)
    client.send_file=send_file

    async def nm_respond(self, message='', buttons=None, **kw):
        return await client.send_message(self.chat_id, message, buttons=buttons, **kw)
    async def nm_reply(self, message='', buttons=None, **kw):
        rid=getattr(getattr(self,'message',None),'id',None) or getattr(self,'id',None)
        if rid and 'reply_to' not in kw and 'reply_to_message_id' not in kw:
            kw['reply_to']=rid
        return await client.send_message(self.chat_id, message, buttons=buttons, **kw)
    events.NewMessage.Event.respond=nm_respond
    events.NewMessage.Event.reply=nm_reply

    omr=Message.reply
    async def mr(self, message='', buttons=None, **kw):
        if _should_api(message, buttons):
            kw.setdefault('reply_to_message_id', self.id)
            res=await api.send_message(self.chat_id,message,buttons=buttons,**kw)
            if res and res.get('ok'): return _proxy(client,api,res,self.chat_id)
            return await omr(self,_plain(message),buttons=buttons,**kw)
        return await omr(self,message,buttons=buttons,**kw)
    Message.reply=mr
    omresp=Message.respond
    async def mresp(self, message='', buttons=None, **kw):
        return await client.send_message(self.chat_id,message,buttons=buttons,**kw)
    Message.respond=mresp
    ome=Message.edit
    async def me(self, text='', buttons=None, **kw):
        if _should_api(text, buttons):
            res=await api.edit_message(self.chat_id,self.id,text,buttons=buttons,**kw)
            if res and res.get('ok'): return _proxy(client,api,res,self.chat_id)
            return await ome(self,_plain(text),buttons=buttons,**kw)
        return await ome(self,text,buttons=buttons,**kw)
    Message.edit=me

    oce=events.CallbackQuery.Event.edit
    async def ce(self, text='', buttons=None, **kw):
        if _should_api(text, buttons):
            chat_id=await client.get_peer_id(await self.get_input_chat()); res=await api.edit_message(chat_id,self.message_id,text,buttons=buttons,**kw)
            if res and res.get('ok'): return _proxy(client,api,res,chat_id)
            return await oce(self,_plain(text),buttons=buttons,**kw)
        return await oce(self,text,buttons=buttons,**kw)
    events.CallbackQuery.Event.edit=ce
    ocr=events.CallbackQuery.Event.respond
    async def cr(self, message='', buttons=None, **kw):
        if _should_api(message, buttons):
            chat_id=await client.get_peer_id(await self.get_input_chat()); res=await api.send_message(chat_id,message,buttons=buttons,**kw)
            if res and res.get('ok'): return _proxy(client,api,res,chat_id)
            return await ocr(self,_plain(message),buttons=buttons,**kw)
        return await ocr(self,message,buttons=buttons,**kw)
    events.CallbackQuery.Event.respond=cr
    ocbr=events.CallbackQuery.Event.reply
    async def cbr(self, message='', buttons=None, **kw):
        if _should_api(message, buttons):
            chat_id=await client.get_peer_id(await self.get_input_chat()); kw.setdefault('reply_to_message_id',self.message_id); res=await api.send_message(chat_id,message,buttons=buttons,**kw)
            if res and res.get('ok'): return _proxy(client,api,res,chat_id)
            return await ocbr(self,_plain(message),buttons=buttons,**kw)
        return await ocbr(self,message,buttons=buttons,**kw)
    events.CallbackQuery.Event.reply=cbr
    oca=events.CallbackQuery.Event.answer
    async def ca(self, message=None, cache_time=0, *, url=None, alert=False):
        return await oca(self, _plain(message) if isinstance(message,str) else message, cache_time, url=url, alert=alert)
    events.CallbackQuery.Event.answer=ca
    return client
