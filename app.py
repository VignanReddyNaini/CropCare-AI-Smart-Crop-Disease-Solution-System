from flask import (
    Flask, render_template, request, redirect,
    url_for, jsonify, session, g, flash, send_file
)
import os, datetime, requests, base64, uuid, re, json, sqlite3, hashlib
from io import BytesIO

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "kisaanseva-secret")

# ─── Folders ─────────────────────────────────────────────
UPLOAD_FOLDER = "static/uploads"
DB_PATH = "kisaanseva.db"

for d in [UPLOAD_FOLDER]:
    os.makedirs(d, exist_ok=True)

# ─── API Keys (SECURE) ───────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY")

# =======================================================
# AUDIO STREAM (NO DISK STORAGE ✅)
# =======================================================

def generate_gtts_audio_stream(text, lang_code):
    try:
        from gtts import gTTS

        clean = re.sub(r'[\U00010000-\U0010ffff]', '', text)
        clean = re.sub(r'[\n\r]+', ' . ', clean).strip()[:2000]

        mp3_fp = BytesIO()
        tts = gTTS(text=clean, lang=lang_code if lang_code else "en")
        tts.write_to_fp(mp3_fp)
        mp3_fp.seek(0)

        return mp3_fp
    except Exception as e:
        print("TTS ERROR:", e)
        return None

@app.route("/audio")
def audio():
    text = request.args.get("text", "")
    lang = request.args.get("lang", "en")

    stream = generate_gtts_audio_stream(text, lang)
    if not stream:
        return {"error": "Audio failed"}, 500

    return send_file(stream, mimetype="audio/mpeg")

# =======================================================
# DATABASE
# =======================================================

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        phone TEXT UNIQUE,
        password_hash TEXT
    )
    """)
    db.commit()
    db.close()

init_db()

# =======================================================
# AUTH
# =======================================================

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

# =======================================================
# AI CALL
# =======================================================

def call_ai(prompt):
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "llama3-8b-8192",
        "messages": [{"role": "user", "content": prompt}]
    }

    try:
        res = requests.post("https://api.groq.com/openai/v1/chat/completions",
                            headers=headers, json=payload)
        return res.json()["choices"][0]["message"]["content"]
    except:
        return "AI error"

# =======================================================
# ROUTES
# =======================================================

@app.route("/", methods=["GET","POST"])
def index():
    result = None
    audio_url = None

    if request.method == "POST":
        crop = request.form.get("crop")

        prompt = f"Give disease analysis for {crop}"
        result = call_ai(prompt)

        # ✅ STREAM AUDIO URL (NO FILE SAVE)
        if result:
            encoded = requests.utils.quote(result)
            audio_url = f"/audio?text={encoded}&lang=en"

    return render_template("index.html", result=result, audio_url=audio_url)

# =======================================================
# LOGIN
# =======================================================

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        phone = request.form["phone"]
        pw = hash_password(request.form["password"])

        user = get_db().execute(
            "SELECT * FROM users WHERE phone=? AND password_hash=?",
            (phone, pw)
        ).fetchone()

        if user:
            session["user_id"] = user["id"]
            return redirect("/")
        else:
            flash("Invalid login")

    return render_template("login.html")

# =======================================================
# REGISTER
# =======================================================

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        db = get_db()
        db.execute(
            "INSERT INTO users (name, phone, password_hash) VALUES (?,?,?)",
            (
                request.form["name"],
                request.form["phone"],
                hash_password(request.form["password"])
            )
        )
        db.commit()
        return redirect("/login")

    return render_template("register.html")

# =======================================================
# MAIN
# =======================================================

if __name__ == "__main__":
    app.run(debug=True)
