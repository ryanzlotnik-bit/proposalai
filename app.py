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
from datetime import datetime, date
from dotenv import load_dotenv
from fpdf import FPDF
import json
import random
import string

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

TRIAL_PROPOSAL_LIMIT = 3

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
    plan = db.Column(db.String(50), default='trial')  # trial, starter, pro
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    proposals = db.relationship('Proposal', backref='user', lazy=True)

    @property
    def proposal_count(self):
        return len(self.proposals)

    @property
    def can_generate(self):
        if self.plan in ('starter', 'pro'):
            return True
        return self.proposal_count < TRIAL_PROPOSAL_LIMIT

    @property
    def is_subscribed(self):
        return self.plan in ('starter', 'pro')


class Proposal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
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
    status = db.Column(db.String(50), default='draft')  # draft, sent, accepted, declined
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    linked_jobs = db.relationship('Job', backref='proposal', lazy=True)

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
    client_name = db.Column(db.String(200), nullable=False)
    client_email = db.Column(db.String(200), default='')
    client_phone = db.Column(db.String(50), default='')
    job_type = db.Column(db.String(100), default='')
    description = db.Column(db.Text, default='')
    revenue = db.Column(db.Float, default=0)
    completed_date = db.Column(db.Date, nullable=False)
    invoice_sent = db.Column(db.Boolean, default=False)
    invoice_sent_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    job_expenses = db.relationship('Expense', backref='job', lazy=True)
    job_tips = db.relationship('Tip', backref='job', lazy=True)

    @property
    def tip_total(self):
        return sum(t.amount for t in self.job_tips)

    @property
    def expense_total(self):
        return sum(e.amount for e in self.job_expenses)

    @property
    def net(self):
        return self.revenue + self.tip_total - self.expense_total


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


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ─── Helpers ───────────────────────────────────────────────────────────────────

def generate_proposal_number():
    today = date.today()
    rand = ''.join(random.choices(string.digits, k=4))
    return f"P-{today.strftime('%Y%m')}-{rand}"


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

    # ── Description ────────────────────────────────────────────────────────────
    if job.description:
        pdf.set_font('Helvetica', '', 10)
        pdf.set_text_color(60, 60, 60)
        pdf.multi_cell(W, 5, job.description[:300], ln=True)
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


def _build_invoice_html(job, user):
    company = user.company_name or user.name
    invoice_num = f"INV-{job.id:04d}"
    completed = job.completed_date.strftime('%B %d, %Y')
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:'Helvetica Neue',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:40px 20px;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="background:#fff;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
<tr><td style="background:#1a1a1a;padding:20px 32px;">
  <table width="100%"><tr>
    <td><div style="font-size:18px;font-weight:700;color:#fff;">{company}</div>
        <div style="font-size:11px;color:#888;">{user.trade_type}{' · Lic #'+user.license_number if user.license_number else ''}</div></td>
    <td style="text-align:right;"><div style="font-size:11px;color:#888;">{user.phone or ''}</div>
        <div style="font-size:11px;color:#888;">{user.email}</div></td>
  </tr></table>
</td></tr>
<tr><td style="background:#F97316;padding:14px 32px;">
  <span style="font-size:18px;font-weight:800;color:#fff;">INVOICE</span>
  <span style="float:right;font-size:13px;color:rgba(255,255,255,0.9);font-weight:600;">{invoice_num}</span>
