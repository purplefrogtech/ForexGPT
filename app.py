import os
import json
import aiohttp
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Tuple, Dict, Optional

import pandas as pd
import numpy as np
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# ─────────── Setup ───────────
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_KEY = os.getenv("ALPHAVANTAGE_API_KEY")

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing in .env")
if not API_KEY:
    raise RuntimeError("ALPHAVANTAGE_API_KEY missing in .env")

AUTH_STR = os.getenv("AUTHORIZED_USERS", "")
AUTHORIZED_USERS = {s.strip() for s in AUTH_STR.split(",") if s.strip()}

DEFAULT_PAIRS = [p.strip().upper() for p in os.getenv("DEFAULT_PAIRS", "EURUSD,USDTRY").split(",") if p.strip()]
DEFAULT_TERMS = [t.strip() for t in os.getenv("DEFAULT_TERMS", "kısa,orta").split(",") if t.strip()]
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "120"))
SIGNAL_COOLDOWN_MIN = int(os.getenv("SIGNAL_COOLDOWN_MIN", "15"))

BASE_URL = "https://www.alphavantage.co/query"
CACHE_TTL = timedelta(minutes=10)
API_CONCURRENCY = asyncio.Semaphore(1)  # Alpha Vantage free tier için tek uçuş

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("forexgpt")

# in-memory cache: {key: (expiry_dt, payload)}
CACHE: Dict[str, Tuple[datetime, dict]] = {}

# kalıcı dosyalar
SUBSCRIBERS_FILE = "subscribers.json"  # { "chat_ids": [int,...] }
STATE_FILE = "state.json"              # { "PAIR|TERM": {"last_signal": "...", "last_ts": "...", "last_push":"...", "history":[...]} }

# ─────────── I18N ───────────
def i18n(lang: str) -> Dict[str, str]:
    tr = {
        "choose_lang": "Lütfen bir dil seçin:",
        "lang_set_tr": "Dil Türkçe olarak ayarlandı.",
        "lang_set_en": "Dil İngilizce olarak ayarlandı.",
        "no_access": "⛔ Bu bota erişim izniniz yok.",
        "enter_pair": "Lütfen işlem çiftini yazınız (örnek: USDTRY):",
        "bad_pair": "Geçersiz parite. 6 harften oluşmalı (ör: EURUSD).",
        "pick_term": "Lütfen vade türünü seçiniz:",
        "analyzing": "Analiz ediliyor...",
        "help": (
            "Komutlar:\n"
            "/start – dil seçimi ve abone olma\n"
            "/forex – parite analizi başlat\n"
            "/help – yardım\n\n"
            f"Arka plan tarayıcı aktif. İzlenen pariteler: {', '.join(DEFAULT_PAIRS)} "
            f"Vadeler: {', '.join(DEFAULT_TERMS)}"
        ),
        "bg_notice": "ℹ️ Bu parite için kısa vade veri bulunamadı; günlük veriye düşüldü.",
        "signal_title": "📡 ForexGPT Sinyal",
        "not_fin_advice": "⚠️ Yatırım tavsiyesi değildir.",
    }
    if lang == "tr":
        return tr
    return {
        "choose_lang": "Please select a language:",
        "lang_set_tr": "Language set to Turkish.",
        "lang_set_en": "Language set to English.",
        "no_access": "⛔ You are not authorized to use this bot.",
        "enter_pair": "Please enter the trading pair (e.g., USDTRY):",
        "bad_pair": "Invalid pair. Should be 6 letters (e.g., EURUSD).",
        "pick_term": "Please select the term type:",
        "analyzing": "Analyzing...",
        "help": (
            "Commands:\n"
            "/start – language & subscribe\n"
            "/forex – start analysis\n"
            "/help – help\n\n"
            f"Background scanner ON. Pairs: {', '.join(DEFAULT_PAIRS)} "
            f"Terms: {', '.join(DEFAULT_TERMS)}"
        ),
        "bg_notice": "ℹ️ Short-term data not available; fell back to DAILY.",
        "signal_title": "📡 ForexGPT Signal",
        "not_fin_advice": "⚠️ This is not financial advice.",
    }

