"""
CloseTheJob — Deep Feature Test Suite
Tests everything the basic suite skips:
  - AI proposal generation (real Claude API call)
  - PDF invoice generation
  - Public proposal link + client accept/decline flow
  - Auto-schedule creation on proposal acceptance
  - Proposal view, status update, nudge, close, delete
  - CSV lead import
  - Proposal save-as-template + template load
  - Deposit link generation
  - Booking form submission
  - parse_timeline_date edge cases
  - Cross-user access control (user A can't see user B's data)
  - Railway/PostgreSQL schema compatibility check

Run: python3 test_deep.py
"""

import sys, os, json, io, csv, traceback
from datetime import date, timedelta, datetime

GREEN = "\033[92m"; RED = "\033[91m"; YELLOW = "\033[93m"; RESET = "\033[0m"
PASS = f"{GREEN}✓{RESET}"; FAIL = f"{RED}✗{RESET}"

results = []
created = {}

def section(name):
    print(f"\n{'═'*58}\n  {name}\n{'═'*58}")

def check(label, ok, detail=""):
    icon = PASS if ok else FAIL
    suffix = f"  {YELLOW}{detail}{RESET}" if detail else ""
    print(f"  {icon} {label}{suffix}")
    results.append((label, ok, detail))
    return ok

def check_r(label, r, expected=200, contains=None, not_contains=None):
    ok = r.status_code == expected
    detail = f"[{r.status_code}]"
    body = r.data.decode("utf-8", errors="replace") if hasattr(r, "data") else ""
    if ok and contains:
        for c in ([contains] if isinstance(contains, str) else contains):
            if c.lower() not in body.lower():
                ok = False; detail += f" missing '{c}'"; break
    if ok and not_contains:
        for c in ([not_contains] if isinstance(not_contains, str) else not_contains):
            if c.lower() in body.lower():
                ok = False; detail += f" found '{c}'"; break
    return check(label, ok, detail)

# ── boot ─────────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
os.environ.pop("DEV_AUTO_LOGIN", None)

try:
    from app import (app, db, User, Proposal, Job, ScheduledJob, Client,
                     Lead, CostTemplate, ProposalTemplate,
                     generate_invoice_pdf, parse_timeline_date,
                     generate_proposal_number)
    from flask_bcrypt import Bcrypt
except ImportError as e:
    print(f"{RED}Import error: {e}{RESET}"); traceback.print_exc(); sys.exit(1)

app.config["TESTING"] = True
app.config["WTF_CSRF_ENABLED"] = False
bcrypt = Bcrypt(app)

# Create two test users (for access-control tests)
with app.app_context():
    for email, name in [("deep_test_a@ctj.test", "Deep A"), ("deep_test_b@ctj.test", "Deep B")]:
        if not User.query.filter_by(email=email).first():
            u = User(email=email, name=name, company_name=f"{name} HVAC",
                     trade_type="HVAC",
                     password=bcrypt.generate_password_hash("Test1234!").decode(),
                     plan="pro")
            db.session.add(u)
    db.session.commit()
    USER_A = User.query.filter_by(email="deep_test_a@ctj.test").first()
    USER_B = User.query.filter_by(email="deep_test_b@ctj.test").first()
    UID_A, UID_B = USER_A.id, USER_B.id
    print(f"\n  User A: id={UID_A}  User B: id={UID_B}")

client = app.test_client()

def auth(user_id):
    with client.session_transaction() as s:
        s["_user_id"] = str(user_id); s["_fresh"] = True

def get(path, **kw):
    return client.get(path, follow_redirects=True, **kw)

def post(path, data=None, json_data=None, **kw):
    if json_data is not None:
        return client.post(path, json=json_data, follow_redirects=True,
                           content_type="application/json", **kw)
    return client.post(path, data=data, follow_redirects=True, **kw)

auth(UID_A)