</td></tr>
<tr><td style="padding:32px;">
  <table width="100%" style="margin-bottom:24px;"><tr>
    <td width="48%" style="background:#f9f9f9;border:1px solid #eee;padding:14px;vertical-align:top;">
      <div style="font-size:10px;font-weight:700;color:#F97316;text-transform:uppercase;margin-bottom:6px;">Billed To</div>
      <div style="font-size:13px;font-weight:700;color:#111;">{job.client_name}</div>
      {'<div style="font-size:11px;color:#555;margin-top:3px;">'+job.client_email+'</div>' if job.client_email else ''}
      {'<div style="font-size:11px;color:#555;">'+job.client_phone+'</div>' if job.client_phone else ''}
    </td>
    <td width="4%"></td>
    <td width="48%" style="background:#f9f9f9;border:1px solid #eee;padding:14px;vertical-align:top;">
      <div style="font-size:10px;font-weight:700;color:#F97316;text-transform:uppercase;margin-bottom:6px;">Invoice Details</div>
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
    <tr style="background:#F97316;"><td style="padding:11px 12px;font-size:13px;font-weight:700;color:#fff;">TOTAL DUE</td>
    <td style="padding:11px 12px;font-size:13px;font-weight:700;color:#fff;text-align:right;">${job.revenue:,.2f}</td></tr>
  </table>
  {'<p style="font-size:11px;color:#888;font-style:italic;">Notes: '+job.notes+'</p>' if job.notes else ''}
  <p style="font-size:12px;color:#666;margin-top:20px;">Please find your invoice attached as a PDF. Thank you for your business!</p>
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

    username = app.config.get('MAIL_USERNAME', '')
    password = app.config.get('MAIL_PASSWORD', '')

    def do_send():
        with app.app_context():
            try:
                pdf_bytes = generate_invoice_pdf(job, user)
                html_body = _build_invoice_html(job, user)
                mime_msg, subject = _build_mime_invoice(job, user, html_body, pdf_bytes)

                # 1) Send to client via Flask-Mail
                flask_msg = Message(
                    subject=subject,
                    recipients=[job.client_email],
                    html=html_body,
                )
                flask_msg.attach(
                    f"INV-{job.id:04d}.pdf",
                    'application/pdf',
                    pdf_bytes,
                )
                mail.send(flask_msg)

                # 2) Save a copy to Gmail Drafts via IMAP
                if username and password and 'gmail' in username.lower():
                    try:
                        imap = imaplib.IMAP4_SSL('imap.gmail.com')
                        imap.login(username, password)
                        imap.append(
                            '[Gmail]/Drafts',
                            '\\Draft',
                            imaplib.Time2Internaldate(time.time()),
                            mime_msg.as_bytes(),
                        )
                        imap.logout()
                    except Exception:
                        pass  # Draft save failure is non-critical

            except Exception:
                pass

    threading.Thread(target=do_send, daemon=True).start()
    return True


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
        flash(f'Welcome, {name}! You have {TRIAL_PROPOSAL_LIMIT} free proposals.', 'success')
        return redirect(url_for('dashboard'))
    return render_template('register.html')


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
    proposals = Proposal.query.filter_by(user_id=current_user.id)\
        .order_by(Proposal.created_at.desc()).all()
    return render_template('dashboard.html', proposals=proposals)


@app.route('/cost-templates/add', methods=['POST'])
@login_required
def add_cost_template():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Name is required.', 'error')
        return redirect(url_for('generate'))
    ct = CostTemplate(
        user_id=current_user.id,
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
    if ct.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('generate'))
    db.session.delete(ct)
    db.session.commit()
    flash('Saved cost removed.', 'success')
    return redirect(url_for('generate'))


@app.route('/generate', methods=['GET', 'POST'])
@login_required
def generate():
    cost_templates = CostTemplate.query.filter_by(user_id=current_user.id).order_by(CostTemplate.created_at).all()

    if not current_user.can_generate:
        flash('You have used all your free proposals. Upgrade to continue.', 'warning')
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
                    user_id=current_user.id,
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
            return render_template('generate.html', form_data=job_data, cost_templates=cost_templates)

        proposal = Proposal(
            user_id=current_user.id,
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
            status='draft',
        )
        db.session.add(proposal)
        db.session.commit()
        return redirect(url_for('view_proposal', proposal_id=proposal.id))

    return render_template('generate.html', form_data={}, cost_templates=cost_templates)


@app.route('/proposal/<int:proposal_id>')
@login_required
def view_proposal(proposal_id):
    proposal = Proposal.query.get_or_404(proposal_id)
    if proposal.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
    return render_template('proposal_view.html', proposal=proposal, user=current_user)


@app.route('/proposal/<int:proposal_id>/status', methods=['POST'])
@login_required
def update_status(proposal_id):
    proposal = Proposal.query.get_or_404(proposal_id)
    if proposal.user_id != current_user.id:
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
    if proposal.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
    db.session.delete(proposal)
    db.session.commit()
    flash('Proposal deleted.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/proposal/<int:proposal_id>/close')
@login_required
def close_job_from_proposal(proposal_id):
    proposal = Proposal.query.get_or_404(proposal_id)
    if proposal.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
    return render_template('close_job.html', proposal=proposal, today=date.today().isoformat())


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
        db.session.commit()
        flash('Profile updated successfully.', 'success')
        return redirect(url_for('profile'))
    return render_template('profile.html')


