# BigChange Scheduler — Deployment Guide

## What's in this folder

```
bigchange-app/
├── app.py              ← Python backend (talks to BigChange API)
├── requirements.txt    ← Python packages needed
├── Procfile            ← Tells Render how to start the app
└── static/
    └── index.html      ← The scheduler dashboard your team uses
```

---

## Step-by-step: Deploy to Render (free hosting)

### Step 1 — Put the code on GitHub

1. Go to **github.com** and sign in
2. Click the **+** button (top right) → **New repository**
3. Name it `bigchange-scheduler`
4. Leave everything else as default → click **Create repository**
5. On the next page, click **uploading an existing file**
6. Drag and drop ALL the files from this folder into the upload area:
   - `app.py`
   - `requirements.txt`
   - `Procfile`
   - The `static` folder (containing `index.html`)
7. Click **Commit changes**

---

### Step 2 — Deploy on Render

1. Go to **render.com** and sign up (free) — use your GitHub account to sign in
2. Click **New +** → **Web Service**
3. Click **Connect a repository** → select `bigchange-scheduler`
4. Fill in the settings:
   - **Name**: bigchange-scheduler (or anything you like)
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
   - **Instance Type**: Free
5. Click **Advanced** → **Add Environment Variable** — add these three:

   | Key | Value |
   |-----|-------|
   | `BIGCHANGE_CLIENT_ID` | your Client ID from BigChange Developer Portal |
   | `BIGCHANGE_CLIENT_SECRET` | your Client Secret from BigChange Developer Portal |
   | `BIGCHANGE_CUSTOMER_ID` | 1564 |

6. Click **Create Web Service**
7. Wait 2-3 minutes while it builds
8. Render will give you a URL like `https://bigchange-scheduler.onrender.com`

---

### Step 3 — Share with your team

Send that URL to your 4 schedulers (it opens the staff hub; the scheduler itself is at `/scheduler`, annual leave at `/leave`. To add another tool to the hub, add one line to the `APPS` list in `static/home.html`). They open it in any browser — no login, no install.

The dashboard:
- Auto-refreshes every 60 seconds
- Shows all unassigned jobs sorted by SLA urgency
- Suggests the best engineer for each job
- Assigns directly into BigChange with one click

---

## Troubleshooting

**"Error loading jobs"** — The API endpoint paths may need adjusting for your BigChange account.
Share the error with your developer (or Claude) and it can be fixed quickly.

**Render goes to sleep after 15 mins (free tier)** — The first load after inactivity takes ~30 seconds to wake up.
To avoid this, upgrade to Render's $7/month plan or use a free uptime monitor like uptimerobot.com to ping it every 10 minutes.

---

## Need help?

Contact your developer or go back to Claude and share any error messages.

---

## Annual leave booking (`/leave`)

Runs inside this same app. Staff go to `/leave`, pick their name, enter their PIN and request leave; the request goes to that employee's manager (set in the admin Staff tab; mark supervisors as managers first). Anyone without a manager goes to the office, and the office can approve anything from the admin tab. The scheduler topbar shows an "N on leave" badge and an "Annual leave" link.

**Extra environment variables on Render**

| Key | Value |
|-----|-------|
| `DATABASE_URL` | Postgres connection string (Render Postgres or Supabase). Required on Render - the free disk is wiped on each deploy, so SQLite would lose bookings. |
| `LEAVE_ADMIN_PASSWORD` | Office admin password (admin sign-in is disabled if unset) |
| `LEAVE_SECRET` | Any long random string (signs login sessions) |
| `LEAVE_DEFAULT_ALLOWANCE` | Optional, default 28 days (includes bank holidays) |
| `LEAVE_YEAR_START_MONTH` | Optional, 1-12, default 1 (January) |

Rules: Mon-Fri only, bank holidays (editable in admin) are booked automatically for all staff and deducted from the allowance, half days supported, requests can't be made in the past (office can book on someone's behalf), clashes with colleagues are flagged but don't block.

API for other tools: `GET /api/leave/off?date=YYYY-MM-DD` (or `from`/`to`) returns approved leave with each person's `resourceId` (set the BigChange resource ID per employee in admin).

### BigChange absence sync

When leave is approved, the app creates an absence in BigChange (`POST /v1/absences`) for that person's BigChange resource, and removes it if the leave is later cancelled. Switched off unless `BIGCHANGE_ABSENCE_TYPE_ID` is set.

| Key | Value |
|-----|-------|
| `BIGCHANGE_ABSENCE_TYPE_ID` | BigChange absence type ID used for annual leave |
| `BIGCHANGE_ABSENCE_TYPE_ID_OTHER` | Optional: absence type ID for unpaid/other leave (those are not synced if unset) |
| `BIGCHANGE_ABSENCE_TYPE_ID_SICK` | Optional: absence type ID for sick days logged by managers (not synced if unset) |
| `LEAVE_SICK_ALLOWANCE` | Default sick days per person per year (default 7; also editable on the admin Staff tab) |

The BigChange API key needs the `resources:write` scope. Each person needs their BigChange resource ID set in the admin Staff tab. Sync problems show on the request in "All requests", with a Retry button.

### Email notifications

Staff get an email when leave is approved, rejected, cancelled or booked for them; managers get one when a request comes in. Uses [Resend](https://resend.com) (free tier is plenty) because Render's free tier blocks normal SMTP. Off unless both are set:

| Key | Value |
|-----|-------|
| `RESEND_API_KEY` | API key from resend.com |
| `LEAVE_EMAIL_FROM` | e.g. `R&S Leave <leave@randsservice.co.uk>` - the domain must be verified in Resend (add the DNS records it gives you) |
| `LEAVE_OFFICE_EMAIL` | Optional: where requests from people with no manager are sent |
| `LEAVE_EMAIL_REPLY_TO` | Optional |

Emails are stored per person (import column `Email`, or the Email button in the Staff tab). Use the "Send test email" button in the Staff tab to check it works.

## Van parts finder

Served at `/parts` (static files in `static/parts`, `parts.py`). The stock list is stored in the leave database; upload a new Excel file from Staff -> "Van parts stock list". Until one is uploaded the bundled `static/parts/stock.json` is used.
