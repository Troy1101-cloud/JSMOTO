from flask import Flask, jsonify, render_template, request, redirect, url_for, session, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import os
import re
import time
import random
import string
import requests
import pandas as pd
from io import BytesIO
from datetime import date, datetime, timedelta, timezone
from dotenv import load_dotenv
from functools import wraps
from supabase import create_client, Client

def compute_status(stock_qty, par_level=5):
    stock_qty = int(stock_qty or 0)
    par_level = int(par_level or 0)

    if stock_qty <= 0:
        return "Out of Stock"
    elif stock_qty <= par_level:
        return "Low Stock"
    return "In Stock"


def now_iso():
    return datetime.utcnow().isoformat()


def normalize_filter_date(value, max_date=None):
    value = (value or '').strip()
    if not value:
        return ''
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return ''
    if max_date and parsed > max_date:
        parsed = max_date
    return parsed.isoformat()


def normalize_date_range(start_value, end_value, max_date=None):
    start_date = normalize_filter_date(start_value, max_date)
    end_date = normalize_filter_date(end_value, max_date)
    if start_date and end_date and start_date > end_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date


def sale_matches_record_status(sale, record_status):
    is_archived = bool(sale.get('is_archived'))
    is_voided = bool(sale.get('is_voided'))

    if record_status == 'archived':
        return is_archived
    if record_status == 'voided':
        return is_voided
    if record_status == 'all':
        return True
    return not is_archived and not is_voided



def is_valid_email(email):
    email = (email or '').strip()
    return bool(re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email))


def generate_numeric_code(length=6):
    return ''.join(random.choice(string.digits) for _ in range(length))


def generate_token(length=32):
    alphabet = string.ascii_letters + string.digits
    return ''.join(random.choice(alphabet) for _ in range(length))



def normalize_customer_key(value):
    value = (value or '').strip().lower()
    return re.sub(r'\s+', ' ', value)


def normalize_phone(value):
    return re.sub(r'\D+', '', value or '')


def get_public_base_url():
    # Set PUBLIC_BASE_URL to your deployed cloud URL, for example https://your-app.onrender.com
    configured = (os.getenv('PUBLIC_BASE_URL') or os.getenv('APP_BASE_URL') or '').strip().rstrip('/')
    if configured:
        return configured
    try:
        return request.host_url.rstrip('/')
    except Exception:
        return ''


def send_transactional_email(to_email, to_name, subject, html_content, text_content=None):
    """Send real transactional email through Resend Email API.

    Required .env values:
      RESEND_API_KEY=your_resend_api_key
      MAIL_FROM_EMAIL=verified_sender@example.com
      MAIL_FROM_NAME=Shop Name
      PUBLIC_BASE_URL=https://your-cloud-app-url
    """
    api_key = (os.getenv('RESEND_API_KEY') or '').strip()
    sender_email = (os.getenv('MAIL_FROM_EMAIL') or '').strip()
    sender_name = (os.getenv('MAIL_FROM_NAME') or '').strip()
    resend_from = (os.getenv('RESEND_FROM_EMAIL') or '').strip()

    if not sender_email and resend_from:
        match = re.match(r'^(.*?)\s*<([^<>@\s]+@[^<>@\s]+)>$', resend_from)
        if match:
            sender_name = sender_name or match.group(1).strip().strip('"')
            sender_email = match.group(2).strip()
        else:
            sender_email = resend_from

    sender_name = (sender_name or get_system_settings().get('shop_name') or 'JS Motoworks').strip()

    if not api_key or not sender_email:
        print('Email skipped: RESEND_API_KEY and sender email are required in .env')
        return False, 'Email API is not configured. Add RESEND_API_KEY and MAIL_FROM_EMAIL or RESEND_FROM_EMAIL to .env.'

    payload = {
        "from": f"{sender_name} <{sender_email}>",
        "to": [to_email],
        "subject": subject,
        "html": html_content,
        "text": text_content or re.sub('<[^<]+?>', '', html_content)
    }

    try:
        res = requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            },
            json=payload,
            timeout=15
        )
        if 200 <= res.status_code < 300:
            return True, 'sent'
        print(f'Resend API error {res.status_code}: {res.text}')
        return False, res.text
    except Exception as e:
        print(f'Email send exception: {e}')
        return False, str(e)



def send_customer_account_verification_email(user):
    """Send customer account verification OTP through Resend."""
    settings = get_system_settings()
    shop_name = settings.get('shop_name', 'JS Motoworks')
    otp_code = user.get('email_verification_token')

    html = f"""
    <div style="font-family:Arial,sans-serif;line-height:1.6;color:#1f2937">
      <h2>{shop_name} Email Verification</h2>
      <p>Hello <strong>{user.get('full_name','Customer')}</strong>,</p>
      <p>Use this verification code to activate your customer account:</p>
      <div style="font-size:30px;font-weight:bold;letter-spacing:6px;background:#f3f4f6;padding:16px 20px;border-radius:10px;text-align:center;color:#111827">
        {otp_code}
      </div>
      <p>This code is needed before you can log in to your customer account.</p>
      <p style="font-size:12px;color:#6b7280">If you did not create this account, you may ignore this email.</p>
    </div>
    """
    text_body = f"{shop_name} email verification code: {otp_code}"
    return send_transactional_email(
        user.get('email'),
        user.get('full_name'),
        f"Your {shop_name} verification code",
        html,
        text_body
    )


def send_customer_password_reset_email(user, reset_code):
    """Send customer password reset OTP through Resend."""
    settings = get_system_settings()
    shop_name = settings.get('shop_name', 'JS Motoworks')

    html = f"""
    <div style="font-family:Arial,sans-serif;line-height:1.6;color:#1f2937">
      <h2>{shop_name} Password Reset</h2>
      <p>Hello <strong>{user.get('full_name','Customer')}</strong>,</p>
      <p>Use this verification code to reset your customer account password:</p>
      <div style="font-size:30px;font-weight:bold;letter-spacing:6px;background:#f3f4f6;padding:16px 20px;border-radius:10px;text-align:center;color:#111827">
        {reset_code}
      </div>
      <p>If you did not request a password reset, you may ignore this email.</p>
      <p style="font-size:12px;color:#6b7280">For security, this code is only for resetting your password.</p>
    </div>
    """
    text_body = f"{shop_name} password reset code: {reset_code}"
    return send_transactional_email(
        user.get('email'),
        user.get('full_name'),
        f"Reset your {shop_name} password",
        html,
        text_body
    )


def format_receipt_datetime(value):
    """Format Supabase UTC/ISO timestamp into PH receipt date/time."""
    if not value:
        return "-"
    try:
        raw = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ph_dt = dt.astimezone(timezone(timedelta(hours=8)))
        return ph_dt.strftime("%m/%d/%Y %I:%M %p")
    except Exception:
        return str(value)


def compute_vat_breakdown(total_amount, vat_rate=0.12):
    """VAT-inclusive breakdown for receipt display."""
    try:
        total = float(total_amount or 0)
    except Exception:
        total = 0.0
    vatable_sales = total / (1 + vat_rate) if total else 0.0
    vat_amount = total - vatable_sales
    return {
        "vat_rate": vat_rate,
        "vatable_sales": round(vatable_sales, 2),
        "vat_amount": round(vat_amount, 2),
        "vat_exempt": 0.0,
        "zero_rated": 0.0,
        "total": round(total, 2)
    }

def send_booking_confirmation_email(booking):
    settings = get_system_settings()
    shop_name = settings.get('shop_name', 'JS Motoworks')
    base_url = get_public_base_url()
    appt_id = booking.get('appointment_id')
    verify_code = booking.get('email_verification_code')
    verify_link = f"{base_url}/verify_booking/{appt_id}/{verify_code}" if base_url and appt_id and verify_code else '#'
    track_link = f"{base_url}/track_booking?contact={booking.get('contact_number', '')}" if base_url else '#'

    html = f"""
    <div style="font-family:Arial,sans-serif;line-height:1.5;color:#222">
      <h2>{shop_name} Booking Request Received</h2>
      <p>Hello <strong>{booking.get('customer_name','Customer')}</strong>,</p>
      <p>Your appointment request has been recorded and is currently <strong>{booking.get('status','Pending')}</strong>.</p>
      <table cellpadding="6" style="border-collapse:collapse;background:#f7f7f7">
        <tr><td><strong>Reference</strong></td><td>#{appt_id}</td></tr>
        <tr><td><strong>Date</strong></td><td>{booking.get('appointment_date')}</td></tr>
        <tr><td><strong>Time</strong></td><td>{booking.get('appointment_time')}</td></tr>
        <tr><td><strong>Motorcycle</strong></td><td>{booking.get('motorcycle_model')}</td></tr>
        <tr><td><strong>Service</strong></td><td>{booking.get('service_requested')}</td></tr>
      </table>
      <p>Please verify your email address by clicking this button:</p>
      <p><a href="{verify_link}" style="background:#ff4b2b;color:white;padding:10px 16px;text-decoration:none;border-radius:6px">Verify Email</a></p>
      <p>You can track your booking here: <a href="{track_link}">{track_link}</a></p>
      <p style="font-size:12px;color:#666">If you did not request this booking, please ignore this email.</p>
    </div>
    """
    text_body = f"{shop_name} booking #{appt_id}\nDate: {booking.get('appointment_date')}\nTime: {booking.get('appointment_time')}\nVerify: {verify_link}\nTrack: {track_link}"
    return send_transactional_email(
        booking.get('customer_email'),
        booking.get('customer_name'),
        f"{shop_name} Booking Request #{appt_id}",
        html,
        text_body
    )


def send_booking_cancelled_email(booking):
    settings = get_system_settings()
    shop_name = settings.get('shop_name', 'JS Motoworks')
    html = f"""
    <div style="font-family:Arial,sans-serif;line-height:1.5;color:#222">
      <h2>{shop_name} Booking Cancelled</h2>
      <p>Hello <strong>{booking.get('customer_name','Customer')}</strong>,</p>
      <p>Your appointment request <strong>#{booking.get('appointment_id')}</strong> has been marked as <strong>Cancelled</strong>.</p>
      <p><strong>Date:</strong> {booking.get('appointment_date')}<br><strong>Time:</strong> {booking.get('appointment_time')}</p>
      <p>If this was not requested by you, please contact the shop immediately.</p>
    </div>
    """
    return send_transactional_email(
        booking.get('customer_email'),
        booking.get('customer_name'),
        f"{shop_name} Booking Cancelled #{booking.get('appointment_id')}",
        html
    )


def send_booking_status_update_email(booking, new_status, admin_notes=''):
    """Send appointment status update email to customer."""
    settings = get_system_settings()
    shop_name = settings.get('shop_name', 'JS Motoworks')
    base_url = get_public_base_url()
    portal_link = f"{base_url}/customer_portal" if base_url else "#"
    track_link = f"{base_url}/track_booking?contact={booking.get('contact_number', '')}" if base_url else "#"

    status_messages = {
        "Confirmed": "Your appointment has been confirmed by the shop.",
        "In Progress": "Your motorcycle service appointment is now in progress.",
        "Completed": "Your service appointment has been marked as completed.",
        "Cancelled": "Your service appointment has been cancelled.",
        "No Show": "Your appointment was marked as no show.",
        "Pending": "Your appointment status has been updated to pending."
    }
    message = status_messages.get(new_status, f"Your appointment status is now {new_status}.")

    notes_html = f"<p><strong>Staff Note:</strong> {admin_notes}</p>" if admin_notes else ""

    html = f"""
    <div style="font-family:Arial,sans-serif;line-height:1.6;color:#1f2937">
      <h2>{shop_name} Appointment Update</h2>
      <p>Hello <strong>{booking.get('customer_name','Customer')}</strong>,</p>
      <p>{message}</p>
      <table cellpadding="8" style="border-collapse:collapse;background:#f8fafc;border:1px solid #e5e7eb">
        <tr><td><strong>Reference</strong></td><td>#{booking.get('appointment_id')}</td></tr>
        <tr><td><strong>Status</strong></td><td>{new_status}</td></tr>
        <tr><td><strong>Date</strong></td><td>{booking.get('appointment_date')}</td></tr>
        <tr><td><strong>Time</strong></td><td>{booking.get('appointment_time')}</td></tr>
        <tr><td><strong>Motorcycle</strong></td><td>{booking.get('motorcycle_model')}</td></tr>
        <tr><td><strong>Service</strong></td><td>{booking.get('service_requested')}</td></tr>
      </table>
      {notes_html}
      <p>You can view your appointment here:</p>
      <p><a href="{portal_link}" style="background:#2563eb;color:white;padding:10px 16px;text-decoration:none;border-radius:6px;font-weight:bold">Open Customer Portal</a></p>
      <p>Public tracker: <a href="{track_link}">{track_link}</a></p>
    </div>
    """
    text_body = f"{shop_name} appointment update\nReference: #{booking.get('appointment_id')}\nStatus: {new_status}\nDate: {booking.get('appointment_date')}\nTime: {booking.get('appointment_time')}\n{admin_notes or ''}\nPortal: {portal_link}"

    return send_transactional_email(
        booking.get('customer_email'),
        booking.get('customer_name'),
        f"{shop_name} Appointment {new_status} #{booking.get('appointment_id')}",
        html,
        text_body
    )


# Appointment rules are also enforced in the backend so users cannot bypass
# schedule limits by editing the browser form.
MAX_BOOKINGS_PER_SLOT = 3
SHOP_OPEN_TIME = "08:00"
SHOP_CLOSE_TIME = "19:00"


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def normalize_contact_number(contact):
    digits = ''.join(ch for ch in str(contact or '') if ch.isdigit())
    if digits.startswith('63') and len(digits) == 12:
        digits = '0' + digits[2:]
    return digits


def is_valid_ph_mobile(contact):
    return len(contact) == 11 and contact.startswith('09')


def build_service_request(service_category, notes):
    service_category = (service_category or '').strip()
    notes = (notes or '').strip()
    if notes:
        return f"{service_category} (Issue: {notes})"
    return service_category


def get_status_badge_class(status):
    status = status or "Pending"
    return {
        "Pending": "warning text-dark",
        "Confirmed": "info text-dark",
        "In Progress": "primary",
        "Completed": "success",
        "Cancelled": "secondary",
        "No Show": "dark"
    }.get(status, "secondary")


