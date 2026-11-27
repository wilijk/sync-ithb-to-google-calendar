import io
import json
import os
import threading
import time
from datetime import datetime
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
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
TZ_NAME = "Asia/Jakarta"
TZ = ZoneInfo(TZ_NAME)
SCOPES = ["https://www.googleapis.com/auth/calendar"]


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
# TOOLS UNTUK GEMINI FUNCTION CALLING
# ==========================================


def get_schedule(start_iso: str, end_iso: str) -> str:
    """Mengambil daftar acara dan jadwal kuliah dari Google Calendar beserta event_id-nya pada rentang waktu ISO 8601 (misal: 2026-11-28T00:00:00+07:00)."""
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

    # Gabungkan interval sibuk yang tumpang tindih
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
            "private": {"source": "ai_assistant"}
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
    """Menghapus acara atau jadwal dari Google Calendar berdasarkan event_id. Selalu panggil get_schedule terlebih dahulu untuk mendapatkan event_id yang tepat sebelum menghapus."""
    service = get_calendar_service()
    service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
    return json.dumps({
        "status": "deleted",
        "event_id": event_id,
    }, ensure_ascii=False)


def get_latest_grades() -> str:
    """Membaca data nilai KHS terbaru yang tersimpan di grades_state.json."""
    if not os.path.exists("grades_state.json"):
        return json.dumps({"status": "not_found", "message": "File grades_state.json belum ada."})
    with open("grades_state.json", "r", encoding="utf-8") as f:
        return f.read()


# ==========================================
# GEMINI AGENT & TELEGRAM HANDLERS
# ==========================================

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
TOOLS = [
    get_schedule,
    find_free_slots,
    create_calendar_event,
    delete_calendar_event,
    get_latest_grades,
]


def ask_gemini_agent(user_content) -> str:
    now = datetime.now(TZ)
    hari_id = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"][now.weekday()]

    system_instruction = (
        f"Kamu adalah Asisten Akademik & Kalender pribadi untuk mahasiswa Informatika ITHB.\n"
        f"Waktu saat ini: {hari_id}, {now.strftime('%Y-%m-%d %H:%M')} WIB (UTC+07:00).\n"
        f"Pengguna dapat memberikan perintah lewat teks maupun pesan suara (voice note).\n"
        f"Gunakan tools yang tersedia untuk membaca jadwal kuliah, mencari waktu kosong, menjadwalkan kegiatan baru, menghapus jadwal, atau mengecek nilai KHS.\n"
        f"Jika pengguna meminta menghapus suatu jadwal, panggil get_schedule terlebih dahulu untuk memastikan nama dan event_id jadwal tersebut sebelum memanggil delete_calendar_event. Jika ada lebih dari satu jadwal yang mirip atau ambigu, tanyakan konfirmasi ke pengguna dulu.\n"
        f"Jika pengguna meminta mencari waktu kosong dalam rentang beberapa hari (misalnya 'minggu ini'), panggil find_free_slots untuk masing-masing hari yang relevan.\n"
        f"Jawab dengan ringkas, jelas, ramah, dan gunakan format poin yang rapi."
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
                    temperature=0.2,
                ),
            )
            return response.text or "Selesai diproses."
        except Exception as e:
            last_error = e
            err_str = str(e)
            if any(code in err_str for code in ("503", "UNAVAILABLE", "429", "404")):
                time.sleep(1)
                continue
            raise

    raise last_error


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != ALLOWED_CHAT_ID:
        return
    await update.message.reply_text(
        "Halo! Asisten Akademik & Kalender ITHB aktif (mendukung Teks & Voice Note).\n\n"
        "Contoh yang bisa kamu kirim lewat ketikan atau suara:\n"
        "• \"Jadwal kuliah besok apa aja?\"\n"
        "• \"Carikan waktu kosong minimal 2 jam besok buat ngerjain tugas\"\n"
        "• \"Tolong blok jam 15.00-17.00 besok buat ngajar privat\"\n"
        "• \"Hapus jadwal ngajar privat besok\"\n"
        "• \"Gimana rekap nilai KHS aku sekarang?\""
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if str(update.effective_chat.id) != ALLOWED_CHAT_ID:
        return

    await update.message.chat.send_action("typing")
    try:
        reply = ask_gemini_agent(update.message.text)
    except Exception as e:
        reply = f"Terjadi kendala saat memproses permintaan: {e}"

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
            "Dengarkan pesan suara pengguna di atas dan jalankan permintaan atau pertanyaan kalender/akademik yang dimaksud.",
        ]
        reply = ask_gemini_agent(contents)
    except Exception as e:
        reply = f"Terjadi kendala saat memproses voice note: {e}"

    await update.message.reply_text(reply)


# ==========================================
# HEALTH-CHECK SERVER UNTUK HOSTING CLOUD
# ==========================================


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

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

    print("AI Assistant Telegram (Teks & Voice) berjalan... Tekan Ctrl+C untuk berhenti.")
    app.run_polling()


if __name__ == "__main__":
    main()