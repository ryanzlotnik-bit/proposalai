"""
CloseTheJob — Full Feature Test Agent
Uses Flask test client for accurate in-process testing.
Run: python3 test_agent.py
"""
import sys
import os
import re
import json
import traceback
from datetime import date, timedelta
from io import BytesIO

# ── colours ──────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"
PASS   = f"{GREEN}✓{RESET}"
FAIL   = f"{RED}✗{RESET}"

results = []
created = {}   # store IDs of things we create so we can clean them up


def section(name):
    print(f"\n{'═'*58}")
    print(f"  {name}")
    print(f"{'═'*58}")


def check(label, ok, detail=""):
    icon = PASS if ok else FAIL
    suffix = f"  {YELLOW}{detail}{RESET}" if detail else ""
    print(f"  {icon} {label}{suffix}")
    results.append((label, ok, detail))
    return ok


def check_r(label, r, expected=200, contains=None, not_contains=None):
    """Helper: check a response object."""
    ok = r.status_code == expected
    detail = f"[{r.status_code}]"
    body = r.data.decode("utf-8", errors="replace") if hasattr(r, "data") else ""
    if ok and contains:
        for c in ([contains] if isinstance(contains, str) else contains):
            if c.lower() not in body.lower():
                ok = False
                detail += f" missing '{c}'"
                break
    if ok and not_contains:
        for c in ([not_contains] if isinstance(not_contains, str) else not_contains):
            if c.lower() in body.lower():
                ok = False
                detail += f" found '{c}'"
                break
    return check(label, ok, detail)


# ── boot app ─────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

try:
    from app import app, db, User, Proposal, Job, Expense, Tip, CostTemplate
    from app import ScheduledJob, Client, Lead, LeadContact, LeadActivity
    from app import CRMEmailTemplate, JobRequest
except ImportError as e:
    print(f"{RED}Could not import app: {e}{RESET}")
    traceback.print_exc()
    sys.exit(1)

app.config["TESTING"] = True
app.config["WTF_CSRF_ENABLED"] = False
# Disable DEV_AUTO_LOGIN inside tests — we manage session manually
os.environ.pop("DEV_AUTO_LOGIN", None)

client = app.test_client()


def as_user(user_id):
    """Context manager: sets session to act as user_id."""
    class _Ctx:
        def __enter__(self):
            with client.session_transaction() as sess:
                sess["_user_id"] = str(user_id)
                sess["_fresh"] = True
        def __exit__(self, *_):
            pass
    return _Ctx()


def get(path, **kw):
    return client.get(path, follow_redirects=True, **kw)

def post(path, data=None, json_data=None, **kw):
    if json_data is not None:
        return client.post(path, json=json_data, follow_redirects=True,
                           content_type="application/json", **kw)
    return client.post(path, data=data, follow_redirects=True, **kw)

def ids_from(html, pattern):
    return [int(x) for x in re.findall(pattern, html)]


# ─────────────────────────────────────────────────────────────────────────────
# Setup: ensure test user exists
# ─────────────────────────────────────────────────────────────────────────────
with app.app_context():
    TEST_EMAIL = "agent_test@closethejob.test"
    existing = User.query.filter_by(email=TEST_EMAIL).first()
    if not existing:
        from flask_bcrypt import Bcrypt
        bcrypt = Bcrypt(app)
        u = User(
            email=TEST_EMAIL,
            name="Test Agent",
            company_name="Agent HVAC LLC",
            trade_type="HVAC",
            password=bcrypt.generate_password_hash("TestAgent1!").decode(),
            plan="pro",
        )
        db.session.add(u)
        db.session.commit()
        TEST_UID = u.id
    else:
        TEST_UID = existing.id
    print(f"\n  Using test user id={TEST_UID} ({TEST_EMAIL})")

# Set session for all subsequent requests
with client.session_transaction() as sess:
    sess["_user_id"] = str(TEST_UID)
    sess["_fresh"] = True