def validate_appointment_payload(customer_name, contact_number, motorcycle_model, service_category, appointment_date, appointment_time):
    errors = []

    if not customer_name:
        errors.append("Customer name is required.")
    if not contact_number:
        errors.append("Contact number is required.")
    elif not is_valid_ph_mobile(contact_number):
        errors.append("Contact number must be a valid Philippine mobile number, e.g., 09XXXXXXXXX.")
    if not motorcycle_model:
        errors.append("Motorcycle model is required.")
    if not service_category:
        errors.append("Service type is required.")

    parsed_date = None
    parsed_time = None

    try:
        parsed_date = date.fromisoformat(appointment_date)
    except Exception:
        errors.append("Invalid appointment date.")

    try:
        parsed_time = datetime.strptime(appointment_time, "%H:%M").time()
    except Exception:
        errors.append("Invalid appointment time.")

    if parsed_date and parsed_time:
        appt_datetime = datetime.combine(parsed_date, parsed_time)
        if appt_datetime < datetime.now():
            errors.append("Appointment date and time cannot be in the past.")

        shop_open_time, shop_close_time, _slot_capacity = get_shop_schedule_settings()
        open_time = datetime.strptime(shop_open_time, "%H:%M").time()
        close_time = datetime.strptime(shop_close_time, "%H:%M").time()
        if parsed_time < open_time or parsed_time > close_time:
            errors.append(f"Appointment time must be within shop hours: {shop_open_time} to {shop_close_time}.")

    return errors

# ==========================================
# 1. SETUP & INITIALIZATION
# ==========================================
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'js_motoworks_super_secret_key')


@app.template_filter('receipt_datetime')
def receipt_datetime_filter(value):
    return format_receipt_datetime(value)

app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

INVENTORY_CATEGORIES = [
    'Parts',
    'Accessories',
    'Lubricants',
    'Hardware',
    'Tools',
    'Consumables',
    'Other'
]

def normalize_inventory_category(category):
    category = (category or '').strip()
    if category in INVENTORY_CATEGORIES:
        return category
    return 'Other'



def get_system_settings():
    """Return dynamic shop settings used by UI, POS receipts, and reports."""
    defaults = {
        "shop_name": "JS Motoworks",
        "shop_address": "13 Sandoval Street, Barangay Pinagbuhatan, Pasig City",
        "contact_number": "0900-000-0000",
        "logo_url": "",
        "shop_open_time": SHOP_OPEN_TIME,
        "shop_close_time": SHOP_CLOSE_TIME,
        "slot_capacity": MAX_BOOKINGS_PER_SLOT,
        "receipt_footer": "Thank you for choosing our shop.",
        "gcash_qr_url": "",
        "theme_mode": "general"
    }
    try:
        rows = supabase.table("system_settings").select("*").limit(1).execute().data or []
        if rows:
            defaults.update({k: v for k, v in rows[0].items() if v not in [None, ""]})
    except Exception as e:
        print(f"Settings load fallback: {e}")
    return defaults


@app.context_processor
def inject_shop_settings():
    return {"shop_settings": get_system_settings(), "inventory_categories": INVENTORY_CATEGORIES}


def save_system_settings(payload):
    rows = supabase.table("system_settings").select("*").execute().data or []
    if rows:
        result = None
        updated_by_id = False
        for row in rows:
            for key in ["settings_id", "setting_id", "id"]:
                if key in row and row.get(key) is not None:
                    result = supabase.table("system_settings").update(payload).eq(key, row[key]).execute()
                    updated_by_id = True
                    break
        if updated_by_id:
            return result

        # system_settings is a singleton table in the app. If the table has no
        # id column, do not use shop_name as the lookup because shop_name itself
        # is editable; keep every existing settings row in sync instead.
        return supabase.table("system_settings").update(payload).neq("shop_name", "__no_matching_shop_name__").execute()
    return supabase.table("system_settings").insert(payload).execute()



def save_uploaded_image(file_storage, prefix='upload'):
    """Save an uploaded image under static/uploads and return its browser path."""
    if not file_storage or not getattr(file_storage, 'filename', ''):
        return None

    filename = secure_filename(file_storage.filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ['.png', '.jpg', '.jpeg', '.webp', '.gif']:
        raise ValueError('Only PNG, JPG, JPEG, WEBP, and GIF image files are allowed.')

    final_name = f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S')}{ext}"
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], final_name)
    file_storage.save(save_path)
    return url_for('static', filename=f'uploads/{final_name}')


def normalize_time_hhmm(value, fallback="08:00"):
    """Accepts Supabase TIME values like 08:00 or 08:00:00 and returns HH:MM."""
    value = str(value or fallback).strip()
    if not value:
        value = fallback
    parts = value.split(":")
    if len(parts) >= 2:
        return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    return fallback


def get_shop_schedule_settings():
    settings = get_system_settings()
    open_time = normalize_time_hhmm(settings.get('shop_open_time') or SHOP_OPEN_TIME, SHOP_OPEN_TIME)
    close_time = normalize_time_hhmm(settings.get('shop_close_time') or SHOP_CLOSE_TIME, SHOP_CLOSE_TIME)
    slot_capacity = safe_int(settings.get('slot_capacity'), MAX_BOOKINGS_PER_SLOT)
    if slot_capacity < 1:
        slot_capacity = MAX_BOOKINGS_PER_SLOT
    return open_time, close_time, slot_capacity


def get_active_services():
    """Load active services safely.

    This prevents /pos from crashing when an older database has not yet added
    service-management columns such as is_active or estimated_duration.
    """
    try:
        rows = (
            supabase.table("services")
            .select("*")
            .eq("is_active", True)
            .order("service_name")
            .execute()
            .data
        ) or []
        return rows
    except Exception as e:
        print(f"Service active-column fallback: {e}")
        try:
            rows = (
                supabase.table("services")
                .select("*")
                .order("service_name")
                .execute()
                .data
            ) or []
            return [row for row in rows if row.get("is_active", True) is not False]
        except Exception as inner_e:
            print(f"Service load failed: {inner_e}")
            return []


# ==========================================
# 2. SECURITY & ROLE DECORATORS
# ==========================================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_only(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('role') != 'Admin':
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function


def roles_required(*allowed_roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('login'))
            if session.get('role') not in allowed_roles:
                return redirect(url_for('index'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def cashier_or_admin(f):
    return roles_required('Admin', 'Cashier')(f)


def inventory_or_admin(f):
    return roles_required('Admin', 'Inventory')(f)


# ==========================================
# 3. AUTHENTICATION & LOGIN
# ==========================================
@app.route('/')
def index():
    if 'user_id' in session:
        role = session.get('role', 'Cashier')
        if role == 'Admin':
            return redirect(url_for('dashboard'))
        elif role == 'Inventory':
            return redirect(url_for('inventory_page'))
        elif role == 'Cashier':
            return redirect(url_for('pos_page'))
        elif role == 'Customer':
            return redirect(url_for('customer_portal'))
        session.clear()
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))

    if request.method == 'POST':
        login_id = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        try:
            # Allow login using username OR email.
            # Some records may have uppercase/lowercase differences, so we try exact username,
            # exact email, then case-insensitive fallbacks.
            response = supabase.table("users").select("*").eq("username", login_id).execute()

            if not response.data:
                response = supabase.table("users").select("*").eq("email", login_id.lower()).execute()

            if not response.data:
                response = supabase.table("users").select("*").ilike("username", login_id).execute()

            if not response.data and is_valid_email(login_id.lower()):
                response = supabase.table("users").select("*").ilike("email", login_id.lower()).execute()

            if response.data and len(response.data) > 0:
                user = response.data[0]

                if check_password_hash(user.get('password_hash', ''), password) and user.get('is_active', True):
                    if user.get('role') == 'Customer' and user.get('email') and user.get('email_verified') is False:
                        return render_template(
                            'login.html',
                            error="Please verify your email address first using the OTP code sent to your email.",
                            verify_email=user.get('email')
                        )

                    session.clear()
                    session['user_id'] = user.get('user_id')
                    session['username'] = user.get('username')
                    session['role'] = user.get('role')
                    display_name = user.get('full_name') or user.get('username')
                    if str(display_name).strip().lower() == 'system admin':
                        display_name = 'Admin'
                    session['full_name'] = display_name
                    session['email'] = user.get('email')

                    if user.get('role') == 'Customer':
                        return redirect(url_for('customer_portal'))

                    return redirect(url_for('index'))

                return render_template('login.html', error="Invalid password or account deactivated.")

            return render_template('login.html', error="Account not found. Use your username or registered email.")

        except Exception as e:
            return render_template('login.html', error=f"Database connection error: {str(e)}")

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ==========================================
# 4. DASHBOARD (Fixed JSON Error)
# ==========================================
@app.route('/dashboard')
@admin_only
def dashboard():
    try:
        today = date.today()
        today_str = today.isoformat()
        month_key = today.strftime('%Y-%m')
        last_7_dates = [(today - timedelta(days=i)) for i in range(6, -1, -1)]
        last_7_labels = [d.strftime('%b %d') for d in last_7_dates]
        last_7_keys = [d.isoformat() for d in last_7_dates]

        sales = (
            supabase.table("sales_transactions")
            .select("*")
            .order("transaction_date", desc=True)
            .limit(800)
            .execute()
            .data or []
        )
        inventory = (
            supabase.table("inventory")
            .select("item_id, item_name, sku, barcode, category, stock_qty, par_level, status, price")
            .order("stock_qty", desc=False)
            .execute()
            .data or []
        )
        appointments = (
            supabase.table("appointments")
            .select("*")
            .order("appointment_date", desc=False)
            .execute()
            .data or []
        )
        sales_items = (
            supabase.table("sales_items")
            .select("item_description, item_type, qty, subtotal")
            .execute()
            .data or []
        )

        active_sales = [s for s in sales if not s.get('is_archived') and not s.get('is_voided')]

        sales_today = 0
        sales_month = 0
        daily_sales_map = {key: 0 for key in last_7_keys}
        payment_map = {}
        recent_sales = active_sales[:6]
        gcash_missing_ref = 0

        for sale in active_sales:
            tx_date = str(sale.get('transaction_date', ''))[:10]
            amount = float(sale.get('total_amount') or 0)
            method = sale.get('payment_method') or 'Unspecified'

            if tx_date == today_str:
                sales_today += amount
            if tx_date.startswith(month_key):
                sales_month += amount
            if tx_date in daily_sales_map:
                daily_sales_map[tx_date] += amount

            payment_map[method] = payment_map.get(method, 0) + amount
            if method == 'GCash' and not sale.get('gcash_reference'):
                gcash_missing_ref += 1

        low_stock_items = []
        out_of_stock_items = []
        for i in inventory:
            qty = int(i.get('stock_qty') or 0)
            par = int(i.get('par_level') or 5)
            if qty <= 0:
                out_of_stock_items.append(i)
            elif qty <= par:
                low_stock_items.append(i)

        critical_items = sorted(
            low_stock_items + out_of_stock_items,
            key=lambda x: (int(x.get('stock_qty') or 0), x.get('item_name') or '')
        )[:10]

        pending_appointments = sum(1 for a in appointments if (a.get('status') or 'Pending') == 'Pending')
        confirmed_appointments = sum(1 for a in appointments if (a.get('status') or '') == 'Confirmed')
        today_appointments = sum(
            1 for a in appointments
            if str(a.get('appointment_date', '')) == today_str and (a.get('status') or '') not in ['Cancelled', 'No Show']
        )

        pending_verification = sum(
            1 for a in appointments
            if not a.get('email_verified') and (a.get('status') or 'Pending') not in ['Cancelled', 'No Show', 'Completed']
        )

        upcoming_appointments = [
            a for a in appointments
            if str(a.get('appointment_date', '')) >= today_str
            and (a.get('status') or 'Pending') not in ['Cancelled', 'No Show', 'Completed']
        ][:6]

        appointment_status_map = {}
        for a in appointments:
            status = a.get('status') or 'Pending'
            appointment_status_map[status] = appointment_status_map.get(status, 0) + 1

        item_map = {}
        for item in sales_items:
            name = item.get('item_description') or 'Unnamed Item'
            qty = int(item.get('qty') or 0)
            subtotal = float(item.get('subtotal') or 0)
            if name not in item_map:
                item_map[name] = {'name': name, 'qty': 0, 'subtotal': 0}
            item_map[name]['qty'] += qty
            item_map[name]['subtotal'] += subtotal
        top_items = sorted(item_map.values(), key=lambda x: x['qty'], reverse=True)[:6]

        customer_map = {}
        for sale in active_sales:
            name = sale.get('customer_name') or ''
            email = sale.get('customer_email') or ''
            contact = sale.get('customer_contact') or ''
            key = normalize_customer_key(email) or normalize_phone(contact) or normalize_customer_key(name)
            if not key:
                continue
            if key not in customer_map:
                customer_map[key] = {
                    "name": name or "Customer",
                    "email": email,
                    "contact": contact,
                    "transactions": 0,
                    "total_spent": 0.0,
                    "last_purchase": None
                }
            customer_map[key]["transactions"] += 1
            customer_map[key]["total_spent"] += float(sale.get('total_amount') or 0)
            tx_date = sale.get('transaction_date')
            if tx_date and (not customer_map[key]["last_purchase"] or str(tx_date) > str(customer_map[key]["last_purchase"])):
                customer_map[key]["last_purchase"] = tx_date

        top_customers = sorted(
            customer_map.values(),
            key=lambda x: (x["total_spent"], x["transactions"]),
            reverse=True
        )[:5]

        action_alerts = []
        if out_of_stock_items:
            action_alerts.append({
                "level": "danger",
                "icon": "fa-box-open",
                "title": f"{len(out_of_stock_items)} item(s) out of stock",
                "detail": "Review inventory before continuing high-volume sales."
            })
        if low_stock_items:
            action_alerts.append({
                "level": "warning",
                "icon": "fa-triangle-exclamation",
                "title": f"{len(low_stock_items)} item(s) below or at par level",
                "detail": "Prepare restocking to prevent missed sales."
            })
        if pending_appointments:
            action_alerts.append({
                "level": "info",
                "icon": "fa-calendar-check",
                "title": f"{pending_appointments} pending appointment(s)",
                "detail": "Confirm or update customer bookings."
            })
        if pending_verification:
            action_alerts.append({
                "level": "warning",
                "icon": "fa-envelope",
                "title": f"{pending_verification} booking(s) pending email verification",
                "detail": "These bookings may need follow-up."
            })
        if gcash_missing_ref:
            action_alerts.append({
                "level": "danger",
                "icon": "fa-qrcode",
                "title": f"{gcash_missing_ref} GCash sale(s) without reference number",
                "detail": "Review sales records for traceability."
            })

        return render_template(
            'dashboard.html',
            sales_today=sales_today,
            sales_month=sales_month,
            low_stock=len(low_stock_items),
            out_of_stock=len(out_of_stock_items),
            pending_appointments=pending_appointments,
            confirmed_appointments=confirmed_appointments,
            today_appointments=today_appointments,
            recent_sales=recent_sales,
            critical_items=critical_items,
            top_items=top_items,
            top_customers=top_customers,
            upcoming_appointments=upcoming_appointments,
            action_alerts=action_alerts,
            pending_verification=pending_verification,
            gcash_missing_ref=gcash_missing_ref,
            chart_dates=last_7_labels,
            chart_sales=[daily_sales_map[k] for k in last_7_keys],
            pay_labels=list(payment_map.keys()),
            pay_data=list(payment_map.values()),
            appt_labels=list(appointment_status_map.keys()),
            appt_data=list(appointment_status_map.values())
        )
    except Exception as e:
        print(f"Dashboard Error: {e}")
        return render_template(
            'dashboard.html',
            sales_today=0,
            sales_month=0,
            low_stock=0,
            out_of_stock=0,
            pending_appointments=0,
            confirmed_appointments=0,
            today_appointments=0,
            recent_sales=[],
            critical_items=[],
            top_items=[],
            top_customers=[],
            upcoming_appointments=[],
            action_alerts=[{"level":"danger","icon":"fa-bug","title":"Dashboard loading error","detail":str(e)}],
            pending_verification=0,
            gcash_missing_ref=0,
            chart_dates=[],
            chart_sales=[],
            pay_labels=[],
            pay_data=[],
            appt_labels=[],
            appt_data=[]
        )