# ─────────── Utilities ───────────
def is_user_authorized(update: Update) -> bool:
    user = update.effective_user
    ident = str(user.id)
    uname = f"@{user.username}" if user and user.username else None
    return ident in AUTHORIZED_USERS or (uname and uname in AUTHORIZED_USERS)

def parse_pair(text: str) -> Optional[Tuple[str, str]]:
    t = (text or "").upper().strip().replace(" ", "")
    if len(t) != 6 or not t.isalpha():
        return None
    return t[:3], t[3:]

def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# ─────────── API & Indicators ───────────
async def fetch_json(params: dict, cache_key: str, retry: int = 2) -> dict:
    """Alpha Vantage çağrısı: rate-limit/uyarı/hata gövdelerini cache'leme; yalnızca sağlıklı veriyi cache'le."""
    now = datetime.now(timezone.utc)
    hit = CACHE.get(cache_key)
    if hit and hit[0] > now:
        return hit[1]

    async with API_CONCURRENCY:
        async with aiohttp.ClientSession() as session:
            backoff = 2
            for attempt in range(retry + 1):
                async with session.get(BASE_URL, params=params, timeout=30) as resp:
                    if resp.status != 200:
                        if attempt < retry:
                            await asyncio.sleep(backoff); backoff *= 2
                            continue
                        raise ValueError(f"HTTP {resp.status}")
                    data = await resp.json()

                # Geçersiz sembol/parametre
                if "Error Message" in data:
                    raise ValueError(data["Error Message"])

                # Rate limit / bilgi mesajları
                if "Note" in data or "Information" in data:
                    msg = data.get("Note") or data.get("Information") or "Rate limited / No data"
                    if attempt < retry:
                        await asyncio.sleep(backoff); backoff *= 2
                        continue
                    raise RuntimeError(f"Alpha Vantage: {msg}")

                # Sağlıklı veri: cache'le ve dön
                CACHE[cache_key] = (now + CACHE_TTL, data)
                return data

        raise ValueError("API temporary error after retries")

async def load_fx_series(pair: str, term: str) -> Tuple[pd.DataFrame, bool]:
    """
    term: kısa/orta/uzun
    returns (df, fell_back_to_daily)
    """
    base, quote = parse_pair(pair) or (None, None)
    if not base:
        raise ValueError("Invalid pair")

    fell_back = False

    if term == "kısa":
        params = {
            "function": "FX_INTRADAY",
            "from_symbol": base,
            "to_symbol": quote,
            "interval": "60min",
            "outputsize": "compact",
            "apikey": API_KEY,
        }
        key_name = "Time Series FX (60min)"
        data = await fetch_json(params, f"FX_INTRADAY-{base}{quote}-60")

        if key_name not in data:
            # intraday yok → DAILY fallback
            fell_back = True
            params = {
                "function": "FX_DAILY",
                "from_symbol": base,
                "to_symbol": quote,
                "outputsize": "compact",
                "apikey": API_KEY,
            }
            key_name = "Time Series FX (Daily)"
            data = await fetch_json(params, f"FX_DAILY-{base}{quote}-fallback")
    elif term == "uzun":
        params = {"function": "FX_WEEKLY", "from_symbol": base, "to_symbol": quote, "apikey": API_KEY}
        key_name = "Time Series FX (Weekly)"
        data = await fetch_json(params, f"FX_WEEKLY-{base}{quote}")
    else:
        params = {"function": "FX_DAILY", "from_symbol": base, "to_symbol": quote, "outputsize": "compact", "apikey": API_KEY}
        key_name = "Time Series FX (Daily)"
        data = await fetch_json(params, f"FX_DAILY-{base}{quote}")

    ts = data.get(key_name)
    if not ts:
        # Teşhis kolaylığı için mevcut anahtarları göster
        raise ValueError(f"Missing '{key_name}'. Available keys: {list(data.keys())[:5]}")

    df = (
        pd.DataFrame(ts)
        .T.rename(columns={"1. open":"open","2. high":"high","3. low":"low","4. close":"close"})
        .astype(float)
        .sort_index()
    )
    df.index = pd.to_datetime(df.index)
    return df, fell_back