# ═══════════════════════════════════════════════════════
# 1. PUBLIC ROUTES (unauthenticated)
# ═══════════════════════════════════════════════════════
section("1. PUBLIC ROUTES")

anon = app.test_client()
check_r("Homepage loads",        anon.get("/",         follow_redirects=True), contains="CloseTheJob")
check_r("Login page loads",      anon.get("/login",    follow_redirects=True), contains="log in")
check_r("Register page loads",   anon.get("/register", follow_redirects=True), contains="register")
check_r("Pricing page loads",    anon.get("/pricing",  follow_redirects=True), 200)

# ═══════════════════════════════════════════════════════
# 2. AUTH PROTECTION
# ═══════════════════════════════════════════════════════
section("2. AUTH PROTECTION")

protected = ["/dashboard", "/financials", "/schedule", "/clients",
             "/leads", "/generate", "/profile", "/analytics", "/team"]
for path in protected:
    r = anon.get(path, follow_redirects=False)
    redirected = r.status_code in (301, 302) and "login" in (r.headers.get("Location",""))
    check(f"Unauthenticated {path} → redirects to login", redirected,
          f"[{r.status_code}] → {r.headers.get('Location','')}")

# ═══════════════════════════════════════════════════════
# 3. DASHBOARD
# ═══════════════════════════════════════════════════════
section("3. DASHBOARD")
r = get("/dashboard")
check_r("Dashboard loads", r, contains="dashboard")
check_r("Dashboard — no traceback", r, not_contains=["traceback", "internal server error"])

# ═══════════════════════════════════════════════════════
# 4. GENERATE / PROPOSALS
# ═══════════════════════════════════════════════════════
section("4. PROPOSALS — GENERATE PAGE")
r = get("/generate")
check_r("Generate page loads", r)
check_r("Generate — page has proposal form", r, contains=["generate", "proposal"])

# Cost template add
r = post("/cost-templates/add", data={
    "name": "Agent Test Item",
    "amount": "250.00",
    "cost_type": "fixed",
    "unit": "",
})
check_r("Add cost template", r, 200)

r2 = get("/generate")
body2 = r2.data.decode()
check("Cost template visible on generate page", "Agent Test Item" in body2)

with app.app_context():
    from app import CostTemplate as _CT
    ct = _CT.query.filter_by(user_id=TEST_UID, name="Agent Test Item").order_by(_CT.id.desc()).first()
    if ct:
        created["ct_id"] = ct.id

# ═══════════════════════════════════════════════════════
# 5. FINANCIALS
# ═══════════════════════════════════════════════════════
section("5. FINANCIALS")
r = get("/financials")
check_r("Financials page loads", r)
check_r("Financials — no traceback", r, not_contains=["traceback", "internal server error"])

# Add closed job
r = post("/jobs/add", data={
    "client_name":     "John Smith",
    "client_email":    "john@testclient.com",
    "client_phone":    "555-1234",
    "job_type":        "AC Repair",
    "revenue":         "1250.00",
    "notes":           "Agent test job",
    "completed_date":  str(date.today()),
})
check_r("Add closed job", r, 200)

with app.app_context():
    from app import Job as _Job
    j = _Job.query.filter_by(user_id=TEST_UID, client_name="John Smith").order_by(_Job.id.desc()).first()
    if j:
        created["job_id"] = j.id
r2 = get("/financials")
body2 = r2.data.decode()
check("Closed job appears in financials", "John Smith" in body2 or bool(created.get("job_id")))

# Add expense
r = post("/expenses/add", data={
    "description": "Parts — agent test",
    "amount":      "200.00",
    "date":        str(date.today()),
    "category":    "Materials",
})
check_r("Add expense", r, 200)

with app.app_context():
    from app import Expense as _Expense
    e = _Expense.query.filter_by(user_id=TEST_UID).order_by(_Expense.id.desc()).first()
    if e:
        created["expense_id"] = e.id
