import os, time, requests, urllib3, base64, sqlite3, threading, logging, random, string, re
from dotenv import load_dotenv
try:
    from googlesearch import search as gsearch
except:
    gsearch = None

urllib3.disable_warnings()
load_dotenv(os.path.expanduser("~/mybot/.env"))

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    filename=os.path.expanduser("~/mybot/bot.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
API_URL = "https://api.groq.com/openai/v1/chat/completions"
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
ADMIN_ID = 7942156578

DAILY_FREE = 5
SPAM_LIMIT = 10
SPAM_WINDOW = 60
GIFT_BONUS = 2

# ── SQLite ───────────────────────────────────────────────────
conn = sqlite3.connect("users.db", check_same_thread=False)
conn.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT, name TEXT,
    blocked INTEGER DEFAULT 0,
    premium INTEGER DEFAULT 0,
    premium_until TEXT DEFAULT NULL,
    daily_count INTEGER DEFAULT 0,
    last_reset TEXT DEFAULT CURRENT_DATE,
    joined TEXT DEFAULT CURRENT_TIMESTAMP,
    lang TEXT DEFAULT 'uz',
    referral_by INTEGER DEFAULT NULL,
    referral_count INTEGER DEFAULT 0,
    total_requests INTEGER DEFAULT 0,
    last_gift TEXT DEFAULT NULL
)""")
conn.commit()

def db_save_user(user_id, username, name, referral_by=None):
    conn.execute("INSERT OR IGNORE INTO users (user_id, username, name, referral_by) VALUES (?,?,?,?)",
                 (user_id, username, name, referral_by))
    conn.execute("UPDATE users SET username=?, name=? WHERE user_id=?",
                 (username, name, user_id))
    conn.commit()
    if referral_by and referral_by != user_id:
        ref = conn.execute("SELECT referral_by FROM users WHERE user_id=?", (user_id,)).fetchone()
        if ref and ref[0] is None:
            conn.execute("UPDATE users SET referral_count=referral_count+1, daily_count=MAX(0,daily_count-2) WHERE user_id=?", (referral_by,))
            conn.execute("UPDATE users SET referral_by=? WHERE user_id=?", (referral_by, user_id))
            conn.commit()
            send_msg(referral_by, "🎁 Do'stingiz botga qo'shildi! +2 limit sovg'a!")

def db_is_blocked(uid):
    r = conn.execute("SELECT blocked FROM users WHERE user_id=?", (uid,)).fetchone()
    return r and r[0] == 1

def db_is_premium(uid):
    if uid == ADMIN_ID:
        return True
    r = conn.execute("SELECT premium, premium_until FROM users WHERE user_id=?", (uid,)).fetchone()
    if not r or not r[0]: return False
    if r[1] and r[1] < time.strftime("%Y-%m-%d"):
        conn.execute("UPDATE users SET premium=0 WHERE user_id=?", (uid,))
        conn.commit()
        return False
    return True

def db_get_lang(uid):
    r = conn.execute("SELECT lang FROM users WHERE user_id=?", (uid,)).fetchone()
    return r[0] if r else "uz"

def db_set_lang(uid, lang):
    conn.execute("UPDATE users SET lang=? WHERE user_id=?", (lang, uid))
    conn.commit()

def db_check_limit(uid):
    today = time.strftime("%Y-%m-%d")
    r = conn.execute("SELECT daily_count, last_reset FROM users WHERE user_id=?", (uid,)).fetchone()
    if not r: return True, DAILY_FREE
    count, last_reset = r
    if last_reset != today:
        conn.execute("UPDATE users SET daily_count=0, last_reset=? WHERE user_id=?", (today, uid))
        conn.commit()
        count = 0
    return DAILY_FREE - count > 0, DAILY_FREE - count

def db_increment(uid):
    conn.execute("UPDATE users SET daily_count=daily_count+1, total_requests=total_requests+1 WHERE user_id=?", (uid,))
    conn.commit()

def db_set_premium(uid, days=30):
    until = time.strftime("%Y-%m-%d", time.localtime(time.time() + days*86400))
    conn.execute("UPDATE users SET premium=1, premium_until=? WHERE user_id=?", (until, uid))
    conn.commit()
    return until

def db_block(uid, val=1):
    conn.execute("UPDATE users SET blocked=? WHERE user_id=?", (val, uid))
    conn.commit()

def db_stats():
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    premium = conn.execute("SELECT COUNT(*) FROM users WHERE premium=1").fetchone()[0]
    blocked = conn.execute("SELECT COUNT(*) FROM users WHERE blocked=1").fetchone()[0]
    today = conn.execute("SELECT COUNT(*) FROM users WHERE date(joined)=date('now')").fetchone()[0]
    total_req = conn.execute("SELECT SUM(total_requests) FROM users").fetchone()[0] or 0
    return total, premium, blocked, today, total_req

def db_users(limit=10):
    return conn.execute("SELECT user_id, name, username, premium, total_requests FROM users ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()

def db_daily_gift(uid):
    today = time.strftime("%Y-%m-%d")
    r = conn.execute("SELECT last_gift FROM users WHERE user_id=?", (uid,)).fetchone()
    if r and r[0] == today: return False
    conn.execute("UPDATE users SET daily_count=MAX(0,daily_count-?), last_gift=? WHERE user_id=?",
                 (GIFT_BONUS, today, uid))
    conn.commit()
    return True

# ── Offset ───────────────────────────────────────────────────
def save_offset(val):
    open(os.path.expanduser("~/mybot/offset.txt"), "w").write(str(val))

def load_offset():
    try:
        return int(open(os.path.expanduser("~/mybot/offset.txt")).read().strip())
    except:
        return 0

# ── Yordamchi ────────────────────────────────────────────────
histories = {}

def clear_histories():
    global histories
    histories = {}
spam = {}
offset = load_offset()
pending_payment = {}
user_state = {}
start_cooldown = {}
processed_ids = set()

def send_msg(chat_id, text, reply_markup=None):
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = reply_markup
    try:
        requests.post(f"{TG}/sendMessage", json=data, verify=False, timeout=10)
    except Exception as e:
        log.error(f"send_msg error: {e}")

def send_action(chat_id, action="typing"):
    try:
        requests.post(f"{TG}/sendChatAction",
                      json={"chat_id": chat_id, "action": action},
                      verify=False, timeout=5)
    except:
        pass

def get_menu(is_prem, lang="uz"):
    badge = "⭐" if is_prem else "🆓"
    menus = {
        "uz": [
            [{"text": "🤖 AI suhbat"}, {"text": "🎤 Ovoz"}, {"text": "🖼 Rasm"}],
            [{"text": "📝 Referat"}, {"text": "🎓 Kurs ishi"}],
            [{"text": "🧮 Matematik"}, {"text": "🌍 Tarjima"}],
            [{"text": "📰 Yangiliklar"}, {"text": "🎨 Rasm tavsif"}],
            [{"text": "📅 Jadval"}, {"text": "🔐 Parol"}],
            [{"text": f"{badge} Statusim"}, {"text": "🎁 Sovga"}, {"text": "📊 Tarixim"}],
            [{"text": "💱 Valyuta"}, {"text": "💳 Premium"}, {"text": "ℹ️ Yordam"}],
        ],
        "ru": [
            [{"text": "🤖 AI чат"}, {"text": "🎤 Голос"}, {"text": "🖼 Фото"}],
            [{"text": "📝 Реферат"}, {"text": "🎓 Курсовая"}],
            [{"text": "🧮 Математика"}, {"text": "🌍 Перевод"}],
            [{"text": "📰 Новости"}, {"text": "🎨 Описание"}],
            [{"text": "📅 График"}, {"text": "🔐 Пароль"}],
            [{"text": f"{badge} Статус"}, {"text": "🎁 Подарок"}, {"text": "📊 История"}],
            [{"text": "💱 Валюта"}, {"text": "💳 Премиум"}, {"text": "ℹ️ Помощь"}],
        ],
        "en": [
            [{"text": "🤖 AI Chat"}, {"text": "🎤 Voice"}, {"text": "🖼 Image"}],
            [{"text": "📝 Essay"}, {"text": "🎓 Coursework"}],
            [{"text": "🧮 Math"}, {"text": "🌍 Translate"}],
            [{"text": "📰 News"}, {"text": "🎨 Art desc"}],
            [{"text": "📅 Schedule"}, {"text": "🔐 Password"}],
            [{"text": f"{badge} Status"}, {"text": "🎁 Gift"}, {"text": "📊 History"}],
            [{"text": "💱 Currency"}, {"text": "💳 Premium"}, {"text": "ℹ️ Help"}],
        ],
    }
    return {"keyboard": menus.get(lang, menus["uz"]), "resize_keyboard": True}

def lang_keyboard():
    return {"keyboard": [[{"text": "🇺🇿 O'zbek"}, {"text": "🇷🇺 Русский"}, {"text": "🇬🇧 English"}]],
            "resize_keyboard": True, "one_time_keyboard": True}

def get_updates():
    global offset
    r = requests.get(f"{TG}/getUpdates",
                     params={"timeout": 30, "offset": offset},
                     timeout=35, verify=False)
    return r.json().get("result", [])

def get_file_url(file_id):
    r = requests.get(f"{TG}/getFile", params={"file_id": file_id}, verify=False, timeout=10)
    path = r.json()["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}"

def image_to_base64(url):
    r = requests.get(url, verify=False, timeout=15)
    return base64.b64encode(r.content).decode()

def is_spam(uid):
    now = time.time()
    times = [t for t in spam.get(uid, []) if now - t < SPAM_WINDOW]
    spam[uid] = times
    if len(times) >= SPAM_LIMIT: return True
    spam[uid].append(now)
    return False

def ask_groq(chat_id, text, system=None):
    h = histories.setdefault(chat_id, [])
    h.append({"role": "user", "content": text})
    if len(h) > 6:
        histories[chat_id] = h[-20:]
    sys_msg = system or """Sen GPT-4 darajasidagi eng aqlli AI yordamchisan. Quyidagi qoidalarga qat'iy amal qil:

