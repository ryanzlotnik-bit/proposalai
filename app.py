from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
import requests as http_requests
import stripe
import os
from datetime import datetime, date
from dotenv import load_dotenv
import json
import random
import string

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///proposalai.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access that page.'

stripe.api_key = os.getenv('STRIPE_SECRET_KEY', '')

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


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ─── Helpers ───────────────────────────────────────────────────────────────────

def generate_proposal_number():
    today = date.today()
    rand = ''.join(random.choices(string.digits, k=4))
    return f"P-{today.strftime('%Y%m')}-{rand}"


def call_claude_for_proposal(job_data, user_data):
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
- Materials/Parts Noted: {job_data['materials_input']}
- Labor: {job_data['labor_hours']} hours at ${job_data['labor_rate']}/hour
- Timeline: {job_data['timeline']}
- Warranty: {job_data['warranty']}
- Special Notes: {job_data['notes']}

Return ONLY valid JSON with exactly this structure (no markdown, no extra text):
{{
  "executive_summary": "2-3 sentence professional overview of the project scope",
  "scope_of_work": [
    "Detailed work item 1",
    "Detailed work item 2",
    "..."
  ],
  "materials_list": [
    {{"name": "Material name", "quantity": "amount", "unit": "each/lf/sf/gal", "unit_cost": 0.00, "total": 0.00}},
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

Use realistic pricing. Calculate all totals accurately. Be professional and specific."""

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


@app.route('/generate', methods=['GET', 'POST'])
@login_required
def generate():
    if not current_user.can_generate:
        flash('You have used all your free proposals. Upgrade to continue.', 'warning')
        return redirect(url_for('pricing'))

    if request.method == 'POST':
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
            return render_template('generate.html', form_data=job_data)

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

    return render_template('generate.html', form_data={})


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


@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    plan = request.form.get('plan', 'starter')
    price_id = os.getenv('STRIPE_STARTER_PRICE_ID') if plan == 'starter' \
        else os.getenv('STRIPE_PRO_PRICE_ID')

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