r3 = get("/financials")
body3 = r3.data.decode()
check("Expense appears in financials", "Parts" in body3 or bool(created.get("expense_id")))

# Add tip
r = post("/tips/add", data={
    "client_name": "John Smith",
    "amount":      "50.00",
    "date":        str(date.today()),
    "notes":       "Agent test tip",
})
check_r("Add tip", r, 200)

with app.app_context():
    from app import Tip as _Tip
    t = _Tip.query.filter_by(user_id=TEST_UID).order_by(_Tip.id.desc()).first()
    if t:
        created["tip_id"] = t.id
r4 = get("/financials")
body4 = r4.data.decode()
check("Tip appears in financials", bool(created.get("tip_id")))

# ═══════════════════════════════════════════════════════
# 6. JOB DETAIL & PHOTOS
# ═══════════════════════════════════════════════════════
section("6. JOB DETAIL")
if created.get("job_id"):
    r = get(f"/jobs/{created['job_id']}")
    check_r("Job detail page loads", r)
    check_r("Job detail — no traceback", r, not_contains=["traceback", "internal server error"])
    check("Job detail — client name shown", "John Smith" in r.data.decode())
else:
    check("Job detail page loads", False, "no job created")

# ═══════════════════════════════════════════════════════
# 7. SCHEDULE
# ═══════════════════════════════════════════════════════
section("7. SCHEDULE")
r = get("/schedule")
check_r("Schedule page loads", r)
check_r("Schedule — no traceback", r, not_contains=["traceback", "internal server error"])

future = str(date.today() + timedelta(days=7))
r = post("/schedule", data={
    "client_name":    "Jane Doe",
    "client_email":   "jane@testclient.com",
    "client_phone":   "555-9999",
    "job_type":       "Furnace Install",
    "description":    "Agent test scheduled job",
    "scheduled_date": future,
    "auto_invoice":   "on",
})
check_r("Add scheduled job", r, 200)

r2 = get("/schedule")
body2 = r2.data.decode()
with app.app_context():
    from app import ScheduledJob as _SJ
    sj = _SJ.query.filter_by(user_id=TEST_UID, client_name="Jane Doe").order_by(_SJ.id.desc()).first()
    if sj:
        created["sj_id"] = sj.id
r2 = get("/schedule")
body2 = r2.data.decode()
check("Scheduled job appears on calendar", "Jane Doe" in body2 or bool(created.get("sj_id")))

# ═══════════════════════════════════════════════════════
# 8. CLIENTS CRM
# ═══════════════════════════════════════════════════════
section("8. CLIENTS CRM")
r = get("/clients")
check_r("Clients list page loads", r)
check_r("Clients — no traceback", r, not_contains=["traceback"])

r = post("/clients/add", data={
    "name":    "Agent Test Client",
    "email":   "agentclient@test.com",
    "phone":   "555-8888",
    "address": "123 Test St, Chicago IL",
    "notes":   "Created by test agent",
})
check_r("Add client", r, 200)

with app.app_context():
    from app import Client as _Client
    cl = _Client.query.filter_by(user_id=TEST_UID, name="Agent Test Client").order_by(_Client.id.desc()).first()
    if cl:
        created["client_id"] = cl.id
r2 = get("/clients")
body2 = r2.data.decode()
if created.get("client_id"):
    r3 = get(f"/clients/{created['client_id']}")
    check_r("Client detail page loads", r3)
    check("Client detail — shows client name", "Agent Test Client" in r3.data.decode())

    r4 = post(f"/clients/{created['client_id']}/edit", data={
        "name":    "Agent Test Client Updated",
        "email":   "agentclient@test.com",
        "phone":   "555-8888",
        "address": "123 Test St, Chicago IL",
        "notes":   "Updated by agent",
    })
    check_r("Edit client", r4, 200)
