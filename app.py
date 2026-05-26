import os
import smtplib
import sqlite3
import warnings
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from secrets import randbelow, token_urlsafe

import numpy as np
from dotenv import load_dotenv
from flask import Flask, render_template, request
from tensorflow.keras.models import load_model
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "signup.db"))
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY")
MAIL_HOST = os.getenv("MAIL_HOST", "smtp.gmail.com")
MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
MAIL_USERNAME = os.getenv("MAIL_USERNAME", "")
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "")
MAIL_FROM = os.getenv("MAIL_FROM", MAIL_USERNAME)
MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "true").lower() in {"1", "true", "yes", "on"}
SMTP_TIMEOUT = int(os.getenv("SMTP_TIMEOUT", "10"))
OTP_EXPIRY_MINUTES = int(os.getenv("OTP_EXPIRY_MINUTES", "10"))
MODEL_NSL_PATH = Path(os.getenv("MODEL_NSL_PATH", BASE_DIR / "model_nsl.h5"))
MODEL_KDD_PATH = Path(os.getenv("MODEL_KDD_PATH", BASE_DIR / "model_kdd.h5"))

app = Flask(__name__)
if not FLASK_SECRET_KEY:
    raise RuntimeError("FLASK_SECRET_KEY is required")
app.config["SECRET_KEY"] = FLASK_SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

model1 = load_model(MODEL_NSL_PATH)
model2 = load_model(MODEL_KDD_PATH)


def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS info (
                user TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                mobile TEXT NOT NULL,
                name TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_signups (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                mobile TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                otp_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )


def cleanup_pending_signups(conn):
    conn.execute("DELETE FROM pending_signups WHERE expires_at < ?", (datetime.utcnow().isoformat(),))


def password_is_hashed(value):
    return isinstance(value, str) and value.startswith(("pbkdf2:sha256:", "scrypt:", "argon2:"))


def send_otp_email(recipient, otp):
    if not MAIL_USERNAME or not MAIL_PASSWORD or not MAIL_FROM:
        raise RuntimeError("SMTP credentials are missing")

    message = EmailMessage()
    message.set_content(
        f"Your OTP is: {otp}\n\n"
        f"This code expires in {OTP_EXPIRY_MINUTES} minutes.\n"
        "If you did not request this, ignore this email."
    )
    message["Subject"] = "OTP"
    message["From"] = MAIL_FROM
    message["To"] = recipient

    with smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=SMTP_TIMEOUT) as smtp:
        if MAIL_USE_TLS:
            smtp.starttls()
        smtp.login(MAIL_USERNAME, MAIL_PASSWORD)
        smtp.send_message(message)