1. TIL: Foydalanuvchi o'zbek tilida yozsa -> o'zbek tilida javob ber. Rus tilida yozsa -> rus tilida. Ingliz tilida yozsa -> ingliz tilida. HECH QACHON tilni o'zgartirma.

2. SUHBAT: Tabiiy, samimiy va insonday gapir. Sovuq robot kabi emas.

3. MASLAHAT: Hayot, ish, o'qish, munosabatlar haqida so'ralsa - chuqur, amaliy maslahat ber.

4. SAVOL: Aniq, to'liq javob ber. Noaniq bo'lsa, aniqlashtiruvchi savol ber.

5. QISQALIK: Kerak bo'lsa qisqa, kerak bo'lsa batafsil yoz. Ortiqcha so'z ishlatma.

6. XATO: Bilmasang, bilmasligingni ayt. To'qima."""
    try:
        r = requests.post(API_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={"model": "llama-3.3-70b-versatile",
                  "messages": [{"role": "system", "content": sys_msg}] + h,
                  "max_tokens": 2048},
            verify=False, timeout=60)
        reply = r.json()["choices"][0]["message"]["content"]
        h.append({"role": "assistant", "content": reply})
        log.info(f"chat_id={chat_id} tokens={len(text)}")
        return reply
    except Exception as e:
        log.error(f"ask_groq error: {e}")
        return f"❌ Xato: {e}"

