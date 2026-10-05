# دلایل ریپورت (Report Reasons) — Scam ≠ Fake

## علت مشکل در نسخهٔ قبلی

| مشکل | کجای کد | نتیجه |
|---|---|---|
| Scam به Fake وصل بود | `_AI_REASON_STEP["scam"] = {"key": "fake", ...}` | Scam و Fake یک مسیر داشتند |
| ریپورت کانال در حالت Scam همیشه Fake بود | `run_path_with_client` → `_report_peer_legacy(..., force_reason=InputReportReasonFake())` | هر ریپورت Scam با دلیل Fake به `account.reportPeer` می‌رفت |
| انتخاب گزینهٔ تلگرام با شباهت متن بود، با کلیدهای ساختگی (`"fake"`, `"spam"`, `"porn"` …) | `_auto_build_scam_path` + `_match_report_choice` | «scam» با «spam» شبیه است (difflib ≈ 0.75)، پس Scam، Fake، Porn و Drugs روی گزینهٔ **Spam** می‌رفتند؛ اگر چیزی پیدا نمی‌شد اولین گزینه (`payload[0]`) انتخاب می‌شد |
| تبدیل مسیر به ReportReason با کلمه‌کلیدی ناقص | `_peer_reason_from_option` / `_reason_from_path` | فقط Drugs (`drug` / `مواد`) و تا حدی Porn درست درمی‌آمدند. Spam، Personal و Copyright → Other؛ «متقلب» و «تقلبی» → Fake؛ «سوءاستفاده جنسی از کودک» → Pornography |
| Copyright و Scam در منوی ریپورت پروفایل نبودند | `_PEER_REPORT_REASONS` | — |
| خطاهای ریپورت پنهان می‌شدند | `sample_step` هر RPCError را `JOIN_FAILED:...` برمی‌گرداند؛ Telethon برای `OptionInvalidError` مقدار `message="BAD_REQUEST"` می‌دهد | اسم واقعی خطا (OPTION_INVALID …) در لاگ نبود |

**چرا Illegal Drugs کار می‌کرد:** در `_reason_from_path` فقط کلمه‌های `drug` و `مواد` هم در متن انگلیسی و هم فارسیِ همهٔ زیرگزینه‌های Drugs هست. بقیه یا کلمه‌شان در متن گزینهٔ آخر نبود (مثلاً «Promoting other content» زیر Spam)، یا ترتیب بررسی غلط بود (`جنسی` قبل از `کودک`).

## API تلگرام (Telethon 1.45 / layer 229)

دو API جدا وجود دارد:

1. **`messages.report` / `stories.report`**: تلگرام منو برمی‌گرداند و بات بایت `option` گزینه را می‌فرستد.
   «Scam or fraud» یک گزینهٔ اصلی است. زیرگزینه‌هایش:
   `Impersonation`، `Deceptive or unrealistic financial claims`، `Malware, phishing`، `Fraudulent seller, product or service`.
2. **`account.reportPeer`**: دلیل ثابت. انواع موجود:
   `Spam, Violence, Pornography, ChildAbuse, Copyright, GeoIrrelevant, Fake, IllegalDrugs, PersonalDetails, Other`.
   **`InputReportReasonScam` وجود ندارد.**

## Mapping جدید (`REPORT_REASON_SPECS` در `main-v6.py`)

| کد داخلی | messages.report (منوی تلگرام) | account.reportPeer |
|---|---|---|
| spam | Spam › Promoting other content | InputReportReasonSpam |
| violence | Violence › Graphic or disturbing content | InputReportReasonViolence |
| porn | Illegal adult content › Pornography | InputReportReasonPornography |
| child | Child abuse › Child sexual abuse | InputReportReasonChildAbuse |
| **fake** | Scam or fraud › **Impersonation** (فقط همین) | **InputReportReasonFake** |
| **scam** | Scam or fraud › Fraudulent seller / Financial claims / Phishing (**هرگز** Impersonation) | ⚠️ ندارد → `InputReportReasonOther` + متن «Scam / fraud» (**هرگز** Fake) |
| drugs | Illegal goods and services › Drugs › Illegal drugs | InputReportReasonIllegalDrugs |
| personal | Personal data › Other personal information | InputReportReasonPersonalDetails |
| copyright | Copyright | InputReportReasonCopyright |
| other | Other › Something else | InputReportReasonOther |
| geo | — | InputReportReasonGeoIrrelevant (فقط گروه‌های مکان‌محور؛ در منو نیست) |

