from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from flask_mail import Mail, Message
import requests as http_requests
import stripe
import os
import calendar
import threading
import imaplib
import time
import email as email_lib
import email.utils
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from fpdf import FPDF
import json
import random
import string
import secrets

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build
    GOOGLE_CALENDAR_ENABLED = True
except ImportError:
    GOOGLE_CALENDAR_ENABLED = False

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///proposalai.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Mail config — set MAIL_USERNAME and MAIL_PASSWORD in .env
app.config['MAIL_SERVER']   = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT']     = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USE_TLS']  = True
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD', '')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_USERNAME', '')

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access that page.'
mail = Mail(app)

stripe.api_key = ''.join(os.getenv('STRIPE_SECRET_KEY', '').split())

TRIAL_DAYS = 30

SPECIALTIES = {
    'HVAC': ['AC Installation', 'AC Repair', 'Furnace Installation', 'Furnace Repair', 'Duct Work'],
    'Plumbing': ['Plumbing Repair', 'Water Heater Installation', 'Pipe Replacement', 'Drain Cleaning'],
    'Electrical': ['Electrical Panel Upgrade', 'Outlet Installation', 'Wiring', 'Lighting Installation'],
    'Roofing': ['Roof Replacement', 'Roof Repair', 'Gutter Installation'],
    'Painting': ['Painting — Interior', 'Painting — Exterior'],
    'Flooring': ['Flooring', 'Tile Work'],
    'Landscaping': ['Landscaping', 'Lawn Care', 'Tree Removal'],
    'General Construction': ['General Construction', 'Remodel', 'Drywall'],
    'Pressure Washing': ['Pressure Washing', 'Window Washing'],
    'Epoxy Flooring': ['Epoxy Flooring'],
    'Other': ['Other'],
}

# ─── Models ────────────────────────────────────────────────────────────────────

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    company_name = db.Column(db.String(200), default='')
    trade_type = db.Column(db.String(100), default='General Contractor')
    phone = db.Column(db.String(30), default='')
    license_number = db.Column(db.String(100), default='')
    address = db.Column(db.String(300), default='')
    website = db.Column(db.String(200), default='')
    subscription_status = db.Column(db.String(50), default='trial')
    stripe_customer_id = db.Column(db.String(200), default='')
    stripe_subscription_id = db.Column(db.String(200), default='')
    plan = db.Column(db.String(50), default='trial')  # trial, starter, pro, enterprise
    trial_expires_at = db.Column(db.DateTime, nullable=True)
    specialty = db.Column(db.String(200), default='')   # comma-separated specialties
    onboarding_done = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    google_access_token = db.Column(db.Text, nullable=True)
    google_refresh_token = db.Column(db.Text, nullable=True)
    google_token_expiry = db.Column(db.Float, nullable=True)
    logo_url = db.Column(db.Text, nullable=True)
    brand_color = db.Column(db.String(7), default='#F97316')
    team_owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    booking_slug = db.Column(db.String(100), unique=True, nullable=True)
    booking_enabled = db.Column(db.Boolean, default=False)
    proposals = db.relationship('Proposal', backref='user', lazy=True)

    @property
    def allowed_job_types(self):
        if not self.specialty:
            return None  # None = show all
        types = []
        for s in self.specialty.split(','):
            types.extend(SPECIALTIES.get(s.strip(), []))
        return types

    @property
    def proposal_count(self):
        return len(self.proposals)

    @property
    def trial_active(self):
        # Stripe trial: paid plan but not yet charged
        if self.subscription_status == 'trialing':
            return True
        return False

    @property
    def trial_days_left(self):
        if not self.trial_expires_at:
            return 0
        return max(0, (self.trial_expires_at - datetime.utcnow()).days)

    @property
    def on_paid_plan(self):
        return self.plan in ('starter', 'pro', 'enterprise')

    @property
    def can_generate(self):
        return self.on_paid_plan  # includes trialing (plan is set on checkout)

    @property
    def is_subscribed(self):
        return self.on_paid_plan

    @property
    def can_auto_invoice(self):
        return self.on_paid_plan

    @property
    def can_see_charts(self):
        return self.on_paid_plan

    @property
    def plan_display(self):
        name = {'trial': 'Trial', 'starter': 'Pro', 'pro': 'Business',
                'enterprise': 'Enterprise'}.get(self.plan, self.plan.capitalize())
        if self.subscription_status == 'trialing':
            return f'{name} (Trial)'
        return name

    @property
    def can_use_branding(self):
        return self.plan in ('pro', 'enterprise')

    @property
    def can_have_team(self):
        return self.plan in ('pro', 'enterprise')

    @property
    def is_enterprise(self):
        return self.plan == 'enterprise'

    @property
    def effective_brand_color(self):
        return self.brand_color or '#F97316'


class Proposal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=True)
    proposal_number = db.Column(db.String(50), nullable=False)
    # Client info
    client_name = db.Column(db.String(200), nullable=False)
    client_email = db.Column(db.String(200), default='')
    client_phone = db.Column(db.String(50), default='')
    client_address = db.Column(db.String(500), default='')
    # Job info
    job_type = db.Column(db.String(100), default='')
    job_description = db.Column(db.Text, default='')
    materials_input = db.Column(db.Text, default='')
    labor_hours = db.Column(db.Float, default=0)
    labor_rate = db.Column(db.Float, default=75)
    timeline = db.Column(db.String(300), default='')
    warranty = db.Column(db.String(300), default='')
    notes = db.Column(db.Text, default='')
    # Generated content (JSON string)
    generated_content = db.Column(db.Text, default='{}')
    grand_total = db.Column(db.Float, default=0)
    tax_rate = db.Column(db.Float, default=0)  # percentage, e.g. 8.5
    deposit_pct = db.Column(db.Float, default=0)  # 0 = no deposit required
    deposit_token = db.Column(db.String(64), unique=True, nullable=True)
    deposit_paid_at = db.Column(db.DateTime, nullable=True)
    template_id = db.Column(db.Integer, nullable=True)  # source template if any
    status = db.Column(db.String(50), default='draft')  # draft, sent, accepted, declined
    public_token = db.Column(db.String(64), unique=True, nullable=True)
    accepted_at = db.Column(db.DateTime, nullable=True)
    reminder_sent_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    linked_jobs = db.relationship('Job', backref='proposal', lazy=True)

    @property
    def tax_amount(self):
        return round(self.grand_total * (self.tax_rate or 0) / 100, 2)

    @property
    def total_with_tax(self):
        return self.grand_total + self.tax_amount

    @property
    def deposit_amount(self):
        return round(self.total_with_tax * (self.deposit_pct or 0) / 100, 2)

    @property
    def content(self):
        try:
            return json.loads(self.generated_content)
        except Exception:
            return {}

    @property
    def formatted_date(self):
        return self.created_at.strftime('%B %d, %Y')

    @property
    def status_color(self):
        return {
            'draft': 'gray',
            'sent': 'blue',
            'accepted': 'green',
            'declined': 'red',
        }.get(self.status, 'gray')


class CostTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    cost_type = db.Column(db.String(20), default='fixed')  # fixed or per_unit
    amount = db.Column(db.Float, default=0)
    unit = db.Column(db.String(50), default='')  # e.g. "sq ft", "linear ft"
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Job(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    proposal_id = db.Column(db.Integer, db.ForeignKey('proposal.id'), nullable=True)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=True)
    client_name = db.Column(db.String(200), nullable=False)
    client_email = db.Column(db.String(200), default='')
    client_phone = db.Column(db.String(50), default='')
    job_type = db.Column(db.String(100), default='')
    description = db.Column(db.Text, default='')
    revenue = db.Column(db.Float, default=0)
    completed_date = db.Column(db.Date, nullable=False)
    invoice_sent = db.Column(db.Boolean, default=False)
    invoice_sent_at = db.Column(db.DateTime, nullable=True)
    pay_token = db.Column(db.String(64), unique=True, nullable=True)
    paid_at = db.Column(db.DateTime, nullable=True)
    payment_reminder_sent_at = db.Column(db.DateTime, nullable=True)
    review_sent_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text, default='')
    completion_summary = db.Column(db.Text, default='')
    service_area = db.Column(db.String(200), default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    job_expenses = db.relationship('Expense', backref='job', lazy=True)
    job_tips = db.relationship('Tip', backref='job', lazy=True)
    photos = db.relationship('JobPhoto', backref='job_ref', lazy=True, cascade='all, delete-orphan')

    @property
    def tip_total(self):
        return sum(t.amount for t in self.job_tips)

    @property
    def expense_total(self):
        return sum(e.amount for e in self.job_expenses)

    @property
    def net(self):
        return self.revenue + self.tip_total - self.expense_total


class JobPhoto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('job.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    photo_type = db.Column(db.String(10), default='before')  # before / after
    filename = db.Column(db.String(200), default='')
    file_data = db.Column(db.LargeBinary, nullable=False)
    mime_type = db.Column(db.String(50), default='image/jpeg')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    job_id = db.Column(db.Integer, db.ForeignKey('job.id'), nullable=True)
    description = db.Column(db.String(300), nullable=False)
    amount = db.Column(db.Float, default=0)
    category = db.Column(db.String(100), default='General')
    date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Tip(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    job_id = db.Column(db.Integer, db.ForeignKey('job.id'), nullable=True)
    amount = db.Column(db.Float, default=0)
    note = db.Column(db.String(300), default='')
    date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ScheduledJob(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=True)
    google_event_id = db.Column(db.String(200), nullable=True)
    client_name = db.Column(db.String(200), nullable=False)
    client_email = db.Column(db.String(200), default='')
    client_phone = db.Column(db.String(50), default='')
    job_type = db.Column(db.String(100), default='')
    description = db.Column(db.Text, default='')
    scheduled_date = db.Column(db.Date, nullable=False)
    estimated_revenue = db.Column(db.Float, default=0)
    invoice_on_complete = db.Column(db.Boolean, default=True)
    status = db.Column(db.String(50), default='scheduled')  # scheduled, invoiced, cancelled
    notes = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), default='')
    phone = db.Column(db.String(50), default='')
    address = db.Column(db.String(500), default='')
    notes = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    proposals = db.relationship('Proposal', backref='client', lazy=True, foreign_keys='Proposal.client_id')
    jobs = db.relationship('Job', backref='client', lazy=True, foreign_keys='Job.client_id')
    scheduled_jobs = db.relationship('ScheduledJob', backref='client', lazy=True, foreign_keys='ScheduledJob.client_id')


class TeamInvite(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    email = db.Column(db.String(150), nullable=False)
    token = db.Column(db.String(64), unique=True, nullable=False)
    accepted = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ProposalTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    job_type = db.Column(db.String(100), default='')
    job_description = db.Column(db.Text, default='')
    materials_input = db.Column(db.Text, default='')
    labor_hours = db.Column(db.Float, default=0)
    labor_rate = db.Column(db.Float, default=75)
    timeline = db.Column(db.String(300), default='')
    warranty = db.Column(db.String(300), default='')
    notes = db.Column(db.Text, default='')
    tax_rate = db.Column(db.Float, default=0)
    deposit_pct = db.Column(db.Float, default=0)
    generated_content = db.Column(db.Text, default='{}')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PromoCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False)
    plan = db.Column(db.String(50), default='enterprise')
    label = db.Column(db.String(200), default='')       # e.g. "John Smith demo"
    uses_remaining = db.Column(db.Integer, nullable=True)  # None = unlimited
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ServiceArea(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    color = db.Column(db.String(7), default='#F97316')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class JobRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    client_name = db.Column(db.String(200), nullable=False)
    client_email = db.Column(db.String(200), default='')
    client_phone = db.Column(db.String(50), default='')
    job_type = db.Column(db.String(100), default='')
    description = db.Column(db.Text, default='')
    preferred_date = db.Column(db.String(100), default='')
    status = db.Column(db.String(50), default='new')  # new, converted, dismissed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ─── Helpers ───────────────────────────────────────────────────────────────────

def uid():
    """Return the effective owner user ID. Team members see their owner's data."""
    if not current_user.is_authenticated:
        return None
    return current_user.team_owner_id or current_user.id


def get_owner_user():
    """Return the effective owner User object."""
    owner_id = current_user.team_owner_id or current_user.id
    if owner_id == current_user.id:
        return current_user
    return User.query.get(owner_id)


REBOOK_INTERVALS = {
    'AC Installation': 365, 'AC Repair': 365, 'Furnace Installation': 365,
    'Furnace Repair': 365, 'Duct Work': 730, 'Water Heater Installation': 3650,
    'Pipe Replacement': 1825, 'Plumbing Repair': 730, 'Drain Cleaning': 365,
    'Electrical Panel Upgrade': 3650, 'Outlet Installation': 1825, 'Wiring': 1825,
    'Lighting Installation': 1825, 'Roof Replacement': 7300, 'Roof Repair': 1825,
    'Gutter Installation': 1825, 'Landscaping': 30, 'Lawn Care': 14,
    'Tree Removal': 365, 'Painting — Interior': 1825, 'Painting — Exterior': 1825,
    'Flooring': 3650, 'Tile Work': 3650, 'Drywall': 1825,
    'General Construction': 1825, 'Remodel': 3650,
    'Pressure Washing': 365, 'Window Washing': 90, 'Epoxy Flooring': 3650,
}


def compute_rebook(jobs):
    """Return dict with score (0-100), status, days_since_last, last_job_type."""
    if not jobs:
        return {'score': 0, 'status': 'no_history', 'days_since': None, 'last_type': None}
    latest = max(jobs, key=lambda j: j.completed_date)
    days_since = (date.today() - latest.completed_date).days
    interval = REBOOK_INTERVALS.get(latest.job_type, 365)
    score = min(int((days_since / interval) * 100), 100)
    status = 'fresh' if score < 40 else ('follow_up' if score < 70 else 'due')
    return {'score': score, 'status': status, 'days_since': days_since, 'last_type': latest.job_type}


def generate_proposal_number():
    today = date.today()
    rand = ''.join(random.choices(string.digits, k=4))
    return f"P-{today.strftime('%Y%m')}-{rand}"


def parse_timeline_date(timeline_str, base_date=None):
    """Best-effort parse of a timeline string into a date. Defaults to base+7 days."""
    import re
    if base_date is None:
        base_date = date.today()
    if not timeline_str:
        return base_date + timedelta(days=7)
    s = timeline_str.lower()
    m = re.search(r'(\d+)\s*(?:-\s*\d+)?\s*day', s)
    if m:
        return base_date + timedelta(days=int(m.group(1)))
    m = re.search(r'(\d+)\s*week', s)
    if m:
        return base_date + timedelta(weeks=int(m.group(1)))
    if 'next week' in s:
        return base_date + timedelta(days=7)
    if 'next month' in s:
        return base_date + timedelta(days=30)
    if 'tomorrow' in s:
        return base_date + timedelta(days=1)
    return base_date + timedelta(days=7)


def build_monthly_chart_data(user_id, months=12):
    today = date.today()
    labels, revenues, expenses_data, tips_data = [], [], [], []
    for i in range(months - 1, -1, -1):
        # Calculate month/year going back i months
        month = today.month - i
        year = today.year
        while month <= 0:
            month += 12
            year -= 1
        label = f"{calendar.month_abbr[month]} '{str(year)[2:]}"
        start = date(year, month, 1)
        _, last_day = calendar.monthrange(year, month)
        end = date(year, month, last_day)

        rev = db.session.query(db.func.sum(Job.revenue)).filter(
            Job.user_id == user_id,
            Job.completed_date >= start,
            Job.completed_date <= end
        ).scalar() or 0

        exp = db.session.query(db.func.sum(Expense.amount)).filter(
            Expense.user_id == user_id,
            Expense.date >= start,
            Expense.date <= end
        ).scalar() or 0

        tip = db.session.query(db.func.sum(Tip.amount)).filter(
            Tip.user_id == user_id,
            Tip.date >= start,
            Tip.date <= end
        ).scalar() or 0

        labels.append(label)
        revenues.append(round(float(rev), 2))
        expenses_data.append(round(float(exp), 2))
        tips_data.append(round(float(tip), 2))

    return labels, revenues, expenses_data, tips_data


def auto_invoice_past_scheduled_jobs(user_id):
    """Find scheduled jobs whose date has passed, create Job records, send invoices."""
    today = date.today()
    user = User.query.get(user_id)
    past = ScheduledJob.query.filter(
        ScheduledJob.user_id == user_id,
        ScheduledJob.scheduled_date <= today,
        ScheduledJob.status == 'scheduled',
    ).all()
    for sj in past:
        job = Job(
            user_id=user_id,
            client_id=sj.client_id,
            client_name=sj.client_name,
            client_email=sj.client_email,
            client_phone=sj.client_phone,
            job_type=sj.job_type,
            description=sj.description,
            revenue=sj.estimated_revenue,
            completed_date=sj.scheduled_date,
            notes=sj.notes,
        )
        db.session.add(job)
        db.session.flush()  # get job.id
        sj.status = 'invoiced'
        db.session.commit()
        if sj.invoice_on_complete and sj.client_email and user and user.can_auto_invoice:
            send_invoice_email(job, user)


def auto_send_followup_reminders(user_id):
    """Send a follow-up nudge email for proposals that have been 'sent' for 3+ days with no response."""
    cutoff = datetime.utcnow() - timedelta(days=3)
    stale = Proposal.query.filter(
        Proposal.user_id == user_id,
        Proposal.status == 'sent',
        Proposal.created_at <= cutoff,
        Proposal.reminder_sent_at == None,
        Proposal.client_email != '',
        Proposal.client_email != None,
    ).all()
    for p in stale:
        send_followup_reminder(p.id, user_id)


def send_followup_reminder(proposal_id, user_id):
    """Send a follow-up reminder email to the client for a pending proposal."""
    if not app.config.get('MAIL_USERNAME'):
        return

    def do_send():
        with app.app_context():
            p = Proposal.query.get(proposal_id)
            u = User.query.get(user_id)
            if not p or not u or not p.client_email:
                return
            if p.status != 'sent' or p.reminder_sent_at:
                return  # already responded or already nudged

            company = u.company_name or u.name
            base_url = os.getenv('APP_URL', '').rstrip('/')
            proposal_link = f"{base_url}/p/{p.public_token}" if base_url and p.public_token else None

            subject = f"Following up on your {p.job_type} proposal — {company}"
            link_btn = f'<p style="text-align:center;margin-bottom:24px;"><a href="{proposal_link}" style="display:inline-block;background:#F97316;color:#fff;font-size:14px;font-weight:700;padding:12px 28px;text-decoration:none;letter-spacing:0.04em;">REVIEW PROPOSAL →</a></p>' if proposal_link else ''
            phone_line = f'<br />{u.phone}' if u.phone else ''
            address_line = f'  ·  {u.address}' if u.address else ''
            html_body = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:'DM Sans',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px;">
<table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;max-width:600px;">
<tr><td style="background:#1a1a1a;padding:0;">
  <div style="background:#F97316;text-align:center;padding:14px 28px;">
    <span style="font-family:'Bebas Neue',Arial,sans-serif;font-size:24px;color:#fff;letter-spacing:0.06em;">{company}</span>
  </div>
</td></tr>
<tr><td style="padding:32px;">
  <p style="font-size:16px;font-weight:700;color:#111;margin-bottom:8px;">Hi {p.client_name},</p>
  <p style="font-size:14px;color:#444;line-height:1.6;margin-bottom:20px;">
    I wanted to follow up on the {p.job_type} proposal I sent over (#{p.proposal_number}).
    Just checking in to see if you had a chance to review it or if you have any questions.
  </p>
  <table style="background:#f9f9f9;border:1px solid #eee;width:100%;margin-bottom:24px;">
    <tr><td style="padding:16px;">
      <div style="font-size:11px;font-weight:700;color:#F97316;text-transform:uppercase;margin-bottom:6px;">Proposal Summary</div>
      <div style="font-size:13px;font-weight:700;color:#111;">#{p.proposal_number} — {p.job_type}</div>
      <div style="font-size:13px;color:#555;margin-top:4px;">Total Estimate: <strong>${p.grand_total:,.2f}</strong></div>
    </td></tr>
  </table>
  {link_btn}
  <p style="font-size:13px;color:#444;line-height:1.6;">
    Happy to answer any questions or adjust anything to better fit your needs. Just reply to this email.
  </p>
  <p style="font-size:13px;color:#666;margin-top:20px;">Thanks,<br /><strong>{u.name}</strong><br />{company}{phone_line}</p>
</td></tr>
<tr><td style="background:#1a1a1a;padding:12px 28px;text-align:center;">
  <p style="font-size:10px;color:#666;margin:0;">{company}{address_line}</p>
</td></tr>
</table></td></tr></table>
</body></html>"""

            try:
                from flask_mail import Message as MailMessage
                msg = MailMessage(subject=subject, recipients=[p.client_email], html=html_body)
                mail.send(msg)
                p.reminder_sent_at = datetime.utcnow()
                db.session.commit()
            except Exception:
                pass

    threading.Thread(target=do_send, daemon=True).start()


def auto_send_payment_reminders(user_id):
    """Send payment reminders for invoices sent 7+ days ago that are still unpaid."""
    cutoff = datetime.utcnow() - timedelta(days=7)
    unpaid = Job.query.filter(
        Job.user_id == user_id,
        Job.invoice_sent == True,
        Job.invoice_sent_at <= cutoff,
        Job.paid_at == None,
        Job.payment_reminder_sent_at == None,
        Job.client_email != '',
        Job.client_email != None,
    ).all()
    for j in unpaid:
        send_payment_reminder(j.id, user_id)


def send_payment_reminder(job_id, user_id):
    """Email client a polite reminder that their invoice is still outstanding."""
    if not app.config.get('MAIL_USERNAME'):
        return

    def do_send():
        with app.app_context():
            j = Job.query.get(job_id)
            u = User.query.get(user_id)
            if not j or not u or not j.client_email:
                return
            if j.paid_at or j.payment_reminder_sent_at:
                return
            company = u.company_name or u.name
            invoice_num = f"INV-{j.id:04d}"
            bc = u.effective_brand_color
            base_url = os.getenv('APP_URL', '').rstrip('/')
            pay_url = f"{base_url}/invoice/{j.pay_token}" if base_url and j.pay_token and stripe.api_key else None
            pay_btn = f'<p style="text-align:center;margin:20px 0;"><a href="{pay_url}" style="display:inline-block;background:{bc};color:#fff;font-size:14px;font-weight:700;padding:12px 28px;text-decoration:none;">PAY NOW →</a></p>' if pay_url else ''
            subject = f"Reminder: Invoice {invoice_num} is still outstanding — {company}"
            html = f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px;">
<table width="600" cellpadding="0" cellspacing="0" style="background:#fff;max-width:600px;">
<tr><td style="background:{bc};padding:14px 32px;"><span style="font-size:18px;font-weight:800;color:#fff;">{company}</span></td></tr>
<tr><td style="padding:32px;">
  <p style="font-size:16px;font-weight:700;color:#111;margin-bottom:8px;">Hi {j.client_name},</p>
  <p style="font-size:14px;color:#444;line-height:1.6;margin-bottom:20px;">
    Just a friendly reminder that invoice <strong>{invoice_num}</strong> for <strong>{j.job_type or 'your recent job'}</strong>
    is still outstanding. The total due is <strong>${j.revenue:,.2f}</strong>.
  </p>
  {pay_btn}
  <p style="font-size:13px;color:#666;margin-top:20px;">
    If you have any questions or have already sent payment, please disregard this message or reply to let us know.
  </p>
  <p style="font-size:13px;color:#666;margin-top:16px;">Thanks,<br /><strong>{u.name}</strong><br />{company}</p>
</td></tr>
<tr><td style="background:#1a1a1a;padding:12px 32px;text-align:center;">
  <p style="font-size:10px;color:#666;margin:0;">{company}</p>
</td></tr>
</table></td></tr></table>
</body></html>"""
            try:
                from flask_mail import Message as MailMessage
                msg = MailMessage(subject=subject, recipients=[j.client_email], html=html)
                mail.send(msg)
                j.payment_reminder_sent_at = datetime.utcnow()
                db.session.commit()
            except Exception:
                pass

    threading.Thread(target=do_send, daemon=True).start()


def auto_send_review_requests(user_id):
    """Send review request emails for jobs completed 1+ day ago."""
    cutoff = datetime.utcnow() - timedelta(days=1)
    jobs = Job.query.filter(
        Job.user_id == user_id,
        Job.created_at <= cutoff,
        Job.review_sent_at == None,
        Job.client_email != '',
        Job.client_email != None,
    ).all()
    for j in jobs:
        send_review_request(j.id, user_id)


def send_review_request(job_id, user_id):
    """Send a Google review request to the client after job completion."""
    if not app.config.get('MAIL_USERNAME'):
        return

    def do_send():
        with app.app_context():
            j = Job.query.get(job_id)
            u = User.query.get(user_id)
            if not j or not u or not j.client_email:
                return
            if j.review_sent_at:
                return
            company = u.company_name or u.name
            bc = u.effective_brand_color
            # Build Google review search link
            search_query = company.replace(' ', '+')
            review_url = f"https://search.google.com/local/writereview?placeid=&query={search_query}"
            subject = f"How did we do? — {company}"
            html = f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px;">
<table width="600" cellpadding="0" cellspacing="0" style="background:#fff;max-width:600px;">
<tr><td style="background:{bc};padding:14px 32px;"><span style="font-size:18px;font-weight:800;color:#fff;">{company}</span></td></tr>
<tr><td style="padding:32px;text-align:center;">
  <p style="font-size:28px;margin-bottom:8px;">⭐⭐⭐⭐⭐</p>
  <p style="font-size:18px;font-weight:700;color:#111;margin-bottom:12px;">How did we do, {j.client_name.split()[0]}?</p>
  <p style="font-size:14px;color:#444;line-height:1.6;margin-bottom:24px;text-align:left;">
    It was great working on your {j.job_type or 'project'}. If you were happy with the work, we'd really appreciate
    a quick Google review — it only takes 30 seconds and means the world to a small business.
  </p>
  <a href="{review_url}" style="display:inline-block;background:{bc};color:#fff;font-size:15px;font-weight:700;padding:14px 32px;text-decoration:none;border-radius:2px;">Leave a Google Review →</a>
  <p style="font-size:12px;color:#999;margin-top:24px;text-align:left;">
    Not satisfied? Please reply to this email and let us know how we can make it right.
  </p>
  <p style="font-size:13px;color:#666;margin-top:16px;text-align:left;">Thank you,<br /><strong>{u.name}</strong><br />{company}</p>
</td></tr>
<tr><td style="background:#1a1a1a;padding:12px 32px;text-align:center;">
  <p style="font-size:10px;color:#666;margin:0;">{company}</p>
</td></tr>
</table></td></tr></table>
</body></html>"""
            try:
                from flask_mail import Message as MailMessage
                msg = MailMessage(subject=subject, recipients=[j.client_email], html=html)
                mail.send(msg)
                j.review_sent_at = datetime.utcnow()
                db.session.commit()
            except Exception:
                pass

    threading.Thread(target=do_send, daemon=True).start()


def generate_invoice_pdf(job, user):
    """Generate a PDF invoice and return bytes."""
    company = user.company_name or user.name
    invoice_num = f"INV-{job.id:04d}"
    completed = job.completed_date.strftime('%B %d, %Y')

    pdf = FPDF()
    pdf.set_margins(18, 18, 18)
    pdf.add_page()
    W = pdf.w - 36  # usable width

    # ── Dark header bar ────────────────────────────────────────────────────────
    pdf.set_fill_color(26, 26, 26)
    pdf.rect(0, 0, pdf.w, 32, 'F')
    pdf.set_y(9)
    pdf.set_font('Helvetica', 'B', 15)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(W / 2, 7, company[:40], ln=False)
    pdf.set_font('Helvetica', '', 9)
    pdf.set_text_color(150, 150, 150)
    contact_right = f"{user.phone or ''}  {user.email}"
    pdf.cell(W / 2, 7, contact_right.strip(), align='R', ln=True)
    pdf.set_font('Helvetica', '', 9)
    trade_str = user.trade_type + (f" · Lic #{user.license_number}" if user.license_number else "")
    pdf.set_x(18)
    pdf.cell(W, 5, trade_str, ln=True)

    # ── Orange title bar ───────────────────────────────────────────────────────
    pdf.set_fill_color(249, 115, 22)
    pdf.rect(0, 32, pdf.w, 14, 'F')
    pdf.set_y(35)
    pdf.set_x(18)
    pdf.set_font('Helvetica', 'B', 13)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(W / 2, 7, 'INVOICE', ln=False)
    pdf.cell(W / 2, 7, invoice_num, align='R', ln=True)

    pdf.set_y(54)
    pdf.set_text_color(17, 17, 17)

    # ── Info boxes ─────────────────────────────────────────────────────────────
    col_w = (W - 6) / 2
    box_y = pdf.get_y()

    # Billed To
    pdf.set_fill_color(249, 249, 249)
    pdf.rect(18, box_y, col_w, 36, 'F')
    pdf.set_draw_color(220, 220, 220)
    pdf.rect(18, box_y, col_w, 36)
    pdf.set_xy(20, box_y + 3)
    pdf.set_font('Helvetica', 'B', 8)
    pdf.set_text_color(249, 115, 22)
    pdf.cell(col_w - 4, 5, 'BILLED TO', ln=True)
    pdf.set_x(20)
    pdf.set_font('Helvetica', 'B', 10)
    pdf.set_text_color(17, 17, 17)
    pdf.cell(col_w - 4, 6, job.client_name[:35], ln=True)
    pdf.set_font('Helvetica', '', 9)
    pdf.set_text_color(80, 80, 80)
    if job.client_email:
        pdf.set_x(20); pdf.cell(col_w - 4, 5, job.client_email[:40], ln=True)
    if job.client_phone:
        pdf.set_x(20); pdf.cell(col_w - 4, 5, job.client_phone, ln=True)

    # Invoice Details
    dx = 18 + col_w + 6
    pdf.set_fill_color(249, 249, 249)
    pdf.rect(dx, box_y, col_w, 36, 'F')
    pdf.rect(dx, box_y, col_w, 36)
    pdf.set_xy(dx + 2, box_y + 3)
    pdf.set_font('Helvetica', 'B', 8)
    pdf.set_text_color(249, 115, 22)
    pdf.cell(col_w - 4, 5, 'INVOICE DETAILS', ln=True)
    pdf.set_text_color(80, 80, 80)
    pdf.set_font('Helvetica', '', 9)
    for label, val in [('Invoice #', invoice_num), ('Job Type', job.job_type or 'Services'), ('Completed', completed)]:
        pdf.set_x(dx + 2)
        pdf.set_font('Helvetica', 'B', 9); pdf.cell(24, 5, label, ln=False)
        pdf.set_font('Helvetica', '', 9); pdf.cell(col_w - 28, 5, str(val)[:30], ln=True)

    pdf.set_y(box_y + 40)

    # ── Description / Completion Summary ───────────────────────────────────────
    desc_text = (job.completion_summary or job.description or '').strip()
    if desc_text:
        pdf.set_font('Helvetica', '', 10)
        pdf.set_text_color(60, 60, 60)
        pdf.multi_cell(W, 5, desc_text[:500], ln=True)
        pdf.ln(4)

    # ── Line items table ───────────────────────────────────────────────────────
    pdf.set_fill_color(245, 245, 245)
    pdf.rect(18, pdf.get_y(), W, 10, 'F')
    pdf.set_font('Helvetica', 'B', 9)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(W * 0.7, 10, 'DESCRIPTION', ln=False, border='B')
    pdf.cell(W * 0.3, 10, 'AMOUNT', align='R', ln=True, border='B')

    pdf.set_font('Helvetica', '', 10)
    pdf.set_text_color(50, 50, 50)
    pdf.cell(W * 0.7, 9, job.job_type or 'Professional Services', ln=False, border='B')
    pdf.cell(W * 0.3, 9, f'${job.revenue:,.2f}', align='R', ln=True, border='B')

    # Total row
    pdf.set_fill_color(249, 115, 22)
    pdf.rect(18, pdf.get_y(), W, 12, 'F')
    pdf.set_font('Helvetica', 'B', 11)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(W * 0.7, 12, 'TOTAL DUE', ln=False)
    pdf.cell(W * 0.3, 12, f'${job.revenue:,.2f}', align='R', ln=True)

    pdf.ln(6)

    if job.notes:
        pdf.set_font('Helvetica', 'I', 9)
        pdf.set_text_color(120, 120, 120)
        pdf.multi_cell(W, 5, f'Notes: {job.notes[:200]}')

    # ── Footer ─────────────────────────────────────────────────────────────────
    pdf.set_y(-20)
    pdf.set_fill_color(26, 26, 26)
    pdf.rect(0, pdf.get_y(), pdf.w, 20, 'F')
    pdf.set_font('Helvetica', '', 9)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(W, 10, f'Thank you for your business · {company}', align='C', ln=True)

    return bytes(pdf.output())


def _build_mime_invoice(job, user, html_body, pdf_bytes):
    """Build a MIME message with HTML body and PDF attachment."""
    company = user.company_name or user.name
    invoice_num = f"INV-{job.id:04d}"
    subject = f"Invoice {invoice_num} — {company}"

    msg = MIMEMultipart('mixed')
    msg['From'] = app.config.get('MAIL_USERNAME', '')
    msg['To'] = job.client_email
    msg['Subject'] = subject
    msg['Date'] = email.utils.formatdate(time.time())

    alt = MIMEMultipart('alternative')
    alt.attach(MIMEText(html_body, 'html'))
    msg.attach(alt)

    pdf_part = MIMEApplication(pdf_bytes, _subtype='pdf')
    pdf_part.add_header('Content-Disposition', 'attachment', filename=f'{invoice_num}.pdf')
    msg.attach(pdf_part)

    return msg, subject


def _build_invoice_html(job, user, pay_url=None):
    company = user.company_name or user.name
    invoice_num = f"INV-{job.id:04d}"
    completed = job.completed_date.strftime('%B %d, %Y')
    bc = user.effective_brand_color
    logo_html = f'<img src="{user.logo_url}" alt="{company}" style="max-height:40px;max-width:160px;object-fit:contain;" />' if user.logo_url else f'<div style="font-size:18px;font-weight:700;color:#fff;">{company}</div>'
    pay_btn = f'<p style="text-align:center;margin:24px 0 8px;"><a href="{pay_url}" style="display:inline-block;background:{bc};color:#fff;font-size:15px;font-weight:800;padding:14px 36px;text-decoration:none;letter-spacing:0.05em;border-radius:2px;">PAY ONLINE →</a></p><p style="text-align:center;font-size:11px;color:#999;margin-bottom:20px;">Secure payment powered by Stripe</p>' if pay_url else ''
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:'Helvetica Neue',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:40px 20px;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="background:#fff;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
<tr><td style="background:#1a1a1a;padding:20px 32px;">
  <table width="100%"><tr>
    <td>{logo_html}
        <div style="font-size:11px;color:#888;margin-top:4px;">{user.trade_type}{' · Lic #'+user.license_number if user.license_number else ''}</div></td>
    <td style="text-align:right;"><div style="font-size:11px;color:#888;">{user.phone or ''}</div>
        <div style="font-size:11px;color:#888;">{user.email}</div></td>
  </tr></table>
</td></tr>
<tr><td style="background:{bc};padding:14px 32px;">
  <span style="font-size:18px;font-weight:800;color:#fff;">INVOICE</span>
  <span style="float:right;font-size:13px;color:rgba(255,255,255,0.9);font-weight:600;">{invoice_num}</span>
</td></tr>
<tr><td style="padding:32px;">
  <table width="100%" style="margin-bottom:24px;"><tr>
    <td width="48%" style="background:#f9f9f9;border:1px solid #eee;padding:14px;vertical-align:top;">
      <div style="font-size:10px;font-weight:700;color:{bc};text-transform:uppercase;margin-bottom:6px;">Billed To</div>
      <div style="font-size:13px;font-weight:700;color:#111;">{job.client_name}</div>
      {'<div style="font-size:11px;color:#555;margin-top:3px;">'+job.client_email+'</div>' if job.client_email else ''}
      {'<div style="font-size:11px;color:#555;">'+job.client_phone+'</div>' if job.client_phone else ''}
    </td>
    <td width="4%"></td>
    <td width="48%" style="background:#f9f9f9;border:1px solid #eee;padding:14px;vertical-align:top;">
      <div style="font-size:10px;font-weight:700;color:{bc};text-transform:uppercase;margin-bottom:6px;">Invoice Details</div>
      <table><tr><td style="font-size:11px;color:#888;padding-right:10px;">Invoice #</td><td style="font-size:11px;font-weight:600;color:#111;">{invoice_num}</td></tr>
      <tr><td style="font-size:11px;color:#888;padding-right:10px;">Job Type</td><td style="font-size:11px;font-weight:600;color:#111;">{job.job_type or 'Services'}</td></tr>
      <tr><td style="font-size:11px;color:#888;padding-right:10px;">Completed</td><td style="font-size:11px;font-weight:600;color:#111;">{completed}</td></tr></table>
    </td>
  </tr></table>
  {'<p style="font-size:12px;color:#444;line-height:1.6;margin-bottom:20px;">'+job.description+'</p>' if job.description else ''}
  <table width="100%" style="border-collapse:collapse;margin-bottom:20px;">
    <tr style="background:#f5f5f5;"><td style="padding:9px 12px;font-size:10px;font-weight:700;color:#555;text-transform:uppercase;border-bottom:1px solid #e0e0e0;">Description</td>
    <td style="padding:9px 12px;font-size:10px;font-weight:700;color:#555;text-transform:uppercase;border-bottom:1px solid #e0e0e0;text-align:right;">Amount</td></tr>
    <tr><td style="padding:12px;font-size:12px;color:#333;border-bottom:1px solid #eee;">{job.job_type or 'Professional Services'}</td>
    <td style="padding:12px;font-size:12px;font-weight:600;color:#111;text-align:right;border-bottom:1px solid #eee;">${job.revenue:,.2f}</td></tr>
    <tr style="background:{bc};"><td style="padding:11px 12px;font-size:13px;font-weight:700;color:#fff;">TOTAL DUE</td>
    <td style="padding:11px 12px;font-size:13px;font-weight:700;color:#fff;text-align:right;">${job.revenue:,.2f}</td></tr>
  </table>
  {'<p style="font-size:11px;color:#888;font-style:italic;">Notes: '+job.notes+'</p>' if job.notes else ''}
  {pay_btn}
  <p style="font-size:12px;color:#666;margin-top:{'8px' if pay_url else '20px'};">Please find your invoice attached as a PDF. Thank you for your business!</p>
</td></tr>
<tr><td style="background:#1a1a1a;padding:14px 32px;text-align:center;">
  <p style="font-size:10px;color:#666;margin:0;">Thank you for your business · {company}</p>
</td></tr>
</table></td></tr></table>
</body></html>"""


def send_invoice_email(job, user):
    """Send invoice email with PDF attachment and save a draft. Returns True on success."""
    if not app.config.get('MAIL_USERNAME') or not job.client_email:
        return False

    # Pass IDs only — ORM objects cannot be used across thread boundaries
    job_id = job.id
    user_id = user.id
    username = app.config.get('MAIL_USERNAME', '')
    password = app.config.get('MAIL_PASSWORD', '')

    def do_send():
        with app.app_context():
            j = Job.query.get(job_id)
            u = User.query.get(user_id)
            if not j or not u:
                return

            # Generate pay token (lazy)
            if not j.pay_token:
                j.pay_token = secrets.token_urlsafe(20)
                db.session.commit()
            base_url = os.getenv('APP_URL', '').rstrip('/')
            pay_url = f"{base_url}/invoice/{j.pay_token}" if base_url and stripe.api_key else None

            # Generate PDF (non-fatal if it fails)
            try:
                pdf_bytes = generate_invoice_pdf(j, u)
            except Exception:
                pdf_bytes = None

            html_body = _build_invoice_html(j, u, pay_url=pay_url)
            invoice_num = f"INV-{j.id:04d}"
            company = u.company_name or u.name
            subject = f"Invoice {invoice_num} — {company}"

            # 1) Send to client
            try:
                flask_msg = Message(subject=subject, recipients=[j.client_email], html=html_body)
                if pdf_bytes:
                    flask_msg.attach(f"{invoice_num}.pdf", 'application/pdf', pdf_bytes)
                mail.send(flask_msg)
                j.invoice_sent = True
                j.invoice_sent_at = datetime.utcnow()
                db.session.commit()
            except Exception:
                pass

            # 2) Save to Gmail Drafts via IMAP
            if pdf_bytes and username and password and 'gmail' in username.lower():
                try:
                    mime_msg, _ = _build_mime_invoice(j, u, html_body, pdf_bytes)
                    imap = imaplib.IMAP4_SSL('imap.gmail.com')
                    imap.login(username, password)
                    imap.append('[Gmail]/Drafts', '\\Draft',
                                imaplib.Time2Internaldate(time.time()), mime_msg.as_bytes())
                    imap.logout()
                except Exception:
                    pass

    threading.Thread(target=do_send, daemon=True).start()
    return True


def push_to_google_calendar(user_id, sj_id):
    """Push a scheduled job to Google Calendar in a background thread."""
    if not GOOGLE_CALENDAR_ENABLED:
        return
    def do_push():
        with app.app_context():
            u = User.query.get(user_id)
            sj = ScheduledJob.query.get(sj_id)
            if not u or not sj or not u.google_access_token:
                return
            try:
                creds = Credentials(
                    token=u.google_access_token,
                    refresh_token=u.google_refresh_token,
                    token_uri='https://oauth2.googleapis.com/token',
                    client_id=os.getenv('GOOGLE_CLIENT_ID'),
                    client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
                )
                service = build('calendar', 'v3', credentials=creds)
                event_body = {
                    'summary': f"{sj.job_type or 'Job'} — {sj.client_name}",
                    'description': sj.description or '',
                    'start': {'date': sj.scheduled_date.isoformat()},
                    'end': {'date': sj.scheduled_date.isoformat()},
                }
                result = service.events().insert(calendarId='primary', body=event_body).execute()
                sj.google_event_id = result.get('id')
                if creds.token != u.google_access_token:
                    u.google_access_token = creds.token
                db.session.commit()
            except Exception:
                pass
    threading.Thread(target=do_push, daemon=True).start()


def delete_from_google_calendar(user_id, event_id):
    """Delete a Google Calendar event in a background thread."""
    if not GOOGLE_CALENDAR_ENABLED or not event_id:
        return
    def do_delete():
        with app.app_context():
            u = User.query.get(user_id)
            if not u or not u.google_access_token:
                return
            try:
                creds = Credentials(
                    token=u.google_access_token,
                    refresh_token=u.google_refresh_token,
                    token_uri='https://oauth2.googleapis.com/token',
                    client_id=os.getenv('GOOGLE_CLIENT_ID'),
                    client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
                )
                service = build('calendar', 'v3', credentials=creds)
                service.events().delete(calendarId='primary', eventId=event_id).execute()
            except Exception:
                pass
    threading.Thread(target=do_delete, daemon=True).start()


def call_claude_for_proposal(job_data, user_data):
    # Build explicit costs text
    explicit_costs = job_data.get('explicit_costs', [])
    if explicit_costs:
        cost_lines = []
        for c in explicit_costs:
            if c['type'] == 'fixed':
                cost_lines.append(f"  - {c['name']}: ${c['amount']:.2f} (fixed)")
            else:
                cost_lines.append(f"  - {c['name']}: ${c['unit_cost']:.2f} per {c['unit']} × {c['quantity']} {c['unit']} = ${c['total']:.2f}")
        costs_section = "Explicit Costs Provided (include ONLY these in materials_list — do NOT add anything else):\n" + '\n'.join(cost_lines)
    else:
        costs_section = "Explicit Costs Provided: None — leave materials_list as an empty array []."

    prompt = f"""You are an expert proposal writer for trade contractors. Generate a detailed, professional job proposal in JSON format.

Contractor:
- Company: {user_data['company_name'] or user_data['name']}
- Trade: {user_data['trade_type']}
- Contact: {user_data['name']}
- Phone: {user_data['phone']}
- License: {user_data['license_number']}

Client & Job:
- Client Name: {job_data['client_name']}
- Job Site: {job_data['client_address']}
- Job Type: {job_data['job_type']}
- Description: {job_data['job_description']}
- Labor: {job_data['labor_hours']} hours at ${job_data['labor_rate']}/hour
- Timeline: {job_data['timeline']}
- Warranty: {job_data['warranty']}
- Special Notes: {job_data['notes']}
{(''.join(f'- {k}: {v}' + chr(10) for k, v in job_data.get('service_details', {}).items())) if job_data.get('service_details') else ''}

{costs_section}

CRITICAL RULES:
1. Only include items in materials_list that are explicitly listed above. Do NOT invent, guess, or add any materials, parts, fees, or costs not provided.
2. If no costs are provided, materials_list must be an empty array.
3. Calculate grand_total as: labor_total + total_materials only.

Return ONLY valid JSON with exactly this structure (no markdown, no extra text):
{{
  "executive_summary": "2-3 sentence professional overview of the project scope",
  "scope_of_work": [
    "Detailed work item 1",
    "Detailed work item 2",
    "..."
  ],
  "materials_list": [
    {{"name": "Item name", "quantity": "amount", "unit": "each/lf/sf/hr", "unit_cost": 0.00, "total": 0.00}},
    ...
  ],
  "labor_description": "Clear description of labor included",
  "labor_hours": {job_data['labor_hours']},
  "labor_rate": {job_data['labor_rate']},
  "labor_total": 0.00,
  "total_materials": 0.00,
  "subtotal": 0.00,
  "grand_total": 0.00,
  "project_timeline": "Specific timeline with milestones",
  "warranty_terms": "Clear warranty statement",
  "payment_terms": "Standard payment schedule (e.g. 50% deposit, balance on completion)",
  "validity_period": "30 days",
  "closing_statement": "Professional 1-2 sentence closing",
  "call_to_action": "Clear next step for client to accept"
}}

Calculate all totals accurately. Be professional and specific."""

    api_key = ''.join(os.getenv('ANTHROPIC_API_KEY', '').split())
    response = http_requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        },
        json={
            'model': 'claude-sonnet-4-6',
            'max_tokens': 4000,
            'messages': [{'role': 'user', 'content': prompt}],
        },
        timeout=90,
    )
    response.raise_for_status()
    raw = response.json()['content'][0]['text'].strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        name = request.form.get('name', '').strip()
        company = request.form.get('company_name', '').strip()
        trade = request.form.get('trade_type', 'General Contractor')
        password = request.form.get('password', '')

        if User.query.filter_by(email=email).first():
            flash('An account with that email already exists.', 'error')
            return redirect(url_for('register'))

        hashed = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(email=email, name=name, company_name=company,
                    trade_type=trade, password=hashed)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        # Apply any pending promo from session
        pending = session.pop('pending_promo', None)
        if pending:
            _apply_promo(pending, user)
        return redirect(url_for('onboarding'))
    return render_template('register.html')


@app.route('/onboarding', methods=['GET', 'POST'])
@login_required
def onboarding():
    if request.method == 'POST':
        selected = request.form.getlist('specialties')
        current_user.specialty = ','.join(selected)
        current_user.onboarding_done = True
        current_user.trade_type = selected[0] if selected else current_user.trade_type
        db.session.commit()
        if current_user.is_subscribed:
            flash(f'Welcome, {current_user.name}! Your {current_user.plan_display} access is active.', 'success')
            return redirect(url_for('dashboard'))
        flash(f'Welcome, {current_user.name}! Pick a plan to start your 30-day free trial — no charge until day 31.', 'success')
        return redirect(url_for('pricing'))
    return render_template('onboarding.html', specialties=list(SPECIALTIES.keys()))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = User.query.filter_by(email=email).first()
        if user and bcrypt.check_password_hash(user.password, password):
            login_user(user)
            next_page = request.args.get('next')
            return redirect(next_page or url_for('dashboard'))
        flash('Invalid email or password.', 'error')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


@app.route('/dashboard')
@login_required
def dashboard():
    auto_invoice_past_scheduled_jobs(uid())
    auto_send_followup_reminders(uid())
    auto_send_payment_reminders(uid())
    auto_send_review_requests(uid())
    today = date.today()

    proposals = Proposal.query.filter_by(user_id=uid())\
        .order_by(Proposal.created_at.desc()).all()

    # Revenue this month
    month_start = date(today.year, today.month, 1)
    revenue_mtd = db.session.query(db.func.sum(Job.revenue)).filter(
        Job.user_id == uid(),
        Job.completed_date >= month_start,
        Job.completed_date <= today,
    ).scalar() or 0

    # Upcoming scheduled jobs
    upcoming_jobs = ScheduledJob.query.filter(
        ScheduledJob.user_id == uid(),
        ScheduledJob.status == 'scheduled',
        ScheduledJob.scheduled_date >= today,
    ).order_by(ScheduledJob.scheduled_date).limit(8).all()

    today_jobs = [j for j in upcoming_jobs if j.scheduled_date == today]

    clients_count = Client.query.filter_by(user_id=uid()).count()

    # Pipeline: sum of sent proposals
    sent_proposals = [p for p in proposals if p.status == 'sent']
    pipeline_value = sum(p.grand_total for p in sent_proposals)

    # Win rate analytics
    closed = [p for p in proposals if p.status in ('accepted', 'declined', 'sent')]
    accepted = [p for p in proposals if p.status == 'accepted']
    win_rate = round(len(accepted) / len(closed) * 100) if closed else 0

    # Win rate by job type
    job_type_stats = {}
    for p in proposals:
        if not p.job_type or p.status == 'draft':
            continue
        jt = p.job_type
        if jt not in job_type_stats:
            job_type_stats[jt] = {'sent': 0, 'accepted': 0}
        if p.status in ('sent', 'accepted', 'declined'):
            job_type_stats[jt]['sent'] += 1
        if p.status == 'accepted':
            job_type_stats[jt]['accepted'] += 1
    for jt in job_type_stats:
        s = job_type_stats[jt]
        s['rate'] = round(s['accepted'] / s['sent'] * 100) if s['sent'] else 0
    job_type_stats = sorted(job_type_stats.items(), key=lambda x: x[1]['rate'], reverse=True)

    # Avg deal size
    avg_deal = round(sum(p.grand_total for p in accepted) / len(accepted)) if accepted else 0

    # Top clients by revenue
    client_revenue = {}
    for j in Job.query.filter_by(user_id=uid()).all():
        if j.client_name:
            client_revenue[j.client_name] = client_revenue.get(j.client_name, 0) + j.revenue
    top_clients = sorted(client_revenue.items(), key=lambda x: x[1], reverse=True)[:5]

    # Incoming job requests
    new_requests = JobRequest.query.filter_by(user_id=uid(), status='new').count()

    return render_template('dashboard.html',
        proposals=proposals,
        upcoming_jobs=upcoming_jobs,
        today_jobs=today_jobs,
        revenue_mtd=revenue_mtd,
        clients_count=clients_count,
        sent_proposals=sent_proposals,
        pipeline_value=pipeline_value,
        today=today,
        win_rate=win_rate,
        avg_deal=avg_deal,
        job_type_stats=job_type_stats,
        top_clients=top_clients,
        new_requests=new_requests,
    )


@app.route('/cost-templates/add', methods=['POST'])
@login_required
def add_cost_template():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Name is required.', 'error')
        return redirect(url_for('generate'))
    ct = CostTemplate(
        user_id=uid(),
        name=name,
        cost_type=request.form.get('cost_type', 'fixed'),
        amount=float(request.form.get('amount', 0) or 0),
        unit=request.form.get('unit', '').strip(),
    )
    db.session.add(ct)
    db.session.commit()
    flash(f'Saved cost "{name}" added.', 'success')
    return redirect(url_for('generate'))


@app.route('/cost-templates/<int:ct_id>/delete', methods=['POST'])
@login_required
def delete_cost_template(ct_id):
    ct = CostTemplate.query.get_or_404(ct_id)
    if ct.user_id != uid():
        flash('Access denied.', 'error')
        return redirect(url_for('generate'))
    db.session.delete(ct)
    db.session.commit()
    flash('Saved cost removed.', 'success')
    return redirect(url_for('generate'))


@app.route('/api/scope-writer', methods=['POST'])
@login_required
def scope_writer():
    data = request.get_json()
    job_type = data.get('job_type', '').strip()
    quick_desc = data.get('quick_description', '').strip()
    if not quick_desc:
        return jsonify({'error': 'No description provided'}), 400

    cost_templates = CostTemplate.query.filter_by(user_id=uid()).order_by(CostTemplate.created_at).all()
    templates_text = ''
    if cost_templates:
        lines = []
        for ct in cost_templates:
            if ct.cost_type == 'fixed':
                lines.append(f'  ID {ct.id}: "{ct.name}" — ${ct.amount:.2f} fixed')
            else:
                lines.append(f'  ID {ct.id}: "{ct.name}" — ${ct.amount:.2f} per {ct.unit}')
        templates_text = 'Contractor\'s saved cost templates:\n' + '\n'.join(lines)
    else:
        templates_text = 'Contractor has no saved cost templates.'

    prompt = f"""You are an expert trade contractor proposal writer. A contractor has described a job in a few words. Expand it into professional proposal content.

Job Type: {job_type or 'General Trade Work'}
Contractor's quick description: "{quick_desc}"

{templates_text}

Return ONLY valid JSON with exactly this structure (no markdown, no extra text):
{{
  "job_description": "A 3-5 sentence professional description of the full scope of work, written from the contractor's perspective. Be specific, technical, and professional. Mention what will be removed/replaced/installed and any relevant conditions.",
  "materials_notes": "A concise comma-separated list of the key materials/parts needed for this job. Keep it brief — 1-2 lines max.",
  "suggested_template_ids": [list of integer IDs from the saved cost templates above that are clearly relevant to this job — empty array if none match],
  "labor_hours_estimate": a single number (integer or float) for estimated labor hours for this job type
}}"""

    api_key = ''.join(os.getenv('ANTHROPIC_API_KEY', '').split())
    try:
        resp = http_requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': 'claude-sonnet-4-6',
                'max_tokens': 800,
                'messages': [{'role': 'user', 'content': prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json()['content'][0]['text'].strip()
        if raw.startswith('```'):
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
        result = json.loads(raw)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/generate', methods=['GET', 'POST'])
@login_required
def generate():
    cost_templates = CostTemplate.query.filter_by(user_id=uid()).order_by(CostTemplate.created_at).all()

    if not current_user.can_generate:
        flash('Choose a plan to start generating proposals.', 'warning')
        return redirect(url_for('pricing'))

    if request.method == 'POST':
        service_details = {}
        for key in request.form:
            if key.startswith('svc_'):
                val = request.form.get(key, '').strip()
                if val:
                    label = key[4:].replace('_', ' ').title()
                    service_details[label] = val

        # Collect explicit costs — saved templates (checked) + ad-hoc rows
        explicit_costs = []

        for ct in cost_templates:
            if request.form.get(f'ct_{ct.id}'):
                if ct.cost_type == 'fixed':
                    explicit_costs.append({
                        'name': ct.name,
                        'type': 'fixed',
                        'amount': ct.amount,
                    })
                else:
                    qty = float(request.form.get(f'ct_{ct.id}_qty', 0) or 0)
                    explicit_costs.append({
                        'name': ct.name,
                        'type': 'per_unit',
                        'unit': ct.unit,
                        'unit_cost': ct.amount,
                        'quantity': qty,
                        'total': round(ct.amount * qty, 2),
                    })

        adhoc_names   = request.form.getlist('adhoc_name[]')
        adhoc_types   = request.form.getlist('adhoc_type[]')
        adhoc_amounts = request.form.getlist('adhoc_amount[]')
        adhoc_units   = request.form.getlist('adhoc_unit[]')
        adhoc_qtys    = request.form.getlist('adhoc_qty[]')
        adhoc_saves   = request.form.getlist('adhoc_save[]')

        for i, name in enumerate(adhoc_names):
            name = name.strip()
            if not name:
                continue
            cost_type = adhoc_types[i] if i < len(adhoc_types) else 'fixed'
            amount = float(adhoc_amounts[i]) if i < len(adhoc_amounts) and adhoc_amounts[i] else 0
            if cost_type == 'per_unit':
                unit = adhoc_units[i] if i < len(adhoc_units) else ''
                qty = float(adhoc_qtys[i]) if i < len(adhoc_qtys) and adhoc_qtys[i] else 0
                explicit_costs.append({
                    'name': name,
                    'type': 'per_unit',
                    'unit': unit,
                    'unit_cost': amount,
                    'quantity': qty,
                    'total': round(amount * qty, 2),
                })
            else:
                explicit_costs.append({'name': name, 'type': 'fixed', 'amount': amount})

            # Save as template if requested
            if str(i) in adhoc_saves:
                new_ct = CostTemplate(
                    user_id=uid(),
                    name=name,
                    cost_type=cost_type,
                    amount=amount,
                    unit=adhoc_units[i] if cost_type == 'per_unit' and i < len(adhoc_units) else '',
                )
                db.session.add(new_ct)

        db.session.commit()

        job_data = {
            'client_name': request.form.get('client_name', '').strip(),
            'client_email': request.form.get('client_email', '').strip(),
            'client_phone': request.form.get('client_phone', '').strip(),
            'client_address': request.form.get('client_address', '').strip(),
            'job_type': request.form.get('job_type', '').strip(),
            'job_description': request.form.get('job_description', '').strip(),
            'materials_input': request.form.get('materials_input', '').strip(),
            'labor_hours': float(request.form.get('labor_hours', 0) or 0),
            'labor_rate': float(request.form.get('labor_rate', 75) or 75),
            'timeline': request.form.get('timeline', '').strip(),
            'warranty': request.form.get('warranty', '').strip(),
            'notes': request.form.get('notes', '').strip(),
            'service_details': service_details,
            'explicit_costs': explicit_costs,
        }

        user_data = {
            'name': current_user.name,
            'company_name': current_user.company_name,
            'trade_type': current_user.trade_type,
            'phone': current_user.phone,
            'license_number': current_user.license_number,
        }

        try:
            content = call_claude_for_proposal(job_data, user_data)
        except Exception as e:
            flash(f'Error generating proposal: {str(e)}', 'error')
            clients = Client.query.filter_by(user_id=uid()).order_by(Client.name).all()
            return render_template('generate.html', form_data=job_data, cost_templates=cost_templates, allowed_job_types=current_user.allowed_job_types, clients=clients)

        # Auto-upsert client in CRM — find by email, then name, else create new
        client_id_form = request.form.get('client_id', '').strip()
        linked_client_id = int(client_id_form) if client_id_form.isdigit() else None
        if not linked_client_id and job_data['client_name']:
            existing = None
            if job_data['client_email']:
                existing = Client.query.filter_by(
                    user_id=uid(), email=job_data['client_email']
                ).first()
            if not existing:
                existing = Client.query.filter(
                    Client.user_id == uid(),
                    db.func.lower(Client.name) == job_data['client_name'].lower()
                ).first()
            if existing:
                linked_client_id = existing.id
                if job_data['client_email'] and not existing.email:
                    existing.email = job_data['client_email']
                if job_data['client_phone'] and not existing.phone:
                    existing.phone = job_data['client_phone']
                if job_data['client_address'] and not existing.address:
                    existing.address = job_data['client_address']
            else:
                new_client = Client(
                    user_id=uid(),
                    name=job_data['client_name'],
                    email=job_data['client_email'],
                    phone=job_data['client_phone'],
                    address=job_data['client_address'],
                )
                db.session.add(new_client)
                db.session.flush()
                linked_client_id = new_client.id

        tax_rate = float(request.form.get('tax_rate', 0) or 0)
        deposit_pct = float(request.form.get('deposit_pct', 0) or 0)
        tmpl_id = request.form.get('template_id') or None

        proposal = Proposal(
            user_id=uid(),
            client_id=linked_client_id,
            proposal_number=generate_proposal_number(),
            client_name=job_data['client_name'],
            client_email=job_data['client_email'],
            client_phone=job_data['client_phone'],
            client_address=job_data['client_address'],
            job_type=job_data['job_type'],
            job_description=job_data['job_description'],
            materials_input=job_data['materials_input'],
            labor_hours=job_data['labor_hours'],
            labor_rate=job_data['labor_rate'],
            timeline=job_data['timeline'],
            warranty=job_data['warranty'],
            notes=job_data['notes'],
            generated_content=json.dumps(content),
            grand_total=content.get('grand_total', 0),
            tax_rate=tax_rate,
            deposit_pct=deposit_pct,
            template_id=int(tmpl_id) if tmpl_id else None,
            status='draft',
            public_token=secrets.token_urlsafe(20),
        )
        db.session.add(proposal)
        db.session.commit()
        return redirect(url_for('view_proposal', proposal_id=proposal.id))

    clients = Client.query.filter_by(user_id=uid()).order_by(Client.name).all()
    templates = ProposalTemplate.query.filter_by(user_id=uid()).order_by(ProposalTemplate.name).all()
    return render_template('generate.html', form_data={}, cost_templates=cost_templates,
                           allowed_job_types=current_user.allowed_job_types, clients=clients,
                           templates=templates)


@app.route('/proposal/<int:proposal_id>')
@login_required
def view_proposal(proposal_id):
    proposal = Proposal.query.get_or_404(proposal_id)
    if proposal.user_id != uid():
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
    if not proposal.public_token:
        proposal.public_token = secrets.token_urlsafe(20)
        db.session.commit()
    return render_template('proposal_view.html', proposal=proposal, user=current_user)


@app.route('/proposal/<int:proposal_id>/status', methods=['POST'])
@login_required
def update_status(proposal_id):
    proposal = Proposal.query.get_or_404(proposal_id)
    if proposal.user_id != uid():
        return jsonify({'error': 'Access denied'}), 403
    new_status = request.json.get('status')
    if new_status in ('draft', 'sent', 'accepted', 'declined'):
        proposal.status = new_status
        db.session.commit()
    return jsonify({'status': proposal.status})


@app.route('/proposal/<int:proposal_id>/delete', methods=['POST'])
@login_required
def delete_proposal(proposal_id):
    proposal = Proposal.query.get_or_404(proposal_id)
    if proposal.user_id != uid():
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
    db.session.delete(proposal)
    db.session.commit()
    flash('Proposal deleted.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/proposal/<int:proposal_id>/nudge', methods=['POST'])
@login_required
def nudge_proposal(proposal_id):
    proposal = Proposal.query.get_or_404(proposal_id)
    if proposal.user_id != uid():
        return jsonify({'error': 'Access denied'}), 403
    if not proposal.client_email:
        return jsonify({'error': 'No client email'}), 400
    # Reset so it sends even if auto already fired
    proposal.reminder_sent_at = None
    db.session.commit()
    send_followup_reminder(proposal.id, uid())
    return jsonify({'ok': True})


@app.route('/proposal/<int:proposal_id>/close')
@login_required
def close_job_from_proposal(proposal_id):
    proposal = Proposal.query.get_or_404(proposal_id)
    if proposal.user_id != uid():
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
    return render_template('close_job.html', proposal=proposal, today=date.today().isoformat())


@app.route('/p/<token>')
def public_proposal(token):
    proposal = Proposal.query.filter_by(public_token=token).first_or_404()
    user = User.query.get(proposal.user_id)
    return render_template('client_proposal.html', proposal=proposal, user=user)


@app.route('/p/<token>/respond', methods=['POST'])
def public_proposal_respond(token):
    proposal = Proposal.query.filter_by(public_token=token).first_or_404()
    action = request.form.get('action')
    if action not in ('accepted', 'declined'):
        return redirect(url_for('public_proposal', token=token))
    if proposal.status in ('accepted', 'declined'):
        return redirect(url_for('public_proposal', token=token))
    proposal.status = action
    new_sj_id = None
    if action == 'accepted':
        proposal.accepted_at = datetime.utcnow()
        sched_date = parse_timeline_date(proposal.timeline, date.today())
        sj = ScheduledJob(
            user_id=proposal.user_id,
            client_id=proposal.client_id,
            client_name=proposal.client_name,
            client_email=proposal.client_email,
            client_phone=proposal.client_phone,
            job_type=proposal.job_type,
            description=proposal.job_description,
            scheduled_date=sched_date,
            estimated_revenue=proposal.grand_total,
            invoice_on_complete=True,
            notes=f"From proposal {proposal.proposal_number}",
        )
        db.session.add(sj)
        db.session.flush()
        new_sj_id = sj.id
    db.session.commit()
    if new_sj_id:
        push_to_google_calendar(proposal.user_id, new_sj_id)
    user = User.query.get(proposal.user_id)
    # Notify contractor
    def notify():
        with app.app_context():
            p = Proposal.query.filter_by(public_token=token).first()
            u = User.query.get(p.user_id)
            if not u or not u.email:
                return
            verb = 'ACCEPTED' if action == 'accepted' else 'DECLINED'
            subject = f"Proposal {p.proposal_number} {verb} by {p.client_name}"
            body = f"""<p>Hi {u.name},</p>
<p><strong>{p.client_name}</strong> has <strong>{verb}</strong> proposal <strong>{p.proposal_number}</strong>
for <strong>${p.grand_total:,.2f}</strong>.</p>
{'<p>Time to get to work!</p>' if action == 'accepted' else '<p>Consider following up to address any concerns.</p>'}
<p style="color:#888;font-size:12px;">— CloseTheJob</p>"""
            try:
                msg = Message(subject=subject, recipients=[u.email], html=body)
                mail.send(msg)
            except Exception:
                pass
    threading.Thread(target=notify, daemon=True).start()
    return redirect(url_for('public_proposal', token=token))


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        current_user.name = request.form.get('name', '').strip()
        current_user.company_name = request.form.get('company_name', '').strip()
        current_user.trade_type = request.form.get('trade_type', '').strip()
        current_user.phone = request.form.get('phone', '').strip()
        current_user.license_number = request.form.get('license_number', '').strip()
        current_user.address = request.form.get('address', '').strip()
        current_user.website = request.form.get('website', '').strip()
        if current_user.can_use_branding:
            current_user.logo_url = request.form.get('logo_url', '').strip() or None
            color = request.form.get('brand_color', '').strip()
            if color and len(color) == 7 and color.startswith('#'):
                current_user.brand_color = color
        # Booking page settings
        new_slug = request.form.get('booking_slug', '').strip().lower()
        if new_slug:
            import re
            new_slug = re.sub(r'[^a-z0-9-]', '-', new_slug)
            # Only save if unique or unchanged
            existing = User.query.filter(User.booking_slug == new_slug, User.id != current_user.id).first()
            if not existing:
                current_user.booking_slug = new_slug
        elif 'booking_slug' in request.form:
            current_user.booking_slug = None
        current_user.booking_enabled = bool(request.form.get('booking_enabled'))
        db.session.commit()
        flash('Profile updated successfully.', 'success')
        return redirect(url_for('profile'))
    google_configured = bool(os.getenv('GOOGLE_CLIENT_ID'))
    return render_template('profile.html', google_configured=google_configured)


@app.route('/pricing')
def pricing():
    return render_template('pricing.html')


# ─── Financials ────────────────────────────────────────────────────────────────

@app.route('/financials')
@login_required
def financials():
    auto_invoice_past_scheduled_jobs(uid())
    auto_send_payment_reminders(uid())
    auto_send_review_requests(uid())
    jobs = Job.query.filter_by(user_id=uid()).order_by(Job.completed_date.desc()).all()
    expenses = Expense.query.filter_by(user_id=uid()).order_by(Expense.date.desc()).all()
    tips = Tip.query.filter_by(user_id=uid()).order_by(Tip.date.desc()).all()
    all_jobs = Job.query.filter_by(user_id=uid()).all()

    total_revenue = sum(j.revenue for j in jobs)
    total_expenses = sum(e.amount for e in expenses)
    total_tips = sum(t.amount for t in tips)
    net_profit = total_revenue + total_tips - total_expenses

    labels, revenues, expenses_monthly, tips_monthly = build_monthly_chart_data(uid())
    chart_data = json.dumps({
        'labels': labels,
        'revenues': revenues,
        'expenses': expenses_monthly,
        'tips': tips_monthly,
    })

    return render_template('financials.html',
        jobs=jobs, expenses=expenses, tips=tips, all_jobs=all_jobs,
        total_revenue=total_revenue, total_expenses=total_expenses,
        total_tips=total_tips, net_profit=net_profit,
        chart_data=chart_data,
        today=date.today().isoformat(),
        can_see_charts=current_user.can_see_charts,
    )


@app.route('/jobs/add', methods=['POST'])
@login_required
def add_job():
    client_name = request.form.get('client_name', '').strip()
    if not client_name:
        flash('Client name is required.', 'error')
        return redirect(url_for('financials'))

    try:
        completed_date = date.fromisoformat(request.form.get('completed_date', ''))
    except ValueError:
        flash('Invalid completion date.', 'error')
        return redirect(url_for('financials'))

    proposal_id = request.form.get('proposal_id') or None
    if proposal_id:
        proposal_id = int(proposal_id)

    job = Job(
        user_id=uid(),
        proposal_id=proposal_id,
        client_name=client_name,
        client_email=request.form.get('client_email', '').strip(),
        client_phone=request.form.get('client_phone', '').strip(),
        job_type=request.form.get('job_type', '').strip(),
        description=request.form.get('description', '').strip(),
        revenue=float(request.form.get('revenue', 0) or 0),
        completed_date=completed_date,
        notes=request.form.get('notes', '').strip(),
    )
    db.session.add(job)
    db.session.commit()

    # Auto-send invoice if requested
    if request.form.get('send_invoice') and job.client_email:
        send_invoice_email(job, current_user)
        flash(f'Job logged. Invoice is being sent to {job.client_email}.', 'success')
    else:
        flash(f'Job "{client_name}" logged successfully.', 'success')

    return redirect(url_for('financials'))


@app.route('/jobs/<int:job_id>/delete', methods=['POST'])
@login_required
def delete_job(job_id):
    job = Job.query.get_or_404(job_id)
    if job.user_id != uid():
        flash('Access denied.', 'error')
        return redirect(url_for('financials'))
    # Delete related expenses and tips first
    Expense.query.filter_by(job_id=job.id).delete()
    Tip.query.filter_by(job_id=job.id).delete()
    db.session.delete(job)
    db.session.commit()
    flash('Job deleted.', 'success')
    return redirect(url_for('financials'))


@app.route('/jobs/<int:job_id>/send-invoice', methods=['POST'])
@login_required
def send_invoice(job_id):
    job = Job.query.get_or_404(job_id)
    if job.user_id != uid():
        flash('Access denied.', 'error')
        return redirect(url_for('financials'))
    if not job.client_email:
        flash('No client email on file for this job.', 'error')
        return redirect(url_for('financials'))
    send_invoice_email(job, current_user)
    flash(f'Invoice is being sent to {job.client_email}.', 'success')
    return redirect(url_for('financials'))


@app.route('/expenses/add', methods=['POST'])
@login_required
def add_expense():
    description = request.form.get('description', '').strip()
    if not description:
        flash('Description is required.', 'error')
        return redirect(url_for('financials'))
    try:
        exp_date = date.fromisoformat(request.form.get('date', ''))
    except ValueError:
        flash('Invalid date.', 'error')
        return redirect(url_for('financials'))

    job_id = request.form.get('job_id') or None
    if job_id:
        job_id = int(job_id)

    expense = Expense(
        user_id=uid(),
        job_id=job_id,
        description=description,
        amount=float(request.form.get('amount', 0) or 0),
        category=request.form.get('category', 'General').strip(),
        date=exp_date,
    )
    db.session.add(expense)
    db.session.commit()
    flash('Expense added.', 'success')
    return redirect(url_for('financials'))


@app.route('/expenses/<int:expense_id>/delete', methods=['POST'])
@login_required
def delete_expense(expense_id):
    expense = Expense.query.get_or_404(expense_id)
    if expense.user_id != uid():
        flash('Access denied.', 'error')
        return redirect(url_for('financials'))
    db.session.delete(expense)
    db.session.commit()
    flash('Expense deleted.', 'success')
    return redirect(url_for('financials'))


@app.route('/tips/add', methods=['POST'])
@login_required
def add_tip():
    try:
        tip_date = date.fromisoformat(request.form.get('date', ''))
    except ValueError:
        flash('Invalid date.', 'error')
        return redirect(url_for('financials'))

    job_id = request.form.get('job_id') or None
    if job_id:
        job_id = int(job_id)

    tip = Tip(
        user_id=uid(),
        job_id=job_id,
        amount=float(request.form.get('amount', 0) or 0),
        note=request.form.get('note', '').strip(),
        date=tip_date,
    )
    db.session.add(tip)
    db.session.commit()
    flash('Tip recorded.', 'success')
    return redirect(url_for('financials'))


@app.route('/tips/<int:tip_id>/delete', methods=['POST'])
@login_required
def delete_tip(tip_id):
    tip = Tip.query.get_or_404(tip_id)
    if tip.user_id != uid():
        flash('Access denied.', 'error')
        return redirect(url_for('financials'))
    db.session.delete(tip)
    db.session.commit()
    flash('Tip deleted.', 'success')
    return redirect(url_for('financials'))


# ─── Schedule ──────────────────────────────────────────────────────────────────

@app.route('/schedule', methods=['GET', 'POST'])
@login_required
def schedule():
    auto_invoice_past_scheduled_jobs(uid())
    if request.method == 'POST':
        client_name = request.form.get('client_name', '').strip()
        if not client_name:
            flash('Client name is required.', 'error')
            return redirect(url_for('schedule'))
        try:
            sched_date = date.fromisoformat(request.form.get('scheduled_date', ''))
        except ValueError:
            flash('Invalid date.', 'error')
            return redirect(url_for('schedule'))
        client_id_raw = request.form.get('client_id', '').strip()
        linked_client_id = int(client_id_raw) if client_id_raw.isdigit() else None
        sj = ScheduledJob(
            user_id=uid(),
            client_id=linked_client_id,
            client_name=client_name,
            client_email=request.form.get('client_email', '').strip(),
            client_phone=request.form.get('client_phone', '').strip(),
            job_type=request.form.get('job_type', '').strip(),
            description=request.form.get('description', '').strip(),
            scheduled_date=sched_date,
            estimated_revenue=float(request.form.get('estimated_revenue', 0) or 0),
            invoice_on_complete=bool(request.form.get('invoice_on_complete')),
            notes=request.form.get('notes', '').strip(),
        )
        db.session.add(sj)
        db.session.commit()
        push_to_google_calendar(uid(), sj.id)
        flash(f'Job scheduled for {sched_date.strftime("%b %d, %Y")}.', 'success')
        return redirect(url_for('schedule'))

    jobs = ScheduledJob.query.filter_by(user_id=uid())\
        .order_by(ScheduledJob.scheduled_date).all()
    upcoming = [j for j in jobs if j.status == 'scheduled']
    past = [j for j in jobs if j.status != 'scheduled']
    clients = Client.query.filter_by(user_id=uid()).order_by(Client.name).all()
    google_connected = bool(current_user.google_access_token)
    return render_template('schedule.html', upcoming=upcoming, past=past, today=date.today().isoformat(), clients=clients, google_connected=google_connected)


@app.route('/schedule/<int:sj_id>/cancel', methods=['POST'])
@login_required
def cancel_scheduled_job(sj_id):
    sj = ScheduledJob.query.get_or_404(sj_id)
    if sj.user_id != uid():
        flash('Access denied.', 'error')
        return redirect(url_for('schedule'))
    if sj.google_event_id:
        delete_from_google_calendar(uid(), sj.google_event_id)
    sj.status = 'cancelled'
    db.session.commit()
    flash('Job cancelled.', 'success')
    return redirect(url_for('schedule'))


@app.route('/schedule/<int:sj_id>/delete', methods=['POST'])
@login_required
def delete_scheduled_job(sj_id):
    sj = ScheduledJob.query.get_or_404(sj_id)
    if sj.user_id != uid():
        flash('Access denied.', 'error')
        return redirect(url_for('schedule'))
    db.session.delete(sj)
    db.session.commit()
    flash('Scheduled job deleted.', 'success')
    return redirect(url_for('schedule'))


# ─── Proposal Templates ────────────────────────────────────────────────────────

@app.route('/proposal/<int:proposal_id>/save-template', methods=['POST'])
@login_required
def save_proposal_template(proposal_id):
    proposal = Proposal.query.get_or_404(proposal_id)
    if proposal.user_id != uid():
        return jsonify({'error': 'Access denied'}), 403
    name = request.json.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    tmpl = ProposalTemplate(
        user_id=uid(),
        name=name,
        job_type=proposal.job_type,
        job_description=proposal.job_description,
        materials_input=proposal.materials_input,
        labor_hours=proposal.labor_hours,
        labor_rate=proposal.labor_rate,
        timeline=proposal.timeline,
        warranty=proposal.warranty,
        notes=proposal.notes,
        tax_rate=proposal.tax_rate,
        deposit_pct=proposal.deposit_pct,
        generated_content=proposal.generated_content,
    )
    db.session.add(tmpl)
    db.session.commit()
    return jsonify({'ok': True, 'id': tmpl.id, 'name': tmpl.name})


@app.route('/templates/<int:tmpl_id>/data')
@login_required
def template_data(tmpl_id):
    tmpl = ProposalTemplate.query.get_or_404(tmpl_id)
    if tmpl.user_id != uid():
        return jsonify({'error': 'Access denied'}), 403
    return jsonify({
        'job_type': tmpl.job_type,
        'job_description': tmpl.job_description,
        'materials_input': tmpl.materials_input,
        'labor_hours': tmpl.labor_hours,
        'labor_rate': tmpl.labor_rate,
        'timeline': tmpl.timeline,
        'warranty': tmpl.warranty,
        'notes': tmpl.notes,
        'tax_rate': tmpl.tax_rate,
        'deposit_pct': tmpl.deposit_pct,
    })


@app.route('/templates/<int:tmpl_id>/delete', methods=['POST'])
@login_required
def delete_template(tmpl_id):
    tmpl = ProposalTemplate.query.get_or_404(tmpl_id)
    if tmpl.user_id != uid():
        flash('Access denied.', 'error')
        return redirect(url_for('generate'))
    db.session.delete(tmpl)
    db.session.commit()
    flash('Template deleted.', 'success')
    return redirect(url_for('generate'))


# ─── Deposits ──────────────────────────────────────────────────────────────────

@app.route('/proposal/<int:proposal_id>/deposit-link')
@login_required
def proposal_deposit_link(proposal_id):
    proposal = Proposal.query.get_or_404(proposal_id)
    if proposal.user_id != uid():
        abort(403)
    if not proposal.deposit_token:
        proposal.deposit_token = secrets.token_urlsafe(20)
        db.session.commit()
    return redirect(url_for('deposit_view', token=proposal.deposit_token))


@app.route('/deposit/<token>')
def deposit_view(token):
    proposal = Proposal.query.filter_by(deposit_token=token).first_or_404()
    user = User.query.get_or_404(proposal.user_id)
    return render_template('deposit_pay.html', proposal=proposal, user=user,
                           stripe_enabled=bool(stripe.api_key))


@app.route('/deposit/<token>/pay', methods=['POST'])
def deposit_pay(token):
    proposal = Proposal.query.filter_by(deposit_token=token).first_or_404()
    if proposal.deposit_paid_at:
        return redirect(url_for('deposit_view', token=token))
    if not stripe.api_key:
        return redirect(url_for('deposit_view', token=token))
    user = User.query.get_or_404(proposal.user_id)
    company = user.company_name or user.name
    amount_cents = max(50, int(round(proposal.deposit_amount * 100)))
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {'name': f"Deposit — {proposal.job_type} — {company}"},
                    'unit_amount': amount_cents,
                },
                'quantity': 1,
            }],
            mode='payment',
            customer_email=proposal.client_email or None,
            success_url=url_for('deposit_success', token=token, _external=True),
            cancel_url=url_for('deposit_view', token=token, _external=True),
            metadata={'proposal_id': proposal.id, 'type': 'deposit'},
        )
        return redirect(session.url)
    except Exception:
        return redirect(url_for('deposit_view', token=token))