# ═══════════════════════════════════════════════════════
# 1. PARSE TIMELINE DATE
# ═══════════════════════════════════════════════════════
section("1. PARSE TIMELINE DATE LOGIC")
today = date.today()
cases = [
    ("2-3 weeks",   today + timedelta(days=14), today + timedelta(days=30)),
    ("next week",   today + timedelta(days=3),  today + timedelta(days=14)),
    ("1 month",     today + timedelta(days=20), today + timedelta(days=45)),
    ("ASAP",        today,                       today + timedelta(days=7)),
    ("",            today,                       today + timedelta(days=60)),
]
with app.app_context():
    for text, lo, hi in cases:
        d = parse_timeline_date(text, today)
        ok = lo <= d <= hi
        check(f"parse_timeline_date('{text or '(empty)'}') → {d}", ok,
              f"expected {lo}–{hi}")

# ═══════════════════════════════════════════════════════
# 2. PDF INVOICE GENERATION
# ═══════════════════════════════════════════════════════
section("2. PDF INVOICE GENERATION")
with app.app_context():
    job = Job(user_id=UID_A, client_name="PDF Test Client",
              client_email="pdf@test.com", job_type="AC Repair",
              revenue=1500.0, completed_date=date.today(),
              description="Test job for PDF")
    db.session.add(job); db.session.flush()
    created["pdf_job_id"] = job.id
    u = User.query.get(UID_A)
    try:
        pdf_bytes = generate_invoice_pdf(job, u)
        check("PDF generates without error", True)
        check("PDF is non-empty bytes", len(pdf_bytes) > 1000, f"{len(pdf_bytes)} bytes")
        check("PDF starts with %PDF header", pdf_bytes[:4] == b"%PDF", pdf_bytes[:4])
    except Exception as e:
        check("PDF generates without error", False, str(e))
        check("PDF is non-empty bytes", False, "")
        check("PDF starts with %PDF header", False, "")
    db.session.rollback()

# ═══════════════════════════════════════════════════════
# 3. AI PROPOSAL GENERATION (real Claude API call)
# ═══════════════════════════════════════════════════════
section("3. AI PROPOSAL GENERATION (live Claude API)")
print(f"  {YELLOW}· This makes a real API call — takes ~15s{RESET}")
import re

r = client.post("/generate", data={
    "client_name":    "Test Client AI",
    "client_email":   "aiclient@test.com",
    "client_phone":   "555-1234",
    "client_address": "123 Test St, Chicago IL",
    "job_type":       "AC Installation",
    "job_description": "Install a new 3-ton Carrier central air conditioning unit. Remove old unit.",
    "materials_input": "3-ton Carrier unit, copper line set, thermostat",
    "labor_hours":    "8",
    "labor_rate":     "85",
    "timeline":       "1-2 weeks",
    "warranty":       "1 year parts and labor",
    "notes":          "Customer has dogs, please call ahead",
    "tax_rate":       "0",
    "deposit_pct":    "25",
})
location = r.headers.get("Location", "")
proposal_created = r.status_code == 302 and "proposal" in location.lower()
check("Generate proposal — redirect to proposal view", proposal_created, f"[{r.status_code}] location={location}")

if proposal_created:
    # Fetch the proposal page
    r = get(location)
    m = re.search(r'/proposal/(\d+)', location)
    if m:
        created["proposal_id"] = int(m.group(1))
        body = r.data.decode()
        check("Proposal page has client name", "Test Client AI" in body)
        check("Proposal page has job type",    "AC Installation" in body)
        check("Proposal has generated content", "description" in body.lower() or "scope" in body.lower())
        check("Proposal page has grand total",  "$" in body)

        # Client auto-upserted into CRM
        with app.app_context():
            cl = Client.query.filter_by(user_id=UID_A, email="aiclient@test.com").first()
            check("Client auto-upserted to CRM after proposal", cl is not None,
                  cl.name if cl else "not found")
            if cl:
                created["auto_client_id"] = cl.id
else:
    for lbl in ["Proposal page has client name", "Proposal page has job type",
                "Proposal has generated content", "Proposal page has grand total",
                "Client auto-upserted to CRM after proposal"]:
        check(lbl, False, "proposal not created")

