import asyncio
from contextlib import closing
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import time
from urllib.parse import parse_qsl, quote

import aiohttp
from aiohttp import web

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
FACEIT_API_KEY = os.getenv("FACEIT_API_KEY", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://daniilgxcode12.github.io/spendly-app/").strip()
API_PORT = int(os.getenv("API_PORT", os.getenv("PORT", "8080")))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@support").strip()
DB_PATH = os.getenv("DB_PATH", "/data/elocoach.db" if os.path.isdir("/data") else "elocoach.db")

FACEIT_BASE = "https://open.faceit.com/data/v4"

PLANS = {
    "month": {
        "title": "EloCoach Pro · 1 месяц",
        "description": "Разбор 20 игр, Тильт-детектор, Map Pool и план тренировок на 30 дней",
        "stars": 50,
        "days": 30,
    },
    "quarter": {
        "title": "EloCoach Pro · 3 месяца",
        "description": "Выгодный тариф (-33%): полный разбор CS2 на 90 дней",
        "stars": 100,
        "days": 90,
    },
    "year": {
        "title": "EloCoach Pro · 1 год",
        "description": "Полный доступ к аналитике FACEIT и советам на 365 дней",
        "stars": 500,
        "days": 365,
    },
}

ELO_THRESHOLDS = {
    1: 501,
    2: 751,
    3: 901,
    4: 1051,
    5: 1201,
    6: 1351,
    7: 1531,
    8: 1751,
    9: 2001,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("elocoach")


def init_db() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                "user_id INTEGER PRIMARY KEY, "
                "faceit_nick TEXT, "
                "faceit_id TEXT, "
                "plan TEXT, "
                "pro_until INTEGER DEFAULT 0, "
                "created_at INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS payments ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id INTEGER NOT NULL, "
                "plan TEXT NOT NULL, "
                "stars INTEGER NOT NULL, "
                "charge_id TEXT UNIQUE NOT NULL, "
                "created_at INTEGER NOT NULL)"
            )


def get_user_record(user_id: int) -> dict:
    now = int(time.time())
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT faceit_nick, faceit_id, plan, pro_until FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    if not row:
        return {
            "faceit_nick": "",
            "faceit_id": "",
            "is_pro": False,
            "plan": None,
            "pro_until": None,
        }

    faceit_nick, faceit_id, plan, pro_until = row
    is_pro = bool(pro_until and pro_until > now)
    return {
        "faceit_nick": faceit_nick or "",
        "faceit_id": faceit_id or "",
        "is_pro": is_pro,
        "plan": plan if is_pro else None,
        "pro_until": pro_until if is_pro else None,
    }


def save_user_faceit(user_id: int, nickname: str, player_id: str) -> None:
    now = int(time.time())
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO users (user_id, faceit_nick, faceit_id, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "faceit_nick = excluded.faceit_nick, faceit_id = excluded.faceit_id",
                (user_id, nickname, player_id, now),
            )


def activate_pro(user_id: int, plan_key: str, stars: int, charge_id: str) -> int:
    now = int(time.time())
    days = PLANS[plan_key]["days"]

    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            row = conn.execute(
                "SELECT pro_until FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            current_until = row[0] if row and row[0] else 0
            new_until = max(now, current_until) + days * 86400

            conn.execute(
                "INSERT INTO users (user_id, plan, pro_until, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "plan = excluded.plan, pro_until = excluded.pro_until",
                (user_id, plan_key, new_until, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO payments (user_id, plan, stars, charge_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, plan_key, stars, charge_id, now),
            )

    return new_until


def verify_init_data(init_data: str) -> dict | None:
    if not init_data or not BOT_TOKEN:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(
            secret_key, data_check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        user_raw = parsed.get("user")
        return json.loads(user_raw) if user_raw else None
    except Exception:
        return None


def extract_nickname(raw: str) -> str:
    text = (raw or "").strip()
    m = re.search(r"faceit\.com/[a-z-]+/players/([^/?#]+)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text.lstrip("@").strip()


def to_float(stats: dict, keys: list[str], default: float = 0.0) -> float:
    for k in keys:
        val = stats.get(k)
        if val is not None and str(val).strip() != "":
            try:
                return float(str(val).replace("%", "").replace(",", ".").strip())
            except ValueError:
                continue
    return default


def to_int(stats: dict, keys: list[str], default: int = 0) -> int:
    return int(round(to_float(stats, keys, float(default))))


async def faceit_get(session: aiohttp.ClientSession, path: str) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {FACEIT_API_KEY}", "Accept": "application/json"}
    async with session.get(f"{FACEIT_BASE}{path}", headers=headers) as resp:
        try:
            data = await resp.json()
        except Exception:
            data = {}
        return resp.status, data


async def find_faceit_player(session: aiohttp.ClientSession, nickname: str) -> tuple[int, dict | None]:
    encoded = quote(nickname, safe="")
    status, data = await faceit_get(session, f"/players?nickname={encoded}")
    if status == 200 and data.get("player_id"):
        return 200, data
    if status in (401, 403, 429):
        return status, None

    # Поиск без учёта регистра букв
    s_status, s_data = await faceit_get(
        session, f"/search/players?nickname={encoded}&game=cs2&limit=5"
    )
    if s_status == 200:
        for item in s_data.get("items", []):
            if item.get("nickname", "").lower() == nickname.lower():
                p_status, p_data = await faceit_get(session, f"/players/{item['player_id']}")
                if p_status == 200:
                    return 200, p_data
    return 404, None


def analyze_cs2_performance(player: dict, recent_items: list[dict], lifetime_data: dict, is_pro: bool) -> dict:
    cs2_info = (player.get("games") or {}).get("cs2") or {}
    skill_level = int(cs2_info.get("skill_level") or 1)
    elo = int(cs2_info.get("faceit_elo") or 0)
    next_elo_target = ELO_THRESHOLDS.get(skill_level)
    elo_to_next = max(0, next_elo_target - elo) if next_elo_target else 0

    # Эталоны по уровню FACEIT
    if skill_level <= 3:
        target_adr, target_kd, target_kr, target_hs = 72.0, 0.98, 0.66, 38.0
    elif skill_level <= 6:
        target_adr, target_kd, target_kr, target_hs = 78.0, 1.05, 0.70, 44.0
    elif skill_level <= 9:
        target_adr, target_kd, target_kr, target_hs = 83.0, 1.12, 0.74, 48.0
    else:
        target_adr, target_kd, target_kr, target_hs = 86.0, 1.15, 0.76, 50.0

    matches = []
    for item in recent_items:
        st = item.get("stats") or {}
        kills = to_int(st, ["Kills"])
        deaths = to_int(st, ["Deaths"])
        assists = to_int(st, ["Assists"])
        rounds = to_int(st, ["Rounds"], 20)
        damage = to_float(st, ["Damage"])
        adr = to_float(st, ["ADR"])
        if adr <= 0 and damage > 0 and rounds > 0:
            adr = round(damage / rounds, 1)

        kd = to_float(st, ["K/D Ratio"])
        if kd <= 0:
            kd = round(kills / max(1, deaths), 2)

        kr = to_float(st, ["K/R Ratio"])
        if kr <= 0 and rounds > 0:
            kr = round(kills / rounds, 2)

        hs = to_float(st, ["Headshots %"])
        mvps = to_int(st, ["MVPs"])
        won = str(st.get("Result", "0")) == "1"
        map_name = str(st.get("Map", "unknown")).replace("de_", "").capitalize()
        score = str(st.get("Score", "-"))
        finished_ms = to_int(st, ["Match Finished At"])

        matches.append({
            "map": map_name,
            "score": score,
            "won": won,
            "kills": kills,
            "deaths": deaths,
            "assists": assists,
            "kd": round(kd, 2),
            "kr": round(kr, 2),
            "adr": round(adr, 1),
            "hs": round(hs, 1),
            "mvps": mvps,
            "finished_at": finished_ms,
        })

    sample = matches[:20] if is_pro else matches[:5]
    analysis_pool = matches[:20] if matches else []
    count = len(analysis_pool)

    if count > 0:
        avg_kd = round(sum(m["kd"] for m in analysis_pool) / count, 2)
        avg_kr = round(sum(m["kr"] for m in analysis_pool) / count, 2)
        adr_vals = [m["adr"] for m in analysis_pool if m["adr"] > 0]
        avg_adr = round(sum(adr_vals) / len(adr_vals), 1) if adr_vals else 0.0
        avg_hs = round(sum(m["hs"] for m in analysis_pool) / count, 1)
        avg_deaths = round(sum(m["deaths"] for m in analysis_pool) / count, 1)
        winrate = round((sum(1 for m in analysis_pool if m["won"]) / count) * 100)
    else:
        lt = lifetime_data.get("lifetime") or {}
        avg_kd = round(to_float(lt, ["Average K/D Ratio"]), 2)
        avg_kr = 0.0
        avg_adr = round(to_float(lt, ["ADR"]), 1)
        avg_hs = round(to_float(lt, ["Average Headshots %"]), 1)
        avg_deaths = 0.0
        winrate = int(round(to_float(lt, ["Win Rate %"])))

    # Тильт-детектор (анализ хронологии от старых к новым)
    chron = list(reversed(analysis_pool))
    cur_loss_streak = 0
    max_loss_streak = 0
    after_two_losses_games = 0
    after_two_losses_lost = 0
    for m in chron:
        if cur_loss_streak >= 2:
            after_two_losses_games += 1
            if not m["won"]:
                after_two_losses_lost += 1
        if m["won"]:
            cur_loss_streak = 0
        else:
            cur_loss_streak += 1
            if cur_loss_streak > max_loss_streak:
                max_loss_streak = cur_loss_streak

    tilt_risk = "Низкий"
    tilt_advice = "Ты умеешь вовремя остановиться — длинных лузстриков за последние матчи нет."
    if max_loss_streak >= 4 or (after_two_losses_games >= 2 and after_two_losses_lost / after_two_losses_games >= 0.65):
        tilt_risk = "Высокий"
        tilt_advice = (
            f"Макс. серия поражений — {max_loss_streak}. После 2 лузов подряд ты проигрываешь "
            f"{after_two_losses_lost} из {after_two_losses_games} следующих игр. Вводи жёсткий стоп-лосс после 2 поражений."
        )
    elif max_loss_streak >= 3:
        tilt_risk = "Средний"
        tilt_advice = (
            f"Была серия из {max_loss_streak} поражений подряд. После двух лузов делай перерыв минимум на 30 минут или уходи на DM."
        )

    # Статистика по картам из lifetime segments
    map_pool = []
    for seg in lifetime_data.get("segments") or []:
        if seg.get("type") != "Map":
            continue
        st = seg.get("stats") or {}
        m_matches = to_int(st, ["Matches"])
        if m_matches < 3:
            continue
        map_pool.append({
            "map": str(seg.get("label", "")).replace("de_", "").capitalize(),
            "matches": m_matches,
            "winrate": int(round(to_float(st, ["Win Rate %"]))),
            "kd": round(to_float(st, ["Average K/D Ratio"]), 2),
            "adr": round(to_float(st, ["ADR"]), 1),
        })
    map_pool.sort(key=lambda x: (-x["winrate"], -x["matches"]))
    best_map = map_pool[0] if map_pool else None
    worst_map = sorted(map_pool, key=lambda x: (x["winrate"], -x["matches"]))[0] if len(map_pool) >= 2 else None

    # Диагностика ошибок и план исправления
    mistakes = []

    if avg_kd >= 1.15 and ((avg_adr > 0 and avg_adr < target_adr - 5) or (avg_kr > 0 and avg_kr < target_kr - 0.05)):
        mistakes.append({
            "severity": "high",
            "title": "Пассивная игра (мало импакта при хорошем K/D)",
            "metric": f"K/D {avg_kd} · ADR {avg_adr or '—'} · K/R {avg_kr}",
            "tip": "Ты часто остаёшься последним и добираешь фраги в сейве. Играй ближе к тиммейту-энтри, чтобы забирать мгновенный размен.",
        })

    if avg_adr > 0 and avg_adr < target_adr - 6:
        mistakes.append({
            "severity": "high",
            "title": "Низкий урон за раунд (ADR)",
            "metric": f"ADR {avg_adr} (цель для {skill_level} lvl: {int(target_adr)}+)",
            "tip": "Добавь базовые хае/молики на дефолтные позиции и чаще перестреливайся на разменах. 15 мин DM на спрей перед каткой поднимут ADR за неделю.",
        })

    if avg_kd < 0.92 and count > 0:
        mistakes.append({
            "severity": "high",
            "title": "Слишком много ранних или лишних смертей",
            "metric": f"K/D {avg_kd} · Смертей за игру: {avg_deaths}",
            "tip": "Не повторяй пик в одну и ту же точку дважды после промаха. После первого контакта меняй позицию или откидывай флеш под отход.",
        })

    if avg_hs > 0 and avg_hs < target_hs - 6:
        mistakes.append({
            "severity": "medium",
            "title": "Прицел держится ниже уровня головы",
            "metric": f"HS {avg_hs}% (норма от {int(target_hs)}%)",
            "tip": "Поставь перед сессией 10 минут на карте Refrag / Yprac Prefire. Выходи на углы с прицелом строго на линии головы соперника.",
        })
    elif avg_hs > 62 and avg_kr > 0 and avg_kr < target_kr:
        mistakes.append({
            "severity": "medium",
            "title": "Перебор с тапами вместо контроля зажима",
            "metric": f"HS {avg_hs}% · K/R {avg_kr}",
            "tip": "Ты теряешь доли секунды на доводку в голову на ближней и средней дистанции. Увереннее переходи на спрей первыми 7–10 пулями.",
        })

    if worst_map and worst_map["winrate"] <= 43 and worst_map["matches"] >= 5:
        mistakes.append({
            "severity": "medium",
            "title": f"Слив ELO на карте {worst_map['map']}",
            "metric": f"{worst_map['winrate']}% побед за {worst_map['matches']} игр",
            "tip": f"Ставь {worst_map['map']} в первый бан на вето. Играй чаще {best_map['map']} ({best_map['winrate']}% WR), пока не разберёшь дефолт на слабой карте.",
        })

    if tilt_risk == "Высокий":
        mistakes.append({
            "severity": "high",
            "title": "Тильт-цепочки съедают твой ELO",
            "metric": f"Серия до {max_loss_streak} поражений",
            "tip": tilt_advice,
        })

    if count >= 6:
        kds = [m["kd"] for m in analysis_pool]
        if max(kds) - min(kds) >= 1.4:
            mistakes.append({
                "severity": "low",
                "title": "Сильные качели в форме",
                "metric": f"Разброс K/D: от {min(kds)} до {max(kds)}",
                "tip": "Не заходи в первый матч на холодную. Фиксированная разминка 15 минут (Aim Botz + 50 фрагов DM) стабилизирует старт.",
            })

    if not mistakes:
        mistakes.append({
            "severity": "low",
            "title": f"Хорошая база — пора давить на {min(10, skill_level + 1)} уровень",
            "metric": f"K/D {avg_kd} · ADR {avg_adr} · WR {winrate}%",
            "tip": "Цифры ровные. Чтобы пробить потолок, докрути гранаты под выход тиммейтов и собирай постоянное дуо/трио в лобби.",
        })

    # Общий Impact Score 0..100
    score_val = 50
    score_val += int((avg_kd - 1.0) * 35)
    if avg_adr > 0:
        score_val += int((avg_adr - 75.0) * 0.7)
    score_val += int((winrate - 50) * 0.35)
    impact_score = max(15, min(99, score_val))

    warmup_plan = [
        "5 мин: Aim Botz — 100 тапов + 100 контр-стрейфов в движении",
        "7 мин: DM (AK-47 / M4) — фокус на первых 6 пулях зажима",
        f"3 мин: повтор 2 базовых смоков и флешек на карте {best_map['map'] if best_map else 'Mirage'}",
    ]

    return {
        "is_pro": is_pro,
        "player": {
            "nickname": player.get("nickname", ""),
            "avatar": player.get("avatar", ""),
            "country": (player.get("country") or "").upper(),
            "level": skill_level,
            "elo": elo,
            "elo_to_next": elo_to_next,
            "next_level": min(10, skill_level + 1) if skill_level < 10 else 10,
        },
        "summary": {
            "matches_analyzed": len(sample),
            "total_pool": count,
            "impact_score": impact_score,
            "avg_kd": avg_kd,
            "avg_kr": avg_kr,
            "avg_adr": avg_adr,
            "avg_hs": avg_hs,
            "winrate": winrate,
        },
        "matches": sample,
        "mistakes": mistakes if is_pro else mistakes[:1],
        "locked_mistakes_count": 0 if is_pro else max(0, len(mistakes) - 1),
        "tilt": {
            "risk": tilt_risk,
            "max_loss_streak": max_loss_streak,
            "advice": tilt_advice,
            "locked": not is_pro,
        },
        "maps": map_pool if is_pro else map_pool[:2],
        "locked_maps_count": 0 if is_pro else max(0, len(map_pool) - 2),
        "best_map": best_map,
        "worst_map": worst_map,
        "warmup": warmup_plan,
    }


async def tg_call(session: aiohttp.ClientSession, method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    async with session.post(url, json=payload) as resp:
        return await resp.json()


async def handle_pre_checkout(session: aiohttp.ClientSession, query: dict) -> None:
    parts = query.get("invoice_payload", "").split(":")
    if len(parts) == 3 and parts[0] == "elocoach" and parts[1] in PLANS:
        await tg_call(
            session,
            "answerPreCheckoutQuery",
            {"pre_checkout_query_id": query["id"], "ok": True},
        )
    else:
        await tg_call(
            session,
            "answerPreCheckoutQuery",
            {
                "pre_checkout_query_id": query["id"],
                "ok": False,
                "error_message": "Тариф не найден. Обновите приложение.",
            },
        )


async def handle_message(session: aiohttp.ClientSession, msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]

    if "successful_payment" in msg:
        payment = msg["successful_payment"]
        parts = payment.get("invoice_payload", "").split(":")
        plan_key = parts[1] if len(parts) >= 2 and parts[1] in PLANS else "month"

        new_until = activate_pro(
            user_id=user_id,
            plan_key=plan_key,
            stars=payment["total_amount"],
            charge_id=payment["telegram_payment_charge_id"],
        )
        until_str = time.strftime("%d.%m.%Y", time.localtime(new_until))
        logger.info("Pro activated: user=%s plan=%s stars=%s", user_id, plan_key, payment["total_amount"])

        await tg_call(
            session,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    f"🎉 Оплата <b>{payment['total_amount']} ⭐</b> прошла успешно!\n"
                    f"Подписка <b>{PLANS[plan_key]['title']}</b> активна до <b>{until_str}</b>."
                ),
                "parse_mode": "HTML",
            },
        )
        return

    text = (msg.get("text") or "").strip()

    if text.startswith("/start"):
        await tg_call(
            session,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    "<b>EloCoach CS2</b> — разбор матчей FACEIT, поиск ошибок и советы для роста ELO прямо в Telegram.\n\n"
                    "Нажми кнопку ниже и введи свой ник FACEIT:"
                ),
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "Открыть EloCoach 🎯", "web_app": {"url": WEBAPP_URL}}]
                    ]
                },
            },
        )
    elif text.startswith("/status"):
        rec = get_user_record(user_id)
        if rec["is_pro"]:
            until_str = time.strftime("%d.%m.%Y", time.localtime(rec["pro_until"]))
            reply = f"⭐ У вас активен <b>EloCoach Pro</b> до {until_str}."
        else:
            reply = "Сейчас тариф Free. Открыть полный разбор 20 игр и Тильт-детектор можно внутри приложения."
        await tg_call(session, "sendMessage", {"chat_id": chat_id, "text": reply, "parse_mode": "HTML"})
    elif text.startswith("/paysupport"):
        await tg_call(
            session,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": f"По вопросам оплаты Telegram Stars и подписки EloCoach Pro напишите: {SUPPORT_USERNAME}",
            },
        )