@app.route('/deposit/<token>/success')
def deposit_success(token):
    proposal = Proposal.query.filter_by(deposit_token=token).first_or_404()
    user = User.query.get_or_404(proposal.user_id)
    return render_template('deposit_success.html', proposal=proposal, user=user)


# ─── Public Booking Page ───────────────────────────────────────────────────────

@app.route('/book/<slug>')
def booking_page(slug):
    user = User.query.filter_by(booking_slug=slug, booking_enabled=True).first_or_404()
    return render_template('booking.html', contractor=user)


@app.route('/book/<slug>/submit', methods=['POST'])
def booking_submit(slug):
    user = User.query.filter_by(booking_slug=slug, booking_enabled=True).first_or_404()
    client_name = request.form.get('client_name', '').strip()
    if not client_name:
        flash('Please enter your name.', 'error')
        return redirect(url_for('booking_page', slug=slug))
    jr = JobRequest(
        user_id=user.id,
        client_name=client_name,
        client_email=request.form.get('client_email', '').strip(),
        client_phone=request.form.get('client_phone', '').strip(),
        job_type=request.form.get('job_type', '').strip(),
        description=request.form.get('description', '').strip(),
        preferred_date=request.form.get('preferred_date', '').strip(),
    )
    db.session.add(jr)
    db.session.commit()
    # Notify contractor
    if app.config.get('MAIL_USERNAME') and user.email:
        def notify():
            with app.app_context():
                u = User.query.get(user.id)
                r = JobRequest.query.get(jr.id)
                if not u or not r:
                    return
                try:
                    from flask_mail import Message as MailMessage
                    base_url = os.getenv('APP_URL', '').rstrip('/')
                    msg = MailMessage(
                        subject=f"New job request from {r.client_name}",
                        recipients=[u.email],
                        html=f"""<p>You have a new job request from your booking page.</p>
<table style="border-collapse:collapse;width:100%;max-width:500px;">
<tr><td style="padding:8px;border:1px solid #eee;font-weight:600;">Name</td><td style="padding:8px;border:1px solid #eee;">{r.client_name}</td></tr>
<tr><td style="padding:8px;border:1px solid #eee;font-weight:600;">Email</td><td style="padding:8px;border:1px solid #eee;">{r.client_email or '—'}</td></tr>
<tr><td style="padding:8px;border:1px solid #eee;font-weight:600;">Phone</td><td style="padding:8px;border:1px solid #eee;">{r.client_phone or '—'}</td></tr>
<tr><td style="padding:8px;border:1px solid #eee;font-weight:600;">Job Type</td><td style="padding:8px;border:1px solid #eee;">{r.job_type or '—'}</td></tr>
<tr><td style="padding:8px;border:1px solid #eee;font-weight:600;">Description</td><td style="padding:8px;border:1px solid #eee;">{r.description or '—'}</td></tr>
<tr><td style="padding:8px;border:1px solid #eee;font-weight:600;">Preferred Date</td><td style="padding:8px;border:1px solid #eee;">{r.preferred_date or '—'}</td></tr>
</table>
<p><a href="{base_url}/job-requests" style="background:#F97316;color:#fff;padding:10px 20px;text-decoration:none;font-weight:700;">View in Dashboard →</a></p>"""
                    )
                    mail.send(msg)
                except Exception:
                    pass
        threading.Thread(target=notify, daemon=True).start()
    return render_template('booking_thanks.html', contractor=user)