def ask_groq_vision(chat_id, image_b64, caption):
    if caption:
        text = caption + "\n\nRasmni batafsil tahlil qil va savol tilidа javob ber."
    else:
        text = "Bu rasmda nima bor? Batafsil o'zbek tilida tushuntir."
    try:
        r = requests.post(API_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={"model": "meta-llama/llama-4-scout-17b-16e-instruct",
                  "messages": [
                      {"role": "system", "content": "Foydalanuvchi qaysi tilda yozsa, o'sha tilda javob ber. Qisqa, aniq va to'g'ridan-to'g'ri javob ber. Ortiqcha tushuntirma, kirish so'z, markdown yozma. Faqat so'ralgan narsani yoz."},
                      {"role": "user", "content": [
                          {"type": "text", "text": text},
                          {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
                      ]}
                  ], "max_tokens": 1024},
            verify=False, timeout=30)
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"❌ Xato: {e}"

def transcribe_voice(file_url):
    audio = requests.get(file_url, verify=False, timeout=15).content
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": ("voice.ogg", audio, "audio/ogg")},
            data={"model": "whisper-large-v3", "response_format": "text"},
            verify=False, timeout=30)
        return r.text.strip()
    except Exception as e:
        return None

def gen_password(length=16):
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(random.choice(chars) for _ in range(length))

def web_search_summary(query):
    try:
        prompt = f"'{query}' mavzusidagi eng so'nggi yangiliklar haqida o'zbek tilida 5 ta qisqa yangilik yoz. Har birini raqam bilan boshla. Haqiqiy va dolzarb ma'lumot ber."
        result = ask_groq(0, prompt, system="Sen yangiliklar muxbirisan. Faqat haqiqiy, so'nggi yangiliklar yoz.")
        return f"📰 <b>{query} bo'yicha yangiliklar:</b>\n\n{result}"
    except Exception as e:
        return f"❌ Xato: {e}"

def process_message(chat_id, user_id, text):
    if db_is_premium(user_id):
        return ask_groq(chat_id, text)
    can, remaining = db_check_limit(user_id)
    if not can:
        send_msg(chat_id, f"⛔ Kunlik limit tugadi!\n\n💳 Premium oling — cheksiz!\n/premium")
        return None
    db_increment(user_id)
    reply = ask_groq(chat_id, text)
    if 0 < remaining - 1 <= 3:
        reply += f"\n\n⚠️ Bugun {remaining-1} ta so'rov qoldi."
    return reply

# ── Threads ──────────────────────────────────────────────────
def premium_reminder():
    while True:
        now = time.localtime()
        if now.tm_hour == 10 and now.tm_min == 0:
            tomorrow = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
            expiring = conn.execute("SELECT user_id FROM users WHERE premium=1 AND premium_until=?", (tomorrow,)).fetchall()
            for (uid,) in expiring:
                send_msg(uid, "⚠️ Premiumingiz ertaga tugaydi! /premium")
            time.sleep(61)
        time.sleep(30)

