import json
import os
import requests
from bs4 import BeautifulSoup

# Muat file .env secara otomatis jika dijalankan di lokal (opsional)
if os.path.exists(".env"):
    with open(".env", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

BASE_URL = os.environ.get("SIAKAD_URL", "https://akademik.ithb.ac.id").rstrip("/")
NIM = os.environ.get("SIAKAD_NIM")
PASSWORD = os.environ.get("SIAKAD_PASS")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

STATE_FILE = "grades_state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials not set. Skipping notification.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    print("Telegram notification sent.")


def login_siakad() -> requests.Session:
    if not NIM or not PASSWORD:
        raise RuntimeError(
            "SIAKAD_NIM atau SIAKAD_PASS kosong! Set di terminal ($env:SIAKAD_NIM=...) atau file .env."
        )

    session = requests.Session()
    session.headers.update(HEADERS)

    login_url = f"{BASE_URL}/default.php?mod=layanan"
    session.get(login_url, timeout=15)

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
    return session


def clean_val(text: str) -> str:
    cleaned = text.replace("\xa0", " ").strip()
    return "-" if cleaned in ("", " ") else cleaned


def fetch_khs(session: requests.Session):
    khs_url = f"{BASE_URL}/default.php?mod=layanan&page=Laporan%20Nilai%20IP%20Semester"

    # 1. Buka halaman awal untuk memastikan login berhasil & ambil parameter default
    init_resp = session.get(khs_url, timeout=20)
    init_resp.raise_for_status()
    init_soup = BeautifulSoup(init_resp.text, "html.parser")

    nama_input = init_soup.find("input", {"name": "NAMA"})
    if not nama_input:
        raise RuntimeError("Login SIAKAD gagal (form KHS tidak ditemukan). Periksa NIM dan Password.")

    nama = nama_input["value"].strip() if nama_input.has_attr("value") else ""

    tahun_select = init_soup.find("select", {"name": "TAHUN"})
    tahun = "2026"
    if tahun_select:
        selected_opt = tahun_select.find("option", selected=True)
        if selected_opt and selected_opt.get("value"):
            tahun = selected_opt["value"].strip()

    prd_select = init_soup.find("select", {"name": "PRD"})
    prd = "1"
    if prd_select:
        selected_prd = prd_select.find("option", selected=True)
        if selected_prd and selected_prd.get("value"):
            prd = selected_prd["value"].strip()

    tahun = os.environ.get("KHS_TAHUN", tahun)
    prd = os.environ.get("KHS_PRD", prd)  # 1 = Ganjil, 2 = Genap, 0 = Pendek

    # 2. Kirim POST untuk menampilkan tabel KHS
    form_data = {
        "NIM": NIM,
        "NAMA": nama,
        "TAHUN": tahun,
        "PRD": prd,
        "show": "1",
    }
    resp = session.post(
        khs_url,
        data=form_data,
        headers={
            "Referer": khs_url,
            "Origin": BASE_URL,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=20,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    table = soup.find("table", class_="simTable")
    if not table:
        print(f"Tabel KHS belum tersedia untuk Tahun {tahun} PRD {prd}.")
        return None

    courses = {}
    ips = "-"
    ipk = "-"

    for row in table.find_all("tr"):
        cols = row.find_all("td")
        if len(cols) == 13:
            no_text = clean_val(cols[0].get_text())
            if not no_text.isdigit():
                continue

            kode = clean_val(cols[1].get_text())
            matkul = clean_val(cols[2].get_text())
            sks = clean_val(cols[4].get_text())

            grades = {
                "Nilai ke-1": clean_val(cols[5].get_text()),
                "Nilai ke-2": clean_val(cols[6].get_text()),
                "Nilai ke-3": clean_val(cols[7].get_text()),
                "Nilai ke-4": clean_val(cols[8].get_text()),
                "Nilai ke-5": clean_val(cols[9].get_text()),
                "Nilai UAS": clean_val(cols[10].get_text()),
                "Nilai Akhir": clean_val(cols[11].get_text()),
                "Nilai Huruf": clean_val(cols[12].get_text()),
            }

            courses[kode] = {
                "matkul": matkul,
                "sks": sks,
                "grades": grades,
            }
        else:
            row_text = " ".join(c.get_text(" ", strip=True) for c in cols)
            if "IPS" in row_text and len(cols) >= 3:
                ips = clean_val(cols[-1].get_text())
            elif "IPK" in row_text and len(cols) >= 3:
                ipk = clean_val(cols[-1].get_text())

    return {
        "periode": f"{tahun}-{prd}",
        "ips": ips,
        "ipk": ipk,
        "courses": courses,
    }


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def main():
    session = login_siakad()
    current = fetch_khs(session)
    if not current:
        return

    old_state = load_state()
    periode = current["periode"]
    old_periode_data = old_state.get(periode, {})
    old_courses = old_periode_data.get("courses", {})

    if not old_courses:
        old_state[periode] = current
        save_state(old_state)
        summary_lines = [
            f"✅ <b>Monitoring Nilai SIAKAD Aktif ({periode})</b>",
            f"Total Mata Kuliah: {len(current['courses'])} | IPS: {current['ips']} | IPK: {current['ipk']}",
            "Bot akan langsung mengirim rincian mata kuliah, nilai ke berapa, dan skornya setiap kali ada nilai baru masuk.",
        ]
        send_telegram("\n".join(summary_lines))
        return

    updates = []
    for kode, info in current["courses"].items():
        prev_info = old_courses.get(kode, {})
        prev_grades = prev_info.get("grades", {})
        curr_grades = info["grades"]

        changed_items = []
        for comp, new_val in curr_grades.items():
            old_val = prev_grades.get(comp, "-")
            if new_val != old_val and new_val != "-":
                if old_val == "-":
                    changed_items.append(f"• <b>{comp}</b>: <b>{new_val}</b> <i>(baru diinput)</i>")
                else:
                    changed_items.append(f"• <b>{comp}</b>: <code>{old_val}</code> → <b>{new_val}</b> <i>(diperbarui)</i>")

        if changed_items:
            block = f"📘 <b>{info['matkul']}</b> (<code>{kode}</code>)\n" + "\n".join(changed_items)
            updates.append(block)

    if updates:
        header = f"🔔 <b>Nilai Baru SIAKAD ITHB ({periode})</b>\n\n"
        footer = f"\n\n📊 <b>IPS:</b> {current['ips']} | <b>IPK:</b> {current['ipk']}"
        send_telegram(header + "\n\n".join(updates) + footer)
        old_state[periode] = current
        save_state(old_state)
    else:
        print("Tidak ada perubahan nilai.")


if __name__ == "__main__":
    main()