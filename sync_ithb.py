from datetime import datetime, timedelta, timezone
import json
import os
import time
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import requests

# Konfigurasi dari Environment Variables / GitHub Secrets
BASE_URL = os.environ.get(
    "SIAKAD_URL", "https://akademik.ithb.ac.id"
).rstrip("/")
NIM = os.environ.get("SIAKAD_NIM")
PASSWORD = os.environ.get("SIAKAD_PASS")
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
SCOPES = ["https://www.googleapis.com/auth/calendar"]
WIB = timezone(timedelta(hours=7))


def get_calendar_service():
  creds = None
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
  session.headers.update({
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
          " (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
      ),
      "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
  })

  login_url = f"{BASE_URL}/default.php?mod=layanan"
  schedule_url = f"{BASE_URL}/modules/layanan/file/page_jadwalkuliah.php"

  # 1. Buka halaman modul layanan dulu untuk mendapatkan cookie PHPSESSID awal
  res_init = session.get(login_url, timeout=15)
  res_init.raise_for_status()

  # 2. Kirim form POST login ke default.php?mod=layanan
  login_payload = {
      "act": "login_act",
      "login": NIM,
      "pass": PASSWORD,
  }
  res_login = session.post(
      login_url,
      data=login_payload,
      headers={
          "Referer": login_url,
          "Origin": BASE_URL,
          "Content-Type": "application/x-www-form-urlencoded",
      },
      timeout=15,
  )
  res_login.raise_for_status()

  # 3. Ambil data JSON jadwal dari endpoint FullCalendar SIAKAD
  params = {
      "action": "process",
      "start": start_ts,
      "end": end_ts,
      "_": int(time.time() * 1000),
  }
  res_sched = session.get(
      schedule_url,
      params=params,
      headers={
          "Accept": "application/json, text/javascript, */*; q=0.01",
          "X-Requested-With": "XMLHttpRequest",
          "Referer": login_url,
      },
      timeout=15,
  )
  res_sched.raise_for_status()

  # Parse JSON dengan penanganan jika server mengirim Content-Type text/html
  try:
    return json.loads(res_sched.text.strip())
  except json.JSONDecodeError as e:
    preview = res_sched.text[:300].replace("\n", " ")
    raise RuntimeError(
        f"Gagal mem-parsing JSON dari SIAKAD. Cuplikan respons: {preview}"
    ) from e


def parse_title(raw_title):
  # Menangani tag <br>, <br/>, atau <br />
  normalized = (
      raw_title.replace("<br/>", "<br>")
      .replace("<br />", "<br>")
      .replace("<BR>", "<br>")
  )
  parts = [p.strip() for p in normalized.split("<br>") if p.strip()]
  kode = parts[0] if len(parts) > 0 else "KULIAH"
  nama = parts[1] if len(parts) > 1 else "Mata Kuliah"
  ruangan = parts[2] if len(parts) > 2 else "-"
  return kode, nama, ruangan


def to_rfc3339(dt_val):
  # Jika SIAKAD mengirim Unix timestamp (int/digit string)
  if isinstance(dt_val, (int, float)) or (
      isinstance(dt_val, str) and dt_val.isdigit()
  ):
    dt = datetime.fromtimestamp(int(dt_val), tz=WIB)
    return dt.isoformat()

  # Jika SIAKAD mengirim format string tanggal (misal "2026-11-23 08:00:00" atau ISO)
  dt_clean = str(dt_val).replace("T", " ").split("+")[0].strip()
  for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
    try:
      dt = datetime.strptime(dt_clean, fmt).replace(tzinfo=WIB)
      return dt.isoformat()
    except ValueError:
      continue
  raise ValueError(f"Format waktu tidak dikenali: {dt_val}")


def sync():
  service = get_calendar_service()

  # Rentang sinkronisasi: 7 hari ke belakang sampai 30 hari ke depan
  now = datetime.now(WIB)
  start_dt = (now - timedelta(days=7)).replace(
      hour=0, minute=0, second=0, microsecond=0
  )
  end_dt = (now + timedelta(days=30)).replace(
      hour=23, minute=59, second=59, microsecond=0
  )

  start_ts = int(start_dt.timestamp())
  end_ts = int(end_dt.timestamp())

  print("Mengambil jadwal dari SIAKAD...")
  siakad_events = fetch_siakad_schedule(start_ts, end_ts)
  print(f"Ditemukan {len(siakad_events)} sesi kelas dari SIAKAD.")

  # Ambil acara yang sudah ada di Google Calendar (khusus yang dibuat skrip ini)
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

    raw_id = str(ev.get("id", f"{kode}_{start_iso[:16]}"))
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

  # Hapus jadwal di Google Calendar jika kelas dibatalkan di SIAKAD
  for uid, existing_ev in gcal_map.items():
    if uid not in active_uids:
      service.events().delete(
          calendarId=CALENDAR_ID, eventId=existing_ev["id"]
      ).execute()
      print(f"[DELETED] {existing_ev.get('summary')} (Kelas dibatalkan/dihapus)")

  print("Sinkronisasi selesai.")


if __name__ == "__main__":
  sync()