def compute_indicators(df: pd.DataFrame, term: str) -> pd.DataFrame:
    df = df.copy()
    if term == "kısa":
        rsi_p, sma_p, ema_p, atr_p = 7, 10, 10, 7
    elif term == "uzun":
        rsi_p, sma_p, ema_p, atr_p = 30, 50, 50, 30
    else:
        rsi_p, sma_p, ema_p, atr_p = 14, 20, 20, 14

    df["sma"] = df["close"].rolling(sma_p).mean()
    df["ema"] = df["close"].ewm(span=ema_p, adjust=False).mean()

    delta = df["close"].diff()
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    roll_up = pd.Series(gain, index=df.index).ewm(alpha=1/rsi_p, adjust=False).mean()
    roll_dn = pd.Series(loss, index=df.index).ewm(alpha=1/rsi_p, adjust=False).mean().replace(0, np.nan)
    rs = roll_up / roll_dn
    df["rsi"] = 100 - (100 / (1 + rs))

    # Bollinger
    bb_p = 20
    ma = df["close"].rolling(bb_p).mean()
    std = df["close"].rolling(bb_p).std(ddof=0)
    df["bb_mid"] = ma
    df["bb_up"] = ma + 2 * std
    df["bb_lo"] = ma - 2 * std

    # ATR
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/atr_p, adjust=False).mean()

    return df

def evaluate_signal(row: pd.Series, prev_signal: str = "NÖTR"):
    price, rsi, sma, ema, atr = row["close"], row["rsi"], row["sma"], row["ema"], row["atr"]
    bb_up, bb_lo = row["bb_up"], row["bb_lo"]

    if pd.isna([rsi, sma, ema, atr, bb_up, bb_lo]).any():
        return prev_signal, {"score": 0.0, "regime": float("nan")}

    regime = atr / price
    low_vol = regime < 0.005
    high_vol = regime > 0.02

    score = 0.0

    # Trend (EMA vs SMA)
    score += (1.5 if ema > sma else -1.5)

    # RSI
    if rsi < 30:
        score += 1.0
    elif rsi > 70:
        score -= 1.0

    # Bollinger dokunuşu
    if price < bb_lo:
        score += (1.2 if low_vol else 0.6)
    elif price > bb_up:
        score -= (1.2 if low_vol else 0.6)

    # Fiyat EMA üstünde/altında
    score += (0.6 if price > ema else -0.6)

    # Çok yüksek volatilite penalize
    if high_vol:
        score -= 0.6

    # Histerezis
    upper, lower = 1.0, -1.0   # sinyal değişim sınırları
    dead_zone = 0.5            # bu aralıkta nötr

    if score > upper:
        signal = "LONG"
    elif score < lower:
        signal = "SHORT"
    elif abs(score) <= dead_zone:
        signal = "NÖTR"
    else:
        signal = prev_signal

    return signal, {"score": round(score, 2), "regime": float(regime)}

def adaptive_tp_sl(price: float, atr: float, signal: str, regime: float) -> Tuple[float, float]:
    mult = 1.5 if regime < 0.005 else 2.0 if regime < 0.02 else 2.5
    if signal == "LONG":
        return round(price + mult*atr, 5), round(price - mult*atr, 5)
    if signal == "SHORT":
        return round(price - mult*atr, 5), round(price + mult*atr, 5)
    return round(price + 2*atr, 5), round(price - 2*atr, 5)