@app.route('/job-requests')
@login_required
def job_requests():
    requests_list = JobRequest.query.filter_by(user_id=uid()).order_by(JobRequest.created_at.desc()).all()
    return render_template('job_requests.html', requests=requests_list)


@app.route('/job-requests/<int:req_id>/convert')
@login_required
def convert_job_request(req_id):
    jr = JobRequest.query.get_or_404(req_id)
    if jr.user_id != uid():
        return redirect(url_for('job_requests'))
    jr.status = 'converted'
    db.session.commit()
    # Pre-fill generate form via query params
    from urllib.parse import urlencode
    params = urlencode({
        'client_name': jr.client_name,
        'client_email': jr.client_email,
        'client_phone': jr.client_phone,
        'job_type': jr.job_type,
        'job_description': jr.description,
    })
    return redirect(url_for('generate') + '?' + params)


@app.route('/job-requests/<int:req_id>/dismiss', methods=['POST'])
@login_required
def dismiss_job_request(req_id):
    jr = JobRequest.query.get_or_404(req_id)
    if jr.user_id != uid():
        return redirect(url_for('job_requests'))
    jr.status = 'dismissed'
    db.session.commit()
    return redirect(url_for('job_requests'))


# ─── Invoice Payments ──────────────────────────────────────────────────────────