def daily_stats():
    while True:
        now = time.localtime()
        if now.tm_hour == 9 and now.tm_min == 0:
            total, premium, blocked, today, total_req = db_stats()
            send_msg(ADMIN_ID,
                f"📊 <b>Kunlik statistika</b>\n\n"
                f"👥 Jami: {total}\n⭐ Premium: {premium}\n"
                f"🆕 Bugun: {today}\n🚫 Bloklangan: {blocked}\n"
                f"📨 Jami so'rovlar: {total_req}")
            time.sleep(61)
        time.sleep(30)

threading.Thread(target=daily_stats, daemon=True).start()
threading.Thread(target=premium_reminder, daemon=True).start()

# ── Asosiy loop ──────────────────────────────────────────────
print("Bot ishga tushdi! ✅")
log.info("Bot started")

while True:
    try:
        updates = get_updates()
        for u in updates:
            uid_upd = u["update_id"]
            if uid_upd in processed_ids:
                offset = uid_upd + 1
                save_offset(offset)
                continue
            processed_ids.add(uid_upd)
            if len(processed_ids) > 500:
                processed_ids.clear()
            offset = uid_upd + 1
            save_offset(offset)
            # CALLBACK QUERY
            cq = u.get("callback_query", {})
            if cq:
                cq_id = cq["id"]
                cq_data = cq.get("data", "")
                cq_chat = cq["message"]["chat"]["id"]
                requests.post(f"{TG}/answerCallbackQuery", json={"callback_query_id": cq_id}, verify=False)
                if cq_data.startswith("cur_"):
                    import urllib.request, json as js
                    url = "https://cbu.uz/uz/arkhiv-kursov-valyut/json/"
                    data = js.loads(urllib.request.urlopen(url, timeout=5).read())
                    rates = {r["Ccy"]: r["Rate"] for r in data}
                    code = cq_data[4:]
                    flags = {"USD":"🇺🇸","EUR":"🇪🇺","RUB":"🇷🇺","GBP":"🇬🇧","CNY":"🇨🇳","TRY":"🇹🇷","KZT":"🇰🇿"}
                    if code == "ALL":
                        msg = "💱 <b>Valyuta kurslari:</b>\n\n"
                        for c in ["USD","EUR","RUB","GBP","CNY","KZT","TRY"]:
                            if c in rates:
                                msg += f"{flags.get(c,'')} <b>{c}</b>: {float(rates[c]):,.2f} som\n"
                    elif code in rates:
                        rate = float(rates[code])
                        msg = (f"{flags.get(code,'')} <b>{code} kursi:</b>\n\n"
                               f"1 {code} = <b>{rate:,.2f} som</b>\n\n"
                               f"💡 Misollar:\n"
                               f"10 {code} = {rate*10:,.0f} som\n"
                               f"100 {code} = {rate*100:,.0f} som\n\n"
                               f"🔄 Teskari:\n"
                               f"10,000 som = {10000/rate:.2f} {code}\n"
                               f"100,000 som = {100000/rate:.2f} {code}\n"
                               f"1,000,000 som = {1000000/rate:.2f} {code}")
                    else:
                        msg = "❌ Topilmadi"
                    send_msg(cq_chat, msg)
                continue

            if "message" not in u: continue
            msg = u["message"]
            chat_id = msg.get("chat", {}).get("id")
            user = msg.get("from", {})
            user_id = user.get("id")
            username = user.get("username", "noname")
            first_name = user.get("first_name", "")

            if not chat_id or not user_id:
                continue

            text = msg.get("text", "")
            photo = msg.get("photo")
            voice = msg.get("voice")

            referral_by = None
            if text and text.startswith("/start "):
                try: referral_by = int(text.split()[1])
                except: pass

            db_save_user(user_id, username, first_name, referral_by)
            lang = db_get_lang(user_id)
            is_prem = db_is_premium(user_id)

            if db_is_blocked(user_id):
                send_msg(chat_id, "🚫 Siz bloklandingiz.")
                continue

            # ── ADMIN ────────────────────────────────────────
            if user_id == ADMIN_ID:
                if text in ["/admin", "/stats"]:
                    total, premium, blocked, today, total_req = db_stats()
                    rows = db_users(10)
                    ul = "\n".join([f"{'⭐' if r[3] else '👤'} {r[1]} @{r[2]} ({r[0]}) — {r[4]} req" for r in rows])
                    send_msg(chat_id,
                        f"👑 <b>Admin panel</b>\n\n"
                        f"👥 {total} | ⭐ {premium} | 🆕 {today} | 🚫 {blocked}\n"
                        f"📨 Jami: {total_req}\n\n<b>Oxirgi 10:</b>\n{ul}\n\n"
                        f"/premium_add [id] [kun]\n/block [id]\n/unblock [id]\n"
                        f"/broadcast [matn]\n/msg [id] [matn]")
                    continue

                if text and text.startswith("/premium_add "):
                    parts = text.split()
                    uid = int(parts[1]); days = int(parts[2]) if len(parts) > 2 else 30
                    until = db_set_premium(uid, days)
                    send_msg(chat_id, f"⭐ {uid} ga {days} kun premium!")
                    send_msg(uid, f"🎉 {days} kunlik Premium berildi!\nMuddati: {until}\n\nCheksiz foydalaning! 🚀")
                    continue

                if text and text.startswith("/block "):
                    uid = int(text.split()[1]); db_block(uid, 1)
                    send_msg(chat_id, f"🚫 {uid} bloklandi!"); continue

                if text and text.startswith("/unblock "):
                    uid = int(text.split()[1]); db_block(uid, 0)
                    send_msg(chat_id, f"✅ {uid} blokdan chiqarildi!"); continue

                if text and text.startswith("/broadcast "):
                    all_users = conn.execute("SELECT user_id FROM users WHERE blocked=0").fetchall()
                    ok, fail = 0, 0
                    for (uid,) in all_users:
                        try:
                            send_msg(uid, f"📢 <b>Xabar:</b>\n\n{text[11:]}"); ok += 1; time.sleep(0.1)
                        except: fail += 1
                    send_msg(chat_id, f"✅ {ok} | ❌ {fail}"); continue

                if text and text.startswith("/msg "):
                    parts = text.split(None, 2)
                    if len(parts) >= 3:
                        send_msg(int(parts[1]), f"📩 <b>Admin xabari:</b>\n\n{parts[2]}")
                        send_msg(chat_id, "✅ Yuborildi!"); continue

                if text and text.startswith("/confirm "):
                    parts = text.split()
                    arg = parts[1]
                    months = int(parts[2]) if len(parts) > 2 else 1
                    days = months * 30
                    if arg.startswith("@"):
                        uname = arg[1:]
                        r = conn.execute("SELECT user_id FROM users WHERE username=?", (uname,)).fetchone()
                        uid = r[0] if r else None
                    else:
                        uid = int(arg)
                    if uid:
                        until = db_set_premium(uid, days)
                        send_msg(chat_id, f"⭐ {uid} — {months} oylik premium tasdiqlandi!\nMuddati: {until}")
                        send_msg(uid, f"✅ {months} oylik Premium faollashdi!\n📅 Muddati: {until} gacha\n\nCheksiz foydalaning! 🚀")
                        pending_payment.pop(uid, None)
                    else:
                        send_msg(chat_id, "❌ Foydalanuvchi topilmadi!")
                    continue

                if text and text.startswith("/reject "):
                    uid = int(text.split()[1])
                    send_msg(chat_id, f"❌ {uid} rad etildi.")
                    send_msg(uid, "❌ To'lovingiz tasdiqlanmadi.")
                    pending_payment.pop(uid, None); continue

            # ── SPAM ─────────────────────────────────────────
            if is_spam(user_id):
                send_msg(chat_id, "⚠️ Juda tez! Biroz kuting."); continue

            # ── TIL holati ───────────────────────────────────
            if user_state.get(user_id) == "lang":
                lang_map = {"🇺🇿 O'zbek": "uz", "🇷🇺 Русский": "ru", "🇬🇧 English": "en"}
                if text in lang_map:
                    new_lang = lang_map[text]
                    db_set_lang(user_id, new_lang); lang = new_lang
                    user_state.pop(user_id, None)
                    send_msg(chat_id, "✅ Til o'zgartirildi!", reply_markup=get_menu(is_prem, new_lang))
                continue

            # ── REFERAT holati ───────────────────────────────
            if user_state.get(user_id) == "referat":
                user_state.pop(user_id, None)
                if not is_prem:
                    can, _ = db_check_limit(user_id)
                    if not can: send_msg(chat_id, "⛔ Limit tugadi! /premium"); continue
                    db_increment(user_id)
                send_action(chat_id)
                send_msg(chat_id, "✍️ Yozilmoqda...")
                reply = ask_groq(chat_id, f"'{text}' mavzusida to'liq referat yoz. Kirish, 3 bo'lim, xulosa bo'lsin. Sarlavhalar bilan.",
                    system="Sen professional akademik yozuvchisan. Sifatli, batafsil referat yoz.")
                send_msg(chat_id, reply); continue

            # ── KURS ISHI holati ─────────────────────────────
            if user_state.get(user_id) == "kurs":
                user_state.pop(user_id, None)
                if not is_prem:
                    can, _ = db_check_limit(user_id)
                    if not can: send_msg(chat_id, "⛔ Limit tugadi! /premium"); continue
                    db_increment(user_id)
                send_action(chat_id)
                send_msg(chat_id, "🎓 Kurs ishi yozilmoqda...")
                reply = ask_groq(chat_id,
                    f"'{text}' mavzusida kurs ishi yoz. Tarkib: kirish, nazariy qism (2 bo'lim), amaliy qism, xulosa, adabiyotlar ro'yxati.",
                    system="Sen professor darajasidagi akademik yozuvchisan. Batafsil, ilmiy kurs ishi yoz.")
                send_msg(chat_id, reply); continue

            # ── MATEMATIK holati ─────────────────────────────
            if user_state.get(user_id) == "math":
                user_state.pop(user_id, None)
                if not is_prem:
                    can, _ = db_check_limit(user_id)
                    if not can: send_msg(chat_id, "⛔ Limit tugadi! /premium"); continue
                    db_increment(user_id)
                send_action(chat_id)
                reply = ask_groq(chat_id, f"Quyidagi masalani yech, faqat javobni yoz, tushuntirma:\n\n{text}",
                    system="Sen matematik mutaxassissan. Faqat to'g'ri javobni yoz, hech qanday tushuntirma berma.")
                send_msg(chat_id, reply); continue

            # ── TARJIMA holati ───────────────────────────────
            if user_state.get(user_id) == "translate":
                user_state.pop(user_id, None)
                send_action(chat_id)
                send_msg(chat_id, "🌍 Tarjima qilinmoqda...")
                reply = ask_groq(chat_id,
                    f"Quyidagi matnni 3 tilda tarjima qil:\n\n🇺🇿 O'zbek tilida:\n🇷🇺 Rus tilida:\n🇬🇧 Ingliz tilida:\n\nMatn: {text}\n\nFaqat tarjimalarni yoz.",
                    system="Sen professional tarjimonsan.")
                send_msg(chat_id, reply); continue

            # ── YANGILIK holati ──────────────────────────────
            if user_state.get(user_id) == "news":
                user_state.pop(user_id, None)
                send_action(chat_id)
                send_msg(chat_id, "📰 Qidirilmoqda...")
                reply = web_search_summary(text)
                send_msg(chat_id, reply); continue

            # ── RASM TAVSIF holati ───────────────────────────
            if user_state.get(user_id) == "artdesc":
                user_state.pop(user_id, None)
                send_action(chat_id)
                reply = ask_groq(chat_id,
                    f"Quyidagi rasm g'oyasi uchun batafsil tasviriy tavsif yoz (DALL-E, Midjourney uchun prompt):\n\n{text}",
                    system="Sen professional rasm yaratuvchi AI prompter san. Ingliz va O'zbek tilida prompt yoz.")
                send_msg(chat_id, reply); continue

            # ── JADVAL holati ────────────────────────────────
            if user_state.get(user_id) == "schedule":
                user_state.pop(user_id, None)
                send_action(chat_id)
                reply = ask_groq(chat_id,
                    f"Quyidagi uchun batafsil jadval/reja tuz:\n\n{text}",
                    system="Sen vaqt menejeri mutaxassissan. Aniq, amaliy jadval tuz.")
                send_msg(chat_id, reply); continue

            # ── PAYMENT SCREENSHOT ───────────────────────────
            if pending_payment.get(user_id) and photo:
                send_msg(ADMIN_ID,
                    f"💳 <b>Yangi to'lov!</b>\n👤 {first_name}\n🔗 @{username}\n🆔 ID: <code>{user_id}</code>\n\n✅ Tasdiqlash: /confirm {user_id}\n❌ Rad etish: /reject {user_id}")
                requests.post(f"{TG}/forwardMessage",
                    json={"chat_id": ADMIN_ID, "from_chat_id": chat_id, "message_id": msg["message_id"]},
                    verify=False)
                send_msg(chat_id, "✅ Screenshot yuborildi! Admin tekshiradi (10-30 daqiqa).")
                pending_payment.pop(user_id, None); continue

            # ── TUGMALAR ─────────────────────────────────────
            if text in ["/start"] or text.startswith("/start "):
                now = time.time()
                if start_cooldown.get(user_id, 0) > now - 3: continue
                start_cooldown[user_id] = now
            if text in ["/start"] or text.startswith("/start "):
                send_msg(chat_id,
                    f"Salom, {first_name}! 👋\n\nMen aqlli AI yordamchiman!\n"
                    f"{'⭐ Admin — cheksiz' if user_id==ADMIN_ID else ('⭐ Premium' if is_prem else f'🆓 Bepul: {DAILY_FREE} ta/kun')}\n\n"
                    f"Quyidagi tugmalardan foydalaning 👇",
                    reply_markup=get_menu(is_prem, lang)); continue

            if text == "/reset":
                can, rem = db_check_limit(user_id)
                send_msg(chat_id, "⭐ Cheksiz so'rovlar!" if is_prem else f"📊 Bugun qolgan: {rem}/{DAILY_FREE}"); continue

            if text in ["/lang", "🌐 Til"]:
                user_state[user_id] = "lang"
                send_msg(chat_id, "🌍 Tilni tanlang:", reply_markup=lang_keyboard()); continue

            if text in ["⭐ Statusim", "🆓 Statusim", "⭐ Статус", "🆓 Статус", "⭐ Status", "🆓 Status"]:
                ref_c = conn.execute("SELECT referral_count FROM users WHERE user_id=?", (user_id,)).fetchone()
                ref_c = ref_c[0] if ref_c else 0
                if is_prem:
                    r = conn.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,)).fetchone()
                    until_txt = r[0] if r and r[0] else "Cheksiz"
                    send_msg(chat_id, f"⭐ <b>Premium</b>\nMuddati: {until_txt}\n👥 Taklif: {ref_c}\n\nCheksiz foydalaning! 🚀")
                else:
                    can, rem = db_check_limit(user_id)
                    send_msg(chat_id,
                        f"🆓 <b>Bepul rejim</b>\n\nBugun qolgan: {rem}/{DAILY_FREE}\n👥 Taklif: {ref_c}\n\n"
                        f"🔗 Referral:\nhttps://t.me/YourBotUsername?start={user_id}\n\n💳 Premium: 20 000 so'm/oy")
                continue

            if text in ["📊 Tarixim", "📊 История", "📊 History"]:
                r = conn.execute("SELECT total_requests, daily_count, joined FROM users WHERE user_id=?", (user_id,)).fetchone()
                if r:
                    send_msg(chat_id, f"📊 <b>Statistika</b>\n\n📨 Jami: {r[0]}\n📅 Bugun: {r[1]}\n🗓 A'zo: {r[2][:10]}")
                continue

            if text in ["🎁 Sovga", "🎁 Подарок", "🎁 Gift"]:
                if is_prem: send_msg(chat_id, "⭐ Siz premiumsiz!")
                elif db_daily_gift(user_id): send_msg(chat_id, f"🎁 +{GIFT_BONUS} limit qo'shildi!")
                else: send_msg(chat_id, "🎁 Bugun oldingiz! Ertaga qaytib keling.")
                continue

            if text in ["💳 Premium", "💳 Премиум", "💳 Premium olish"]:
                pending_payment[user_id] = True
                send_msg(chat_id,
                    "💳 <b>Premium tarif rejalari:</b>\n\n1️⃣ <b>1 oy</b> — 20 000 som\n2️⃣ <b>3 oy</b> — 60 000 som\n3️⃣ <b>6 oy</b> — 120 000 som\n\n✅ Nimalar kiradi:\n"
                    "• 🤖 Cheksiz AI suhbat\n• 🖼 Rasm tahlil\n• 🎤 Ovoz matnga\n• 📝 Referat yozish\n• 🎓 Kurs ishi\n• 🧮 Matematik\n• 🌍 Tarjima\n• 📰 Yangiliklar\n• 📅 Jadval\n• 🔐 Parol generatsiya\n\n"
                    "📲 To'lov:\nClick/Payme: +998 70 018 44 47\nKarta: 4916 9903 2163 4716\n\n"
                    "To'lovdan keyin <b>SCREENSHOT</b> yuboring!"

"Yoki to'lov chekini @Gaffarov_3 ga yuboring!"); continue

            if text in ["ℹ️ Yordam", "/help", "ℹ️ Помощь", "ℹ️ Help"]:
                send_msg(chat_id,
                    f"ℹ️ <b>Yordam</b>\n\n🆓 Bepul: {DAILY_FREE} ta/kun\n⭐ Premium: cheksiz\n\n"
                    f"Buyruqlar:\n/reset — limitni ko'rish\n/lang — til\n/clear — tozalash\n/premium — premium\n\n"
                    f"🎁 Kunlik sovg'a: +{GIFT_BONUS} limit\n"
                    f"👥 Do'st taklif: +2 limit"); continue

            if text in ["💱 Valyuta", "💱 Валюта", "💱 Currency"]:
                keyboard = {"inline_keyboard": [
                    [{"text": "🇺🇸 USD", "callback_data": "cur_USD"},
                     {"text": "🇪🇺 EUR", "callback_data": "cur_EUR"},
                     {"text": "🇷🇺 RUB", "callback_data": "cur_RUB"}],
                    [{"text": "🇬🇧 GBP", "callback_data": "cur_GBP"},
                     {"text": "🇨🇳 CNY", "callback_data": "cur_CNY"},
                     {"text": "🇹🇷 TRY", "callback_data": "cur_TRY"}],
                    [{"text": "🇰🇿 KZT", "callback_data": "cur_KZT"},
                     {"text": "📊 Hammasi", "callback_data": "cur_ALL"}]
                ]}
                send_msg(chat_id, "💱 Qaysi valyutani ko'rmoqchisiz?", reply_markup=keyboard)
                continue

            if text in ["/clear"]:
                histories.pop(chat_id, None)
                send_msg(chat_id, "🗑 Suhbat tozalandi!"); continue

            # ── YANGI XUSUSIYATLAR ────────────────────────────
            if text in ["📝 Referat", "📝 Реферат", "📝 Essay"]:
                histories.pop(chat_id, None); user_state[user_id] = "referat"
                send_msg(chat_id, "📝 Referat mavzusini yozing:"); continue

            if text in ["🎓 Kurs ishi", "🎓 Курсовая", "🎓 Coursework"]:
                histories.pop(chat_id, None); user_state[user_id] = "kurs"
                send_msg(chat_id, "🎓 Kurs ishi mavzusini yozing:"); continue

            if text in ["🧮 Matematik", "🧮 Математика", "🧮 Math"]:
                histories.pop(chat_id, None); user_state[user_id] = "math"
                send_msg(chat_id, "🧮 Masala yoki misolni yozing:"); continue

            if text in ["🌍 Tarjima", "🌍 Перевод", "🌍 Translate"]:
                histories.pop(chat_id, None); user_state[user_id] = "translate"
                send_msg(chat_id, "🌍 Tarjima qilinadigan matnni yuboring:"); continue

            if text in ["📰 Yangiliklar", "📰 Новости", "📰 News"]:
                histories.pop(chat_id, None); user_state[user_id] = "news"
                send_msg(chat_id, "📰 Qaysi mavzu bo'yicha yangilik qidiraman?"); continue

            if text in ["🎨 Rasm tavsif", "🎨 Описание", "🎨 Art desc"]:
                histories.pop(chat_id, None); user_state[user_id] = "artdesc"
                send_msg(chat_id, "🎨 Qanday rasm yaratmoqchisiz? Tasvirlab bering:"); continue

            if text in ["📅 Jadval", "📅 График", "📅 Schedule"]:
                histories.pop(chat_id, None); user_state[user_id] = "schedule"
                send_msg(chat_id, "📅 Nima uchun jadval tuzamiz? (kunlik, haftalik, o'quv...)"); continue

            if text in ["🔐 Parol", "🔐 Пароль", "🔐 Password"]:
                passwords = [gen_password(16) for _ in range(5)]
                reply = "🔐 <b>Xavfsiz parollar:</b>\n\n" + "\n".join([f"<code>{p}</code>" for p in passwords])
                send_msg(chat_id, reply); continue

            if text in ["🤖 AI suhbat", "🤖 AI чат", "🤖 AI Chat",
                        "🖼 Rasm tahlil", "🖼 Фото", "🖼 Image",
                        "🎤 Ovoz", "🎤 Голос", "🎤 Voice"]:
                send_msg(chat_id, "Yuboring! 👇"); continue

            # ── RASM ─────────────────────────────────────────
            if photo:
                if not is_prem:
                    can, _ = db_check_limit(user_id)
                    if not can: send_msg(chat_id, "⛔ Limit tugadi! /premium"); continue
                    db_increment(user_id)
                send_action(chat_id, "typing")
                send_msg(chat_id, "🔍 Tahlil qilinmoqda...")
                url = get_file_url(photo[-1]["file_id"])
                b64 = image_to_base64(url)
                reply = ask_groq_vision(chat_id, b64, msg.get("caption", ""))
                send_msg(chat_id, reply); continue

            # ── OVOZ ─────────────────────────────────────────
            if voice:
                if not is_prem:
                    can, _ = db_check_limit(user_id)
                    if not can: send_msg(chat_id, "⛔ Limit tugadi! /premium"); continue
                    db_increment(user_id)
                send_action(chat_id, "typing")
                send_msg(chat_id, "🎤 Matnga o'girilmoqda...")
                url = get_file_url(voice["file_id"])
                transcribed = transcribe_voice(url)
                if transcribed:
                    send_msg(chat_id, f"📝 Siz dedingiz:\n{transcribed}")
                    reply = ask_groq(chat_id, transcribed)
                    send_msg(chat_id, reply)
                else:
                    send_msg(chat_id, "❌ Ovozni taniy olmadim.")
                continue

            # ── MATN ─────────────────────────────────────────
            if text:
                send_action(chat_id)
                reply = process_message(chat_id, user_id, text)
                if reply:
                    send_msg(chat_id, reply)

    except Exception as e:
        log.error(f"Main loop: {e}")
        print(f"Xato: {e}")
        time.sleep(3)