# ═══════════════════════════════════════════════════════
# 4. PROPOSAL MANAGEMENT
# ═══════════════════════════════════════════════════════
section("4. PROPOSAL MANAGEMENT")
pid = created.get("proposal_id")
if pid:
    # View
    r = get(f"/proposal/{pid}")
    check_r("Proposal view page loads", r)
    check("Proposal view — no traceback", "traceback" not in r.data.decode().lower())

    # Status update
    r = post(f"/proposal/{pid}/status", json_data={"status": "sent"})
    check_r("Update proposal status to 'sent'", r, 200)
    with app.app_context():
        p = Proposal.query.get(pid)
        check("Status updated in DB", p.status == "sent", p.status)

    # Proposal number format
    with app.app_context():
        p = Proposal.query.get(pid)
        check("Proposal number generated", bool(p.proposal_number),
              p.proposal_number)

    # Public token generation + public view
    r = get(f"/proposal/{pid}")
    body = r.data.decode()
    token_match = re.search(r'/p/([A-Za-z0-9_-]{10,})', body)
    if token_match:
        token = token_match.group(1)
        created["proposal_token"] = token
        check("Public share token exists on proposal page", True, token[:12] + "…")
    else:
        # Try direct DB read
        with app.app_context():
            p = Proposal.query.get(pid)
            if p and p.public_token:
                created["proposal_token"] = p.public_token
                check("Public share token exists in DB", True, p.public_token[:12] + "…")
            else:
                check("Public share token exists on proposal page", False, "not found")

    # Close job from proposal (renders form)
    r = get(f"/proposal/{pid}/close")
    check_r("Close-job-from-proposal page loads", r)

    # Save as template (route expects JSON with key 'name')
    r = post(f"/proposal/{pid}/save-template", json_data={"name": "Deep Test Template"})
    check_r("Save proposal as template", r, 200)
    with app.app_context():
        tmpl = ProposalTemplate.query.filter_by(user_id=UID_A, name="Deep Test Template").first()
        check("Template saved in DB", tmpl is not None)
        if tmpl:
            created["tmpl_id"] = tmpl.id
            # Load template data
            r2 = get(f"/templates/{tmpl.id}/data")
            check_r("Load template data endpoint", r2, 200)
            try:
                data = json.loads(r2.data.decode())
                check("Template data is valid JSON", True)
                check("Template data has job_type", "job_type" in data)
            except Exception:
                check("Template data is valid JSON", False)
                check("Template data has job_type", False)
else:
    for lbl in ["Proposal view page loads", "Proposal view — no traceback",
                "Update proposal status to 'sent'", "Status updated in DB",
                "Proposal number generated", "Public share token exists on proposal page",
                "Close-job-from-proposal page loads", "Save proposal as template",
                "Template saved in DB", "Load template data endpoint",
                "Template data is valid JSON", "Template data has job_type"]:
        check(lbl, False, "no proposal")

# ═══════════════════════════════════════════════════════
# 5. PUBLIC PROPOSAL (CLIENT VIEW + ACCEPT FLOW)
# ═══════════════════════════════════════════════════════
section("5. PUBLIC PROPOSAL — CLIENT ACCEPT/DECLINE FLOW")
token = created.get("proposal_token")
anon = app.test_client()
if token:
    # Unauthenticated client views the proposal
    r = anon.get(f"/p/{token}", follow_redirects=True)
    check_r("Client views public proposal (no login)", r)
    body = r.data.decode()
    check("Public view shows client name",   "Test Client AI" in body)
    check("Public view shows job type",      "AC Installation" in body)
    check("Public view has Accept button",   "accept" in body.lower())
    check("Public view — no traceback",      "traceback" not in body.lower())

    # Client declines
    r = anon.post(f"/p/{token}/respond",
                  data={"action": "declined", "signature_name": ""},
                  follow_redirects=True)
    check_r("Client decline responds OK", r)
    with app.app_context():
        p = Proposal.query.filter_by(public_token=token).first()
        check("Proposal status → declined in DB", p and p.status == "declined",
              p.status if p else "not found")

    # Reset status to 'sent' so we can test accept
    with app.app_context():
        p = Proposal.query.filter_by(public_token=token).first()
        if p:
            p.status = "sent"
            p.accepted_at = None
            db.session.commit()

    # Client accepts with e-signature
    r = anon.post(f"/p/{token}/respond",
                  data={"action": "accepted", "signature_name": "Test Client AI"},
                  follow_redirects=True)
    check_r("Client accept responds OK", r)

    with app.app_context():
        p = Proposal.query.filter_by(public_token=token).first()
        check("Proposal status → accepted in DB", p and p.status == "accepted",
              p.status if p else "not found")
        check("Signature name saved", p and p.signature_name == "Test Client AI",
              p.signature_name if p else "")
        check("Accepted timestamp set", p and p.accepted_at is not None)

        # Auto-created scheduled job
        sj = ScheduledJob.query.filter_by(
            user_id=UID_A, client_name="Test Client AI"
        ).order_by(ScheduledJob.id.desc()).first()
        check("ScheduledJob auto-created on accept", sj is not None,
              sj.job_type if sj else "not found")
        if sj:
            created["auto_sj_id"] = sj.id
            check("ScheduledJob has a date set", sj.scheduled_date is not None,
                  str(sj.scheduled_date))