@app.route('/invoice/<token>')
def invoice_view(token):
    job = Job.query.filter_by(pay_token=token).first_or_404()
    user = User.query.get_or_404(job.user_id)
    return render_template('invoice_pay.html', job=job, user=user,
                           stripe_key=os.getenv('STRIPE_PUBLISHABLE_KEY', ''),
                           stripe_enabled=bool(stripe.api_key))


@app.route('/invoice/<token>/pay', methods=['POST'])
def invoice_pay(token):
    job = Job.query.filter_by(pay_token=token).first_or_404()
    user = User.query.get_or_404(job.user_id)
    if job.paid_at:
        return redirect(url_for('invoice_view', token=token))
    if not stripe.api_key:
        return redirect(url_for('invoice_view', token=token))
    company = user.company_name or user.name
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {'name': f"{job.job_type or 'Professional Services'} — {company}"},
                    'unit_amount': max(50, int(round(job.revenue * 100))),
                },
                'quantity': 1,
            }],
            mode='payment',
            customer_email=job.client_email or None,
            success_url=url_for('invoice_success', token=token, _external=True),
            cancel_url=url_for('invoice_view', token=token, _external=True),
            metadata={'job_id': job.id, 'type': 'invoice'},
        )
        return redirect(session.url)
    except Exception as e:
        return redirect(url_for('invoice_view', token=token))