# ==========================================
# 5. INVENTORY & AUDIT LOGS
# ==========================================
@app.route('/inventory', methods=['GET', 'POST'])
@inventory_or_admin
def inventory_page():
    try:
        if request.method == 'POST':
            scanned_sku = request.form.get('sku', '').strip()
            # SKU and barcode are treated as one required item code.
            barcode = scanned_sku
            item_name = request.form.get('item_name', '').strip()
            brand = request.form.get('brand', '').strip()
            category = normalize_inventory_category(request.form.get('category', ''))
            specs = request.form.get('specs', '').strip()
            price = float(request.form.get('price', 0) or 0)
            added_qty = int(request.form.get('stock_qty', 0) or 0)
            par_level = int(request.form.get('par_level', 5) or 5)

            if not scanned_sku:
                return "SKU is required.", 400

            if not item_name:
                return "Item name is required.", 400

            # Existing Item Code / Barcode = Stock In, not duplicate item creation.
            check_res = (
                supabase.table("inventory")
                .select("item_id, sku, barcode, stock_qty, par_level")
                .eq("sku", scanned_sku)
                .execute()
            )

            if not check_res.data:
                check_res = (
                    supabase.table("inventory")
                    .select("item_id, sku, barcode, stock_qty, par_level")
                    .eq("barcode", scanned_sku)
                    .execute()
                )

            if check_res.data:
                current_item = check_res.data[0]
                current_qty = int(current_item.get("stock_qty", 0) or 0)
                current_par = int(current_item.get("par_level", par_level) or par_level)

                new_qty = current_qty + added_qty
                new_status = compute_status(new_qty, current_par)

                supabase.table("inventory").update({
                    "stock_qty": new_qty,
                    "status": new_status,
                    "last_updated": now_iso()
                }).eq("item_id", current_item["item_id"]).execute()

                supabase.table("stock_logs").insert({
                    "sku": scanned_sku,
                    "action": "Stock In",
                    "qty": added_qty,
                    "username": session.get('username'),
                    "remarks": "Added via Inventory Form"
                }).execute()

            else:

                new_status = compute_status(added_qty, par_level)

                try:
                    supabase.table("inventory").insert({
                        "sku": scanned_sku,
                        "barcode": scanned_sku,
                        "item_name": item_name,
                        "brand": brand if brand else None,
                        "category": category if category else None,
                        "specs": specs if specs else None,
                        "price": price,
                        "stock_qty": added_qty,
                        "par_level": par_level,
                        "status": new_status,
                        "last_updated": now_iso()
                    }).execute()
                except Exception as insert_error:
                    if 'duplicate key value violates unique constraint' in str(insert_error) and 'inventory_pkey' in str(insert_error):
                        return "Database Error: inventory item_id sequence is out of sync. Run phase37_required_supabase_migration.sql, then try adding the item again.", 500
                    raise insert_error

                supabase.table("stock_logs").insert({
                    "sku": scanned_sku,
                    "action": "Stock In",
                    "qty": added_qty,
                    "username": session.get('username'),
                    "remarks": "New inventory item added"
                }).execute()

            return redirect(url_for('inventory_page'))

        items = supabase.table("inventory").select("*").order("item_id", desc=True).execute().data or []

        search = request.args.get('search', '').strip().lower()
        category_filter = request.args.get('category', '').strip()
        status_filter = request.args.get('status', '').strip()

        if search:
            def matches(item):
                haystack = f"{item.get('sku','')} {item.get('barcode','')} {item.get('item_name','')} {item.get('brand','')} {item.get('category','')} {item.get('specs','')}".lower()
                return search in haystack
            items = [item for item in items if matches(item)]

        if category_filter:
            items = [item for item in items if (item.get('category') or 'Other') == category_filter]

        if status_filter:
            items = [item for item in items if (item.get('status') or compute_status(item.get('stock_qty'), item.get('par_level'))) == status_filter]

        inventory_summary = {
            "total_items": len(items),
            "in_stock": sum(1 for i in items if compute_status(i.get('stock_qty'), i.get('par_level')) == 'In Stock'),
            "low_stock": sum(1 for i in items if compute_status(i.get('stock_qty'), i.get('par_level')) == 'Low Stock'),
            "out_of_stock": sum(1 for i in items if compute_status(i.get('stock_qty'), i.get('par_level')) == 'Out of Stock'),
            "total_stock_value": sum(float(i.get('price') or 0) * int(i.get('stock_qty') or 0) for i in items)
        }

        filters = {
            "search": search,
            "category": category_filter,
            "status": status_filter
        }

        return render_template(
            'inventory.html',
            items=items,
            categories=INVENTORY_CATEGORIES,
            inventory_summary=inventory_summary,
            filters=filters
        )

    except Exception as e:
        return f"Database Error: {str(e)}"

