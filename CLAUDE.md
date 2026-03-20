# CloseTheJob — CLAUDE.md

## Project overview
Flask SaaS for trade contractors. Full business platform: AI proposals, job scheduling, invoicing, financials, client CRM, Google Calendar sync.

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
- google-auth-oauthlib + google-api-python-client (Google Calendar)
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

This applies to ALL background work: emails, Google Calendar pushes, invoice generation. Every async helper in this codebase follows this pattern.

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

### Database migrations (no Flask-Migrate)
We don't use Alembic. New columns are added via raw SQL in the `with app.app_context()` block at the bottom of app.py. Each statement is wrapped in try/except so it silently passes on fresh installs (where `db.create_all()` already created the column) and on existing deployments (where it adds the missing column).

```python
with app.app_context():
    db.create_all()
    from sqlalchemy import text
    for _sql in [
        'ALTER TABLE "user" ADD COLUMN new_field TEXT',
        'ALTER TABLE proposal ADD COLUMN client_id INTEGER',
    ]:
        try:
            db.session.execute(text(_sql))
            db.session.commit()
        except Exception:
            db.session.rollback()
```

Always add new columns here when adding them to models — Railway's PostgreSQL won't pick them up otherwise.

### Client auto-upsert
When any form creates a new proposal or scheduled job with client info, always upsert the CRM automatically. Never require the user to manually save a contact. Match by email first, then name (case-insensitive), then create new:

```python
existing = Client.query.filter_by(user_id=uid, email=email).first()
if not existing:
    existing = Client.query.filter(
        Client.user_id == uid,
        db.func.lower(Client.name) == name.lower()
    ).first()
if existing:
    linked_client_id = existing.id
    # backfill any missing info
else:
    new_client = Client(user_id=uid, name=name, email=email, ...)
    db.session.add(new_client)
    db.session.flush()
    linked_client_id = new_client.id
```

### Google Calendar OAuth
Tokens stored on `User.google_access_token` / `google_refresh_token` / `google_token_expiry`. All calendar operations run in background threads (same threading rule). The `push_to_google_calendar(user_id, sj_id)` and `delete_from_google_calendar(user_id, event_id)` helpers handle everything. If `GOOGLE_CLIENT_ID` is not set, these are no-ops. The OAuth flow is at `/auth/google` → `/auth/google/callback`.

### Cascading automation chain
The full flow when a client accepts a proposal:
1. `public_proposal_respond` → sets `proposal.status = 'accepted'`
2. Creates a `ScheduledJob` using `parse_timeline_date(proposal.timeline)` for the date
3. Calls `push_to_google_calendar(user_id, sj_id)` in background
4. When that date passes, `auto_invoice_past_scheduled_jobs()` fires → creates `Job` → calls `send_invoice_email()` (if user.can_auto_invoice)

### Feature gating philosophy
Gate automations, not core features. Free users should be able to use the product and see its value — they hit upgrade walls when they want to save time, not when they try to do their job.

| Feature | Free | Pro |
|---|---|---|
| AI proposals | 3 to try | Unlimited |
| Scheduling, CRM, financials | ✓ | ✓ |
| Auto-invoicing | ✗ | ✓ |
| Financial charts | ✗ | ✓ |
| Google Calendar sync | ✗ | ✓ |

Check `user.can_auto_invoice` before sending auto-invoices. Check `can_see_charts` before rendering Chart.js. Don't gate CRM or scheduling — those sell the platform.

Plan DB values: `trial` (free), `starter` (Pro $49), `pro` (Business $99). Use `user.plan_display` for human-readable names.

### Dashboard is a command center, not a feature page
The dashboard pulls data from all modules and shows the user the state of their whole business. It should answer: "What jobs do I have today? What's my revenue this month? What proposals are pending?" — not just list one feature's data. Pass `revenue_mtd`, `upcoming_jobs`, `today_jobs`, `clients_count`, `pipeline_value` etc. from the route.

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
| `STRIPE_STARTER_PRICE_ID` | Stripe price ID for $49/mo plan (Pro) |
| `STRIPE_PRO_PRICE_ID` | Stripe price ID for $99/mo plan (Business) |
| `GOOGLE_CLIENT_ID` | Google OAuth2 client ID for Calendar sync |
| `GOOGLE_CLIENT_SECRET` | Google OAuth2 client secret |