@app.route('/invoice/<token>/success')
def invoice_success(token):
    job = Job.query.filter_by(pay_token=token).first_or_404()
    user = User.query.get_or_404(job.user_id)
    return render_template('invoice_success.html', job=job, user=user)


# ─── Stripe ────────────────────────────────────────────────────────────────────

@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    plan = request.form.get('plan', 'starter')
    price_map = {
        'starter':    ''.join((os.getenv('STRIPE_STARTER_PRICE_ID') or '').split()),
        'pro':        ''.join((os.getenv('STRIPE_PRO_PRICE_ID') or '').split()),
        'enterprise': ''.join((os.getenv('STRIPE_ENTERPRISE_PRICE_ID') or '').split()),
    }
    price_id = price_map.get(plan, '')

    if not price_id:
        flash('Stripe is not yet configured. Add your Stripe keys to .env to enable billing.', 'warning')
        return redirect(url_for('pricing'))

    try:
        session_params = dict(
            customer_email=current_user.email,
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=url_for('subscription_success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('pricing', _external=True),
            metadata={'user_id': current_user.id, 'plan': plan},
        )
        # Add 30-day free trial for new (unsubscribed) users
        if not current_user.is_subscribed:
            session_params['subscription_data'] = {'trial_period_days': TRIAL_DAYS}
        checkout_session = stripe.checkout.Session.create(**session_params)
        return redirect(checkout_session.url)
    except Exception as e:
        flash(f'Payment error: {str(e)}', 'error')
        return redirect(url_for('pricing'))


@app.route('/subscription/success')
@login_required
def subscription_success():
    session_id = request.args.get('session_id')
    if session_id and stripe.api_key:
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id,
                expand=['subscription'])
            plan = checkout_session.metadata.get('plan', 'starter')
            current_user.plan = plan
            current_user.stripe_customer_id = checkout_session.customer
            sub = checkout_session.subscription
            if sub:
                current_user.stripe_subscription_id = sub if isinstance(sub, str) else sub.get('id', '')
                # Check if Stripe put the subscription in trial
                sub_status = sub.get('status', 'active') if isinstance(sub, dict) else 'active'
                if sub_status == 'trialing':
                    current_user.subscription_status = 'trialing'
                    current_user.trial_expires_at = datetime.utcnow() + timedelta(days=TRIAL_DAYS)
                else:
                    current_user.subscription_status = 'active'
            else:
                current_user.subscription_status = 'trialing'
                current_user.trial_expires_at = datetime.utcnow() + timedelta(days=TRIAL_DAYS)
            db.session.commit()
        except Exception:
            pass
    flash('Your 30-day free trial has started! Your card won\'t be charged until the trial ends.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/test-email')
@login_required
def test_email():
    """Send a test email synchronously and show the exact error."""
    username = app.config.get('MAIL_USERNAME', '')
    password = app.config.get('MAIL_PASSWORD', '')

    lines = []
    lines.append(f"MAIL_USERNAME set: {'YES — ' + username if username else 'NO'}")
    lines.append(f"MAIL_PASSWORD set: {'YES' if password else 'NO'}")
    lines.append(f"MAIL_SERVER: {app.config.get('MAIL_SERVER')}")
    lines.append(f"MAIL_PORT: {app.config.get('MAIL_PORT')}")
    lines.append("")

    if not username or not password:
        lines.append("ERROR: Missing MAIL_USERNAME or MAIL_PASSWORD in environment variables.")
        lines.append("Go to Railway → your service → Variables tab and add them.")
    else:
        try:
            msg = Message(
                subject="CloseTheJob — Email Test",
                recipients=[current_user.email],
                html="<p>Email is working! Your invoice emails will deliver successfully.</p>",
            )
            mail.send(msg)
            lines.append(f"SUCCESS: Test email sent to {current_user.email}")
        except Exception as e:
            lines.append(f"FAILED: {type(e).__name__}: {str(e)}")

    return "<pre style='font-family:monospace;padding:40px;background:#111;color:#eee;min-height:100vh;'>" + "\n".join(lines) + "</pre>"


@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    webhook_secret = os.getenv('STRIPE_WEBHOOK_SECRET', '')
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except Exception:
        return jsonify({'error': 'Invalid signature'}), 400

    if event['type'] == 'customer.subscription.updated':
        sub = event['data']['object']
        user = User.query.filter_by(stripe_subscription_id=sub['id']).first()
        if user:
            if sub.get('status') == 'active' and user.subscription_status == 'trialing':
                # Trial just converted to paid — card was charged
                user.subscription_status = 'active'
                db.session.commit()
            elif sub.get('status') == 'past_due':
                user.subscription_status = 'past_due'
                db.session.commit()

    if event['type'] == 'customer.subscription.deleted':
        sub = event['data']['object']
        user = User.query.filter_by(stripe_subscription_id=sub['id']).first()
        if user:
            user.plan = 'trial'
            user.subscription_status = 'cancelled'
            db.session.commit()

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        meta = session.get('metadata', {})
        if meta.get('type') == 'invoice':
            job_id = meta.get('job_id')
            if job_id:
                job = Job.query.get(int(job_id))
                if job and not job.paid_at:
                    job.paid_at = datetime.utcnow()
                    db.session.commit()
        elif meta.get('type') == 'deposit':
            proposal_id = meta.get('proposal_id')
            if proposal_id:
                proposal = Proposal.query.get(int(proposal_id))
                if proposal and not proposal.deposit_paid_at:
                    proposal.deposit_paid_at = datetime.utcnow()
                    db.session.commit()

    return jsonify({'received': True})


# ─── Job Detail + Photos ───────────────────────────────────────────────────────

@app.route('/jobs/<int:job_id>')
@login_required
def job_detail(job_id):
    j = Job.query.get_or_404(job_id)
    if j.user_id != uid():
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
    service_areas = ServiceArea.query.filter_by(user_id=uid()).order_by(ServiceArea.name).all() if current_user.is_enterprise else []
    return render_template('job_detail.html', job=j, service_areas=service_areas)


@app.route('/jobs/<int:job_id>/photos/<int:photo_id>')
@login_required
def serve_job_photo(job_id, photo_id):
    from flask import Response
    p = JobPhoto.query.get_or_404(photo_id)
    if p.user_id != uid():
        return '', 403
    return Response(p.file_data, mimetype=p.mime_type)


@app.route('/jobs/<int:job_id>/photos', methods=['POST'])
@login_required
def upload_job_photo(job_id):
    j = Job.query.get_or_404(job_id)
    if j.user_id != uid():
        return jsonify({'error': 'Access denied'}), 403
    f = request.files.get('photo')
    if not f or not f.filename:
        return jsonify({'error': 'No file provided'}), 400
    photo_type = request.form.get('photo_type', 'before')
    mime = f.mimetype or 'image/jpeg'
    data = f.read()
    if len(data) > 8 * 1024 * 1024:
        return jsonify({'error': 'File too large (max 8MB)'}), 400
    p = JobPhoto(job_id=job_id, user_id=uid(), photo_type=photo_type,
                 filename=f.filename, file_data=data, mime_type=mime)
    db.session.add(p)
    db.session.commit()
    return jsonify({'id': p.id, 'photo_type': p.photo_type,
                    'url': url_for('serve_job_photo', job_id=job_id, photo_id=p.id)})