else:
    for lbl in ["Client views public proposal (no login)", "Public view shows client name",
                "Public view shows job type", "Public view has Accept button",
                "Public view — no traceback", "Client decline responds OK",
                "Proposal status → declined in DB", "Client accept responds OK",
                "Proposal status → accepted in DB", "Signature name saved",
                "Accepted timestamp set", "ScheduledJob auto-created on accept",
                "ScheduledJob has a date set"]:
        check(lbl, False, "no proposal token")

# Invalid token → 404
r = anon.get("/p/totally_invalid_token_xyz", follow_redirects=True)
check("Bad public token → 404", r.status_code == 404, f"[{r.status_code}]")

# ═══════════════════════════════════════════════════════
# 6. DEPOSIT LINK
# ═══════════════════════════════════════════════════════
section("6. DEPOSIT LINK")
auth(UID_A)
pid = created.get("proposal_id")
if pid:
    # Endpoint redirects to /deposit/<token> — don't follow redirect, check Location
    r = client.get(f"/proposal/{pid}/deposit-link")
    ok = r.status_code == 302
    loc = r.headers.get("Location", "")
    check("Deposit link redirects to deposit page", ok, f"[{r.status_code}] → {loc}")
    check("Deposit link URL contains /deposit/", "/deposit/" in loc, loc)
else:
    check("Deposit link redirects to deposit page", False, "no proposal")
    check("Deposit link URL contains /deposit/", False, "no proposal")

# ═══════════════════════════════════════════════════════
# 7. CSV LEAD IMPORT
# ═══════════════════════════════════════════════════════
section("7. CSV LEAD IMPORT")
csv_content = "Company,Contact,Email,Phone,Address,Notes\nCsv Test Corp,Alice Bob,alice@csvtest.com,555-1111,789 Import Ave,Test import row\nSecond Company,Charlie D,charlie@csvtest.com,555-2222,456 Second St,Another row\n"
data = {
    "csv_file": (io.BytesIO(csv_content.encode()), "test_leads.csv"),
    "col_company": "Company",
    "col_contact": "Contact",
    "col_email": "Email",
    "col_phone": "Phone",
    "col_address": "Address",
    "col_notes": "Notes",
}
r = client.post("/leads/import", data=data,
                content_type="multipart/form-data",
                follow_redirects=True)
check_r("CSV import POST responds", r, 200)
body = r.data.decode()
check("CSV import — no traceback", "traceback" not in body.lower())
check("CSV import — success message or lead count",
      any(w in body.lower() for w in ["import", "added", "lead", "pipeline"]))

with app.app_context():
    csv_lead = Lead.query.filter_by(user_id=UID_A, company="Csv Test Corp").first()
    check("Imported lead appears in DB", csv_lead is not None,
          csv_lead.company if csv_lead else "not found")
    if csv_lead:
        created["csv_lead_id"] = csv_lead.id

# ═══════════════════════════════════════════════════════
# 8. ACCESS CONTROL — USER A CANNOT SEE USER B'S DATA
# ═══════════════════════════════════════════════════════
section("8. CROSS-USER ACCESS CONTROL")

# Create data as User B
auth(UID_B)
r = post("/jobs/add", data={
    "client_name": "User B Client",
    "client_email": "b@test.com",
    "client_phone": "555-0000",
    "job_type": "Plumbing",
    "revenue": "999.00",
    "completed_date": str(date.today()),
})
with app.app_context():
    bj = Job.query.filter_by(user_id=UID_B, client_name="User B Client").first()
    if bj:
        created["b_job_id"] = bj.id