async def poll_updates(app: web.Application) -> None:
    session: aiohttp.ClientSession = app["http_session"]
    offset = 0

    try:
        await tg_call(session, "deleteWebhook", {"drop_pending_updates": False})
        logger.info("Polling Telegram запущен")
    except Exception as exc:
        logger.error("Ошибка старта polling: %s", exc)

    while True:
        try:
            data = await tg_call(
                session,
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": ["message", "pre_checkout_query"],
                },
            )
            if not data.get("ok"):
                logger.error("Ошибка Telegram API: %s", data)
                await asyncio.sleep(5)
                continue

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                if "pre_checkout_query" in upd:
                    await handle_pre_checkout(session, upd["pre_checkout_query"])
                elif "message" in upd:
                    await handle_message(session, upd["message"])
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Ошибка polling: %s", exc)
            await asyncio.sleep(3)


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)

    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data"
    return response


async def api_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "faceit_ready": bool(FACEIT_API_KEY)})


async def api_me(request: web.Request) -> web.Response:
    user = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if not user:
        return web.json_response({"faceit_nick": "", "is_pro": False, "pro_until": None})
    return web.json_response(get_user_record(int(user["id"])))


async def api_analyze(request: web.Request) -> web.Response:
    if not FACEIT_API_KEY:
        return web.json_response(
            {"error": "На сервере не добавлена переменная FACEIT_API_KEY"}, status=500
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Неверный формат запроса"}, status=400)

    nickname = extract_nickname(body.get("nickname", ""))
    if not nickname:
        return web.json_response({"error": "Введите никнейм FACEIT"}, status=400)

    tg_user = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    is_pro = False
    if tg_user:
        rec = get_user_record(int(tg_user["id"]))
        is_pro = rec["is_pro"]

    session: aiohttp.ClientSession = request.app["http_session"]
    status, player = await find_faceit_player(session, nickname)

    if status in (401, 403):
        return web.json_response(
            {"error": "FACEIT отклонил ключ API. Проверьте FACEIT_API_KEY в Amvera."}, status=502
        )
    if status == 429:
        return web.json_response(
            {"error": "Лимит запросов FACEIT. Повторите через 30 секунд."}, status=429
        )
    if status != 200 or not player:
        return web.json_response(
            {"error": f"Игрок «{nickname}» не найден на FACEIT"}, status=404
        )

    games = player.get("games") or {}
    if "cs2" not in games:
        return web.json_response(
            {"error": f"У игрока {player.get('nickname')} нет статистики CS2 на FACEIT"}, status=404
        )

    player_id = player["player_id"]
    if tg_user:
        save_user_faceit(int(tg_user["id"]), player.get("nickname", nickname), player_id)

    (_, recent_resp), (_, lifetime_resp) = await asyncio.gather(
        faceit_get(session, f"/players/{player_id}/games/cs2/stats?limit=20"),
        faceit_get(session, f"/players/{player_id}/stats/cs2"),
    )

    recent_items = recent_resp.get("items") or []
    result = analyze_cs2_performance(player, recent_items, lifetime_resp or {}, is_pro)
    return web.json_response(result)


async def api_create_invoice(request: web.Request) -> web.Response:
    user = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if not user:
        return web.json_response({"error": "Откройте приложение внутри Telegram"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    plan_key = body.get("plan")
    if plan_key not in PLANS:
        return web.json_response({"error": "Unknown plan"}, status=400)

    plan = PLANS[plan_key]
    res = await tg_call(
        request.app["http_session"],
        "createInvoiceLink",
        {
            "title": plan["title"],
            "description": plan["description"],
            "payload": f"elocoach:{plan_key}:{user['id']}",
            "provider_token": "",
            "currency": "XTR",
            "prices": [{"label": plan["title"], "amount": plan["stars"]}],
        },
    )
    if not res.get("ok"):
        logger.error("Ошибка создания счёта: %s", res)
        return web.json_response({"error": "Не удалось создать счёт Stars"}, status=500)

    return web.json_response({"invoice_url": res["result"]})


async def on_startup(app: web.Application) -> None:
    app["http_session"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35))
    app["polling_task"] = asyncio.create_task(poll_updates(app))


async def on_cleanup(app: web.Application) -> None:
    app["polling_task"].cancel()
    try:
        await app["polling_task"]
    except asyncio.CancelledError:
        pass
    await app["http_session"].close()


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Добавьте BOT_TOKEN в разделе Переменные на Amvera")

    init_db()
    logger.info("База данных готова: %s", DB_PATH)

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", api_health)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/me", api_me)
    app.router.add_post("/api/analyze", api_analyze)
    app.router.add_post("/api/create-invoice", api_create_invoice)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", API_PORT)
    await site.start()
    logger.info("HTTP API запущен на порту %s", API_PORT)

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())