@app.route('/pricing')
def pricing():
    return render_template('pricing.html')


# ─── Financials ────────────────────────────────────────────────────────────────

@app.route('/financials')
@login_required
def financials():
    jobs = Job.query.filter_by(user_id=current_user.id).order_by(Job.completed_date.desc()).all()
    expenses = Expense.query.filter_by(user_id=current_user.id).order_by(Expense.date.desc()).all()
    tips = Tip.query.filter_by(user_id=current_user.id).order_by(Tip.date.desc()).all()
    all_jobs = Job.query.filter_by(user_id=current_user.id).all()

    total_revenue = sum(j.revenue for j in jobs)
    total_expenses = sum(e.amount for e in expenses)
    total_tips = sum(t.amount for t in tips)
    net_profit = total_revenue + total_tips - total_expenses

    labels, revenues, expenses_monthly, tips_monthly = build_monthly_chart_data(current_user.id)
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
        user_id=current_user.id,
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
        success = send_invoice_email(job, current_user)
        if success:
            job.invoice_sent = True
            job.invoice_sent_at = datetime.utcnow()
            db.session.commit()
            flash(f'Job logged and invoice sent to {job.client_email}.', 'success')
        else:
            flash('Job logged. Invoice email failed — check MAIL_USERNAME and MAIL_PASSWORD in .env.', 'warning')
    else:
        flash(f'Job "{client_name}" logged successfully.', 'success')

    return redirect(url_for('financials'))


@app.route('/jobs/<int:job_id>/delete', methods=['POST'])
@login_required
def delete_job(job_id):
    job = Job.query.get_or_404(job_id)
    if job.user_id != current_user.id:
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
    if job.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('financials'))
    if not job.client_email:
        flash('No client email on file for this job.', 'error')
        return redirect(url_for('financials'))
    success = send_invoice_email(job, current_user)
    if success:
        job.invoice_sent = True
        job.invoice_sent_at = datetime.utcnow()
        db.session.commit()
        flash(f'Invoice sent to {job.client_email}.', 'success')
    else:
        flash('Invoice send failed. Set MAIL_USERNAME and MAIL_PASSWORD in your .env file.', 'error')
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
        user_id=current_user.id,
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
    if expense.user_id != current_user.id:
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
        user_id=current_user.id,
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
    if tip.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('financials'))
    db.session.delete(tip)
    db.session.commit()
    flash('Tip deleted.', 'success')
    return redirect(url_for('financials'))


# ─── Stripe ────────────────────────────────────────────────────────────────────

@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    plan = request.form.get('plan', 'starter')
    price_id = ''.join((os.getenv('STRIPE_STARTER_PRICE_ID') or '').split()) if plan == 'starter' \
        else ''.join((os.getenv('STRIPE_PRO_PRICE_ID') or '').split())

    if not price_id:
        flash('Stripe is not yet configured. Add your Stripe keys to .env to enable billing.', 'warning')
        return redirect(url_for('pricing'))

    try:
        checkout_session = stripe.checkout.Session.create(
            customer_email=current_user.email,
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=url_for('subscription_success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('pricing', _external=True),
            metadata={'user_id': current_user.id, 'plan': plan},
        )
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
            checkout_session = stripe.checkout.Session.retrieve(session_id)
            plan = checkout_session.metadata.get('plan', 'starter')
            current_user.plan = plan
            current_user.subscription_status = 'active'
            current_user.stripe_customer_id = checkout_session.customer
            current_user.stripe_subscription_id = checkout_session.subscription
            db.session.commit()
        except Exception:
            pass
    flash('Subscription activated! You now have unlimited proposals.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    webhook_secret = os.getenv('STRIPE_WEBHOOK_SECRET', '')
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except Exception:
        return jsonify({'error': 'Invalid signature'}), 400

    if event['type'] == 'customer.subscription.deleted':
        sub = event['data']['object']
        user = User.query.filter_by(stripe_subscription_id=sub['id']).first()
        if user:
            user.plan = 'trial'
            user.subscription_status = 'cancelled'
            db.session.commit()

    return jsonify({'received': True})


# ─── Init ──────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5001))
    app.run(debug=False, host='0.0.0.0', port=port)