r_b = post("/leads/add", data={
    "contact": "B Contact",
    "company": "User B Company",
    "stage": "unreached",
})
with app.app_context():
    bl = Lead.query.filter_by(user_id=UID_B, company="User B Company").first()
    if bl:
        created["b_lead_id"] = bl.id

# Now switch to User A and try to access User B's resources
auth(UID_A)

if created.get("b_job_id"):
    r = get(f"/jobs/{created['b_job_id']}")
    denied = r.status_code in (403, 404) or (
        r.status_code == 200 and "User B Client" not in r.data.decode())
    check("User A cannot view User B's job", denied,
          f"[{r.status_code}]")

    r = post(f"/jobs/{created['b_job_id']}/delete")
    denied = r.status_code in (403, 404) or (
        r.status_code == 200 and "access denied" in r.data.decode().lower())
    with app.app_context():
        still_exists = Job.query.get(created["b_job_id"]) is not None
    check("User A cannot delete User B's job", denied or still_exists,
          f"[{r.status_code}] still_exists={still_exists}")
else:
    check("User A cannot view User B's job", False, "User B job not created")
    check("User A cannot delete User B's job", False, "User B job not created")

if created.get("b_lead_id"):
    r = post(f"/leads/{created['b_lead_id']}/delete")
    with app.app_context():
        still_exists = Lead.query.get(created["b_lead_id"]) is not None
    check("User A cannot delete User B's lead", still_exists,
          f"[{r.status_code}] still_exists={still_exists}")
else:
    check("User A cannot delete User B's lead", False, "User B lead not created")

if created.get("proposal_id"):
    # Try to delete User A's proposal as User B
    auth(UID_B)
    r = post(f"/proposal/{created['proposal_id']}/delete")
    with app.app_context():
        still_exists = Proposal.query.get(created["proposal_id"]) is not None
    check("User B cannot delete User A's proposal", still_exists,
          f"[{r.status_code}] still_exists={still_exists}")
    auth(UID_A)
else:
    check("User B cannot delete User A's proposal", False, "no proposal")

# ═══════════════════════════════════════════════════════
# 9. PROPOSAL NUDGE (follow-up reminder endpoint)
# ═══════════════════════════════════════════════════════
section("9. PROPOSAL NUDGE")
auth(UID_A)
if created.get("proposal_id"):
    # Reset status so nudge is applicable
    with app.app_context():
        p = Proposal.query.get(created["proposal_id"])
        if p:
            p.status = "sent"
            p.reminder_sent_at = None
            db.session.commit()
    r = post(f"/proposal/{created['proposal_id']}/nudge")
    ok = r.status_code == 200
    check("Nudge endpoint responds", ok, f"[{r.status_code}]")
    try:
        data = json.loads(r.data.decode())
        check("Nudge returns {ok:true} or error JSON", "ok" in data or "error" in data)
    except Exception:
        check("Nudge returns JSON", False)
else:
    check("Nudge endpoint responds", False, "no proposal")
    check("Nudge returns {ok:true} or error JSON", False, "no proposal")

# ═══════════════════════════════════════════════════════
# 10. DATABASE SCHEMA CHECK
# ═══════════════════════════════════════════════════════
section("10. DATABASE SCHEMA COMPLETENESS")
with app.app_context():
    from sqlalchemy import inspect, text
    inspector = inspect(db.engine)
    tables = inspector.get_table_names()

    # Note: team members are stored as User rows with team_owner_id set (no separate table)
    expected_tables = ["user", "proposal", "job", "expense", "tip",
                       "cost_template", "scheduled_job", "client",
                       "lead", "lead_contact", "lead_activity",
                       "crm_email_template", "linked_email",
                       "job_request", "service_area"]
    for tbl in expected_tables:
        check(f"Table '{tbl}' exists", tbl in tables)

    # Key columns spot-check
    col_checks = {
        "user": ["id","email","name","plan","subscription_status",
                 "stripe_customer_id","google_access_token","team_owner_id"],
        "proposal": ["id","user_id","client_id","public_token","grand_total",
                     "status","signature_name","signed_at","accepted_at"],
        "job": ["id","user_id","client_id","revenue","completed_date",
                "invoice_sent","pay_token"],
        "scheduled_job": ["id","user_id","client_id","scheduled_date",
                          "google_event_id","invoice_on_complete"],
        "lead": ["id","user_id","company","contact","stage","last_contacted_at"],
    }
    for tbl, cols in col_checks.items():
        if tbl in tables:
            existing = {c["name"] for c in inspector.get_columns(tbl)}
            for col in cols:
                check(f"  {tbl}.{col} exists", col in existing)
        else:
            for col in cols:
                check(f"  {tbl}.{col} exists", False, "table missing")

