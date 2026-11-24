from datetime import datetime, timedelta, timezone
import json
import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import requests

# Konfigurasi dari Environment Variables / GitHub Secrets
SIAKAD_URL = os.environ.get("SIAKAD_URL", "https://ithb.ac.id")
NIM = os.environ.get("SIAKAD_NIM")
PASSWORD = os.environ.get("SIAKAD_PASS")
CALENDAR_ID = os.environ.get(
    "GOOGLE_CALENDAR_ID", "primary"
)  # Bisa diganti ID sub-kalender
SCOPES = ["https://www.googleapis.com/auth/calendar"]
WIB = timezone(timedelta(hours=7))


def get_calendar_service():
  creds = None
  # Saat jalan di GitHub Actions, token dibaca dari Secret
  if os.environ.get("GOOGLE_TOKEN_JSON"):
    token_info = json.loads(os.environ["GOOGLE_TOKEN_JSON"])
    creds = Credentials.from_authorized_user_info(token_info, SCOPES)
  elif os.path.exists("token.json"):
    creds = Credentials.from_authorized_user_file("token.json", SCOPES)

  if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
      creds.refresh(Request())
    else:
      raise RuntimeError(
          "Token tidak valid atau tidak ditemukan. Jalankan auth_setup.py"
          " dulu."
      )

  return build("calendar", "v3", credentials=creds)


def fetch_siakad_schedule(start_ts, end_ts):
  session = requests.Session()
  # 1. Login ke SIAKAD
  login_payload = {"act": "login_act", "login": NIM, "pass": PASSWORD}
  res_login = session.post(SIAKAD_URL, data=login_payload, timeout=15)
  res_login.raise_for_status()

  # 2. Ambil data jadwal JSON
  params = {"start": start_ts, "end": end_ts}
  res_sched = session.get(SIAKAD_URL, params=params, timeout=15)
  res_sched.raise_for_status()

  return res_sched.json()


def parse_title(raw_title):
  # Format: Kode Matkul<br>Nama Matkul<br>Ruangan
  parts = [p.strip() for p in raw_title.split("<br>") if p.strip()]
  kode = parts[0] if len(parts) > 0 else "KULIAH"
  nama = parts[1] if len(parts) > 1 else "Mata Kuliah"
  ruangan = parts[2] if len(parts) > 2 else "-"
  return kode, nama, ruangan


def to_rfc3339(dt_str):
  # Mengonversi format waktu dari SIAKAD (misal "2026-11-25 08:00:00" atau ISO) ke WIB
  dt_clean = dt_str.replace("T", " ").split("+")[0].strip()
  dt = datetime.strptime(dt_clean, "%Y-%m-%d %H:%M:%S").replace(tzinfo=WIB)
  return dt.isoformat()


def sync():
  service = get_calendar_service()

  # Rentang sinkronisasi: 7 hari ke belakang sampai 30 hari ke depan
  now = datetime.now(WIB)
  start_dt = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0)
  end_dt = (now + timedelta(days=30)).replace(hour=23, minute=59, second=59)

  start_ts = int(start_dt.timestamp())
  end_ts = int(end_dt.timestamp())

  print("Mengambil jadwal dari SIAKAD...")
  siakad_events = fetch_siakad_schedule(start_ts, end_ts)

  # Ambil acara yang sudah ada di Google Calendar (hanya yang dibuat oleh skrip ini)
  existing_res = (
      service.events()
      .list(
          calendarId=CALENDAR_ID,
          timeMin=start_dt.isoformat(),
          timeMax=end_dt.isoformat(),
          privateExtendedProperty="source=ithb_sync",
          singleEvents=True,
      )
      .execute()
  )
  existing_items = existing_res.get("items", [])

  # Petakan acara Google Calendar berdasarkan siakad_uid
  gcal_map = {}
  for item in existing_items:
    props = item.get("extendedProperties", {}).get("private", {})
    uid = props.get("siakad_uid")
    if uid:
      gcal_map[uid] = item

  active_uids = set()

  for ev in siakad_events:
    kode, nama, ruangan = parse_title(ev.get("title", ""))
    start_iso = to_rfc3339(ev["start"])
    end_iso = to_rfc3339(ev["end"])

    # Gunakan ID bawaan SIAKAD jika ada, atau kombinasi kode + tanggal mulai
    raw_id = str(ev.get("id", f"{kode}_{start_iso[:10]}"))
    uid = f"ithb_{raw_id}"
    active_uids.add(uid)

    summary = f"{kode} - {nama}"
    event_body = {
        "summary": summary,
        "location": ruangan,
        "description": f"Sinkronisasi otomatis SIAKAD ITHB.\nRuangan: {ruangan}",
        "start": {"dateTime": start_iso, "timeZone": "Asia/Jakarta"},
        "end": {"dateTime": end_iso, "timeZone": "Asia/Jakarta"},
        "extendedProperties": {
            "private": {"source": "ithb_sync", "siakad_uid": uid}
        },
    }

    if uid in gcal_map:
      existing_ev = gcal_map[uid]
      # Cek apakah ada perubahan nama, ruangan, jam mulai, atau jam selesai
      is_changed = (
          existing_ev.get("summary") != summary
          or existing_ev.get("location", "") != ruangan
          or existing_ev["start"].get("dateTime") != start_iso
          or existing_ev["end"].get("dateTime") != end_iso
      )
      if is_changed:
        service.events().update(
            calendarId=CALENDAR_ID, eventId=existing_ev["id"], body=event_body
        ).execute()
        print(f"[UPDATED] {summary} ({start_iso} @ {ruangan})")
    else:
      service.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()
      print(f"[INSERTED] {summary} ({start_iso} @ {ruangan})")

  # Hapus jadwal di Google Calendar yang sudah tidak ada / dibatalkan di SIAKAD
  for uid, existing_ev in gcal_map.items():
    if uid not in active_uids:
      service.events().delete(
          calendarId=CALENDAR_ID, eventId=existing_ev["id"]
      ).execute()
      print(f"[DELETED] {existing_ev.get('summary')} (Kelas dibatalkan/dihapus)")

  print("Sinkronisasi selesai.")


if __name__ == "__main__":
  sync()