**Gmail setup:** Requires a Gmail App Password (Google Account → Security → 2-Step Verification → App passwords). IMAP must be enabled in Gmail settings for draft saving.

**Google Calendar setup:** Create OAuth2 credentials in Google Cloud Console. Add authorized redirect URI: `https://your-domain.railway.app/auth/google/callback`. Locally: `http://localhost:5001/auth/google/callback`.

---

## Database models
- `User` — contractor account (+ google_access_token, google_refresh_token, google_token_expiry, plan_display property, can_auto_invoice, can_see_charts)
- `Proposal` — AI-generated proposal (client info, job info, generated_content JSON, grand_total, status, public_token, client_id FK)
- `Job` — closed/completed job for financials (revenue, client info, invoice tracking, client_id FK)
- `Expense` — expense records for P&L
- `Tip` — tip records
- `CostTemplate` — saved cost line items (name, amount, cost_type, unit)
- `ScheduledJob` — future jobs on the schedule calendar (date, auto_invoice flag, client_id FK, google_event_id)
- `Client` — CRM contacts (name, email, phone, address, notes; linked to Proposal/Job/ScheduledJob via client_id)

---

## Key routes
| Route | Notes |
|---|---|
| `/` | Landing page |
| `/dashboard` | Business command center — cross-module stats + today's jobs |
| `/generate` | AI proposal form (auto-upserts CRM on submit) |
| `/proposal/<id>` | View proposal (contractor, login required) |
| `/p/<token>` | Public client-facing proposal view (no login) |
| `/p/<token>/respond` | Client accept/decline → auto-creates ScheduledJob + Google Calendar event |
| `/financials` | Revenue/expenses/tips + charts (charts gated on Pro) |
| `/schedule` | Calendar + job scheduling (auto-invoice gated on Pro) |
| `/clients` | CRM contact list |
| `/clients/<id>` | Client detail — full history of proposals, jobs, scheduled jobs |
| `/auth/google` | Start Google Calendar OAuth flow |
| `/auth/google/callback` | OAuth callback — stores tokens on User |
| `/onboarding` | Post-signup specialty picker |
| `/pricing` | Pricing page (Free / Pro $49 / Business $99) |
| `/test-email` | Email diagnostic — sends synchronously and shows error |

---

## Features shipped
- [x] AI proposal generation (Claude API)
- [x] PDF invoice generation (fpdf2)
- [x] Invoice email with PDF attachment + Gmail draft copy
- [x] Financials (closed jobs, expenses, tips, Chart.js graphs — Pro only)
- [x] Job scheduling with interactive monthly calendar
- [x] Auto-invoicing when scheduled job date passes (Pro only)
- [x] Specialty onboarding (filters proposal form by trade)
- [x] Saved cost templates (fixed + per-unit)
- [x] Client-facing proposal link with accept/decline
- [x] Full-screen loading overlay on proposal generation
- [x] Close job from proposal (fast path to log revenue)
- [x] Stripe subscription billing
- [x] Client CRM (auto-saves from every proposal, linked across all modules)
- [x] Google Calendar sync (OAuth2, auto-push on schedule, delete on cancel)
- [x] Proposal acceptance → auto-creates ScheduledJob + Google Calendar event
- [x] Dashboard as business command center (cross-module stats)
- [x] Feature-gated pricing (automations gated, core features free)

## Known issues / next up
- Email delivery to clients still unconfirmed working in production (check Railway MAIL_USERNAME / MAIL_PASSWORD env vars)
- Mobile responsiveness needs improvement (contractors use phones in the field)
- Follow-up reminders not yet built
- parse_timeline_date is best-effort — contractor should be able to edit the auto-scheduled date