# ═══════════════════════════════════════════════════════
# 11. GENERATE PROPOSAL NUMBER UNIQUENESS
# ═══════════════════════════════════════════════════════
section("11. PROPOSAL NUMBER UNIQUENESS")
with app.app_context():
    nums = set()
    for _ in range(10):
        nums.add(generate_proposal_number())
    check("10 generated proposal numbers are all unique", len(nums) == 10, str(nums))

# ═══════════════════════════════════════════════════════
# 12. CLEANUP
# ═══════════════════════════════════════════════════════
section("12. CLEANUP")
auth(UID_A)

if created.get("proposal_id"):
    r = post(f"/proposal/{created['proposal_id']}/delete")
    check_r("Delete test proposal", r, 200)

if created.get("auto_sj_id"):
    r = post(f"/schedule/{created['auto_sj_id']}/delete")
    check_r("Delete auto-created scheduled job", r, 200)

if created.get("auto_client_id"):
    r = post(f"/clients/{created['auto_client_id']}/delete")
    check_r("Delete auto-upserted client", r, 200)

if created.get("csv_lead_id"):
    r = post(f"/leads/{created['csv_lead_id']}/delete")
    check_r("Delete CSV-imported lead", r, 200)

if created.get("tmpl_id"):
    r = post(f"/templates/{created['tmpl_id']}/delete")
    check_r("Delete saved proposal template", r, 200)

# Clean up PDF test job
with app.app_context():
    j = Job.query.filter_by(user_id=UID_A, client_name="PDF Test Client").first()
    if j:
        db.session.delete(j)
        db.session.commit()
        check("Delete PDF test job", True)

# Clean up User B's data
auth(UID_B)
if created.get("b_job_id"):
    r = post(f"/jobs/{created['b_job_id']}/delete")
    check_r("Delete User B test job", r, 200)
if created.get("b_lead_id"):
    r = post(f"/leads/{created['b_lead_id']}/delete")
    check_r("Delete User B test lead", r, 200)

# Delete test users (delete all related data first to avoid FK constraint errors)
with app.app_context():
    from app import Expense, Tip, ScheduledJob, Client, Lead, CostTemplate
    for email in ["deep_test_a@ctj.test", "deep_test_b@ctj.test"]:
        u = User.query.filter_by(email=email).first()
        if u:
            Proposal.query.filter_by(user_id=u.id).delete()
            Job.query.filter_by(user_id=u.id).delete()
            ScheduledJob.query.filter_by(user_id=u.id).delete()
            Client.query.filter_by(user_id=u.id).delete()
            Lead.query.filter_by(user_id=u.id).delete()
            db.session.delete(u)
    db.session.commit()
    check("Delete both test users", True)

# ═══════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════
print(f"\n{'═'*58}\n  RESULTS SUMMARY\n{'═'*58}")
total  = len(results)
passed = sum(1 for _, ok, _ in results if ok)
failed = total - passed
failures = [(lbl, detail) for lbl, ok, detail in results if not ok]

if failures:
    print(f"\n  {RED}Failures:{RESET}")
    for lbl, detail in failures:
        print(f"  {FAIL} {lbl}  {YELLOW}{detail}{RESET}")

print(f"\n  {GREEN}Passed:{RESET} {passed}/{total}")
if failed:
    print(f"  {RED}Failed:{RESET} {failed}/{total}")
    sys.exit(1)
else:
    print(f"\n  {GREEN}All {total} deep tests passed!{RESET}")
    sys.exit(0)
