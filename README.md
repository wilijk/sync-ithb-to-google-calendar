# SIAKAD ITHB to Google Calendar Auto-Sync

A Python and GitHub Actions automation tool to synchronize class and exam schedules from **SIAKAD ITHB** (`akademik.ithb.ac.id`) directly to **Google Calendar** automatically in the background every 30 minutes.

## Key Features

* **Automated Sync:** Runs independently in the cloud via GitHub Actions every 30 minutes without needing your laptop or PC turned on.
* **Real-Time Updates & Removals:** Automatically updates event times or room numbers when schedule changes occur in SIAKAD, and deletes events if a class is canceled.
* **Duplicate Prevention:** Built-in `concurrency` control and time-slot deduplication ensure no overlapping duplicate events are created.
* **Secure & Private:** SIAKAD credentials and Google OAuth tokens are securely encrypted inside GitHub Secrets.

## Prerequisites

Before getting started, make sure you have:

1. A free **GitHub** account.
2. A **Google / Gmail** account for Google Calendar.
3. **Python 3.10 or newer** installed on your computer (only needed once locally to generate your Google login token).
4. **Git** installed on your computer (optional; you can also use GitHub Desktop or manual file uploads).

## Repository Structure

Ensure your repository contains the following files:

* `sync_ithb.py` — Main script to fetch schedules from SIAKAD and update Google Calendar.
* `auth_setup.py` — One-time local script to generate `token.json`.
* `requirements.txt` — List of required Python dependencies.
* `.github/workflows/sync.yml` — GitHub Actions workflow configuration for automated scheduling.
* `.gitignore` — Prevents sensitive credential files (`token.json` and `credentials.json`) from being pushed to GitHub.

## Step-by-Step Tutorial

### Step 1: Fork & Clone the Repository

1. Click the **Fork** button in the top-right corner of this repository page to copy the project into your own GitHub account.
2. Open your terminal or Command Prompt and run the following commands to clone your forked repository:

   ```bash
   git clone https://github.com/<your-github-username>/<repo-name>.git
   cd <repo-name>
   ```

### Step 2: Configure Google Cloud Console (`credentials.json`)

This step grants the script permission to access and manage your Google Calendar.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and sign in with your Google account.
2. Click the project dropdown in the top-left corner -> click **New Project** -> name your project (e.g., `ITHB-Calendar-Sync`) -> click **Create**.
3. Make sure your newly created project is selected in the top bar.
4. Enable the Google Calendar API:
   * Open the left navigation menu (`☰`) -> **APIs & Services** -> **Library**.
   * Search for **Google Calendar API**, select it, and click **Enable**.
5. Configure the OAuth Consent Screen:
   * Go to **APIs & Services** -> **OAuth consent screen**.
   * Select **External** -> click **Create**.
   * Fill in the **App name** (e.g., `SIAKAD ITHB Sync`), **User support email**, and **Developer contact information** with your Gmail address -> click **Save and Continue**.
   * Under **Scopes**, click **Add or Remove Scopes**, search for `https://www.googleapis.com/auth/calendar`, check the box, and click **Update** -> **Save and Continue**.
   * Under **Test users**, click **Add Users** and enter your Gmail address -> click **Save and Continue**.
   * *(Highly Recommended)* Return to the **OAuth consent screen** page and click **Publish App** (changing the status from *Testing* to *In Production*) so your Google login token does not expire every 7 days.
6. Create OAuth Credentials:
   * Go to **APIs & Services** -> **Credentials**.
   * Click **+ Create Credentials** -> select **OAuth client ID**.
   * For **Application type**, choose **Desktop app** -> give it any name -> click **Create**.
   * In the confirmation pop-up, click **Download JSON**.
   * Rename the downloaded file to **`credentials.json`** and move it into your local project folder.

### Step 3: Generate Your Local Authentication Token (`token.json`)

1. Ensure `requirements.txt` contains the following packages:

   ```text
   requests
   google-api-python-client
   google-auth-httplib2
   google-auth-oauthlib
   ```