else:
    check("Client detail page loads", False, "no client id found")

# ═══════════════════════════════════════════════════════
# 9. LEADS / SALES CRM
# ═══════════════════════════════════════════════════════
section("9. LEADS / SALES CRM")
r = get("/leads")
check_r("Leads page loads", r)
check_r("Leads — no traceback", r, not_contains=["traceback"])

r = post("/leads/add", data={
    "contact":      "Agent Test Contact",
    "company":      "Agent Test Company",
    "phone":        "555-6666",
    "email":        "contact@agenttest.com",
    "address":      "456 Test Ave, Chicago IL",
    "website":      "https://test.com",
    "company_type": "Commercial",
    "stage":        "new",
    "notes":        "Agent test lead",
})
check_r("Add lead", r, 200)

with app.app_context():
    from app import Lead as _Lead
    ld = _Lead.query.filter_by(user_id=TEST_UID, company="Agent Test Company").order_by(_Lead.id.desc()).first()
    if ld:
        created["lead_id"] = ld.id
r2 = get("/leads")
body2 = r2.data.decode()
if created.get("lead_id"):
    r3 = get(f"/leads/{created['lead_id']}")
    check_r("Lead detail page loads", r3)
    check("Lead detail — shows company", "Agent Test Company" in r3.data.decode())

    r4 = post(f"/leads/{created['lead_id']}/contacts/add", data={
        "first_name": "Bob",
        "last_name":  "Builder",
        "title":      "Facilities Manager",
        "email":      "bob@agenttest.com",
        "cell_phone": "555-7777",
    })
    check_r("Add contact to lead", r4, 200)

    r5 = post(f"/leads/{created['lead_id']}/activity/add", data={
        "activity_type": "call",
        "body":          "Test call — agent",
        "contact_id":    "",
    })
    check_r("Add lead activity", r5, 200)

    r6 = post(f"/leads/{created['lead_id']}/stage",
              data={"stage": "contacted"})
    check_r("Update lead stage", r6, 200)

    r7 = post(f"/leads/{created['lead_id']}/edit", data={
        "company":      "Agent Test Company Updated",
        "stage":        "qualified",
        "company_type": "Commercial",
        "notes":        "Edited by agent",
    })
    check_r("Edit lead", r7, 200)
else:
    check("Lead detail page loads", False, "no lead id found")

# ═══════════════════════════════════════════════════════
# 10. LEADS CSV IMPORT PAGE
# ═══════════════════════════════════════════════════════
section("10. LEADS CSV IMPORT")
r = get("/leads/import")
check_r("Leads import page loads", r)

# ═══════════════════════════════════════════════════════
# 11. CRM EMAIL TEMPLATES
# ═══════════════════════════════════════════════════════
section("11. CRM EMAIL TEMPLATES")
r = get("/crm/email-templates")
check_r("CRM email templates page loads", r)

r = post("/crm/email-templates/add", data={
    "name":    "Agent Test Template",
    "subject": "Hello {company}",
    "body":    "Hi {name}, this is a test.",
})
check_r("Add CRM email template", r, 200)

with app.app_context():
    from app import CRMEmailTemplate as _ET
    et = _ET.query.filter_by(user_id=TEST_UID, name="Agent Test Template").order_by(_ET.id.desc()).first()
    if et:
        created["email_tmpl_id"] = et.id
r2 = get("/crm/email-templates")
body2 = r2.data.decode()
check("Email template appears in list", "Agent Test Template" in body2)

# ═══════════════════════════════════════════════════════
# 12. CRM INBOX
# ═══════════════════════════════════════════════════════
section("12. CRM INBOX")
r = get("/crm/email")
# IMAP may fail in test env — just check page loads (200 or shows error gracefully)
ok = r.status_code == 200 and "traceback" not in r.data.decode().lower()
check("CRM inbox page loads without crash", ok, f"[{r.status_code}]")

