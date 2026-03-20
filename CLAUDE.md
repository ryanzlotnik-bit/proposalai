# CloseTheJob — CLAUDE.md

## Project overview
Flask SaaS for trade contractors. Full business platform: AI proposals, job scheduling, invoicing, financials, client portal.

**Run locally:** `cd /Users/ryanzlotnik/ProposalAI && python3 app.py` (port 5001)
**Deploy:** `git add -A && git commit -m "..." && git push` — Railway auto-deploys on push to main

---

## Tech stack
- Flask + SQLAlchemy (SQLite local, PostgreSQL on Railway via `DATABASE_URL`)
- Flask-Login, Flask-Bcrypt (auth)
- Flask-Mail + Gmail SMTP (emails)
- fpdf2 (PDF invoice generation)
- imaplib (Gmail Drafts via IMAP)
- Anthropic claude-sonnet-4-6 (proposal generation)
- Stripe (subscriptions)
- Chart.js CDN (financial charts)
- Tailwind CSS CDN + custom CSS variables
- Fonts: Bebas Neue (display), DM Sans (body), JetBrains Mono (labels/code)

---

## Design system
Dark theme. Key CSS variables (defined in base.html):
- `--bg: #0C0C0C` / `--surface: #141414` / `--surface-2: #1E1E1E`
- `--orange: #F97316` (primary accent — buttons, highlights, borders)
- `--text: #F0EDE8` / `--text-muted: #6B6860`
- `--green: #4ADE80` / `--red: #F87171` / `--blue: #60A5FA`
- Clip-path on buttons/icons: `polygon(0 0, 90% 0, 100% 10%, 100% 100%, 10% 100%, 0 90%)`
- Dot grid background: `radial-gradient(circle, #252525 1px, transparent 1px) 28px 28px`

---

## Critical patterns

### ⚠️ Threading + SQLAlchemy (DO NOT break this rule)
Never pass ORM objects into background threads. Pass only IDs, re-query inside the thread.

```python
# WRONG — causes DetachedInstanceError or stale data
def send_email(job, user):
    def do_send():
        mail.send(...)  # job/user are detached from session
    threading.Thread(target=do_send).start()

# CORRECT
def send_email(job, user):
    job_id = job.id
    user_id = user.id
    def do_send():
        with app.app_context():
            j = Job.query.get(job_id)
            u = User.query.get(user_id)
            # now safe to use j and u
    threading.Thread(target=do_send, daemon=True).start()
```

### Email sending
All emails sent in background threads (never block request). Set `invoice_sent = True` INSIDE the thread AFTER `mail.send()` succeeds — not before, not in the route handler.

### Public tokens
Use `secrets.token_urlsafe(20)` for share links. Generate lazily on first view if missing:
```python
if not proposal.public_token:
    proposal.public_token = secrets.token_urlsafe(20)
    db.session.commit()
```

### Loading overlays
For any action that takes >2s (Claude API calls, etc.), show a full-screen overlay on form submit. Never let the screen appear frozen.

---

## Environment variables
Set locally in `.env`, set in Railway Variables tab for production. Never commit `.env`.

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API for proposal generation |
| `SECRET_KEY` | Flask session secret |
| `DATABASE_URL` | PostgreSQL URI (Railway sets automatically) |
| `MAIL_USERNAME` | Gmail address for sending emails |
| `MAIL_PASSWORD` | Gmail App Password (not account password) |
| `STRIPE_SECRET_KEY` | Stripe secret key |
| `STRIPE_PUBLISHABLE_KEY` | Stripe publishable key |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret |
| `STRIPE_STARTER_PRICE_ID` | Stripe price ID for $49/mo plan |
| `STRIPE_PRO_PRICE_ID` | Stripe price ID for $99/mo plan |

**Gmail setup:** Requires a Gmail App Password (Google Account → Security → 2-Step Verification → App passwords). IMAP must be enabled in Gmail settings for draft saving.

---

## Database models
- `User` — contractor account (name, email, company_name, trade_type, specialty, phone, license_number, address, subscription_status, onboarding_done)
- `Proposal` — AI-generated proposal (client info, job info, generated_content JSON, grand_total, status, public_token)
- `Job` — closed/completed job for financials (revenue, client info, invoice tracking)
- `Expense` — expense records for P&L
- `Tip` — tip records
- `CostTemplate` — saved cost line items for proposal generation (name, amount, is_per_unit, unit_label)
- `ScheduledJob` — future jobs on the schedule calendar (date, auto_invoice flag)

---

## Key routes
| Route | Notes |
|---|---|
| `/` | Landing page |
| `/dashboard` | Proposal list + calls `auto_invoice_past_scheduled_jobs()` |
| `/generate` | AI proposal form |
| `/proposal/<id>` | View proposal (contractor, login required) |
| `/p/<token>` | Public client-facing proposal view (no login) |
| `/p/<token>/respond` | Client accept/decline POST |
| `/financials` | Revenue/expenses/tips dashboard + charts |
| `/schedule` | Calendar + job scheduling |
| `/onboarding` | Post-signup specialty picker |
| `/test-email` | Email diagnostic — sends synchronously and shows error |

---

## Features shipped
- [x] AI proposal generation (Claude API)
- [x] PDF invoice generation (fpdf2)
- [x] Invoice email with PDF attachment + Gmail draft copy
- [x] Financials (closed jobs, expenses, tips, Chart.js graphs)
- [x] Job scheduling with interactive monthly calendar
- [x] Auto-invoicing when scheduled job date passes
- [x] Specialty onboarding (filters proposal form by trade)
- [x] Saved cost templates (fixed + per-unit)
- [x] Client-facing proposal link with accept/decline
- [x] Full-screen loading overlay on proposal generation
- [x] Close job from proposal (fast path to log revenue)
- [x] Stripe subscription billing

## Known issues / next up
- Email delivery to clients still unconfirmed working in production (check Railway MAIL_USERNAME / MAIL_PASSWORD env vars)
- Mobile responsiveness needs improvement (contractors use phones in the field)
- Client CRM (saved contact list) not yet built
- Follow-up reminders not yet built
- Google Calendar sync not yet built