@app.route('/jobs/<int:job_id>/photos/<int:photo_id>/delete', methods=['POST'])
@login_required
def delete_job_photo(job_id, photo_id):
    p = JobPhoto.query.get_or_404(photo_id)
    if p.user_id != uid():
        return jsonify({'error': 'Access denied'}), 403
    db.session.delete(p)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/jobs/<int:job_id>/generate-summary', methods=['POST'])
@login_required
def generate_job_summary(job_id):
    j = Job.query.get_or_404(job_id)
    if j.user_id != uid():
        return jsonify({'error': 'Access denied'}), 403
    photo_count = len(j.photos)
    before_count = sum(1 for p in j.photos if p.photo_type == 'before')
    after_count = sum(1 for p in j.photos if p.photo_type == 'after')
    prompt = f"""You are an expert trade contractor writing a professional job completion summary for an invoice.

Job Details:
- Job Type: {j.job_type or 'Trade Services'}
- Description: {j.description or 'N/A'}
- Revenue: ${j.revenue:,.2f}
- Completed: {j.completed_date.strftime('%B %d, %Y')}
- Photos Uploaded: {before_count} before, {after_count} after
- Notes: {j.notes or 'None'}

Write a 2-3 sentence professional job completion summary. Write it from the contractor's perspective in past tense. Be specific about what was done, mention before/after conditions if photos are present, and end with a quality assurance statement. Keep it factual and professional — suitable for an invoice.

Return ONLY the summary text, no quotes, no labels."""
    api_key = ''.join(os.getenv('ANTHROPIC_API_KEY', '').split())
    try:
        resp = http_requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
            json={'model': 'claude-sonnet-4-6', 'max_tokens': 300,
                  'messages': [{'role': 'user', 'content': prompt}]},
            timeout=30,
        )
        resp.raise_for_status()
        summary = resp.json()['content'][0]['text'].strip()
        j.completion_summary = summary
        db.session.commit()
        return jsonify({'summary': summary})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── Pricing Advisor ────────────────────────────────────────────────────────────

@app.route('/pricing-advisor', methods=['GET', 'POST'])
@login_required
def pricing_advisor():
    result = None
    form_data = {}
    if request.method == 'POST':
        service = request.form.get('service', '').strip()
        current_price = request.form.get('current_price', '').strip()
        zip_code = request.form.get('zip_code', '').strip()
        trade = request.form.get('trade', current_user.trade_type or 'General Contractor')
        form_data = {'service': service, 'current_price': current_price, 'zip_code': zip_code, 'trade': trade}
        if service:
            prompt = f"""You are a trade industry pricing expert. Give a contractor specific, actionable market rate guidance.

Contractor Info:
- Trade: {trade}
- Service: {service}
- Their Current Price: {('$' + current_price) if current_price else 'Not provided'}
- Location ZIP: {zip_code or 'Not provided (use national averages)'}

Provide market rate benchmarking in this exact JSON format (no markdown):
{{
  "low": <number — low end of typical market rate for this service>,
  "mid": <number — middle/typical market rate>,
  "high": <number — premium/high end market rate>,
  "unit": "per job" or "per hour" or "per sq ft" etc — whichever is most natural for this service,
  "positioning": "below" or "at" or "above" — where their current price sits vs market (only if current price was provided, else "unknown"),
  "gap_dollars": <number — how many dollars below/above mid they are, 0 if unknown>,
  "insight": "2-3 sentences of specific, actionable pricing advice for this contractor. Mention regional factors if the ZIP was provided. Be direct about whether they should raise prices and by how much.",
  "premium_factors": ["factor 1 that justifies charging more", "factor 2", "factor 3"]
}}"""
            api_key = ''.join(os.getenv('ANTHROPIC_API_KEY', '').split())
            try:
                resp = http_requests.post(
                    'https://api.anthropic.com/v1/messages',
                    headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
                    json={'model': 'claude-sonnet-4-6', 'max_tokens': 600,
                          'messages': [{'role': 'user', 'content': prompt}]},
                    timeout=30,
                )
                resp.raise_for_status()
                raw = resp.json()['content'][0]['text'].strip()
                if raw.startswith('```'):
                    raw = raw.split('```')[1]
                    if raw.startswith('json'):
                        raw = raw[4:]
                result = json.loads(raw)
            except Exception as e:
                flash(f'Could not get pricing data: {str(e)}', 'error')
    return render_template('pricing_advisor.html', result=result, form_data=form_data,
                           trade=current_user.trade_type or 'General Contractor')


# ─── Client CRM ────────────────────────────────────────────────────────────────

@app.route('/clients')
@login_required
def clients():
    all_clients = Client.query.filter_by(user_id=uid()).order_by(Client.name).all()
    client_data = {}
    for c in all_clients:
        c_jobs = Job.query.filter_by(user_id=uid(), client_id=c.id).all()
        rb = compute_rebook(c_jobs)
        client_data[c.id] = {
            'revenue': sum(j.revenue for j in c_jobs),
            'job_count': len(c_jobs),
            'rebook': rb,
        }
    rebook_due = [c for c in all_clients if client_data[c.id]['rebook']['status'] == 'due']
    return render_template('clients.html', clients=all_clients, client_data=client_data, rebook_due=rebook_due)


@app.route('/clients/add', methods=['POST'])
@login_required
def add_client():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Client name is required.', 'error')
        return redirect(url_for('clients'))
    c = Client(
        user_id=uid(),
        name=name,
        email=request.form.get('email', '').strip(),
        phone=request.form.get('phone', '').strip(),
        address=request.form.get('address', '').strip(),
        notes=request.form.get('notes', '').strip(),
    )
    db.session.add(c)
    db.session.commit()
    flash(f'{name} added to your contacts.', 'success')
    return redirect(url_for('clients'))


@app.route('/clients/<int:client_id>')
@login_required
def client_detail(client_id):
    c = Client.query.get_or_404(client_id)
    if c.user_id != uid():
        flash('Access denied.', 'error')
        return redirect(url_for('clients'))
    proposals = Proposal.query.filter_by(user_id=uid(), client_id=client_id).order_by(Proposal.created_at.desc()).all()
    jobs = Job.query.filter_by(user_id=uid(), client_id=client_id).order_by(Job.completed_date.desc()).all()
    scheduled = ScheduledJob.query.filter_by(user_id=uid(), client_id=client_id).order_by(ScheduledJob.scheduled_date.desc()).all()
    total_revenue = sum(j.revenue for j in jobs)
    avg_job_value = total_revenue / len(jobs) if jobs else 0
    rebook = compute_rebook(jobs)
    return render_template('client_detail.html', client=c, proposals=proposals, jobs=jobs, scheduled=scheduled,
                           total_revenue=total_revenue, avg_job_value=avg_job_value, rebook=rebook)


@app.route('/clients/<int:client_id>/edit', methods=['POST'])
@login_required
def edit_client(client_id):
    c = Client.query.get_or_404(client_id)
    if c.user_id != uid():
        flash('Access denied.', 'error')
        return redirect(url_for('clients'))
    c.name = request.form.get('name', '').strip() or c.name
    c.email = request.form.get('email', '').strip()
    c.phone = request.form.get('phone', '').strip()
    c.address = request.form.get('address', '').strip()
    c.notes = request.form.get('notes', '').strip()
    db.session.commit()
    flash('Contact updated.', 'success')
    return redirect(url_for('client_detail', client_id=client_id))


@app.route('/clients/<int:client_id>/delete', methods=['POST'])
@login_required
def delete_client(client_id):
    c = Client.query.get_or_404(client_id)
    if c.user_id != uid():
        flash('Access denied.', 'error')
        return redirect(url_for('clients'))
    Proposal.query.filter_by(client_id=client_id).update({'client_id': None})
    Job.query.filter_by(client_id=client_id).update({'client_id': None})
    ScheduledJob.query.filter_by(client_id=client_id).update({'client_id': None})
    db.session.delete(c)
    db.session.commit()
    flash('Contact deleted.', 'success')
    return redirect(url_for('clients'))


# ─── Team Members ──────────────────────────────────────────────────────────────

@app.route('/team')
@login_required
def team():
    if not get_owner_user().can_have_team:
        flash('Team members are a Business plan feature.', 'warning')
        return redirect(url_for('pricing'))
    invites = TeamInvite.query.filter_by(owner_id=uid()).order_by(TeamInvite.created_at.desc()).all()
    members = User.query.filter_by(team_owner_id=uid()).all()
    return render_template('team.html', invites=invites, members=members)


@app.route('/team/invite', methods=['POST'])
@login_required
def team_invite():
    if not get_owner_user().can_have_team:
        return redirect(url_for('pricing'))
    email = request.form.get('email', '').strip().lower()
    if not email:
        flash('Please enter an email address.', 'error')
        return redirect(url_for('team'))
    existing = TeamInvite.query.filter_by(owner_id=uid(), email=email).first()
    if existing:
        flash('An invite was already sent to that address.', 'warning')
        return redirect(url_for('team'))
    token = secrets.token_urlsafe(24)
    invite = TeamInvite(owner_id=uid(), email=email, token=token)
    db.session.add(invite)
    db.session.commit()
    # Send invite email
    if app.config.get('MAIL_USERNAME'):
        owner = get_owner_user()
        base_url = os.getenv('APP_URL', '').rstrip('/')
        join_url = f"{base_url}/team/join/{token}"
        try:
            from flask_mail import Message as MailMessage
            msg = MailMessage(
                subject=f"{owner.name} invited you to join CloseTheJob",
                recipients=[email],
                html=f"""<p>Hi,</p>
<p><strong>{owner.name}</strong> ({owner.company_name or ''}) has invited you to join their CloseTheJob account as a team member.</p>
<p><a href="{join_url}" style="display:inline-block;background:#F97316;color:#fff;padding:12px 24px;text-decoration:none;font-weight:700;">Accept Invitation →</a></p>
<p style="font-size:12px;color:#888;">Or copy this link: {join_url}</p>"""
            )
            mail.send(msg)
        except Exception:
            pass
    flash(f'Invite sent to {email}.', 'success')
    return redirect(url_for('team'))


@app.route('/team/join/<token>', methods=['GET', 'POST'])
def team_join(token):
    invite = TeamInvite.query.filter_by(token=token, accepted=False).first_or_404()
    owner = User.query.get_or_404(invite.owner_id)
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        password = request.form.get('password', '').strip()
        if not name or not password or len(password) < 8:
            flash('Please fill in all fields (password must be 8+ characters).', 'error')
            return render_template('team_join.html', invite=invite, owner=owner)
        existing = User.query.filter_by(email=invite.email).first()
        if existing:
            existing.team_owner_id = owner.id
            invite.accepted = True
            db.session.commit()
            flash('Your account is now linked. Log in to get started.', 'success')
            return redirect(url_for('login'))
        hashed = bcrypt.generate_password_hash(password).decode('utf-8')
        member = User(
            email=invite.email,
            name=name,
            password=hashed,
            trade_type=owner.trade_type,
            team_owner_id=owner.id,
            onboarding_done=True,
        )
        db.session.add(member)
        invite.accepted = True
        db.session.commit()
        flash('Account created! Log in to get started.', 'success')
        return redirect(url_for('login'))
    return render_template('team_join.html', invite=invite, owner=owner)


@app.route('/team/remove/<int:member_id>', methods=['POST'])
@login_required
def team_remove(member_id):
    member = User.query.get_or_404(member_id)
    if member.team_owner_id != uid():
        return redirect(url_for('team'))
    member.team_owner_id = None
    db.session.commit()
    flash('Team member removed.', 'success')
    return redirect(url_for('team'))


# ─── Google Calendar OAuth ─────────────────────────────────────────────────────

@app.route('/auth/google')
@login_required
def google_auth():
    if not GOOGLE_CALENDAR_ENABLED or not os.getenv('GOOGLE_CLIENT_ID'):
        flash('Google Calendar integration is not configured.', 'warning')
        return redirect(url_for('profile'))
    try:
        config = {
            'web': {
                'client_id': os.getenv('GOOGLE_CLIENT_ID'),
                'client_secret': os.getenv('GOOGLE_CLIENT_SECRET'),
                'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                'token_uri': 'https://oauth2.googleapis.com/token',
                'redirect_uris': [url_for('google_callback', _external=True)],
            }
        }
        flow = Flow.from_client_config(config, scopes=['https://www.googleapis.com/auth/calendar.events'])
        flow.redirect_uri = url_for('google_callback', _external=True)
        auth_url, state = flow.authorization_url(access_type='offline', prompt='consent')
        session['google_oauth_state'] = state
        return redirect(auth_url)
    except Exception:
        flash('Could not start Google Calendar connection.', 'error')
        return redirect(url_for('profile'))


@app.route('/auth/google/callback')
@login_required
def google_callback():
    if not GOOGLE_CALENDAR_ENABLED or not os.getenv('GOOGLE_CLIENT_ID'):
        flash('Google Calendar integration is not configured.', 'warning')
        return redirect(url_for('profile'))
    try:
        config = {
            'web': {
                'client_id': os.getenv('GOOGLE_CLIENT_ID'),
                'client_secret': os.getenv('GOOGLE_CLIENT_SECRET'),
                'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                'token_uri': 'https://oauth2.googleapis.com/token',
                'redirect_uris': [url_for('google_callback', _external=True)],
            }
        }
        flow = Flow.from_client_config(config, scopes=['https://www.googleapis.com/auth/calendar.events'],
                                        state=session.get('google_oauth_state'))
        flow.redirect_uri = url_for('google_callback', _external=True)
        flow.fetch_token(code=request.args.get('code'))
        creds = flow.credentials
        current_user.google_access_token = creds.token
        if creds.refresh_token:
            current_user.google_refresh_token = creds.refresh_token
        current_user.google_token_expiry = creds.expiry.timestamp() if creds.expiry else None
        db.session.commit()
        flash('Google Calendar connected! Jobs you schedule will sync automatically.', 'success')
    except Exception:
        flash('Failed to connect Google Calendar. Please try again.', 'error')
    return redirect(url_for('profile'))


@app.route('/auth/google/disconnect', methods=['POST'])
@login_required
def google_disconnect():
    current_user.google_access_token = None
    current_user.google_refresh_token = None
    current_user.google_token_expiry = None
    db.session.commit()
    flash('Google Calendar disconnected.', 'success')
    return redirect(url_for('profile'))


# ─── Admin Panel ───────────────────────────────────────────────────────────────

ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', '').strip().lower()

def is_admin():
    return current_user.is_authenticated and ADMIN_EMAIL and current_user.email.lower() == ADMIN_EMAIL


@app.route('/admin')
@login_required
def admin_panel():
    if not is_admin():
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
    users = User.query.order_by(User.created_at.desc()).all()
    promos = PromoCode.query.order_by(PromoCode.created_at.desc()).all()
    return render_template('admin.html', users=users, promos=promos)