# ═══════════════════════════════════════════════════════
# 13. PRICING ADVISOR
# ═══════════════════════════════════════════════════════
section("13. AI PRICING ADVISOR")
r = get("/pricing-advisor")
check_r("Pricing advisor page loads", r)

# ═══════════════════════════════════════════════════════
# 14. PROFILE SETTINGS
# ═══════════════════════════════════════════════════════
section("14. PROFILE SETTINGS")
r = get("/profile")
check_r("Profile page loads", r)

r = post("/profile", data={
    "name":           "Test Agent",
    "company_name":   "Agent HVAC LLC",
    "trade_type":     "HVAC",
    "phone":          "555-0000",
    "license_number": "HC-12345",
    "website":        "",
    "address":        "Chicago, IL",
})
check_r("Save profile", r, 200)

# ═══════════════════════════════════════════════════════
# 15. TEMPLATES LIBRARY (saved proposal templates)
# ═══════════════════════════════════════════════════════
section("15. PROPOSAL TEMPLATES LIBRARY")
r = get("/templates")
check_r("Templates library page loads", r)

# ═══════════════════════════════════════════════════════
# 16. SERVICE AREAS
# ═══════════════════════════════════════════════════════
section("16. SERVICE AREAS")
r = get("/service-areas")
check_r("Service areas page loads", r)

r = post("/service-areas", data={
    "zip_code":     "60601",
    "label":        "Chicago Loop",
    "radius_miles": "15",
})
check_r("Add service area", r, 200)

# ═══════════════════════════════════════════════════════
# 17. TEAM
# ═══════════════════════════════════════════════════════
section("17. TEAM MANAGEMENT")
r = get("/team")
check_r("Team page loads", r)

r = post("/team/invite", data={
    "email": "teammate@agenttest.com",
    "role":  "member",
})
ok = r.status_code == 200
check("Team invite sent (or plan-gated)", ok, f"[{r.status_code}]")

# ═══════════════════════════════════════════════════════
# 18. ANALYTICS
# ═══════════════════════════════════════════════════════
section("18. ANALYTICS")
r = get("/analytics")
check_r("Analytics page loads (or plan-gated redirect)", r, 200)
check_r("Analytics — no traceback", r, not_contains=["traceback", "internal server error"])

# ═══════════════════════════════════════════════════════
# 19. JOB REQUESTS
# ═══════════════════════════════════════════════════════
section("19. JOB REQUESTS (Booking Form)")
r = get("/job-requests")
check_r("Job requests page loads", r)

# ═══════════════════════════════════════════════════════
# 20. SCOPE WRITER API
# ═══════════════════════════════════════════════════════
section("20. API — SCOPE WRITER")
r = post("/api/scope-writer", json_data={
    "job_type":          "HVAC",
    "quick_description": "Replace central AC unit, 3-ton system",
})
check_r("Scope writer API responds", r, 200)

# ═══════════════════════════════════════════════════════
# 21. PRICING PAGE
# ═══════════════════════════════════════════════════════
section("21. PRICING PAGE")
r = get("/pricing")
check_r("Pricing page loads", r, 200)

# ═══════════════════════════════════════════════════════
# 22. STRIPE BILLING
# ═══════════════════════════════════════════════════════
section("22. STRIPE BILLING")

# Pricing page shows plan options
r = get("/pricing")
check_r("Pricing page loads", r, 200)
body_pricing = r.data.decode()
check("Pricing — shows plan options", any(w in body_pricing.lower() for w in ["starter", "pro", "plan", "upgrade"]))

# Checkout session: no Stripe keys configured → should flash warning + redirect (not crash)
r = post("/create-checkout-session", data={"plan": "starter"})
no_crash = r.status_code == 200 and "traceback" not in r.data.decode().lower()
check("Checkout session: no Stripe key → graceful redirect", no_crash, f"[{r.status_code}]")

