import io
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, time as dt_time
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

from google import genai
from google.auth.transport.requests import Request
from google.genai import types
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Muat file .env secara otomatis jika dijalankan di lokal
if os.path.exists(".env"):
    with open(".env", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
TZ_NAME = "Asia/Jakarta"
TZ = ZoneInfo(TZ_NAME)
SCOPES = ["https://www.googleapis.com/auth/calendar"]
MEMORY_FILE = "aira_memory.json"

# Menyimpan 12 interaksi terakhir agar percakapan nyambung
CHAT_HISTORY = deque(maxlen=12)


def load_memory_store() -> dict:
    if not os.path.exists(MEMORY_FILE):
        return {"facts": [], "tasks": []}
    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("facts", [])
    data.setdefault("tasks", [])
    return data


def save_memory_store(data: dict):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_calendar_service():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    elif os.environ.get("GOOGLE_TOKEN_JSON"):
        token_info = json.loads(os.environ["GOOGLE_TOKEN_JSON"])
        creds = Credentials.from_authorized_user_info(token_info, SCOPES)

    if not creds:
        raise RuntimeError("token.json atau GOOGLE_TOKEN_JSON tidak ditemukan.")

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        if os.path.exists("token.json"):
            with open("token.json", "w", encoding="utf-8") as f:
                f.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


# ==========================================
# TOOLS UNTUK AIRA (FUNCTION CALLING)
# ==========================================


def get_schedule(start_iso: str, end_iso: str) -> str:
    """Mengambil daftar acara dan jadwal dari Google Calendar beserta event_id-nya pada rentang waktu ISO 8601 (misal: 2026-11-28T00:00:00+07:00)."""
    service = get_calendar_service()
    events_result = (
        service.events()
        .list(
            calendarId=CALENDAR_ID,
            timeMin=start_iso,
            timeMax=end_iso,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    items = events_result.get("items", [])
    if not items:
        return json.dumps({"status": "empty", "events": []})

    formatted = []
    for ev in items:
        start = ev["start"].get("dateTime", ev["start"].get("date"))
        end = ev["end"].get("dateTime", ev["end"].get("date"))
        formatted.append({
            "event_id": ev.get("id"),
            "summary": ev.get("summary", "(Tanpa Judul)"),
            "start": start,
            "end": end,
            "location": ev.get("location", ""),
        })
    return json.dumps({"status": "ok", "events": formatted}, ensure_ascii=False)


def find_free_slots(
    date_str: str,
    min_duration_minutes: int = 60,
    start_hour: int = 7,
    end_hour: int = 21,
) -> str:
    """Mencari celah waktu kosong pada tanggal tertentu (format YYYY-MM-DD) dengan durasi minimal tertentu dalam menit antara jam start_hour hingga end_hour."""
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    day_start = datetime(target_date.year, target_date.month, target_date.day, start_hour, 0, tzinfo=TZ)
    day_end = datetime(target_date.year, target_date.month, target_date.day, end_hour, 0, tzinfo=TZ)

    service = get_calendar_service()
    events_result = (
        service.events()
        .list(
            calendarId=CALENDAR_ID,
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    busy_intervals = []
    for ev in events_result.get("items", []):
        s_str = ev["start"].get("dateTime")
        e_str = ev["end"].get("dateTime")
        if s_str and e_str:
            s_dt = datetime.fromisoformat(s_str).astimezone(TZ)
            e_dt = datetime.fromisoformat(e_str).astimezone(TZ)
            busy_intervals.append((max(s_dt, day_start), min(e_dt, day_end)))

    busy_intervals.sort(key=lambda x: x[0])

    merged = []
    for s, e in busy_intervals:
        if s >= e:
            continue
        if not merged or s > merged[-1][1]:
            merged.append([s, e])
        else:
            merged[-1][1] = max(merged[-1][1], e)

    free_slots = []
    cursor = day_start
    for s, e in merged:
        if (s - cursor).total_seconds() / 60 >= min_duration_minutes:
            free_slots.append({
                "start": cursor.strftime("%H:%M"),
                "end": s.strftime("%H:%M"),
                "duration_minutes": int((s - cursor).total_seconds() / 60),
            })
        cursor = max(cursor, e)

    if (day_end - cursor).total_seconds() / 60 >= min_duration_minutes:
        free_slots.append({
            "start": cursor.strftime("%H:%M"),
            "end": day_end.strftime("%H:%M"),
            "duration_minutes": int((day_end - cursor).total_seconds() / 60),
        })

    return json.dumps({
        "date": date_str,
        "min_duration_minutes": min_duration_minutes,
        "free_slots": free_slots,
    }, ensure_ascii=False)


def create_calendar_event(
    summary: str,
    start_iso: str,
    end_iso: str,
    description: str = "",
) -> str:
    """Membuat jadwal atau acara baru di Google Calendar. Gunakan format ISO 8601 lengkap dengan offset +07:00 (misal: 2026-11-28T15:00:00+07:00)."""
    service = get_calendar_service()
    event_body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_iso, "timeZone": TZ_NAME},
        "end": {"dateTime": end_iso, "timeZone": TZ_NAME},
        "extendedProperties": {
            "private": {"source": "aira_assistant"}
        },
    }
    created = service.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()
    return json.dumps({
        "status": "created",
        "event_id": created.get("id"),
        "summary": created.get("summary"),
        "start": created["start"].get("dateTime"),
        "end": created["end"].get("dateTime"),
        "link": created.get("htmlLink"),
    }, ensure_ascii=False)


def delete_calendar_event(event_id: str) -> str:
    """Menghapus acara atau jadwal dari Google Calendar berdasarkan event_id. Selalu panggil get_schedule terlebih dahulu untuk mendapatkan event_id yang tepat."""
    service = get_calendar_service()
    service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
    return json.dumps({"status": "deleted", "event_id": event_id}, ensure_ascii=False)


def manage_tasks(action: str, task_text: str = "", task_index: int = -1) -> str:
    """Mengelola daftar tugas / to-do list pribadi. action bisa berupa: 'add', 'list', 'done', atau 'delete'. Untuk 'done'/'delete', gunakan task_index (mulai dari 1)."""
    store = load_memory_store()
    tasks = store["tasks"]

    if action == "add" and task_text:
        tasks.append({
            "task": task_text,
            "done": False,
            "created_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        })
        save_memory_store(store)
        return json.dumps({"status": "added", "task": task_text, "total_tasks": len(tasks)}, ensure_ascii=False)

    if action in ("done", "delete") and 1 <= task_index <= len(tasks):
        target = tasks[task_index - 1]
        if action == "done":
            target["done"] = True
        else:
            tasks.pop(task_index - 1)
        save_memory_store(store)
        return json.dumps({"status": action, "task": target}, ensure_ascii=False)

    numbered = [{"index": i + 1, **t} for i, t in enumerate(tasks)]
    return json.dumps({"status": "ok", "tasks": numbered}, ensure_ascii=False)


def remember_fact(fact: str) -> str:
    """Menyimpan fakta, preferensi, catatan penting, atau konteks jangka panjang pengguna ke memori permanen Aira."""
    store = load_memory_store()
    if fact not in store["facts"]:
        store["facts"].append(fact)
        save_memory_store(store)
    return json.dumps({"status": "remembered", "fact": fact}, ensure_ascii=False)


def get_latest_grades() -> str:
    """Membaca data nilai KHS mahasiswa (termasuk riwayat 2025-1, 2025-2, dan semester aktif) dari grades_state.json."""
    if not os.path.exists("grades_state.json"):
        return json.dumps({"status": "not_found", "message": "File grades_state.json belum ada."})
    with open("grades_state.json", "r", encoding="utf-8") as f:
        return f.read()


# ==========================================
# AIRA BRAIN & TELEGRAM HANDLERS
# ==========================================

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
TOOLS = [
    get_schedule,
    find_free_slots,
    create_calendar_event,
    delete_calendar_event,
    manage_tasks,
    remember_fact,
    get_latest_grades,
]


def ask_aira(user_content, history_label: str = "") -> str:
    now = datetime.now(TZ)
    hari_id = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"][now.weekday()]
    store = load_memory_store()

    facts_block = "\n".join(f"- {f}" for f in store["facts"]) if store["facts"] else "- (Belum ada catatan permanen)"
    pending_tasks = [f"{i+1}. {t['task']}" for i, t in enumerate(store["tasks"]) if not t.get("done")]
    tasks_block = "\n".join(pending_tasks) if pending_tasks else "- (Tidak ada tugas tertunda)"

    recent_chat = "\n".join(CHAT_HISTORY) if CHAT_HISTORY else "(Percakapan baru)"

    system_instruction = (
        f"Kamu adalah Aira (Artificial Intelligence & Routine Assistant), asisten AI pribadi yang cerdas, sigap, hangat, dan suportif (panggil pengguna dengan 'Jer' secara natural dan akrab, bukan kaku seperti robot).\n"
        f"Waktu saat ini: {hari_id}, {now.strftime('%Y-%m-%d %H:%M')} WIB (UTC+07:00).\n\n"
        f"MEMORI PERMANEN TENTANG PENGGUNA:\n{facts_block}\n\n"
        f"DAFTAR TUGAS AKTIF SAAT INI:\n{tasks_block}\n\n"
        f"RIWAYAT PERCAKAPAN TERAKHIR:\n{recent_chat}\n\n"
        f"ATURAN OPERASIONAL:\n"
        f"1. Pengguna bisa mengirim teks maupun voice note. Pahami maksudnya secara menyeluruh dan langsung eksekusi tool yang tepat tanpa bertele-tele.\n"
        f"2. Jika pengguna menyebut sesuatu yang layak diingat jangka panjang (preferensi, jadwal rutin, proyek, info penting), panggil remember_fact.\n"
        f"3. Jika pengguna meminta menghapus jadwal, panggil get_schedule dulu untuk mendapat event_id, lalu panggil delete_calendar_event.\n"
        f"4. Jawab dengan ringkas, jelas, ramah, dan terstruktur."
    )

    models_to_try = [
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.8-flash",
    ]
    last_error = None

    for model_name in models_to_try:
        try:
            response = gemini_client.models.generate_content(
                model=model_name,
                contents=user_content,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    tools=TOOLS,
                    temperature=0.3,
                ),
            )
            reply_text = response.text or "Sudah beres ya, Jer."
            if history_label:
                CHAT_HISTORY.append(f"User: {history_label}")
            CHAT_HISTORY.append(f"Aira: {reply_text}")
            return reply_text
        except Exception as e:
            last_error = e
            err_str = str(e)
            if any(code in err_str for code in ("503", "UNAVAILABLE", "429", "404")):
                time.sleep(1)
                continue
            raise

    raise last_error


async def morning_briefing_job(context: ContextTypes.DEFAULT_TYPE):
    """Mengirim briefing pagi otomatis setiap pukul 06:30 WIB."""
    if not ALLOWED_CHAT_ID:
        return
    try:
        prompt = (
            "Berikan sapaan pagi dan Morning Briefing singkat untuk hari ini: "
            "cek seluruh jadwal kalender hari ini menggunakan get_schedule, "
            "gabungkan dengan daftar tugas aktif yang belum selesai, dan beri rangkuman yang enak dibaca."
        )
        briefing = ask_aira(prompt, history_label="[Sistem: Morning Briefing 06:30]")
        await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text=briefing)
    except Exception as e:
        print(f"Gagal mengirim morning briefing: {e}")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != ALLOWED_CHAT_ID:
        return
    await update.message.reply_text(
        "Halo Jer! Aira sudah aktif dan siap bantu aktivitasmu hari ini.\n\n"
        "Yang bisa aku bantu lewat chat atau voice note:\n"
        "• Atur Kalender (cek jadwal, cari waktu kosong, buat & hapus acara)\n"
        "• To-Do List & Memori (catat tugas, ide, atau hal penting yang perlu aku ingat)\n"
        "• Pantau Nilai KHS (cek rekap nilai kuliah dari 2025-1 sampai sekarang)\n"
        "• Morning Briefing otomatis tiap jam 06.30 pagi"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if str(update.effective_chat.id) != ALLOWED_CHAT_ID:
        return

    await update.message.chat.send_action("typing")
    user_text = update.message.text
    try:
        reply = ask_aira(user_text, history_label=user_text)
    except Exception as e:
        reply = f"Maaf Jer, ada kendala sistem pas memproses permintaanmu: {e}"

    await update.message.reply_text(reply)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.voice:
        return
    if str(update.effective_chat.id) != ALLOWED_CHAT_ID:
        return

    await update.message.chat.send_action("typing")
    try:
        voice_file = await update.message.voice.get_file()
        buf = io.BytesIO()
        await voice_file.download_to_memory(buf)
        audio_bytes = buf.getvalue()

        contents = [
            types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
            "Dengarkan pesan suara pengguna di atas dan jalankan perintah atau pertanyaan yang dimaksud sebagai Aira.",
        ]
        reply = ask_aira(contents, history_label="[Voice Note dari User]")
    except Exception as e:
        reply = f"Maaf Jer, ada kendala pas memproses voice note kamu: {e}"

    await update.message.reply_text(reply)


# ==========================================
# HEALTH-CHECK SERVER UNTUK HOSTING CLOUD
# ==========================================


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"AIRA ONLINE")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


def main():
    if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
        raise RuntimeError("Pastikan TELEGRAM_BOT_TOKEN dan GEMINI_API_KEY sudah diisi di .env!")

    threading.Thread(target=start_health_server, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # Jadwalkan Morning Briefing otomatis pukul 06:30 WIB setiap hari
    if app.job_queue:
        app.job_queue.run_daily(
            morning_briefing_job,
            time=dt_time(hour=6, minute=30, tzinfo=TZ),
            name="morning_briefing",
        )

    print("Aira aktif (Teks, Voice, Memori, & Morning Briefing)... Tekan Ctrl+C untuk berhenti.")
    app.run_polling()


if __name__ == "__main__":
    main()