@app.route('/delete_item/<int:item_id>', methods=['POST'])
@admin_only
def delete_item(item_id):
    try:
        # Soft archive only: do not delete historical records.
        response = supabase.table("inventory").update({
            "status": "Out of Stock",
            "stock_qty": 0,
            "last_updated": now_iso()
        }).eq("item_id", item_id).execute()

        if response.data:
            item = response.data[0]
            supabase.table("stock_logs").insert({
                "sku": item.get("sku"),
                "action": "Archive",
                "qty": 0,
                "username": session.get('username'),
                "remarks": "Item archived; stock set to zero"
            }).execute()
            return jsonify({"success": True, "message": "Item archived successfully."})
        return jsonify({"success": False, "message": "Item not found."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@app.route('/edit_item', methods=['POST'])
@admin_only
def edit_item():
    try:
        item_id = int(request.form.get('item_id'))
        sku = request.form.get('sku', '').strip()
        # SKU and barcode are unified, so barcode follows the same required item code.
        barcode = sku
        item_name = request.form.get('item_name', '').strip()
        brand = request.form.get('brand', '').strip()
        category = normalize_inventory_category(request.form.get('category', ''))
        specs = request.form.get('specs', '').strip()
        price = float(request.form.get('price', 0) or 0)
        stock_qty = int(request.form.get('stock_qty', 0) or 0)
        par_level = int(request.form.get('par_level', 5) or 5)

        if not sku:
            return "SKU is required.", 400

        if not item_name:
            return "Item name is required.", 400

        current_res = (
            supabase.table("inventory")
            .select("item_id, sku, barcode")
            .eq("item_id", item_id)
            .execute()
        )
        if not current_res.data:
            return "Item not found.", 404

        current_item = current_res.data[0]
        old_sku = current_item.get("sku")

        # SKU must remain unique even when it is edited.
        sku_check = (
            supabase.table("inventory")
            .select("item_id")
            .eq("sku", sku)
            .neq("item_id", item_id)
            .execute()
        )
        if sku_check.data:
            return "SKU already exists for another item.", 400

        status = compute_status(stock_qty, par_level)

        supabase.table("inventory").update({
            "sku": sku,
            "barcode": sku,
            "item_name": item_name,
            "brand": brand if brand else None,
            "category": category if category else None,
            "specs": specs if specs else None,
            "price": price,
            "stock_qty": stock_qty,
            "par_level": par_level,
            "status": status,
            "last_updated": now_iso()
        }).eq("item_id", item_id).execute()

        if old_sku != sku:
            remarks = f"Item details edited; SKU changed from {old_sku} to {sku}"
        else:
            remarks = "Item details edited"

        supabase.table("stock_logs").insert({
            "sku": sku,
            "action": "Edit",
            "qty": 0,
            "username": session.get('username'),
            "remarks": remarks
        }).execute()

        return redirect(url_for('inventory_page'))

    except Exception as e:
        return f"Database Error: {str(e)}"

@app.route('/restock', methods=['POST'])
@inventory_or_admin
def restock_item():
    try:
        item_id_raw = request.form.get('item_id')
        added_qty = int(request.form.get('added_qty', 0) or 0)
        current_user = session.get('username')

        if added_qty <= 0:
            return "Restock quantity must be greater than zero.", 400

        query = supabase.table("inventory").select("item_id, sku, stock_qty, par_level")

        if item_id_raw:
            item_res = query.eq("item_id", int(item_id_raw)).execute()
        else:
            sku = request.form.get('sku', '').strip()
            item_res = query.eq("sku", sku).execute()

        if item_res.data:
            item = item_res.data[0]
            current_qty = int(item.get('stock_qty', 0) or 0)
            par_level = int(item.get('par_level', 5) or 5)
            new_qty = current_qty + added_qty
            new_status = compute_status(new_qty, par_level)

            supabase.table("inventory").update({
                "stock_qty": new_qty,
                "status": new_status,
                "last_updated": now_iso()
            }).eq("item_id", item["item_id"]).execute()

            supabase.table("stock_logs").insert({
                "sku": item.get("sku"),
                "action": "Restock",
                "qty": added_qty,
                "username": current_user,
                "remarks": "Manual Restock"
            }).execute()

        return redirect(url_for('inventory_page'))

    except Exception as e:
        return f"Database Error: {str(e)}"

@app.route('/adjust_stock', methods=['POST'])
@inventory_or_admin
def adjust_stock():
    try:
        item_id = int(request.form.get('item_id'))
        adjustment_type = request.form.get('adjustment_type', '').strip()
        qty = int(request.form.get('qty', 0) or 0)
        remarks = request.form.get('remarks', '').strip()

        if adjustment_type not in ['Stock In', 'Stock Out', 'Damaged', 'Lost', 'Correction']:
            return "Invalid stock adjustment type.", 400
        if qty <= 0:
            return "Quantity must be greater than zero.", 400

        item_res = supabase.table("inventory").select("item_id, sku, stock_qty, par_level").eq("item_id", item_id).execute()
        if not item_res.data:
            return "Item not found.", 404

        item = item_res.data[0]
        current_qty = int(item.get('stock_qty') or 0)
        par_level = int(item.get('par_level') or 5)

        if adjustment_type == 'Stock In':
            new_qty = current_qty + qty
            log_qty = qty
        else:
            if qty > current_qty:
                return f"Cannot deduct {qty}; only {current_qty} in stock.", 400
            new_qty = current_qty - qty
            log_qty = -qty

        new_status = compute_status(new_qty, par_level)

        supabase.table("inventory").update({
            "stock_qty": new_qty,
            "status": new_status,
            "last_updated": now_iso()
        }).eq("item_id", item_id).execute()

        supabase.table("stock_logs").insert({
            "sku": item.get("sku"),
            "action": adjustment_type,
            "qty": log_qty,
            "username": session.get('username'),
            "remarks": remarks or f"Inventory adjustment: {adjustment_type}"
        }).execute()

        return redirect(url_for('inventory_page'))

    except Exception as e:
        return f"Stock Adjustment Error: {str(e)}", 500


@app.route('/audit_logs')
@inventory_or_admin
def audit_logs():
    try:
        action_filter = request.args.get('action', '').strip()
        sku_filter = request.args.get('sku', '').strip()
        user_filter = request.args.get('username', '').strip()
        today = date.today()
        today_str = today.isoformat()
        start_date = normalize_filter_date(request.args.get('start_date', ''), today)
        end_date = normalize_filter_date(request.args.get('end_date', ''), today)
        limit_raw = request.args.get('limit', '200').strip()

        try:
            limit = int(limit_raw)
        except ValueError:
            limit = 200
        limit = max(25, min(limit, 1000))

        query = supabase.table("stock_logs").select("*")

        if action_filter:
            query = query.eq("action", action_filter)
        if sku_filter:
            query = query.ilike("sku", f"%{sku_filter}%")
        if user_filter:
            query = query.ilike("username", f"%{user_filter}%")
        if start_date:
            query = query.gte("log_date", f"{start_date}T00:00:00")
        if end_date:
            query = query.lte("log_date", f"{end_date}T23:59:59")

        logs = query.order("log_date", desc=True).limit(limit).execute().data or []

        # Enrich audit logs with inventory item details for readability.
        skus = sorted({log.get("sku") for log in logs if log.get("sku")})
        item_lookup = {}
        if skus:
            try:
                inv_rows = (
                    supabase.table("inventory")
                    .select("sku, item_name, brand, category")
                    .in_("sku", skus)
                    .execute()
                    .data
                ) or []
                item_lookup = {row.get("sku"): row for row in inv_rows}
            except Exception:
                item_lookup = {}

        for log in logs:
            item_info = item_lookup.get(log.get("sku"), {})
            log["item_name"] = item_info.get("item_name") or "Item record not found / old SKU"
            log["brand"] = item_info.get("brand") or ""
            log["category"] = item_info.get("category") or ""

        summary = {
            "total_logs": len(logs),
            "stock_in": sum(1 for l in logs if l.get("action") in ["Stock In", "Restock"]),
            "stock_out": sum(1 for l in logs if l.get("action") in ["Checkout", "Stock Out", "Damaged", "Lost", "Correction"]),
            "edits": sum(1 for l in logs if l.get("action") == "Edit"),
            "void_returns": sum(1 for l in logs if l.get("action") == "Void Return"),
            "archives": sum(1 for l in logs if l.get("action") == "Archive"),
            "adjustments": sum(1 for l in logs if l.get("action") in ["Damaged", "Lost", "Correction"]),
        }

        actions = [
            "Stock In",
            "Restock",
            "Checkout",
            "Stock Out",
            "Damaged",
            "Lost",
            "Correction",
            "Edit",
            "Archive",
            "Void Return",
        ]

        filters = {
            "action": action_filter,
            "sku": sku_filter,
            "username": user_filter,
            "start_date": start_date,
            "end_date": end_date,
            "limit": limit,
        }

        return render_template(
            'audit_logs.html',
            logs=logs,
            summary=summary,
            filters=filters,
            actions=actions,
            today=today_str
        )

    except Exception as e:
        return f"Audit Log Error: {str(e)}", 500


# ==========================================
# 6. POS & CHECKOUT
# ==========================================
@app.route('/pos')
@cashier_or_admin
def pos_page():
    # Robust loader: the POS must still open even if service-management
    # optional columns are not yet present during migration.
    services = get_active_services()
    return render_template('pos.html', services=services)

@app.route('/api/get_item/<path:sku>', methods=['GET'])
@cashier_or_admin
def get_item(sku):
    clean_code = re.sub(r'[\r\n\t]+', '', (sku or '')).strip()
    try:
        if not clean_code:
            return jsonify({'status': 'error', 'message': 'No barcode or item code received.'})

        # Scanner can use the unified Item Code / Barcode field.
        item_res = supabase.table("inventory").select("*").eq("sku", clean_code).execute()

        if not item_res.data:
            item_res = supabase.table("inventory").select("*").eq("barcode", clean_code).execute()

        if not item_res.data:
            return jsonify({'status': 'error', 'message': f'Item code/barcode {clean_code} was not found in inventory.'})

        item = item_res.data[0]
        stock_qty = int(item.get('stock_qty', 0) or 0)

        if stock_qty <= 0:
            return jsonify({'status': 'error', 'message': f"Item '{item.get('item_name','')}' is out of stock."})

        return jsonify({
            'status': 'success',
            'message': 'Item scanned successfully.',
            'data': {
                'sku': item.get('sku'),
                'barcode': item.get('barcode'),
                'item_name': item.get('item_name'),
                'brand': item.get('brand'),
                'category': item.get('category'),
                'price': float(item.get('price') or 0),
                'stock_qty': stock_qty
            }
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})







def print_receipt_direct_escpos(receipt_no, cashier_name, cart, total, method, gcash_ref=None):
    """POS-58 ESC/POS direct print with stronger density/bold commands."""
    try:
        import win32print
    except Exception as e:
        print(f"Printer Error: {e}")
        return False, str(e)

    printer_name = os.getenv("RECEIPT_PRINTER_NAME", "POS-58").strip() or "POS-58"
    date_str = datetime.now().strftime('%y-%m-%d %H:%M')

    try:
        settings = supabase.table("system_settings").select("*").limit(1).execute().data
        s = settings[0] if settings else {}
    except Exception:
        s = {}

    shop_name = str(s.get('shop_name', 'JS Motoworks'))[:28]
    shop_address = str(s.get('shop_address', 'Makati City'))[:28]
    contact = str(s.get('contact_number', '0900-000-0000'))[:24]

    def line(text=""):
        return (str(text) + "\\n").encode("cp437", errors="replace")

    def center(text, width=32):
        text = str(text)
        if len(text) >= width:
            return text[:width]
        return text.center(width)

    def money(value):
        try:
            return f"{float(value or 0):.2f}"
        except Exception:
            return "0.00"

    try:
        total_float = float(total or 0)
    except Exception:
        total_float = 0.0
    vatable = total_float / 1.12 if total_float else 0.0
    vat = total_float - vatable

    # ESC/POS commands
    ESC = b"\\x1b"
    GS = b"\\x1d"
    init = ESC + b"@"                 # Initialize
    bold_on = ESC + b"E\\x01"          # Emphasized mode ON
    bold_off = ESC + b"E\\x00"         # Emphasized mode OFF
    align_left = ESC + b"a\\x00"
    align_center = ESC + b"a\\x01"
    font_normal = ESC + b"!\\x00"
    line_spacing = ESC + b"3\\x18"     # Set line spacing to 24 dots
    # Common POS58 density/heat command. Unsupported printers usually ignore it.
    heat_density = ESC + b"7\\x0f\\xff\\x03"
    # Some POS58 firmwares support DC2 # n for density. Unsupported printers ignore it.
    density_cmd = b"\\x12\\x23\\x08"

    data = bytearray()
    data += init
    data += line_spacing
    data += heat_density
    data += density_cmd
    data += font_normal

    data += align_center + bold_on
    data += line(shop_name.upper())
    data += bold_off
    data += line(shop_address)
    data += line("CP: " + contact)
    data += line("SALES RECEIPT")
    data += align_left
    data += line("--------------------------------")
    data += line("OR#: " + str(receipt_no)[:27])
    data += line("Date: " + date_str)
    data += line("Cashier: " + str(cashier_name)[:22])
    data += line("Pay: " + str(method))
    if method == "GCash" and gcash_ref:
        data += line("Ref: " + str(gcash_ref)[:27])

    data += line("--------------------------------")
    data += bold_on + line("QTY ITEM              AMOUNT") + bold_off

    for item in cart:
        try:
            qty = str(item.get("qty", 1))[:3].ljust(3)
            desc = str(item.get("name", "Item"))[:16].ljust(16)
            amt = money(float(item.get("qty", 1)) * float(item.get("price", 0))).rjust(8)
            data += line(f"{qty} {desc} {amt}")
        except Exception:
            continue

    data += line("--------------------------------")
    data += bold_on
    data += line("TOTAL: PHP " + money(total_float))
    data += bold_off
    data += line("VATable: PHP " + money(vatable))
    data += line("VAT 12%: PHP " + money(vat))
    data += line("Method: " + str(method))

    data += line("--------------------------------")
    data += align_center + bold_on
    data += line("THANK YOU! RIDE SAFE!")
    data += bold_off + align_left
    data += b"\\n\\n\\n"

    try:
        hprinter = win32print.OpenPrinter(printer_name)
        try:
            hjob = win32print.StartDocPrinter(hprinter, 1, ("JS Motoworks Receipt", None, "RAW"))
            try:
                win32print.StartPagePrinter(hprinter)
                win32print.WritePrinter(hprinter, bytes(data))
                win32print.EndPagePrinter(hprinter)
            finally:
                win32print.EndDocPrinter(hprinter)
        finally:
            win32print.ClosePrinter(hprinter)
        return True, f"Printed to {printer_name} using ESC/POS"
    except Exception as e:
        print(f"Printer Error: {e}")
        return False, str(e)


@app.route('/checkout', methods=['POST'])
@cashier_or_admin
def checkout():
    if 'user_id' not in session:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    data = request.json or {}
    cart = data.get('cart') or []
    payment_method = data.get('payment_method')
    total_amount = data.get('total_amount')
    gcash_ref = data.get('gcash_ref')
    customer = data.get('customer') or {}
    customer_name = (customer.get('name') or '').strip()
    customer_contact = (customer.get('contact') or '').strip()
    customer_email = (customer.get('email') or '').strip().lower()
    customer_user_id = customer.get('customer_id')

    if not cart:
        return jsonify({"status": "error", "message": "Cart is empty."}), 400

    if payment_method == 'GCash' and not (gcash_ref or '').strip():
        return jsonify({"status": "error", "message": "GCash reference number is required before confirming payment."}), 400

    receipt_no = f"JS-{datetime.now().strftime('%Y%m%d%H%M%S')}-{random.randint(100, 999)}"

    try:
        # Pre-check stock before creating a receipt so zero-stock items cannot be sold.
        for item in cart:
            if item.get('type') == 'Part':
                qty = int(item.get('qty', 0) or 0)
                sku = item.get('sku')
                inv_check = supabase.table("inventory").select("item_name, stock_qty, status").eq("sku", sku).execute()
                if not inv_check.data:
                    return jsonify({"status": "error", "message": f"Item {sku} no longer exists in inventory."}), 400
                inv_item = inv_check.data[0]
                available_qty = int(inv_item.get('stock_qty') or 0)
                if available_qty <= 0:
                    return jsonify({"status": "error", "message": f"{inv_item.get('item_name')} is already Out of Stock."}), 400
                if qty > available_qty:
                    return jsonify({"status": "error", "message": f"Only {available_qty} stock left for {inv_item.get('item_name')}."}), 400

        # 1. Create Transaction
        tx_res = supabase.table("sales_transactions").insert({
            "receipt_no": receipt_no,
            "cashier_id": session['user_id'],
            "total_amount": total_amount,
            "payment_method": payment_method,
            "gcash_reference": gcash_ref,
            "customer_name": customer_name or None,
            "customer_contact": customer_contact or None,
            "customer_email": customer_email or None,
            "customer_user_id": customer_user_id,
            "user_id": session['user_id']
        }).execute()

        if not tx_res.data:
            return jsonify({"status": "error", "message": "Transaction failed"}), 500

        transaction_id = tx_res.data[0]['transaction_id']

        # 2. Insert Items & Auto-Deduct Inventory
        for item in cart:
            qty = int(item.get('qty', 0) or 0)
            price = float(item.get('price', 0) or 0)

            sales_item_payload = {
                "transaction_id": transaction_id,
                "item_description": item['name'],
                "item_type": item['type'],
                "qty": qty,
                "unit_price": price,
                "subtotal": (qty * price)
            }
            if item.get('type') == 'Part':
                sales_item_payload["sku"] = item.get('sku')
            supabase.table("sales_items").insert(sales_item_payload).execute()

            if item['type'] == 'Part':
                item_data = (
                    supabase.table("inventory")
                    .select("stock_qty, par_level")
                    .eq("sku", item['sku'])
                    .execute()
                )

                if item_data.data:
                    current_qty = int(item_data.data[0]['stock_qty'] or 0)
                    par_level = int(item_data.data[0].get('par_level', 5) or 5)

                    new_qty = max(0, current_qty - qty)
                    new_status = compute_status(new_qty, par_level)

                    supabase.table("inventory").update({
                        "stock_qty": new_qty,
                        "status": new_status,
                        "last_updated": now_iso()
                    }).eq("sku", item['sku']).execute()

                    supabase.table("stock_logs").insert({
                        "sku": item['sku'],
                        "action": "Checkout",
                        "qty": qty,
                        "username": session.get('username'),
                        "remarks": f"Sold in receipt {receipt_no}"
                    }).execute()

        receipt_url = url_for('print_receipt', receipt_no=receipt_no)

        # Legacy POS-58 direct print: uses the cart data immediately after checkout,
        # matching the previously working approach.
        cashier_name = session.get('full_name') or session.get('username') or str(session.get('user_id', 'Cashier'))
        print_ok, print_msg = print_receipt_direct_escpos(
            receipt_no=receipt_no,
            cashier_name=cashier_name,
            cart=cart,
            total=total_amount,
            method=payment_method,
            gcash_ref=gcash_ref
        )

        return jsonify({
            "status": "success",
            "message": "Transaction completed!" + (" Printed using POS-58 ESC/POS." if print_ok else " Saved, but POS-58 print failed: " + print_msg),
            "receipt": receipt_no,
            "receipt_url": receipt_url,
            "print_status": "printed" if print_ok else "failed",
            "print_message": print_msg
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# 7. BOOKING & APPOINTMENTS
# ==========================================
@app.route('/book', methods=['GET', 'POST'])
def book_appointment():
    min_date = date.today().isoformat()

    # Fully-developed customer flow: booking requires a logged-in Customer account.
    # The customer identity/email comes from the authenticated session, so no separate cancellation code is needed.
    if not session.get('user_id') or session.get('role') != 'Customer':
        return redirect(url_for('login'))

    user_res = supabase.table("users").select("user_id, full_name, email").eq("user_id", session['user_id']).execute()
    current_user = user_res.data[0] if user_res.data else {}
    account_name = current_user.get('full_name') or session.get('full_name') or ''
    account_email = (current_user.get('email') or session.get('email') or '').strip().lower()

    if not account_email:
        return "Your customer account needs an email address before booking. Please ask the admin to update your account email.", 400

    if request.method == 'POST':
        try:
            customer_name = account_name
            customer_email = account_email
            contact_number = normalize_contact_number(request.form.get('contact_number', ''))
            motorcycle_model = request.form.get('motorcycle_model', '').strip()
            service_category = request.form.get('service_category', '').strip()
            customer_notes = request.form.get('customer_notes', '').strip()
            appointment_date = request.form.get('appointment_date', '').strip()
            appointment_time = request.form.get('appointment_time', '').strip()

            errors = validate_appointment_payload(
                customer_name,
                contact_number,
                motorcycle_model,
                service_category,
                appointment_date,
                appointment_time
            )

            if not errors:
                duplicate_res = (
                    supabase.table("appointments")
                    .select("appointment_id, status")
                    .eq("user_id", session['user_id'])
                    .eq("appointment_date", appointment_date)
                    .eq("appointment_time", appointment_time)
                    .execute()
                )
                duplicate_active = [
                    row for row in (duplicate_res.data or [])
                    if (row.get('status') or 'Pending') not in ['Cancelled', 'Completed', 'No Show']
                ]
                if duplicate_active:
                    errors.append("You already have an active booking for this exact date and time.")

            if not errors:
                slot_res = (
                    supabase.table("appointments")
                    .select("appointment_id, status")
                    .eq("appointment_date", appointment_date)
                    .eq("appointment_time", appointment_time)
                    .execute()
                )
                active_bookings = [
                    row for row in (slot_res.data or [])
                    if (row.get('status') or 'Pending') not in ['Cancelled', 'Completed', 'No Show']
                ]
                _open_time, _close_time, slot_capacity = get_shop_schedule_settings()
                if len(active_bookings) >= slot_capacity:
                    errors.append("This schedule is already full. Please choose another time slot.")

            if errors:
                return render_template(
                    'booking.html',
                    error=" ".join(errors),
                    form_data=request.form,
                    min_date=min_date,
                    shop_open=get_shop_schedule_settings()[0],
                    shop_close=get_shop_schedule_settings()[1],
                    account_name=account_name,
                    account_email=account_email
                )

            service = build_service_request(service_category, customer_notes)
            # Customer account is already OTP-verified before booking, so no second booking email verification is needed.
            email_verification_code = None
            payload = {
                "user_id": session['user_id'],
                "customer_name": customer_name,
                "customer_email": customer_email,
                "contact_number": contact_number,
                "motorcycle_model": motorcycle_model,
                "service_requested": service,
                "appointment_date": appointment_date,
                "appointment_time": appointment_time,
                "status": "Pending",
                "cancel_code": None,
                "email_verification_code": None,
                "email_verified": True,
                "email_verified_at": now_iso()
            }

            booking_res = supabase.table("appointments").insert(payload).execute()
            booking = booking_res.data[0] if booking_res.data else payload

            email_sent, email_message = send_booking_confirmation_email(booking)
            update_payload = {
                "confirmation_email_sent_at": now_iso() if email_sent else None,
                "email_delivery_status": "Sent" if email_sent else f"Failed: {str(email_message)[:180]}"
            }
            try:
                supabase.table("appointments").update(update_payload).eq("appointment_id", booking.get('appointment_id')).execute()
            except Exception as e:
                print(f"Email status update skipped: {e}")
            booking.update(update_payload)

            return render_template(
                'book_success.html',
                name=customer_name,
                email=customer_email,
                date=appointment_date,
                time=appointment_time,
                contact=contact_number,
                status=booking.get('status', 'Pending'),
                appointment_id=booking.get('appointment_id'),
                email_sent=email_sent,
                email_message=email_message,
                verify_url=None,
                account_verified=True
            )

        except Exception as e:
            return render_template(
                'booking.html',
                error=f"Booking error: {str(e)}",
                form_data=request.form,
                min_date=min_date,
                shop_open=get_shop_schedule_settings()[0],
                shop_close=get_shop_schedule_settings()[1],
                account_name=account_name,
                account_email=account_email
            )

    return render_template(
        'booking.html',
        form_data={},
        min_date=min_date,
        shop_open=get_shop_schedule_settings()[0],
        shop_close=get_shop_schedule_settings()[1],
        account_name=account_name,
        account_email=account_email
    )


@app.route('/verify_booking/<int:id>/<code>')
def verify_booking_email(id, code):
    try:
        appt_res = (
            supabase.table("appointments")
            .select("appointment_id, contact_number, email_verification_code, email_verified")
            .eq("appointment_id", id)
            .execute()
        )
        if not appt_res.data:
            return "Booking not found.", 404
        appt = appt_res.data[0]
        if str(appt.get('email_verification_code') or '') != str(code or ''):
            return "Invalid or expired verification link.", 403
        supabase.table("appointments").update({
            "email_verified": True,
            "email_verified_at": now_iso()
        }).eq("appointment_id", id).execute()
        return redirect(url_for('track_booking', contact=appt.get('contact_number', ''), verified='1'))
    except Exception as e:
        return f"Verification Error: {str(e)}", 500


@app.route('/resend_booking_email/<int:id>', methods=['POST'])
def resend_booking_email(id):
    try:
        contact_number = normalize_contact_number(request.form.get('contact_number', ''))
        customer_email = request.form.get('customer_email', '').strip().lower()
        appt_res = (
            supabase.table("appointments")
            .select("*")
            .eq("appointment_id", id)
            .execute()
        )
        if not appt_res.data:
            return "Booking not found.", 404
        appt = appt_res.data[0]
        if contact_number != normalize_contact_number(appt.get('contact_number', '')) or customer_email != (appt.get('customer_email') or '').lower():
            return "Email resend verification failed.", 403
        email_sent, email_message = send_booking_confirmation_email(appt)
        if email_sent:
            supabase.table("appointments").update({
                "confirmation_email_sent_at": now_iso(),
                "email_delivery_status": "Sent"
            }).eq("appointment_id", id).execute()
        return redirect(url_for('track_booking', contact=contact_number))
    except Exception as e:
        return f"Resend Email Error: {str(e)}", 500


@app.route('/track_booking', methods=['GET', 'POST'])
def track_booking():
    status_data = None
    search_contact = ""
    verified = request.args.get('verified') == '1'

    if request.method == 'POST':
        search_contact = normalize_contact_number(request.form.get('contact_number', ''))
    else:
        search_contact = normalize_contact_number(request.args.get('contact', ''))

    if search_contact:
        if is_valid_ph_mobile(search_contact):
            status_data = (
                supabase.table('appointments')
                .select('*')
                .eq('contact_number', search_contact)
                .order('appointment_date', desc=True)
                .execute()
                .data
            ) or []
        else:
            status_data = []

    return render_template('track_booking.html', status_data=status_data, contact=search_contact, verified=verified)


@app.route('/appointments')
@login_required
def appointments_page():
    if session.get('role') not in ['Admin', 'Cashier']:
        return redirect(url_for('index'))

    try:
        status_filter = request.args.get('status', '').strip()
        today = date.today()
        max_date = today.isoformat()
        date_from, date_to = normalize_date_range(
            request.args.get('date_from', ''),
            request.args.get('date_to', ''),
            today
        )
        search = request.args.get('search', '').strip().lower()

        query = supabase.table("appointments").select("*").order("appointment_date", desc=False).order("appointment_time", desc=False)
        if status_filter:
            query = query.eq("status", status_filter)
        if date_from:
            query = query.gte("appointment_date", date_from)
        if date_to:
            query = query.lte("appointment_date", date_to)

        appointments_data = query.execute().data or []

        if search:
            def matches(a):
                haystack = f"{a.get('customer_name','')} {a.get('contact_number','')} {a.get('customer_email','')} {a.get('motorcycle_model','')} {a.get('service_requested','')}".lower()
                return search in haystack
            appointments_data = [a for a in appointments_data if matches(a)]

        status_options = ['Pending', 'Confirmed', 'In Progress', 'Completed', 'Cancelled', 'No Show']
        summary = {s: 0 for s in status_options}
        email_summary = {"verified": 0, "unverified": 0, "sent": 0, "failed": 0, "not_sent": 0}

        for a in appointments_data:
            st = a.get('status') or 'Pending'
            summary[st] = summary.get(st, 0) + 1

            if a.get('email_verified'):
                email_summary["verified"] += 1
            else:
                email_summary["unverified"] += 1

            delivery = (a.get('email_delivery_status') or '').lower()
            if 'sent' in delivery:
                email_summary["sent"] += 1
            elif 'fail' in delivery or 'error' in delivery:
                email_summary["failed"] += 1
            else:
                email_summary["not_sent"] += 1

        return render_template(
            'appointments.html',
            appointments=appointments_data,
            summary=summary,
            email_summary=email_summary,
            status_options=status_options,
            filters={
                "status": status_filter,
                "date_from": date_from,
                "date_to": date_to,
                "search": search
            },
            max_date=max_date
        )
    except Exception as e:
        return f"Appointments Error: {str(e)}", 500

@app.route('/update_appointment/<int:id>', methods=['POST'])
@cashier_or_admin
def update_appointment(id):
    valid_statuses = ['Pending', 'Confirmed', 'In Progress', 'Completed', 'Cancelled', 'No Show']
    new_status = request.form.get('status', 'Pending').strip()
    admin_notes = (request.form.get('admin_notes', '') or '').strip()
    send_email_flag = request.form.get('send_email') == 'on'

    if new_status not in valid_statuses:
        return "Invalid appointment status.", 400

    appt_res = (
        supabase.table("appointments")
        .select("*")
        .eq("appointment_id", id)
        .execute()
    )
    if not appt_res.data:
        return "Appointment not found.", 404

    appointment = appt_res.data[0]
    old_status = appointment.get('status') or 'Pending'

    update_payload = {
        "status": new_status,
        "updated_by": session.get('username', 'Admin'),
        "updated_at": now_iso()
    }

    if admin_notes:
        update_payload["admin_notes"] = admin_notes

    if new_status == 'Cancelled':
        update_payload.update({
            "cancelled_at": now_iso(),
            "cancellation_requested_by": session.get('username', 'Admin'),
            "cancellation_reason": admin_notes or appointment.get('cancellation_reason')
        })

    try:
        supabase.table("appointments").update(update_payload).eq("appointment_id", id).execute()
    except Exception as e:
        # Fallback for older Supabase schemas/schema cache: update only the required status field.
        print(f"Appointment status metadata update fallback: {e}")
        supabase.table("appointments").update({"status": new_status}).eq("appointment_id", id).execute()

    # Send email update only when staff checked the checkbox and status changed or notes were added.
    if send_email_flag and appointment.get('customer_email') and (new_status != old_status or admin_notes):
        appointment.update(update_payload)
        if new_status == 'Cancelled':
            email_sent, email_msg = send_booking_cancelled_email(appointment)
        else:
            email_sent, email_msg = send_booking_status_update_email(appointment, new_status, admin_notes)

        try:
            supabase.table("appointments").update({
                "email_delivery_status": "Sent" if email_sent else f"Failed: {str(email_msg)[:180]}",
                "confirmation_email_sent_at": now_iso() if email_sent else appointment.get("confirmation_email_sent_at")
            }).eq("appointment_id", id).execute()
        except Exception as e:
            print(f"Appointment email status update skipped: {e}")

    return redirect(url_for('appointments_page'))


@app.route('/delete_appointment/<int:id>', methods=['POST'])
@cashier_or_admin
def delete_appointment(id):
    supabase.table("appointments").update({
        "status": "Cancelled",
        "cancelled_at": now_iso(),
        "cancellation_requested_by": session.get('username', 'Admin')
    }).eq("appointment_id", id).execute()

    return redirect(url_for('appointments_page'))


@app.route('/cancel_booking/<int:id>', methods=['POST'])
@login_required
def cancel_booking(id):
    try:
        cancel_reason = (request.form.get('cancel_reason', '') or '').strip()

        appt_res = (
            supabase.table("appointments")
            .select("*")
            .eq("appointment_id", id)
            .execute()
        )

        if not appt_res.data:
            return "Appointment not found.", 404

        appt = appt_res.data[0]
        current_status = appt.get('status') or 'Pending'

        if current_status not in ['Pending', 'Confirmed']:
            return "Only Pending or Confirmed bookings can be cancelled by the customer.", 400

        if session.get('role') != 'Customer' or appt.get('user_id') != session.get('user_id'):
            return "Please login using the customer account that owns this booking to cancel it.", 403

        update_payload = {
            "status": "Cancelled",
            "cancelled_at": now_iso(),
            "cancellation_requested_by": session.get('username', 'Customer Portal'),
            "cancellation_reason": cancel_reason or None
        }
        supabase.table("appointments").update(update_payload).eq("appointment_id", id).execute()
        appt.update(update_payload)
        send_booking_cancelled_email(appt)

        return redirect(url_for('customer_portal'))

    except Exception as e:
        return f"Cancellation Error: {str(e)}", 500


@app.route('/api/available_slots')
@app.route('/available_slots')
@app.route('/api/slots')
def available_slots():
    """Return available booking slots as JSON only.

    This endpoint must never return an HTML error page because the booking page
    reads it using fetch(...).json().
    """
    appointment_date = request.args.get('date', '').strip()

    if not appointment_date:
        return jsonify({"status": "error", "message": "date is required", "slots": []}), 400

    try:
        selected_date = date.fromisoformat(appointment_date)
    except Exception:
        return jsonify({"status": "error", "message": "invalid date", "slots": []}), 400

    try:
        # Prevent past-date slot loading.
        if selected_date < date.today():
            return jsonify({"status": "success", "slots": [], "message": "Past dates are not available."})

        slot_rows = (
            supabase.table("appointments")
            .select("appointment_time, status")
            .eq("appointment_date", appointment_date)
            .execute()
            .data
        ) or []

        active_counts = {}
        for row in slot_rows:
            status = row.get('status') or 'Pending'
            if status in ['Cancelled', 'Completed', 'No Show']:
                continue
            appt_time = normalize_time_hhmm(row.get('appointment_time'), '')
            if not appt_time:
                continue
            active_counts[appt_time] = active_counts.get(appt_time, 0) + 1

        shop_open_time, shop_close_time, slot_capacity = get_shop_schedule_settings()

        start_dt = datetime.strptime(normalize_time_hhmm(shop_open_time, SHOP_OPEN_TIME), "%H:%M")
        end_dt = datetime.strptime(normalize_time_hhmm(shop_close_time, SHOP_CLOSE_TIME), "%H:%M")

        if end_dt < start_dt:
            return jsonify({
                "status": "error",
                "message": "Shop close time must be later than opening time. Check Settings.",
                "slots": []
            }), 400

        slots = []
        current = start_dt
        while current <= end_dt:
            time_value = current.strftime("%H:%M")
            used = active_counts.get(time_value, 0)
            slots.append({
                "time": time_value,
                "remaining": max(0, slot_capacity - used),
                "full": used >= slot_capacity
            })
            current += timedelta(minutes=30)

        return jsonify({
            "status": "success",
            "slots": slots,
            "shop_open": shop_open_time,
            "shop_close": shop_close_time,
            "slot_capacity": slot_capacity
        })

    except Exception as e:
        print(f"Available slots API error: {e}")
        return jsonify({
            "status": "error",
            "message": f"Unable to load slots: {str(e)}",
            "slots": []
        }), 500


# ==========================================
# 8. SALES HISTORY & REPORTS
# ==========================================
@app.route('/sales')
@cashier_or_admin
def sales_history():
    try:
        today = date.today()
        max_date = today.isoformat()
        start_date, end_date = normalize_date_range(
            request.args.get('start_date', ''),
            request.args.get('end_date', ''),
            today
        )
        payment_method = request.args.get('payment_method', '').strip()
        customer_search = request.args.get('customer', '').strip().lower()
        receipt_search = request.args.get('receipt', '').strip().lower()
        gcash_ref_search = request.args.get('gcash_ref', '').strip().lower()
        record_status = request.args.get('record_status', 'active').strip().lower()
        if record_status not in ['active', 'archived', 'voided', 'all']:
            record_status = 'active'

        sales = (
            supabase.table("sales_transactions")
            .select("*")
            .order("transaction_date", desc=True)
            .limit(1000)
            .execute()
            .data or []
        )
        sales_items = supabase.table("sales_items").select("*").execute().data or []

        filtered_sales = []
        for sale in sales:
            if not sale_matches_record_status(sale, record_status):
                continue

            tx_date = str(sale.get('transaction_date', ''))[:10]
            method = sale.get('payment_method') or ''

            if start_date and tx_date < start_date:
                continue
            if end_date and tx_date > end_date:
                continue
            if payment_method and method != payment_method:
                continue
            if customer_search:
                customer_blob = f"{sale.get('customer_name','')} {sale.get('customer_email','')} {sale.get('customer_contact','')}".lower()
                if customer_search not in customer_blob:
                    continue
            if receipt_search and receipt_search not in str(sale.get('receipt_no','')).lower():
                continue
            if gcash_ref_search and gcash_ref_search not in str(sale.get('gcash_reference','')).lower():
                continue

            filtered_sales.append(sale)

        items_by_tx = {}
        for item in sales_items:
            tx_id = item.get('transaction_id')
            items_by_tx.setdefault(tx_id, []).append(item)

        for sale in filtered_sales:
            sale['items'] = items_by_tx.get(sale.get('transaction_id'), [])

        total_sales = sum(float(s.get('total_amount') or 0) for s in filtered_sales)
        total_transactions = len(filtered_sales)
        cash_total = sum(float(s.get('total_amount') or 0) for s in filtered_sales if s.get('payment_method') == 'Cash')
        gcash_total = sum(float(s.get('total_amount') or 0) for s in filtered_sales if s.get('payment_method') == 'GCash')
        avg_transaction = total_sales / total_transactions if total_transactions else 0
        missing_gcash_refs = sum(1 for s in filtered_sales if s.get('payment_method') == 'GCash' and not s.get('gcash_reference'))

        item_summary = {}
        for sale in filtered_sales:
            for item in sale.get('items', []):
                name = item.get('item_description') or 'Unnamed Item'
                item_type = item.get('item_type') or 'Item'
                if name not in item_summary:
                    item_summary[name] = {'name': name, 'type': item_type, 'qty': 0, 'subtotal': 0}
                item_summary[name]['qty'] += int(item.get('qty') or 0)
                item_summary[name]['subtotal'] += float(item.get('subtotal') or 0)
        top_items = sorted(item_summary.values(), key=lambda x: x['subtotal'], reverse=True)[:10]

        customer_summary = {}
        for sale in filtered_sales:
            customer_key = sale.get('customer_user_id') or sale.get('customer_email') or sale.get('customer_contact') or sale.get('customer_name')
            if not customer_key:
                continue
            customer_name = sale.get('customer_name') or sale.get('customer_email') or sale.get('customer_contact') or 'Customer'
            if customer_key not in customer_summary:
                customer_summary[customer_key] = {'name': customer_name, 'count': 0, 'total': 0}
            customer_summary[customer_key]['count'] += 1
            customer_summary[customer_key]['total'] += float(sale.get('total_amount') or 0)
        top_customers = sorted(customer_summary.values(), key=lambda x: x['total'], reverse=True)[:10]

        filters = {
            "start_date": start_date,
            "end_date": end_date,
            "payment_method": payment_method,
            "customer": customer_search,
            "receipt": receipt_search,
            "gcash_ref": gcash_ref_search,
            "record_status": record_status
        }

        status_urls = {}
        for status in ['active', 'archived', 'voided', 'all']:
            args = {k: v for k, v in filters.items() if v}
            args['record_status'] = status
            status_urls[status] = url_for('sales_history', **args)

        return render_template(
            'sales_history.html',
            sales=filtered_sales,
            total_sales=total_sales,
            total_transactions=total_transactions,
            cash_total=cash_total,
            gcash_total=gcash_total,
            avg_transaction=avg_transaction,
            missing_gcash_refs=missing_gcash_refs,
            top_items=top_items,
            top_customers=top_customers,
            filters=filters,
            start_date=start_date,
            end_date=end_date,
            payment_method=payment_method,
            customer_search=customer_search,
            receipt_search=receipt_search,
            gcash_ref_search=gcash_ref_search,
            record_status=record_status,
            status_urls=status_urls,
            max_date=max_date
        )
    except Exception as e:
        return f"Database Error: {str(e)}"

@app.route('/export_sales_excel')
@cashier_or_admin
def export_sales_excel():
    today = date.today()
    start_date, end_date = normalize_date_range(
        request.args.get('start_date', ''),
        request.args.get('end_date', ''),
        today
    )
    payment_method = request.args.get('payment_method', '').strip()
    customer_search = request.args.get('customer', '').strip().lower()
    receipt_search = request.args.get('receipt', '').strip().lower()
    gcash_ref_search = request.args.get('gcash_ref', '').strip().lower()
    record_status = request.args.get('record_status', 'active').strip().lower()
    if record_status not in ['active', 'archived', 'voided', 'all']:
        record_status = 'active'

    tx_res = supabase.table("sales_transactions").select("*").order("transaction_date", desc=True).execute().data or []
    items_res = supabase.table("sales_items").select("*").execute().data or []

    filtered_tx = []
    for sale in tx_res:
        if not sale_matches_record_status(sale, record_status):
            continue

        tx_date = str(sale.get('transaction_date', ''))[:10]
        method = sale.get('payment_method') or ''

        if start_date and tx_date < start_date:
            continue
        if end_date and tx_date > end_date:
            continue
        if payment_method and method != payment_method:
            continue
        if customer_search:
            customer_blob = f"{sale.get('customer_name','')} {sale.get('customer_email','')} {sale.get('customer_contact','')}".lower()
            if customer_search not in customer_blob:
                continue
        if receipt_search and receipt_search not in str(sale.get('receipt_no','')).lower():
            continue
        if gcash_ref_search and gcash_ref_search not in str(sale.get('gcash_reference','')).lower():
            continue

        filtered_tx.append(sale)

    allowed_tx_ids = {s.get('transaction_id') for s in filtered_tx}
    filtered_items = [i for i in items_res if i.get('transaction_id') in allowed_tx_ids]

    tx_rows = []
    for s in filtered_tx:
        tx_rows.append({
            "Receipt No": s.get("receipt_no"),
            "Transaction Date": s.get("transaction_date"),
            "Customer Name": s.get("customer_name"),
            "Customer Contact": s.get("customer_contact"),
            "Customer Email": s.get("customer_email"),
            "Payment Method": s.get("payment_method"),
            "GCash Reference": s.get("gcash_reference"),
            "Total Amount": float(s.get("total_amount") or 0),
            "Cashier/User ID": s.get("user_id"),
            "Archived": "Yes" if s.get("is_archived") else "No",
            "Voided": "Yes" if s.get("is_voided") else "No",
            "Void Reason": s.get("void_reason") or ""
        })

    item_rows = []
    for i in filtered_items:
        item_rows.append({
            "Transaction ID": i.get("transaction_id"),
            "Type": i.get("item_type"),
            "SKU": i.get("sku"),
            "Description": i.get("item_description"),
            "Qty": i.get("qty"),
            "Unit Price": float(i.get("unit_price") or 0),
            "Subtotal": float(i.get("subtotal") or 0)
        })

    summary_rows = [
        {"Metric": "Total Sales", "Value": sum(r["Total Amount"] for r in tx_rows)},
        {"Metric": "Total Transactions", "Value": len(tx_rows)},
        {"Metric": "Cash Sales", "Value": sum(r["Total Amount"] for r in tx_rows if r["Payment Method"] == "Cash")},
        {"Metric": "GCash Sales", "Value": sum(r["Total Amount"] for r in tx_rows if r["Payment Method"] == "GCash")},
        {"Metric": "GCash Missing Reference", "Value": sum(1 for r in tx_rows if r["Payment Method"] == "GCash" and not r["GCash Reference"])},
        {"Metric": "Date From", "Value": start_date or "All"},
        {"Metric": "Date To", "Value": end_date or "All"},
        {"Metric": "Payment Filter", "Value": payment_method or "All"},
        {"Metric": "Record Status", "Value": record_status.title()},
        {"Metric": "Customer Filter", "Value": customer_search or "None"},
    ]

    df_summary = pd.DataFrame(summary_rows)
    df_transactions = pd.DataFrame(tx_rows)
    df_items = pd.DataFrame(item_rows)

    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df_summary.to_excel(writer, index=False, sheet_name='Summary')
        df_transactions.to_excel(writer, index=False, sheet_name='Transactions')
        df_items.to_excel(writer, index=False, sheet_name='Itemized Sales')

        for sheet_name in writer.book.sheetnames:
            ws = writer.book[sheet_name]
            for column_cells in ws.columns:
                length = max(len(str(cell.value)) if cell.value is not None else 0 for cell in column_cells)
                ws.column_dimensions[column_cells[0].column_letter].width = min(max(length + 2, 14), 45)
            ws.freeze_panes = "A2"

    output.seek(0)
    return send_file(output, download_name=f"Sales_Report_{date.today()}.xlsx", as_attachment=True)

@app.route('/print_receipt/<receipt_no>')
@cashier_or_admin
def print_receipt(receipt_no):
    tx_res = supabase.table("sales_transactions").select("*").eq("receipt_no", receipt_no).execute()
    if not tx_res.data:
        return "Receipt not found.", 404

    transaction = tx_res.data[0]
    items = supabase.table("sales_items").select("*").eq("transaction_id", transaction['transaction_id']).execute().data or []
    settings = supabase.table("system_settings").select("*").limit(1).execute().data
    shop = settings[0] if settings else {}

    receipt_date = format_receipt_datetime(transaction.get('transaction_date'))
    vat_breakdown = compute_vat_breakdown(transaction.get('total_amount'), 0.12)

    return render_template(
        'receipt.html',
        transaction=transaction,
        items=items,
        shop=shop,
        receipt_date=receipt_date,
        vat_breakdown=vat_breakdown
    )
@app.route('/reprint_direct/<receipt_no>', methods=['POST'])
@cashier_or_admin
def reprint_direct(receipt_no):
    return jsonify({
        "status": "info",
        "message": "Cloud deployment uses browser-based printing. Open the receipt page and click Print.",
        "receipt_url": url_for('print_receipt', receipt_no=receipt_no)
    })


# ==========================================
# 9. SERVICE MANAGEMENT
# ==========================================
@app.route('/services', methods=['GET', 'POST'])
@admin_only
def manage_services():
    try:
        if request.method == 'POST':
            service_name = request.form.get('service_name', '').strip()
            labor_fee = safe_float(request.form.get('labor_fee'), 0)
            category = request.form.get('category', 'Labor').strip() or 'Labor'
            description = request.form.get('description', '').strip()
            duration_minutes = safe_int(request.form.get('duration_minutes'), 30)

            if not service_name:
                return "Service name is required.", 400
            if labor_fee < 0:
                return "Labor fee cannot be negative.", 400
            if duration_minutes < 0:
                duration_minutes = 0

            supabase.table("services").insert({
                "service_name": service_name,
                "labor_fee": labor_fee,
                "category": category,
                "description": description if description else None,
                "estimated_duration": duration_minutes,
                "is_active": True
            }).execute()
            return redirect(url_for('manage_services'))

        services = (
            supabase.table("services")
            .select("*")
            .order("service_name")
            .execute()
            .data
        ) or []

        search = request.args.get('search', '').strip().lower()
        status_filter = request.args.get('status', '').strip()
        category_filter = request.args.get('category', '').strip()

        if search:
            services = [
                s for s in services
                if search in f"{s.get('service_name','')} {s.get('category','')} {s.get('description','')}".lower()
            ]

        if status_filter == 'Active':
            services = [s for s in services if s.get('is_active') is not False]
        elif status_filter == 'Archived':
            services = [s for s in services if s.get('is_active') is False]

        if category_filter:
            services = [s for s in services if (s.get('category') or 'Labor') == category_filter]

        service_categories = sorted(list(set([(s.get('category') or 'Labor') for s in services] + ['Labor', 'Repair', 'Maintenance', 'Installation', 'Inspection', 'Other'])))

        summary = {
            "total": len(services),
            "active": sum(1 for s in services if s.get('is_active') is not False),
            "archived": sum(1 for s in services if s.get('is_active') is False),
            "average_fee": (sum(float(s.get('labor_fee') or 0) for s in services) / len(services)) if services else 0
        }

        return render_template(
            'services.html',
            services=services,
            service_categories=service_categories,
            summary=summary,
            filters={
                "search": search,
                "status": status_filter,
                "category": category_filter
            }
        )
    except Exception as e:
        return f"Service Management Error: {str(e)}", 500


@app.route('/services/edit/<int:service_id>', methods=['POST'])
@admin_only
def edit_service(service_id):
    try:
        service_name = request.form.get('service_name', '').strip()
        labor_fee = safe_float(request.form.get('labor_fee'), 0)
        category = request.form.get('category', 'Labor').strip() or 'Labor'
        description = request.form.get('description', '').strip()
        duration_minutes = safe_int(request.form.get('duration_minutes'), 30)
        is_active = request.form.get('is_active') == 'on'

        if not service_name:
            return "Service name is required.", 400
        if labor_fee < 0:
            return "Labor fee cannot be negative.", 400

        supabase.table("services").update({
            "service_name": service_name,
            "labor_fee": labor_fee,
            "category": category,
            "description": description if description else None,
            "estimated_duration": duration_minutes,
            "is_active": is_active
        }).eq("service_id", service_id).execute()
        return redirect(url_for('manage_services'))
    except Exception as e:
        return f"Service Edit Error: {str(e)}", 500


@app.route('/services/archive/<int:service_id>', methods=['POST'])
@admin_only
def archive_service(service_id):
    try:
        supabase.table("services").update({"is_active": False}).eq("service_id", service_id).execute()
        return redirect(url_for('manage_services'))
    except Exception as e:
        return f"Service Archive Error: {str(e)}", 500


@app.route('/services/restore/<int:service_id>', methods=['POST'])
@admin_only
def restore_service(service_id):
    try:
        supabase.table("services").update({"is_active": True}).eq("service_id", service_id).execute()
        return redirect(url_for('manage_services'))
    except Exception as e:
        return f"Service Restore Error: {str(e)}", 500


# ==========================================
# 9. USER MANAGEMENT & SETTINGS
# ==========================================
@app.route('/users', methods=['GET', 'POST'])
@admin_only
def manage_users():
    """Staff account management only. Customer accounts are handled in /customers."""
    allowed_staff_roles = ['Admin', 'Cashier', 'Inventory']

    if request.method == 'POST':
        role = request.form.get('role', '').strip()
        if role not in allowed_staff_roles:
            return "Invalid staff role. Customer accounts must be managed under Customer Records.", 400

        email = request.form.get('email', '').strip().lower() or None
        username = request.form.get('username', '').strip()
        full_name = request.form.get('full_name', '').strip()
        password = request.form.get('password', '')

        if not username or not full_name or not password:
            return "Username, full name, and password are required.", 400

        hashed_pw = generate_password_hash(password)
        supabase.table("users").insert({
            "username": username,
            "email": email,
            "email_verified": True if email else False,
            "password_hash": hashed_pw,
            "full_name": full_name,
            "role": role,
            "is_active": True
        }).execute()
        return redirect(url_for('manage_users'))

    all_users = supabase.table("users").select("*").order("created_at", desc=True).execute().data or []
    users_list = [u for u in all_users if u.get('role') != 'Customer']
    return render_template('users.html', users=users_list)



@app.route('/api/customers/search')
@login_required
def api_customers_search():
    """Search registered customers first, then sales-only customers for POS cart assignment."""
    if session.get('role') not in ['Admin', 'Cashier']:
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    q = (request.args.get('q') or '').strip().lower()
    try:
        registered = supabase.table("users").select("user_id, full_name, username, email, email_verified, is_active").eq("role", "Customer").execute().data or []
        sales = supabase.table("sales_transactions").select("customer_user_id, customer_name, customer_contact, customer_email, total_amount, transaction_date, is_voided, is_archived").execute().data or []

        customer_map = {}

        def match_customer(name='', email='', contact=''):
            if not q:
                return True
            haystack = f"{name or ''} {email or ''} {contact or ''}".lower()
            return q in haystack

        def add_customer(key, data):
            if not key:
                return
            if key not in customer_map:
                customer_map[key] = data
            else:
                current = customer_map[key]
                # Registered account always wins over walk-in/sales-only record.
                if data.get("source_rank", 0) > current.get("source_rank", 0):
                    data["transaction_count"] = current.get("transaction_count", 0) + data.get("transaction_count", 0)
                    data["total_spent"] = current.get("total_spent", 0) + data.get("total_spent", 0)
                    if not data.get("last_purchase"):
                        data["last_purchase"] = current.get("last_purchase")
                    customer_map[key] = data
                else:
                    current["transaction_count"] = current.get("transaction_count", 0) + data.get("transaction_count", 0)
                    current["total_spent"] = current.get("total_spent", 0) + data.get("total_spent", 0)
                    if data.get("last_purchase") and (not current.get("last_purchase") or str(data.get("last_purchase")) > str(current.get("last_purchase"))):
                        current["last_purchase"] = data.get("last_purchase")
                    if data.get("contact") and not current.get("contact"):
                        current["contact"] = data.get("contact")
                    if data.get("email") and not current.get("email"):
                        current["email"] = data.get("email")

        for u in registered:
            name = u.get("full_name") or u.get("username") or "Customer"
            email = u.get("email") or ""
            if not match_customer(name, email, ""):
                continue
            key = f"user:{u.get('user_id')}" if u.get('user_id') else normalize_customer_key(email)
            add_customer(key, {
                "customer_id": u.get("user_id"),
                "name": name,
                "contact": "",
                "email": email,
                "registered": True,
                "email_verified": bool(u.get("email_verified")),
                "is_active": bool(u.get("is_active")),
                "transaction_count": 0,
                "total_spent": 0.0,
                "last_purchase": None,
                "source": "Registered Account",
                "source_rank": 2
            })

        for s in sales:
            if s.get("is_voided") or s.get("is_archived"):
                continue
            name = s.get("customer_name") or ""
            email = s.get("customer_email") or ""
            contact = s.get("customer_contact") or ""
            if not (name or email or contact):
                continue
            if not match_customer(name, email, contact):
                continue

            key = (f"user:{s.get('customer_user_id')}" if s.get('customer_user_id') else None) or normalize_customer_key(email) or normalize_phone(contact) or normalize_customer_key(name)
            add_customer(key, {
                "customer_id": None,
                "name": name or "Walk-in Customer",
                "contact": contact,
                "email": email,
                "registered": False,
                "email_verified": False,
                "is_active": True,
                "transaction_count": 1,
                "total_spent": float(s.get("total_amount") or 0),
                "last_purchase": s.get("transaction_date"),
                "source": "Sales Record",
                "source_rank": 1
            })

        customers = list(customer_map.values())
        for c in customers:
            if c.get("transaction_count", 0) >= 5 or c.get("total_spent", 0) >= 10000:
                c["suki_level"] = "Top Suki"
            elif c.get("transaction_count", 0) >= 3 or c.get("total_spent", 0) >= 5000:
                c["suki_level"] = "Suki"
            elif c.get("transaction_count", 0) >= 1:
                c["suki_level"] = "Customer"
            else:
                c["suki_level"] = "Registered"

        customers.sort(key=lambda x: (1 if x.get("registered") else 0, x.get("total_spent", 0), x.get("transaction_count", 0)), reverse=True)
        return jsonify({"status": "success", "customers": customers[:20]})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/customers')
@admin_only
def customer_records():
    """Customer account list + suki ranking based on registered accounts and POS sales."""
    try:
        search = request.args.get('search', '').strip().lower()
        suki_filter = request.args.get('suki_level', '').strip()
        account_filter = request.args.get('account_type', '').strip()

        users = supabase.table("users").select("*").eq("role", "Customer").order("created_at", desc=True).execute().data or []
        tx = supabase.table("sales_transactions").select("*").order("transaction_date", desc=True).execute().data or []

        customer_map = {}

        def make_key(user_id=None, email='', contact='', name=''):
            if user_id:
                return f"user:{user_id}"
            return normalize_customer_key(email) or normalize_phone(contact) or normalize_customer_key(name)

        def ensure_customer(key, name='', email='', contact='', source='Sales Record', user_id=None):
            if not key:
                return None
            if key not in customer_map:
                customer_map[key] = {
                    "key": key,
                    "user_id": user_id,
                    "name": name or 'Walk-in Customer',
                    "email": email or '',
                    "contact": contact or '',
                    "source": source,
                    "has_account": bool(user_id),
                    "email_verified": False,
                    "is_active": True,
                    "created_at": None,
                    "transaction_count": 0,
                    "total_spent": 0.0,
                    "last_purchase": None,
                    "last_receipt": None,
                    "suki_level": "New Customer"
                }
            else:
                c = customer_map[key]
                if name and c.get("name") in ['', 'Walk-in Customer']:
                    c["name"] = name
                if email and not c.get("email"):
                    c["email"] = email
                if contact and not c.get("contact"):
                    c["contact"] = contact
                if user_id:
                    c["user_id"] = user_id
                    c["has_account"] = True
            return customer_map[key]

        for u in users:
            user_id = u.get('user_id')
            email = u.get('email') or ''
            name = u.get('full_name') or u.get('username') or 'Customer'
            key = make_key(user_id=user_id, email=email, name=name)
            c = ensure_customer(key, name=name, email=email, source='Registered Account', user_id=user_id)
            if c:
                c["has_account"] = True
                c["email_verified"] = bool(u.get('email_verified'))
                c["is_active"] = bool(u.get('is_active'))
                c["created_at"] = u.get('created_at')

        for sale in tx:
            if sale.get('is_archived') or sale.get('is_voided'):
                continue

            name = sale.get('customer_name') or ''
            email = sale.get('customer_email') or ''
            contact = sale.get('customer_contact') or ''
            customer_user_id = sale.get('customer_user_id')
            key = make_key(user_id=customer_user_id, email=email, contact=contact, name=name)

            if not key:
                continue

            c = ensure_customer(key, name=name, email=email, contact=contact, source='POS Sales', user_id=customer_user_id)
            amount = float(sale.get('total_amount') or 0)
            c["transaction_count"] += 1
            c["total_spent"] += amount

            tx_date = sale.get('transaction_date')
            if tx_date and (not c["last_purchase"] or str(tx_date) > str(c["last_purchase"])):
                c["last_purchase"] = tx_date
                c["last_receipt"] = sale.get('receipt_no')

        customers = []
        for c in customer_map.values():
            if c["transaction_count"] >= 5 or c["total_spent"] >= 10000:
                c["suki_level"] = "Top Suki"
            elif c["transaction_count"] >= 3 or c["total_spent"] >= 5000:
                c["suki_level"] = "Suki"
            elif c["transaction_count"] >= 1:
                c["suki_level"] = "Customer"
            else:
                c["suki_level"] = "Registered"

            if search:
                blob = f"{c.get('name','')} {c.get('email','')} {c.get('contact','')} {c.get('last_receipt','')}".lower()
                if search not in blob:
                    continue

            if suki_filter and c["suki_level"] != suki_filter:
                continue

            if account_filter == 'Registered' and not c.get('has_account'):
                continue
            if account_filter == 'Sales Only' and c.get('has_account'):
                continue
            if account_filter == 'Verified' and not c.get('email_verified'):
                continue
            if account_filter == 'Unverified' and (not c.get('has_account') or c.get('email_verified')):
                continue

            customers.append(c)

        customers.sort(key=lambda x: (x.get("total_spent", 0), x.get("transaction_count", 0), 1 if x.get("has_account") else 0), reverse=True)

        summary = {
            "total_customers": len(customers),
            "registered_accounts": sum(1 for c in customers if c.get("has_account")),
            "sales_only": sum(1 for c in customers if not c.get("has_account")),
            "verified_accounts": sum(1 for c in customers if c.get("email_verified")),
            "suki_count": sum(1 for c in customers if c.get("suki_level") in ["Suki", "Top Suki"]),
            "top_suki_count": sum(1 for c in customers if c.get("suki_level") == "Top Suki"),
            "total_customer_sales": sum(c.get("total_spent", 0) for c in customers)
        }

        filters = {
            "search": search,
            "suki_level": suki_filter,
            "account_type": account_filter
        }

        return render_template('customers.html', customers=customers, summary=summary, filters=filters)
    except Exception as e:
        return f"Customer Records Error: {str(e)}", 500


@app.route('/export_customers_excel')
@admin_only
def export_customers_excel():
    try:
        # Reuse the same aggregation logic, then export the current filtered customer records.
        search = request.args.get('search', '').strip().lower()
        suki_filter = request.args.get('suki_level', '').strip()
        account_filter = request.args.get('account_type', '').strip()

        users = supabase.table("users").select("*").eq("role", "Customer").order("created_at", desc=True).execute().data or []
        tx = supabase.table("sales_transactions").select("*").order("transaction_date", desc=True).execute().data or []

        customer_map = {}

        def make_key(user_id=None, email='', contact='', name=''):
            if user_id:
                return f"user:{user_id}"
            return normalize_customer_key(email) or normalize_phone(contact) or normalize_customer_key(name)

        def ensure_customer(key, name='', email='', contact='', source='Sales Record', user_id=None):
            if not key:
                return None
            if key not in customer_map:
                customer_map[key] = {
                    "key": key, "user_id": user_id, "name": name or 'Walk-in Customer',
                    "email": email or '', "contact": contact or '', "source": source,
                    "has_account": bool(user_id), "email_verified": False, "is_active": True,
                    "created_at": None, "transaction_count": 0, "total_spent": 0.0,
                    "last_purchase": None, "last_receipt": None, "suki_level": "New Customer"
                }
            else:
                c = customer_map[key]
                if name and c.get("name") in ['', 'Walk-in Customer']:
                    c["name"] = name
                if email and not c.get("email"):
                    c["email"] = email
                if contact and not c.get("contact"):
                    c["contact"] = contact
                if user_id:
                    c["user_id"] = user_id
                    c["has_account"] = True
            return customer_map[key]

        for u in users:
            user_id = u.get('user_id')
            email = u.get('email') or ''
            name = u.get('full_name') or u.get('username') or 'Customer'
            key = make_key(user_id=user_id, email=email, name=name)
            c = ensure_customer(key, name=name, email=email, source='Registered Account', user_id=user_id)
            if c:
                c["has_account"] = True
                c["email_verified"] = bool(u.get('email_verified'))
                c["is_active"] = bool(u.get('is_active'))
                c["created_at"] = u.get('created_at')

        for sale in tx:
            if sale.get('is_archived') or sale.get('is_voided'):
                continue
            name = sale.get('customer_name') or ''
            email = sale.get('customer_email') or ''
            contact = sale.get('customer_contact') or ''
            customer_user_id = sale.get('customer_user_id')
            key = make_key(user_id=customer_user_id, email=email, contact=contact, name=name)
            if not key:
                continue
            c = ensure_customer(key, name=name, email=email, contact=contact, source='POS Sales', user_id=customer_user_id)
            amount = float(sale.get('total_amount') or 0)
            c["transaction_count"] += 1
            c["total_spent"] += amount
            tx_date = sale.get('transaction_date')
            if tx_date and (not c["last_purchase"] or str(tx_date) > str(c["last_purchase"])):
                c["last_purchase"] = tx_date
                c["last_receipt"] = sale.get('receipt_no')

        rows = []
        for c in customer_map.values():
            if c["transaction_count"] >= 5 or c["total_spent"] >= 10000:
                c["suki_level"] = "Top Suki"
            elif c["transaction_count"] >= 3 or c["total_spent"] >= 5000:
                c["suki_level"] = "Suki"
            elif c["transaction_count"] >= 1:
                c["suki_level"] = "Customer"
            else:
                c["suki_level"] = "Registered"

            if search:
                blob = f"{c.get('name','')} {c.get('email','')} {c.get('contact','')} {c.get('last_receipt','')}".lower()
                if search not in blob:
                    continue
            if suki_filter and c["suki_level"] != suki_filter:
                continue
            if account_filter == 'Registered' and not c.get('has_account'):
                continue
            if account_filter == 'Sales Only' and c.get('has_account'):
                continue
            if account_filter == 'Verified' and not c.get('email_verified'):
                continue
            if account_filter == 'Unverified' and (not c.get('has_account') or c.get('email_verified')):
                continue

            rows.append({
                "Customer Name": c.get("name"),
                "Contact": c.get("contact"),
                "Email": c.get("email"),
                "Account Type": "Registered" if c.get("has_account") else "Sales Only / Walk-in",
                "Email Verified": "Yes" if c.get("email_verified") else "No",
                "Purchase Count": c.get("transaction_count"),
                "Total Spent": c.get("total_spent"),
                "Last Purchase": c.get("last_purchase"),
                "Last Receipt": c.get("last_receipt"),
                "Suki Level": c.get("suki_level")
            })

        df = pd.DataFrame(rows)
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Customer Records')
            ws = writer.book['Customer Records']
            for column_cells in ws.columns:
                length = max(len(str(cell.value)) if cell.value is not None else 0 for cell in column_cells)
                ws.column_dimensions[column_cells[0].column_letter].width = min(max(length + 2, 14), 45)
            ws.freeze_panes = "A2"

        output.seek(0)
        return send_file(output, download_name=f"Customer_Records_{date.today()}.xlsx", as_attachment=True)
    except Exception as e:
        return f"Customer Export Error: {str(e)}", 500


@app.route('/delete_user/<int:id>', methods=['POST'])
@admin_only
def delete_user(id):
    if id == session.get('user_id'):
        return "Error: You cannot deactivate your own account.", 403
    supabase.table("users").update({"is_active": False}).eq("user_id", id).execute()
    return redirect(url_for('manage_users'))



@app.route('/settings', methods=['GET', 'POST'])
@admin_only
def system_settings():
    try:
        if request.method == 'POST':
            slot_capacity = safe_int(request.form.get('slot_capacity'), MAX_BOOKINGS_PER_SLOT)
            if slot_capacity < 1:
                slot_capacity = MAX_BOOKINGS_PER_SLOT

            current_settings = get_system_settings()
            gcash_qr_url = current_settings.get('gcash_qr_url', '')
            uploaded_qr = request.files.get('gcash_qr_file')
            if uploaded_qr and uploaded_qr.filename:
                gcash_qr_url = save_uploaded_image(uploaded_qr, prefix='gcash_qr')

            payload = {
                "shop_name": request.form.get('shop_name', '').strip() or 'JS Motoworks',
                "shop_address": request.form.get('shop_address', '').strip(),
                "contact_number": request.form.get('contact_number', '').strip(),
                "shop_open_time": request.form.get('shop_open_time', SHOP_OPEN_TIME).strip() or SHOP_OPEN_TIME,
                "shop_close_time": request.form.get('shop_close_time', SHOP_CLOSE_TIME).strip() or SHOP_CLOSE_TIME,
                "slot_capacity": slot_capacity,
                "receipt_footer": request.form.get('receipt_footer', '').strip() or 'Thank you for choosing our shop.',
                "gcash_qr_url": gcash_qr_url,
                "theme_mode": request.form.get('theme_mode', 'general').strip() or 'general'
            }
            save_system_settings(payload)
            return redirect(url_for('system_settings'))
        return render_template('settings.html', settings=get_system_settings())
    except Exception as e:
        return f"Settings Error: {str(e)}", 500



@app.route('/settings/test_email', methods=['POST'])
@admin_only
def settings_test_email():
    try:
        test_email = request.form.get('test_email', '').strip().lower()
        if not is_valid_email(test_email):
            return redirect(url_for('system_settings', email_test='invalid'))

        settings = get_system_settings()
        shop_name = settings.get('shop_name', 'JS Motoworks')
        html = f"""
        <div style="font-family:Arial,sans-serif;line-height:1.6;color:#1f2937">
            <h2>{shop_name} Email Test</h2>
            <p>This is a test email from your JS Motoworks system settings.</p>
            <p>If you received this message, your Resend email configuration is working.</p>
            <p><strong>Base URL:</strong> {get_public_base_url()}</p>
        </div>
        """
        sent, msg = send_transactional_email(
            test_email,
            'Admin',
            f'{shop_name} Email Test',
            html,
            f'{shop_name} Email Test. Base URL: {get_public_base_url()}'
        )
        return redirect(url_for('system_settings', email_test='sent' if sent else 'failed', email_msg=str(msg)[:120]))
    except Exception as e:
        return redirect(url_for('system_settings', email_test='failed', email_msg=str(e)[:120]))


@app.route('/settings/remove_gcash_qr', methods=['POST'])
@admin_only
def remove_gcash_qr():
    try:
        settings = get_system_settings()
        settings['gcash_qr_url'] = ''
        save_system_settings(settings)
        return redirect(url_for('system_settings'))
    except Exception as e:
        return f"Remove QR Error: {str(e)}", 500


@app.route('/archive_sale/<receipt_no>', methods=['POST'])
@admin_only
def archive_sale(receipt_no):
    try:
        tx_res = supabase.table("sales_transactions").select("transaction_id, is_voided").eq("receipt_no", receipt_no).execute()
        if not tx_res.data:
            return "Receipt not found.", 404
        supabase.table("sales_transactions").update({
            "is_archived": True,
            "archived_at": now_iso(),
            "archived_by": session.get('username')
        }).eq("receipt_no", receipt_no).execute()
        return redirect(url_for('sales_history'))
    except Exception as e:
        return f"Archive Error: {str(e)}", 500


@app.route('/restore_sale/<receipt_no>', methods=['POST'])
@admin_only
def restore_sale(receipt_no):
    try:
        tx_res = supabase.table("sales_transactions").select("transaction_id").eq("receipt_no", receipt_no).execute()
        if not tx_res.data:
            return "Receipt not found.", 404

        supabase.table("sales_transactions").update({
            "is_archived": False,
            "archived_at": None,
            "archived_by": None
        }).eq("receipt_no", receipt_no).execute()
        return redirect(url_for('sales_history', record_status='archived'))
    except Exception as e:
        return f"Restore Error: {str(e)}", 500


@app.route('/void_sale/<receipt_no>', methods=['POST'])
@cashier_or_admin
def void_sale(receipt_no):
    try:
        reason = request.form.get('void_reason', '').strip() or 'No reason provided'
        tx_res = supabase.table("sales_transactions").select("*").eq("receipt_no", receipt_no).execute()
        if not tx_res.data:
            return "Receipt not found.", 404

        transaction = tx_res.data[0]
        if transaction.get('is_voided'):
            return "This receipt is already voided.", 400

        items = supabase.table("sales_items").select("*").eq("transaction_id", transaction['transaction_id']).execute().data or []
        for item in items:
            if item.get('item_type') != 'Part':
                continue
            sku = item.get('sku')
            if not sku:
                continue
            qty = int(item.get('qty') or 0)
            inv_res = supabase.table("inventory").select("stock_qty, par_level").eq("sku", sku).execute()
            if inv_res.data:
                current_qty = int(inv_res.data[0].get('stock_qty') or 0)
                par_level = int(inv_res.data[0].get('par_level') or 5)
                restored_qty = current_qty + qty
                supabase.table("inventory").update({
                    "stock_qty": restored_qty,
                    "status": compute_status(restored_qty, par_level),
                    "last_updated": now_iso()
                }).eq("sku", sku).execute()
                supabase.table("stock_logs").insert({
                    "sku": sku,
                    "action": "Void Return",
                    "qty": qty,
                    "username": session.get('username'),
                    "remarks": f"Returned from voided receipt {receipt_no}: {reason}"
                }).execute()

        supabase.table("sales_transactions").update({
            "is_voided": True,
            "void_reason": reason,
            "voided_by": session.get('username'),
            "voided_at": now_iso()
        }).eq("receipt_no", receipt_no).execute()
        if session.get('role') == 'Admin':
            return redirect(url_for('sales_history'))
        return redirect(url_for('pos_page'))
    except Exception as e:
        return f"Void Error: {str(e)}", 500


# ==========================================
# 10. RECEIPT PRINTING NOTE
# ==========================================
# In the cloud-hosted version, receipt printing is handled through the browser.
# The server renders /print_receipt/<receipt_no>, and the cashier uses the browser print dialog.
# This avoids local-only Windows printer dependencies that do not work on Linux cloud hosts.

# ==========================================
# CUSTOMER SIGN-UP
# ==========================================
@app.route('/register', methods=['GET', 'POST'])
def register():
    """Customer self-registration with email OTP verification."""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip().lower()

        try:
            if not username or not password or not full_name or not email:
                return render_template('register.html', error="Full name, username, email, and password are required.")

            if not is_valid_email(email):
                return render_template('register.html', error="Please enter a valid email address.")

            check_user = supabase.table("users").select("user_id").eq("username", username).execute()
            if check_user.data:
                return render_template('register.html', error="Username already exists.")

            check_email = supabase.table("users").select("user_id").eq("email", email).execute()
            if check_email.data:
                return render_template('register.html', error="Email already exists. Please verify or log in using that email.")

            hashed_pw = generate_password_hash(password)
            otp_code = generate_numeric_code(6)

            insert_res = supabase.table("users").insert({
                "username": username,
                "email": email,
                "email_verified": False,
                "email_verification_token": otp_code,
                "email_verification_sent_at": now_iso(),
                "email_verified_at": None,
                "password_hash": hashed_pw,
                "full_name": full_name,
                "role": "Customer",
                "is_active": True
            }).execute()

            new_user = insert_res.data[0] if insert_res.data else {
                "user_id": None,
                "username": username,
                "email": email,
                "full_name": full_name,
                "email_verification_token": otp_code
            }

            sent, email_msg = send_customer_account_verification_email(new_user)

            if sent:
                return render_template(
                    'verify_customer_email.html',
                    email=email,
                    msg="Account created. We sent a 6-digit verification code to your email."
                )

            return render_template(
                'verify_customer_email.html',
                email=email,
                error=f"Account created, but the OTP email was not sent. Check the email environment settings. Error: {email_msg}"
            )

        except Exception as e:
            return render_template('register.html', error=f"Registration error: {str(e)}")

    return render_template('register.html')


@app.route('/verify_customer_email', methods=['GET', 'POST'])
def verify_customer_email():
    """Verify customer account using 6-digit email OTP."""
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        otp_code = re.sub(r'\D+', '', request.form.get('otp_code', '').strip())

        if not is_valid_email(email):
            return render_template('verify_customer_email.html', error="Enter a valid email address.", email=email)

        if not otp_code or len(otp_code) != 6:
            return render_template('verify_customer_email.html', error="Enter the 6-digit OTP code.", email=email)

        try:
            user_res = (
                supabase.table("users")
                .select("user_id, full_name, email, email_verified, email_verification_token")
                .eq("email", email)
                .eq("role", "Customer")
                .execute()
            )

            if not user_res.data:
                return render_template('verify_customer_email.html', error="No customer account found for that email.", email=email)

            user = user_res.data[0]
            if user.get('email_verified'):
                return render_template('login.html', msg="Your email is already verified. You may log in.")

            if str(user.get('email_verification_token') or '').strip() != otp_code:
                return render_template('verify_customer_email.html', error="Invalid OTP code. Please check your email or resend a new code.", email=email)

            supabase.table("users").update({
                "email_verified": True,
                "email_verified_at": now_iso(),
                "email_verification_token": None
            }).eq("user_id", user["user_id"]).execute()

            return render_template('login.html', msg="Email verified successfully. You may now log in.")

        except Exception as e:
            return render_template('verify_customer_email.html', error=f"Verification error: {str(e)}", email=email)

    email = request.args.get('email', '').strip().lower()
    return render_template('verify_customer_email.html', email=email)


@app.route('/resend_customer_otp', methods=['POST'])
def resend_customer_otp():
    """Resend customer verification OTP."""
    email = request.form.get('email', '').strip().lower()

    if not is_valid_email(email):
        return render_template('verify_customer_email.html', error="Enter a valid email address before resending OTP.", email=email)

    try:
        user_res = (
            supabase.table("users")
            .select("user_id, username, full_name, email, email_verified")
            .eq("email", email)
            .eq("role", "Customer")
            .execute()
        )

        if not user_res.data:
            return render_template('verify_customer_email.html', error="No customer account found for that email.", email=email)

        user = user_res.data[0]
        if user.get('email_verified'):
            return render_template('login.html', msg="Your email is already verified. You may log in.")

        otp_code = generate_numeric_code(6)
        supabase.table("users").update({
            "email_verification_token": otp_code,
            "email_verification_sent_at": now_iso()
        }).eq("user_id", user["user_id"]).execute()

        user["email_verification_token"] = otp_code
        sent, email_msg = send_customer_account_verification_email(user)

        if sent:
            return render_template('verify_customer_email.html', email=email, msg="A new OTP code was sent to your email.")

        return render_template('verify_customer_email.html', email=email, error=f"OTP could not be sent. Check email settings. Error: {email_msg}")

    except Exception as e:
        return render_template('verify_customer_email.html', email=email, error=f"Resend OTP error: {str(e)}")


@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    """Customer password reset by email OTP."""
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()

        if not is_valid_email(email):
            return render_template('forgot_password.html', error="Enter a valid email address.", email=email, step='request')

        try:
            user_res = (
                supabase.table("users")
                .select("user_id, username, full_name, email, role, is_active")
                .eq("email", email)
                .eq("role", "Customer")
                .execute()
            )

            if not user_res.data:
                return render_template('forgot_password.html', error="No customer account found for that email.", email=email, step='request')

            user = user_res.data[0]
            if user.get('is_active') is False:
                return render_template('forgot_password.html', error="This customer account is deactivated. Please contact the shop.", email=email, step='request')

            reset_code = generate_numeric_code(6)
            supabase.table("users").update({
                "email_verification_token": f"RESET:{reset_code}",
                "email_verification_sent_at": now_iso()
            }).eq("user_id", user["user_id"]).execute()

            sent, email_msg = send_customer_password_reset_email(user, reset_code)
            if sent:
                return render_template('forgot_password.html', email=email, msg="We sent a password reset code to your email.", step='reset')

            return render_template('forgot_password.html', error=f"Reset email could not be sent. Error: {email_msg}", email=email, step='request')

        except Exception as e:
            return render_template('forgot_password.html', error=f"Password reset error: {str(e)}", email=email, step='request')

    email = request.args.get('email', '').strip().lower()
    return render_template('forgot_password.html', email=email, step='request')


@app.route('/reset_password', methods=['POST'])
def reset_password():
    email = request.form.get('email', '').strip().lower()
    reset_code = re.sub(r'\D+', '', request.form.get('reset_code', '').strip())
    password = request.form.get('password', '')
    confirm_password = request.form.get('confirm_password', '')

    if not is_valid_email(email):
        return render_template('forgot_password.html', error="Enter a valid email address.", email=email, step='reset')
    if not reset_code or len(reset_code) != 6:
        return render_template('forgot_password.html', error="Enter the 6-digit reset code.", email=email, step='reset')
    if len(password) < 6:
        return render_template('forgot_password.html', error="Password must be at least 6 characters.", email=email, step='reset')
    if password != confirm_password:
        return render_template('forgot_password.html', error="Passwords do not match.", email=email, step='reset')

    try:
        user_res = (
            supabase.table("users")
            .select("user_id, email_verification_token, is_active")
            .eq("email", email)
            .eq("role", "Customer")
            .execute()
        )

        if not user_res.data:
            return render_template('forgot_password.html', error="No customer account found for that email.", email=email, step='reset')

        user = user_res.data[0]
        if user.get('is_active') is False:
            return render_template('forgot_password.html', error="This customer account is deactivated. Please contact the shop.", email=email, step='reset')

        expected_token = f"RESET:{reset_code}"
        if str(user.get('email_verification_token') or '').strip() != expected_token:
            return render_template('forgot_password.html', error="Invalid reset code. Please check your email or request a new code.", email=email, step='reset')

        supabase.table("users").update({
            "password_hash": generate_password_hash(password),
            "email_verification_token": None,
            "email_verified": True,
            "email_verified_at": now_iso()
        }).eq("user_id", user["user_id"]).execute()

        return render_template('login.html', msg="Password reset successfully. You may now log in.")

    except Exception as e:
        return render_template('forgot_password.html', error=f"Password reset error: {str(e)}", email=email, step='reset')


@app.route('/verify_account/<int:user_id>/<token>')
def verify_account(user_id, token):
    """Compatibility route for old verification links. New flow uses OTP."""
    try:
        user_res = (
            supabase.table("users")
            .select("email")
            .eq("user_id", user_id)
            .execute()
        )
        email = user_res.data[0].get('email') if user_res.data else ''
        return render_template(
            'verify_customer_email.html',
            email=email,
            msg="This system now uses a 6-digit OTP code. Enter the OTP from your email, or resend a new one."
        )
    except Exception:
        return render_template(
            'verify_customer_email.html',
            msg="This system now uses a 6-digit OTP code. Enter your email and resend a new OTP."
        )


# ==========================================
# CUSTOMER PORTAL (Status Tracker)
# ==========================================
@app.route('/customer_portal')
@login_required
def customer_portal():
    if session.get('role') != 'Customer':
        return redirect(url_for('index'))

    my_bookings = (
        supabase.table("appointments")
        .select("*")
        .eq("user_id", session['user_id'])
        .order("appointment_date", desc=True)
        .execute()
        .data
    ) or []

    return render_template('customer_portal.html', bookings=my_bookings)


if __name__ == '__main__':
    app.run(debug=True)