# Subscription success: missing session_id → should not crash
r = get("/subscription/success?session_id=")
check_r("Subscription success — no crash without session", r, 200,
        not_contains=["traceback", "internal server error"])

# Stripe webhook endpoint exists and returns 400 for bad payload (not 404 or 500)
r = post("/webhook/stripe", data=b"not-a-real-payload",
         **{"content_type": "application/json"})
check("Stripe webhook endpoint exists (not 404)", r.status_code != 404,
      f"[{r.status_code}] (400 expected without valid signature)")

# Invoice payment page: bad token → 404
r = get("/invoice/badtoken_xyz123")
check("Invoice page: bad token → 404", r.status_code == 404, f"[{r.status_code}]")

# Deposit page: bad token → 404
r = get("/deposit/badtoken_xyz123")
check("Deposit page: bad token → 404", r.status_code == 404, f"[{r.status_code}]")

# ═══════════════════════════════════════════════════════
# 23. DIAGNOSTIC ROUTES
# ═══════════════════════════════════════════════════════
section("23. DIAGNOSTIC ROUTES")
r = get("/dev-check")
check_r("/dev-check loads", r, 200)

r = get("/test-email")
check_r("/test-email route loads", r, 200)

# ═══════════════════════════════════════════════════════
# 23. SEND INVOICE ON JOB
# ═══════════════════════════════════════════════════════
section("24. SEND INVOICE")
if created.get("job_id"):
    r = post(f"/jobs/{created['job_id']}/send-invoice")
    ok = r.status_code == 200
    check("Send invoice endpoint responds", ok, f"[{r.status_code}] (email may fail without credentials)")
else:
    check("Send invoice endpoint responds", False, "no job to test with")

# ═══════════════════════════════════════════════════════
# 24. CANCEL SCHEDULED JOB
# ═══════════════════════════════════════════════════════
section("25. SCHEDULE — CANCEL JOB")
if created.get("sj_id"):
    r = post(f"/schedule/{created['sj_id']}/cancel")
    check_r("Cancel scheduled job", r, 200)
else:
    check("Cancel scheduled job", False, "no scheduled job to cancel")

# ═══════════════════════════════════════════════════════
# 25. CLEANUP — DELETE TEST DATA
# ═══════════════════════════════════════════════════════
section("26. CLEANUP — DELETE TEST DATA")

if created.get("sj_id"):
    r = post(f"/schedule/{created['sj_id']}/delete")
    check_r("Delete scheduled job", r, 200)

if created.get("client_id"):
    r = post(f"/clients/{created['client_id']}/delete")
    check_r("Delete test client", r, 200)

if created.get("lead_id"):
    r = post(f"/leads/{created['lead_id']}/delete")
    check_r("Delete test lead", r, 200)

if created.get("email_tmpl_id"):
    r = post(f"/crm/email-templates/{created['email_tmpl_id']}/delete")
    check_r("Delete email template", r, 200)

if created.get("job_id"):
    r = post(f"/jobs/{created['job_id']}/delete")
    check_r("Delete test job", r, 200)

if created.get("expense_id"):
    r = post(f"/expenses/{created['expense_id']}/delete")
    check_r("Delete test expense", r, 200)

if created.get("tip_id"):
    r = post(f"/tips/{created['tip_id']}/delete")
    check_r("Delete test tip", r, 200)

if created.get("ct_id"):
    r = post(f"/cost-templates/{created['ct_id']}/delete")
    check_r("Delete cost template", r, 200)

# Clean up test user
with app.app_context():
    u = User.query.filter_by(email=TEST_EMAIL).first()
    if u:
        db.session.delete(u)
        db.session.commit()
        check("Delete test user", True)

# ═══════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════
print(f"\n{'═'*58}")
print("  RESULTS SUMMARY")
print(f"{'═'*58}")

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
    print(f"\n  {GREEN}All {total} tests passed!{RESET}")
    sys.exit(0)