- گزینه‌ها از منویی که خود تلگرام برمی‌گرداند انتخاب می‌شوند (اول متن fa/en/ar، بعد کلید). گزینه‌ای که مال دلیل دیگری است هیچ‌وقت انتخاب نمی‌شود.
- اگر تلگرام گزینهٔ موردنظر را نشان ندهد، گزارش ارسال **نمی‌شود** و خطای `SCAM_OPTION_UNAVAILABLE` / `FAKE_OPTION_UNAVAILABLE` / … با لیست گزینه‌های موجود در لاگ ثبت می‌شود. جایگزین حدسی یا `payload[0]` دیگر وجود ندارد.

## Scam و Fake در ربات

- منوی اصلی دو دکمهٔ جدا دارد: «💰 ریپورت اسکم» (`mode="scam"`) و «🎭 ریپورت فیک» (`mode="fake"`).
- ویزارد Scam: «Scam or fraud» را خودکار می‌زند و فقط زیرگزینه‌های کلاهبرداری را به کاربر نشان می‌دهد. دکمهٔ Impersonation، چه قدیمی و چه جعلی، رد می‌شود.
- ویزارد Fake: «Scam or fraud › Impersonation» را خودکار می‌زند.
- هندلرهای اجرا جدا هستند: `_run_scam_report` و `_run_fake_report`. هر کدام اگر مسیرش مال دلیل دیگری باشد کار را رد می‌کند (`SCAM_PATH_INVALID` / `FAKE_PATH_INVALID`).
- دسترسی `report_fake` به پنل اضافه شد. پلن‌هایی که قبلاً `report_scam` داشتند خودکار `report_fake` هم می‌گیرند.
- تحلیل AI هم `fake` و `child` را می‌شناسد. هر دلیل مسیر خودش را می‌رود (قبلاً همه با `mode="scam"` و Fake می‌رفتند).

## لاگ خطا

هر خطای ریپورت این خط را می‌نویسد:

```
report_rpc_error method=messages.report error=OPTION_INVALID code=400 class=OptionInvalidError mode=scam reason=scam option='74' step=2 peer=InputPeerChannel:123 msg_ids=[10] sess=... hint=...
```

`error=` نام واقعی خطای تلگرام است: `OPTION_INVALID`، `PEER_ID_INVALID`، `MESSAGE_ID_INVALID`، `MESSAGE_ID_REQUIRED`، `CHANNEL_PRIVATE`، `FLOOD_WAIT` و هر خطای دیگر. نتیجهٔ هر اکانت به شکل `RPC_ERROR:<NAME>:<code>` است.
ریپورت موفق این خط را می‌نویسد:

```
report_sent method=messages.report mode=scam reason=scam path=['7', '74'] ...
```

## تست

**آفلاین**: کد واقعی ربات در برابر یک سرور شبیه‌سازی‌شدهٔ تلگرام اجرا می‌شود:

```
pip install -r requirements.txt -r tests/requirements-test.txt
python -m pytest tests -q            # 98 تست
python tests/reason_matrix.py        # جدول نتیجهٔ هر دلیل
```

**روی تلگرام واقعی** (فقط ادمین): دستور زیر منوی واقعی ریپورت آن پست را با یک اکانت می‌خواند و نشان می‌دهد هر دلیل دقیقاً کدام گزینه را می‌فرستد. گزینهٔ نهایی ارسال نمی‌شود، پس **هیچ ریپورتی ثبت نمی‌شود**.

```
/reportcheck https://t.me/<channel>/<post_id>
```