def store_pending_signup(username, name, email, mobile, password_hash, otp_hash):
    token = token_urlsafe(32)
    now = datetime.utcnow()
    expires_at = now + timedelta(minutes=OTP_EXPIRY_MINUTES)

    with get_db() as conn:
        cleanup_pending_signups(conn)
        conn.execute(
            """
            INSERT INTO pending_signups (
                token, username, name, email, mobile, password_hash, otp_hash, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token,
                username,
                name,
                email,
                mobile,
                password_hash,
                otp_hash,
                now.isoformat(),
                expires_at.isoformat(),
            ),
        )

    return token


def lookup_pending_signup(token):
    with get_db() as conn:
        cleanup_pending_signups(conn)
        return conn.execute("SELECT * FROM pending_signups WHERE token = ?", (token,)).fetchone()


def finalize_signup(token, pending):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO info (`user`, `email`, `password`, `mobile`, `name`) VALUES (?, ?, ?, ?, ?)",
            (
                pending["username"],
                pending["email"],
                pending["password_hash"],
                pending["mobile"],
                pending["name"],
            ),
        )
        conn.execute("DELETE FROM pending_signups WHERE token = ?", (token,))


def predict_class(model, values, shape):
    features = np.asarray(values, dtype=float).reshape(-1, shape, 1)
    probabilities = model.predict(features)
    return int(np.argmax(probabilities, axis=1)[0])


init_db()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/home")
def home():
    return render_template("home.html")


@app.route("/home1")
def home1():
    return render_template("home1.html")


@app.route("/logon")
def logon():
    return render_template("signup.html")


@app.route("/login")
def login():
    return render_template("signin.html")


@app.route("/signup", methods=["POST"])
def signup():
    username = request.form.get("user", "").strip()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    mobile = request.form.get("mobile", "").strip()
    password = request.form.get("password", "")

    if not all([username, name, email, mobile, password]):
        return render_template("signup.html", error="All fields are required.")

    with get_db() as conn:
        cleanup_pending_signups(conn)
        existing_user = conn.execute(
            "SELECT 1 FROM info WHERE user = ? OR email = ?",
            (username, email),
        ).fetchone()
        pending_user = conn.execute(
            "SELECT 1 FROM pending_signups WHERE username = ? OR email = ?",
            (username, email),
        ).fetchone()

    if existing_user or pending_user:
        return render_template("signup.html", error="Account already exists for this user or email.")

    otp = f"{randbelow(1_000_000):06d}"
    password_hash = generate_password_hash(password)
    otp_hash = generate_password_hash(otp)

    try:
        token = store_pending_signup(username, name, email, mobile, password_hash, otp_hash)
        send_otp_email(email, otp)
    except Exception:
        with get_db() as conn:
            conn.execute("DELETE FROM pending_signups WHERE email = ?", (email,))
        app.logger.exception("OTP signup failed")
        return render_template(
            "signup.html",
            error="OTP email failed. Check SMTP config and retry.",
        ), 500

    return render_template("val.html", token=token, email=email)


@app.route("/predict_lo", methods=["POST"])
def predict_lo():
    token = request.form.get("token", "").strip()
    message = request.form.get("message", "").strip()

    pending = lookup_pending_signup(token)
    if pending is None:
        return render_template("signup.html", error="OTP session expired. Sign up again.")

    if not check_password_hash(pending["otp_hash"], message):
        return render_template("val.html", error="Invalid OTP.", token=token, email=pending["email"])

    finalize_signup(token, pending)
    return render_template("signin.html", message="Account verified. Sign in now.")


@app.route("/signin", methods=["POST"])
def signin():
    username = request.form.get("user", "").strip()
    password = request.form.get("password", "")

    if not username or not password:
        return render_template("signin.html", error="Username and password are required.")

    with get_db() as conn:
        user = conn.execute(
            "SELECT user, password FROM info WHERE user = ?",
            (username,),
        ).fetchone()

    if user is None:
        return render_template("signin.html", error="Invalid credentials.")

    stored_password = user["password"] or ""
    valid_password = check_password_hash(stored_password, password) or stored_password == password

    if not valid_password:
        return render_template("signin.html", error="Invalid credentials.")

    if not password_is_hashed(stored_password):
        with get_db() as conn:
            conn.execute(
                "UPDATE info SET password = ? WHERE user = ? AND password = ?",
                (generate_password_hash(password), username, stored_password),
            )

    return render_template("home.html")


@app.route("/notebook1")
def notebook1():
    return render_template("NSLKDD.html")


@app.route("/notebook2")
def notebook2():
    return render_template("KDDCUP.html")


@app.route("/notebook3")
def notebook3():
    return render_template("UNSW_NB15.html")


@app.route("/notebook4")
def notebook4():
    return render_template("Bot_IoT.html")


@app.route("/predict", methods=["POST"])
def predict():
    prediction_class = predict_class(model1, request.form.values(), 12)

    if prediction_class == 0:
        output = "There is an Attack Detected, Attack Type is DDoS!"
    elif prediction_class == 1:
        output = "There is an Attack Detected, Attack Type is Probe!"
    elif prediction_class == 2:
        output = "There is an Attack Detected, Attack Type is R2L!"
    elif prediction_class == 3:
        output = "There is an Attack Detected, Attack Type is U2R!"
    else:
        output = "There is a No Attack Detected, it is Normal!"

    return render_template("prediction.html", output=output)


@app.route("/predict1", methods=["POST"])
def predict1():
    prediction_class = predict_class(model2, request.form.values(), 14)

    if prediction_class == 1:
        output = "There is an Attack Detected, Attack Type is DDoS!"
    else:
        output = "There is a No Attack Detected, it is Normal!"

    return render_template("prediction.html", output=output)


@app.route("/notebook")
def notebook7():
    return render_template("Notebook.html")


if __name__ == "__main__":
    app.run(
        debug=os.getenv("FLASK_DEBUG", "false").lower() in {"1", "true", "yes", "on"}
    )