# ─────────── Bot Handlers ───────────
async def select_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Türkçe", callback_data="lang_tr")],
        [InlineKeyboardButton("English", callback_data="lang_en")],
    ]
    await update.message.reply_text(i18n("tr")["choose_lang"], reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_language_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "lang_tr":
        context.user_data["language"] = "tr"
        await q.edit_message_text(i18n("tr")["lang_set_tr"])
    else:
        context.user_data["language"] = "en"
        await q.edit_message_text(i18n("en")["lang_set_en"])

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("language", "tr")
    await update.message.reply_text(i18n(lang)["help"])

async def start_forex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_authorized(update):
        await update.message.reply_text(i18n("tr")["no_access"])
        return

    # abone kaydı
    subs = load_json(SUBSCRIBERS_FILE, {"chat_ids": []})
    if update.effective_chat.id not in subs["chat_ids"]:
        subs["chat_ids"].append(update.effective_chat.id)
        save_json(SUBSCRIBERS_FILE, subs)

    lang = context.user_data.get("language", "tr")
    await update.message.reply_text(i18n(lang)["enter_pair"])
    context.user_data["awaiting_pair"] = True

async def get_vade_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_pair"):
        return
    lang = context.user_data.get("language", "tr")
    pair = (update.message.text or "").upper().strip()
    if not parse_pair(pair):
        await update.message.reply_text(i18n(lang)["bad_pair"])
        return
    context.user_data["pair"] = pair
    context.user_data["awaiting_pair"] = False
    keyboard = [
        [InlineKeyboardButton("Kısa Vade" if lang=="tr" else "Short Term", callback_data="kısa")],
        [InlineKeyboardButton("Orta Vade" if lang=="tr" else "Medium Term", callback_data="orta")],
        [InlineKeyboardButton("Uzun Vade" if lang=="tr" else "Long Term", callback_data="uzun")],
    ]
    await update.message.reply_text(
        (f"İşlem çifti: {pair}\n" if lang=="tr" else f"Pair: {pair}\n") + i18n(lang)["pick_term"],
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_vade_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = context.user_data.get("language", "tr")
    texts = i18n(lang)
    await q.edit_message_text(texts["analyzing"])
    pair = context.user_data.get("pair")
    if not pair:
        await q.edit_message_text("İşlem çifti bulunamadı. / Pair missing. /forex")
        return
    term = q.data
    try:
        df, fell_back = await load_fx_series(pair, term)
        df = compute_indicators(df, term)
        last = df.iloc[-1]
        signal, parts = evaluate_signal(last)
        price, atr = float(last["close"]), float(last["atr"])
        tp, sl = adaptive_tp_sl(price, atr, signal, parts["regime"])
        emoji = "🚀 Long" if signal=="LONG" else "📉 Short" if signal=="SHORT" else "⏸️ Neutral"
        lines = [
            f"🪬 {'İşlem Çifti' if lang=='tr' else 'Pair'}: {pair}",
            f"{'Vade' if lang=='tr' else 'Term'}: {term.capitalize()}",
            f"{'Geri düşüş' if lang=='tr' else 'Fallback'}: {'Evet' if fell_back else 'Hayır'}",
            "",
            f"{emoji}",
            f"TP: {tp} | SL: {sl}",
        ]
        if fell_back:
            lines.append(texts["bg_notice"])
        lines.append(texts["not_fin_advice"])
        await q.edit_message_text("\n".join(lines))
    except Exception as e:
        logger.exception("Analysis error")
        prefix = "Hata: " if lang=="tr" else "Error: "
        await q.edit_message_text(prefix + str(e))

# ─────────── Background Daemon ───────────
async def signal_scanner(app: Application):
    """
    Sürekli çalışır. DEFAULT_PAIRS x DEFAULT_TERMS tarar.
    Sinyal değişimi gördüğünde tüm subscriber'lara bildirir.
    Cooldown: aynı pair-term için son push'tan sonra X dakika geçmeli.
    Flip-flop: LONG→SHORT→LONG (ve tersi) hızlı dalgalanmayı engeller.
    """
    logger.info("Signal scanner started. Pairs=%s Terms=%s", DEFAULT_PAIRS, DEFAULT_TERMS)
    state = load_json(STATE_FILE, {})  # key: "PAIR|TERM"

    while True:
        loop_start = datetime.now(timezone.utc)
        try:
            subs = load_json(SUBSCRIBERS_FILE, {"chat_ids": []}).get("chat_ids", [])
            if not subs:
                # Abone yoksa bekle ve devam et
                await asyncio.sleep(SCAN_INTERVAL_SEC)
                continue

            for pair in DEFAULT_PAIRS:
                for term in DEFAULT_TERMS:
                    key = f"{pair}|{term}"
                    try:
                        df, fell_back = await load_fx_series(pair, term)
                        df = compute_indicators(df, term)
                        last = df.iloc[-1]
                        last_ts = last.name.isoformat()

                        prev = state.get(key, {})
                        prev_ts = prev.get("last_ts")

                        # Aynı mum ise işlem yapma (state'i değiştirmeye gerek yok)
                        if last_ts == prev_ts:
                            continue

                        prev_sig = prev.get("last_signal", "NÖTR")
                        signal, parts = evaluate_signal(last, prev_signal=prev_sig)
                        price, atr = float(last["close"]), float(last["atr"])
                        tp, sl = adaptive_tp_sl(price, atr, signal, parts["regime"])

                        # Cooldown kontrolü
                        last_push_iso = prev.get("last_push")
                        cooldown_ok = True
                        if last_push_iso:
                            last_push = datetime.fromisoformat(last_push_iso)
                            cooldown_ok = (signal != prev_sig) or (
                                (loop_start - last_push) > timedelta(minutes=SIGNAL_COOLDOWN_MIN)
                            )

                        # Flip-flop kontrolünü bildirim göndermeden ÖNCE yap
                        prev_history = prev.get("history", [])
                        candidate_history = (prev_history + [signal])[-3:]
                        is_flipflop = candidate_history in (["LONG", "SHORT", "LONG"], ["SHORT", "LONG", "SHORT"])

                        changed = (signal != prev_sig) and (signal in ("LONG", "SHORT"))
                        should_notify = changed and cooldown_ok and not is_flipflop

                        if should_notify:
                            text = (
                                f"📡 ForexGPT Sinyal\n"
                                f"Parite: {pair} | Vade: {term.capitalize()} {'(DAILY fallback)' if fell_back else ''}\n"
                                f"Sinyal: {'🚀 LONG' if signal=='LONG' else '📉 SHORT'}\n"
                                f"Fiyat: {price:.5f}\n"
                                f"TP: {tp}  |  SL: {sl}\n"
                                f"RSI: {last['rsi']:.2f} | EMA: {last['ema']:.5f} | SMA: {last['sma']:.5f}\n"
                                f"ATR: {atr:.5f} (regime {parts['regime']:.4f}) | Score: {parts['score']}\n\n"
                                f"⚠️ Yatırım tavsiyesi değildir."
                            )
                            for cid in subs:
                                try:
                                    await app.bot.send_message(chat_id=cid, text=text)
                                except Exception as send_err:
                                    logger.warning("send fail chat_id=%s err=%s", cid, send_err)

                            # Bildirim atıldı → state'i güncelle
                            state[key] = {
                                "last_signal": signal,
                                "last_ts": last_ts,
                                "last_push": loop_start.isoformat(),
                                "history": candidate_history,
                            }
                            save_json(STATE_FILE, state)

                        else:
                            if is_flipflop:
                                logger.info("Flip-flop detected for %s, skipping notification", key)
                            # Bildirim yok → yine de state'i güncel tut
                            state[key] = {
                                "last_signal": signal,
                                "last_ts": last_ts,
                                "last_push": prev.get("last_push"),
                                "history": candidate_history,
                            }
                            save_json(STATE_FILE, state)

                        # (nazik) pacing: alpha limitlerine yaklaşmamak için küçük uyku
                        await asyncio.sleep(1)

                    except Exception as pair_err:
                        logger.warning("scan error %s %s: %s", pair, term, pair_err)
                        await asyncio.sleep(1)

        except Exception as loop_err:
            logger.exception("scanner loop error: %s", loop_err)

        # döngü bekleme
        spent = (datetime.now(timezone.utc) - loop_start).total_seconds()
        delay = max(5, SCAN_INTERVAL_SEC - int(spent))
        await asyncio.sleep(delay)

# PTB post-init: background task’ı başlat
async def on_post_init(app: Application):
    app.job = asyncio.create_task(signal_scanner(app))  # background task

# ─────────── Boot ───────────
def main():
    app = Application.builder().token(TOKEN).post_init(on_post_init).build()
    app.add_handler(CommandHandler("start", select_language))
    app.add_handler(CallbackQueryHandler(handle_language_selection, pattern="^lang_"))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("forex", start_forex))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, get_vade_type))
    app.add_handler(CallbackQueryHandler(handle_vade_selection, pattern="^(kısa|orta|uzun)$"))
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