@app.route('/admin/grant', methods=['POST'])
@login_required
def admin_grant():
    if not is_admin():
        return jsonify({'error': 'Access denied'}), 403
    email = request.form.get('email', '').strip().lower()
    plan = request.form.get('plan', 'enterprise')
    expires_days = request.form.get('expires_days', '')
    user = User.query.filter(db.func.lower(User.email) == email).first()
    if not user:
        flash(f'No account found for {email}.', 'error')
        return redirect(url_for('admin_panel'))
    user.plan = plan
    user.subscription_status = 'active'
    user.trial_expires_at = (datetime.utcnow() + timedelta(days=int(expires_days))
                              if expires_days and expires_days.isdigit() else None)
    db.session.commit()
    flash(f'✓ {user.name} ({user.email}) granted {plan.capitalize()} access.', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin/revoke', methods=['POST'])
@login_required
def admin_revoke():
    if not is_admin():
        return jsonify({'error': 'Access denied'}), 403
    user_id = request.form.get('user_id', type=int)
    user = User.query.get_or_404(user_id)
    user.plan = 'trial'
    user.subscription_status = 'trial'
    user.trial_expires_at = None
    db.session.commit()
    flash(f'Access revoked for {user.email}.', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin/promo/create', methods=['POST'])
@login_required
def admin_create_promo():
    if not is_admin():
        return jsonify({'error': 'Access denied'}), 403
    plan = request.form.get('plan', 'enterprise')
    label = request.form.get('label', '').strip()
    uses = request.form.get('uses', '').strip()
    expires_days = request.form.get('expires_days', '').strip()
    token = secrets.token_urlsafe(20)
    promo = PromoCode(
        token=token,
        plan=plan,
        label=label,
        uses_remaining=int(uses) if uses.isdigit() else None,
        expires_at=datetime.utcnow() + timedelta(days=int(expires_days)) if expires_days.isdigit() else None,
    )
    db.session.add(promo)
    db.session.commit()
    flash(f'Promo link created. Share this link: {request.host_url}promo/{token}', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin/promo/<int:promo_id>/delete', methods=['POST'])
@login_required
def admin_delete_promo(promo_id):
    if not is_admin():
        return jsonify({'error': 'Access denied'}), 403
    promo = PromoCode.query.get_or_404(promo_id)
    db.session.delete(promo)
    db.session.commit()
    flash('Promo link deleted.', 'success')
    return redirect(url_for('admin_panel'))


def _apply_promo(token, user):
    """Apply a promo code to a user. Returns True if applied."""
    promo = PromoCode.query.filter_by(token=token).first()
    if not promo:
        return False
    if promo.expires_at and datetime.utcnow() > promo.expires_at:
        return False
    if promo.uses_remaining is not None and promo.uses_remaining <= 0:
        return False
    user.plan = promo.plan
    user.subscription_status = 'active'
    user.trial_expires_at = None
    if promo.uses_remaining is not None:
        promo.uses_remaining -= 1
    db.session.commit()
    return True


@app.route('/promo/<token>')
def redeem_promo(token):
    promo = PromoCode.query.filter_by(token=token).first()
    if not promo:
        flash('This invite link is invalid.', 'error')
        return redirect(url_for('index'))
    if promo.expires_at and datetime.utcnow() > promo.expires_at:
        flash('This invite link has expired.', 'error')
        return redirect(url_for('index'))
    if promo.uses_remaining is not None and promo.uses_remaining <= 0:
        flash('This invite link has already been used.', 'error')
        return redirect(url_for('index'))

    if current_user.is_authenticated:
        applied = _apply_promo(token, current_user)
        if applied:
            flash(f'You now have full {promo.plan.capitalize()} access — enjoy!', 'success')
            return redirect(url_for('dashboard'))
        flash('Could not apply this invite link.', 'error')
        return redirect(url_for('dashboard'))

    # Not logged in — store token in session, send to register
    session['pending_promo'] = token
    flash('Create your free account to activate your invite.', 'success')
    return redirect(url_for('register'))


# ─── Enterprise: Advanced Analytics ───────────────────────────────────────────

@app.route('/analytics')
@login_required
def analytics():
    if not current_user.is_enterprise:
        flash('Advanced Analytics is an Enterprise feature. Upgrade to unlock it.', 'warning')
        return redirect(url_for('pricing'))

    owner_id = uid()
    jobs = Job.query.filter_by(user_id=owner_id).all()
    expenses = Expense.query.filter_by(user_id=owner_id).all()
    proposals = Proposal.query.filter_by(user_id=owner_id).all()

    # ── Summary stats ──
    total_revenue = sum(j.revenue for j in jobs)
    total_jobs = len(jobs)
    total_expenses_all = sum(e.amount for e in expenses)
    net_profit = total_revenue - total_expenses_all
    avg_job_val = round(total_revenue / total_jobs, 2) if total_jobs else 0

    # ── Win rate ──
    actionable = [p for p in proposals if p.status in ('sent', 'accepted', 'declined')]
    accepted_count = sum(1 for p in proposals if p.status == 'accepted')
    win_rate = round(accepted_count / len(actionable) * 100) if actionable else 0
    pipeline_value = sum(p.grand_total for p in proposals if p.status == 'sent')

    # ── Revenue by job type ──
    job_type_revenue = {}
    job_type_count = {}
    job_type_expenses = {}
    for j in jobs:
        jt = j.job_type or 'Other'
        job_type_revenue[jt] = job_type_revenue.get(jt, 0) + j.revenue
        job_type_count[jt] = job_type_count.get(jt, 0) + 1
        job_type_expenses[jt] = job_type_expenses.get(jt, 0) + j.expense_total

    job_type_profit = {jt: round(job_type_revenue[jt] - job_type_expenses.get(jt, 0), 2)
                       for jt in job_type_revenue}
    job_type_avg = {jt: round(job_type_revenue[jt] / job_type_count[jt], 2)
                    for jt in job_type_revenue}

    jt_sorted = sorted(job_type_revenue.items(), key=lambda x: x[1], reverse=True)

    # ── Top clients ──
    client_revenue = {}
    client_job_count = {}
    for j in jobs:
        cn = j.client_name or 'Unknown'
        client_revenue[cn] = client_revenue.get(cn, 0) + j.revenue
        client_job_count[cn] = client_job_count.get(cn, 0) + 1
    top_clients = sorted(client_revenue.items(), key=lambda x: x[1], reverse=True)[:10]

    # ── Service area breakdown ──
    sa_revenue = {}
    for j in jobs:
        sa = j.service_area or 'Unassigned'
        sa_revenue[sa] = sa_revenue.get(sa, 0) + j.revenue
    sa_sorted = sorted(sa_revenue.items(), key=lambda x: x[1], reverse=True)

    # ── Monthly trend (12 months) ──
    labels, revenues_m, expenses_m, tips_m = build_monthly_chart_data(owner_id, months=12)
    mom_growth = None
    if len(revenues_m) >= 2 and revenues_m[-2] > 0:
        mom_growth = round((revenues_m[-1] - revenues_m[-2]) / revenues_m[-2] * 100, 1)

    chart_data = json.dumps({
        'labels': labels,
        'revenues': revenues_m,
        'expenses': expenses_m,
        'tips': tips_m,
        'jtLabels': [x[0] for x in jt_sorted[:10]],
        'jtRevenues': [round(x[1], 2) for x in jt_sorted[:10]],
        'jtProfits': [job_type_profit.get(x[0], 0) for x in jt_sorted[:10]],
        'clientLabels': [x[0] for x in top_clients],
        'clientRevenues': [round(x[1], 2) for x in top_clients],
        'saLabels': [x[0] for x in sa_sorted],
        'saRevenues': [round(x[1], 2) for x in sa_sorted],
    })

    service_areas = ServiceArea.query.filter_by(user_id=owner_id).order_by(ServiceArea.name).all()

    return render_template('analytics.html',
        total_revenue=total_revenue,
        total_jobs=total_jobs,
        avg_job_val=avg_job_val,
        net_profit=net_profit,
        win_rate=win_rate,
        pipeline_value=pipeline_value,
        accepted_count=accepted_count,
        total_proposals=len(actionable),
        mom_growth=mom_growth,
        top_clients=top_clients,
        jt_sorted=jt_sorted,
        job_type_avg=job_type_avg,
        job_type_profit=job_type_profit,
        sa_sorted=sa_sorted,
        chart_data=chart_data,
        service_areas=service_areas,
    )


# ─── Enterprise: Templates Library ─────────────────────────────────────────────

@app.route('/templates')
@login_required
def templates_library():
    templates = ProposalTemplate.query.filter_by(user_id=uid()).order_by(ProposalTemplate.created_at.desc()).all()
    all_job_types = []
    for types in SPECIALTIES.values():
        all_job_types.extend(types)
    return render_template('templates_library.html', templates=templates, all_job_types=sorted(all_job_types))


@app.route('/templates/new', methods=['POST'])
@login_required
def create_template():
    if not current_user.is_enterprise:
        flash('Bulk Proposal Templates require an Enterprise plan.', 'warning')
        return redirect(url_for('pricing'))
    name = request.form.get('name', '').strip()
    if not name:
        flash('Template name is required.', 'error')
        return redirect(url_for('templates_library'))
    tmpl = ProposalTemplate(
        user_id=uid(),
        name=name,
        job_type=request.form.get('job_type', '').strip(),
        job_description=request.form.get('job_description', '').strip(),
        materials_input=request.form.get('materials_input', '').strip(),
        labor_hours=float(request.form.get('labor_hours') or 0),
        labor_rate=float(request.form.get('labor_rate') or 75),
        timeline=request.form.get('timeline', '').strip(),
        warranty=request.form.get('warranty', '').strip(),
        notes=request.form.get('notes', '').strip(),
        tax_rate=float(request.form.get('tax_rate') or 0),
        deposit_pct=float(request.form.get('deposit_pct') or 0),
    )
    db.session.add(tmpl)
    db.session.commit()
    flash(f'Template "{name}" created.', 'success')
    return redirect(url_for('templates_library'))


@app.route('/templates/<int:tmpl_id>/edit', methods=['POST'])
@login_required
def edit_template(tmpl_id):
    tmpl = ProposalTemplate.query.get_or_404(tmpl_id)
    if tmpl.user_id != uid():
        flash('Access denied.', 'error')
        return redirect(url_for('templates_library'))
    tmpl.name = request.form.get('name', tmpl.name).strip()
    tmpl.job_type = request.form.get('job_type', '').strip()
    tmpl.job_description = request.form.get('job_description', '').strip()
    tmpl.materials_input = request.form.get('materials_input', '').strip()
    tmpl.labor_hours = float(request.form.get('labor_hours') or 0)
    tmpl.labor_rate = float(request.form.get('labor_rate') or 75)
    tmpl.timeline = request.form.get('timeline', '').strip()
    tmpl.warranty = request.form.get('warranty', '').strip()
    tmpl.notes = request.form.get('notes', '').strip()
    tmpl.tax_rate = float(request.form.get('tax_rate') or 0)
    tmpl.deposit_pct = float(request.form.get('deposit_pct') or 0)
    db.session.commit()
    flash(f'Template "{tmpl.name}" updated.', 'success')
    return redirect(url_for('templates_library'))


# ─── Enterprise: Service Areas ──────────────────────────────────────────────────

@app.route('/service-areas', methods=['GET', 'POST'])
@login_required
def service_areas():
    if not current_user.is_enterprise:
        flash('Multi-location support is an Enterprise feature.', 'warning')
        return redirect(url_for('pricing'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        color = request.form.get('color', '#F97316').strip()
        if name:
            existing = ServiceArea.query.filter_by(user_id=current_user.id, name=name).first()
            if not existing:
                sa = ServiceArea(user_id=current_user.id, name=name, color=color)
                db.session.add(sa)
                db.session.commit()
                flash(f'Service area "{name}" added.', 'success')
        return redirect(url_for('service_areas'))
    areas = ServiceArea.query.filter_by(user_id=current_user.id).order_by(ServiceArea.name).all()
    # Revenue per area
    jobs = Job.query.filter_by(user_id=current_user.id).all()
    area_stats = {}
    for j in jobs:
        sa = j.service_area or ''
        if sa:
            area_stats[sa] = area_stats.get(sa, 0) + j.revenue
    return render_template('service_areas.html', areas=areas, area_stats=area_stats)


@app.route('/service-areas/<int:area_id>/delete', methods=['POST'])
@login_required
def delete_service_area(area_id):
    sa = ServiceArea.query.get_or_404(area_id)
    if sa.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('service_areas'))
    db.session.delete(sa)
    db.session.commit()
    flash('Service area removed.', 'success')
    return redirect(url_for('service_areas'))


@app.route('/jobs/<int:job_id>/set-area', methods=['POST'])
@login_required
def set_job_service_area(job_id):
    job = Job.query.get_or_404(job_id)
    if job.user_id != uid():
        return jsonify({'error': 'Access denied'}), 403
    job.service_area = request.form.get('service_area', '').strip()
    db.session.commit()
    return redirect(request.referrer or url_for('financials'))


# ─── Init ──────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()
    # Migrate: add new columns to existing tables if missing
    from sqlalchemy import text
    _migrations = [
        'ALTER TABLE "user" ADD COLUMN specialty VARCHAR(200) DEFAULT \'\'',
        'ALTER TABLE "user" ADD COLUMN onboarding_done BOOLEAN DEFAULT 0',
        'ALTER TABLE "user" ADD COLUMN google_access_token TEXT',
        'ALTER TABLE "user" ADD COLUMN google_refresh_token TEXT',
        'ALTER TABLE "user" ADD COLUMN google_token_expiry FLOAT',
        'ALTER TABLE proposal ADD COLUMN client_id INTEGER',
        'ALTER TABLE proposal ADD COLUMN public_token VARCHAR(64)',
        'ALTER TABLE proposal ADD COLUMN accepted_at DATETIME',
        'ALTER TABLE job ADD COLUMN client_id INTEGER',
        'ALTER TABLE job ADD COLUMN invoice_sent_at DATETIME',
        'ALTER TABLE scheduled_job ADD COLUMN client_id INTEGER',
        'ALTER TABLE scheduled_job ADD COLUMN google_event_id VARCHAR(200)',
        'ALTER TABLE proposal ADD COLUMN reminder_sent_at DATETIME',
        'ALTER TABLE job ADD COLUMN pay_token VARCHAR(64)',
        'ALTER TABLE job ADD COLUMN paid_at DATETIME',
        'ALTER TABLE job ADD COLUMN payment_reminder_sent_at DATETIME',
        'ALTER TABLE job ADD COLUMN review_sent_at DATETIME',
        'ALTER TABLE "user" ADD COLUMN logo_url TEXT',
        'ALTER TABLE "user" ADD COLUMN brand_color VARCHAR(7) DEFAULT \'#F97316\'',
        'ALTER TABLE "user" ADD COLUMN team_owner_id INTEGER',
        'ALTER TABLE "user" ADD COLUMN booking_slug VARCHAR(100)',
        'ALTER TABLE "user" ADD COLUMN booking_enabled BOOLEAN DEFAULT 0',
        'ALTER TABLE proposal ADD COLUMN tax_rate FLOAT DEFAULT 0',
        'ALTER TABLE proposal ADD COLUMN deposit_pct FLOAT DEFAULT 0',
        'ALTER TABLE proposal ADD COLUMN deposit_token VARCHAR(64)',
        'ALTER TABLE proposal ADD COLUMN deposit_paid_at DATETIME',
        'ALTER TABLE proposal ADD COLUMN template_id INTEGER',
        'ALTER TABLE job ADD COLUMN completion_summary TEXT DEFAULT \'\'',
        'ALTER TABLE "user" ADD COLUMN trial_expires_at DATETIME',
        'ALTER TABLE job ADD COLUMN service_area VARCHAR(200) DEFAULT \'\'',
        'ALTER TABLE promo_code ADD COLUMN label VARCHAR(200) DEFAULT \'\'',
    ]
    for _sql in _migrations:
        try:
            db.session.execute(text(_sql))
            db.session.commit()
        except Exception:
            db.session.rollback()

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5001))
    app.run(debug=False, host='0.0.0.0', port=port)