2. Ensure `auth_setup.py` contains the following code:

   ```python
   import os.path
   from google.auth.transport.requests import Request
   from google.oauth2.credentials import Credentials
   from google_auth_oauthlib.flow import InstalledAppFlow

   SCOPES = ["https://www.googleapis.com/auth/calendar"]

   def main():
       creds = None
       if os.path.exists("token.json"):
           creds = Credentials.from_authorized_user_file("token.json", SCOPES)
       if not creds or not creds.valid:
           if creds and creds.expired and creds.refresh_token:
               creds.refresh(Request())
           else:
               flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
               creds = flow.run_local_server(port=0)
           with open("token.json", "w") as token:
               token.write(creds.to_json())
       print("Success! token.json has been generated.")

   if __name__ == "__main__":
       main()
   ```

3. Install the dependencies and run the authentication script from your terminal inside the project folder:

   ```bash
   pip install -r requirements.txt
   python auth_setup.py
   ```

4. A browser window will open automatically. Sign in with your Google account (if you see a *"Google hasn't verified this app"* warning, click **Advanced** -> **Go to ... (unsafe)** -> **Allow**).
5. Once the terminal prints `Success! token.json has been generated.`, open **`token.json`** with Notepad or VS Code and copy (`Ctrl+A` then `Ctrl+C`) its entire contents.

> **Security Warning:** Never upload `credentials.json` or `token.json` to a public GitHub repository. Make sure both filenames are listed in your `.gitignore` file.

### Step 4: Add GitHub Secrets

1. Open your forked repository on GitHub.
2. Go to the **Settings** tab -> in the left sidebar, select **Secrets and variables** -> click **Actions**.
3. Click **New repository secret** and create the following 5 secrets with the exact names and values below:

| Name | Secret Value |
| :--- | :--- |
| `SIAKAD_URL` | `https://akademik.ithb.ac.id` |
| `SIAKAD_NIM` | Your student ID / NIM (e.g., `1123001`) |
| `SIAKAD_PASS` | Your SIAKAD account password |
| `GOOGLE_TOKEN_JSON` | Paste the entire JSON text copied from `token.json` |
| `GOOGLE_CALENDAR_ID` | `primary` *(or a specific Calendar ID if using a separate calendar)* |

*(Optional)* If you want your class schedule in a dedicated calendar separate from your personal events:
* Open Google Calendar in your browser -> create a new calendar (e.g., `ITHB Classes`) -> open **Settings and sharing** for that calendar -> scroll down to **Integrate calendar** -> copy the **Calendar ID** (formatted like `abc123...@group.calendar.google.com`) and use it as your `GOOGLE_CALENDAR_ID`.

### Step 5: Enable GitHub Actions

1. Open the **Actions** tab in your GitHub repository.
2. If you see a green button saying **I understand my workflows, go ahead and enable them**, click it to activate workflows on your fork.
3. In the left sidebar, click **Sync Jadwal ITHB to Google Calendar**.
4. Click the **Run workflow** dropdown on the right -> click the green **Run workflow** button to trigger your first manual sync.
5. Wait about 30–60 seconds until a green checkmark (`✓`) appears.
6. Open [Google Calendar](https://calendar.google.com/) and refresh the page—all your classes and exam schedules will now appear and stay automatically synced every 30 minutes.

## Troubleshooting

* **`invalid_scope: Bad Request`:** Delete your local `token.json`, verify that both `auth_setup.py` and `sync_ithb.py` use `https://www.googleapis.com/auth/calendar`, re-run `python auth_setup.py`, and update your `GOOGLE_TOKEN_JSON` secret.
* **`Token has been expired or revoked`:** Your OAuth app in Google Cloud Console is still set to *Testing*, which expires tokens after 7 days. Change its status to **In Production** (Publish App), regenerate `token.json` using `python auth_setup.py`, and update `GOOGLE_TOKEN_JSON`.
* **Workflow stops running every 30 minutes:** GitHub automatically disables scheduled `cron` workflows if a repository has no commit activity for 60 days. Simply visit the **Actions** tab and click **Enable workflow** to resume.
