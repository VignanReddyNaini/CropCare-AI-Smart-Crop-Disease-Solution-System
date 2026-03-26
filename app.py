"""
KisaanSeva — Complete Farmer Platform v2.0
==========================================
Changes:
  - Producer / Consumer role-based login & registration
  - Removed all synthetic/demo seed data
  - Dynamic location detection (IP geolocation → weather by coords)
  - Market listings tied to logged-in user
  - Real AGMARKNET-style mandi prices fetched fresh each day
  - Auth-protected routes for posting listings / community questions
"""

from flask import (
    Flask, render_template, request, redirect,
    url_for, jsonify, session, g, flash
)
import os, datetime, requests, base64, uuid, re, json, sqlite3, hashlib, traceback

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "kisaanseva-2025-hackathon-secret")

# ─── Folders ──────────────────────────────────────────────────────────────────
UPLOAD_FOLDER = "static/uploads"
DB_PATH       = "kisaanseva.db"
for d in [UPLOAD_FOLDER, "static/icons"]:
    os.makedirs(d, exist_ok=True)

# ─── API Keys ─────────────────────────────────────────────────────────────────
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "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")
GROQ_URL        = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL      = "meta-llama/llama-4-scout-17b-16e-instruct"
WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY", "0c5b50550b42145f2aa4498297c22a48")

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db: db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL,
        phone         TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT 'producer',
        village       TEXT,
        district      TEXT,
        state         TEXT,
        lat           REAL,
        lng           REAL,
        created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS market_listings (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER REFERENCES users(id),
        type        TEXT NOT NULL DEFAULT 'sell',
        category    TEXT NOT NULL DEFAULT 'produce',
        crop        TEXT NOT NULL DEFAULT '',
        quantity    REAL,
        unit        TEXT DEFAULT 'kg',
        price       REAL DEFAULT 0,
        location    TEXT,
        district    TEXT,
        state       TEXT,
        contact     TEXT,
        description TEXT,
        image_path  TEXT,
        lang        TEXT DEFAULT 'en',
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_active   INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS community_posts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER REFERENCES users(id),
        title       TEXT NOT NULL DEFAULT '',
        body        TEXT NOT NULL DEFAULT '',
        category    TEXT DEFAULT 'general',
        author_name TEXT DEFAULT 'Farmer',
        location    TEXT,
        lang        TEXT DEFAULT 'en',
        upvotes     INTEGER DEFAULT 0,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS community_replies (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id     INTEGER REFERENCES community_posts(id),
        user_id     INTEGER REFERENCES users(id),
        body        TEXT NOT NULL DEFAULT '',
        author_name TEXT DEFAULT 'Farmer',
        is_expert   INTEGER DEFAULT 0,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS scan_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER REFERENCES users(id),
        session_id  TEXT,
        crop        TEXT,
        disease     TEXT,
        severity    TEXT,
        result      TEXT,
        image_path  TEXT,
        lang        TEXT,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS mandi_cache (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        state       TEXT,
        district    TEXT,
        commodity   TEXT,
        market      TEXT,
        min_price   REAL,
        max_price   REAL,
        modal_price REAL,
        fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    db.commit()

    # ── MIGRATION: safely add any missing columns to existing tables ──────────
    def add_column_if_missing(db, table, column, col_def):
        try:
            cols = [row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()]
            if column not in cols:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
                db.commit()
                print(f"[MIGRATION] Added column '{column}' to '{table}'")
        except Exception as e:
            print(f"[MIGRATION WARNING] {table}.{column}: {e}")

    # market_listings migrations
    add_column_if_missing(db, "market_listings", "user_id",    "INTEGER REFERENCES users(id)")
    add_column_if_missing(db, "market_listings", "is_active",  "INTEGER DEFAULT 1")
    add_column_if_missing(db, "market_listings", "lang",       "TEXT DEFAULT 'en'")
    add_column_if_missing(db, "market_listings", "image_path", "TEXT")
    add_column_if_missing(db, "market_listings", "district",   "TEXT")
    add_column_if_missing(db, "market_listings", "state",      "TEXT")
    add_column_if_missing(db, "market_listings", "category",   "TEXT DEFAULT 'produce'")

    # community_posts migrations
    add_column_if_missing(db, "community_posts", "user_id",     "INTEGER REFERENCES users(id)")
    add_column_if_missing(db, "community_posts", "upvotes",     "INTEGER DEFAULT 0")
    add_column_if_missing(db, "community_posts", "author_name", "TEXT DEFAULT 'Farmer'")
    add_column_if_missing(db, "community_posts", "location",    "TEXT")
    add_column_if_missing(db, "community_posts", "lang",        "TEXT DEFAULT 'en'")

    # community_replies migrations
    add_column_if_missing(db, "community_replies", "user_id",     "INTEGER REFERENCES users(id)")
    add_column_if_missing(db, "community_replies", "author_name", "TEXT DEFAULT 'Farmer'")
    add_column_if_missing(db, "community_replies", "is_expert",   "INTEGER DEFAULT 0")

    # scan_history migrations
    add_column_if_missing(db, "scan_history", "user_id",    "INTEGER REFERENCES users(id)")
    add_column_if_missing(db, "scan_history", "session_id", "TEXT")
    add_column_if_missing(db, "scan_history", "disease",    "TEXT")
    add_column_if_missing(db, "scan_history", "severity",   "TEXT")

    # users migrations
    add_column_if_missing(db, "users", "village",  "TEXT")
    add_column_if_missing(db, "users", "district", "TEXT")
    add_column_if_missing(db, "users", "state",    "TEXT")
    add_column_if_missing(db, "users", "lat",      "REAL")
    add_column_if_missing(db, "users", "lng",      "REAL")
    add_column_if_missing(db, "users", "role",     "TEXT DEFAULT 'producer'")

    db.close()

init_db()

# ══════════════════════════════════════════════════════════════════════════════
# AUTH HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

def login_required(role=None):
    """Decorator factory — optional role check."""
    def decorator(f):
        from functools import wraps
        @wraps(f)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                flash("Please login to continue.", "warning")
                return redirect(url_for("login", next=request.url, lang=get_lang()))
            if role and user["role"] != role:
                flash(f"This section is for {role}s only.", "error")
                return redirect(url_for("index", lang=get_lang()))
            return f(*args, **kwargs)
        return wrapped
    return decorator

# ══════════════════════════════════════════════════════════════════════════════
# LANGUAGE DATA
# ══════════════════════════════════════════════════════════════════════════════
LANGUAGES = {
    "en":{"name":"English","native":"English"},
    "hi":{"name":"Hindi","native":"हिंदी"},
    "te":{"name":"Telugu","native":"తెలుగు"},
    "ta":{"name":"Tamil","native":"தமிழ்"},
    "kn":{"name":"Kannada","native":"ಕನ್ನಡ"},
    "mr":{"name":"Marathi","native":"मराठी"},
    "gu":{"name":"Gujarati","native":"ગુજરાતી"},
    "pa":{"name":"Punjabi","native":"ਪੰਜਾਬੀ"},
    "bn":{"name":"Bengali","native":"বাংলা"},
    "ml":{"name":"Malayalam","native":"മലയാളം"},
}
LANG_BCP47 = {"en":"en-IN","hi":"hi-IN","te":"te-IN","ta":"ta-IN","kn":"kn-IN","mr":"mr-IN","gu":"gu-IN","pa":"pa-IN","bn":"bn-IN","ml":"ml-IN"}

SOIL_TYPES = {
    "Loamy":"Loamy","Sandy":"Sandy","Clay":"Clay","Black Cotton":"Black Cotton",
    "Red Laterite":"Red Laterite","Alluvial":"Alluvial","Sandy Loam":"Sandy Loam",
    "Clay Loam":"Clay Loam","Silty":"Silty","Saline":"Saline / Usar",
}

# ── Platform UI ───────────────────────────────────────────────────────────────
PLATFORM_UI = {
    "en": {
        "nav_doctor":"🌿 Crop Doctor","nav_mandi":"💰 Mandi","nav_market":"🛒 Market",
        "nav_community":"👥 Community","nav_schemes":"📋 Schemes",
        "platform_name":"KisaanSeva","platform_tagline":"Complete Farmer Platform",
    },
    "hi": {
        "nav_doctor":"🌿 फसल डॉक्टर","nav_mandi":"💰 मंडी","nav_market":"🛒 बाजार",
        "nav_community":"👥 समुदाय","nav_schemes":"📋 योजनाएं",
        "platform_name":"किसान सेवा","platform_tagline":"संपूर्ण किसान मंच",
    },
    "te": {
        "nav_doctor":"🌿 పంట డాక్టర్","nav_mandi":"💰 మండి","nav_market":"🛒 మార్కెట్",
        "nav_community":"👥 సమాజం","nav_schemes":"📋 పథకాలు",
        "platform_name":"కిసాన్ సేవ","platform_tagline":"సంపూర్ణ రైతు వేదిక",
    },
}
for _lang in ["ta","kn","mr","gu","pa","bn","ml"]:
    PLATFORM_UI[_lang] = PLATFORM_UI["en"].copy()

# ── Mandi UI strings ──────────────────────────────────────────────────────────
MANDI_UI = {
    "en": {
        "mandi_title":"💰 Live Mandi Prices","mandi_sub":"Real-time crop prices from local mandis",
        "mandi_search_ph":"Search crops...","mandi_all_crops":"All Crops",
        "mandi_prices_title":"Today's Prices","mandi_nearby_title":"Nearby Mandis",
        "mandi_map_btn":"📍 Map","mandi_wa_btn":"📲 WhatsApp",
        "mandi_modal_min":"Min Price","mandi_modal_modal":"Modal Price","mandi_modal_max":"Max Price",
        "mandi_share_wa":"📲 Share on WhatsApp","mandi_close":"Close",
        "mandi_disclaimer":"⚠️ Prices sourced from AGMARKNET. Actual mandi prices may vary. Always verify with your local APMC before selling.",
        "mandi_footer":"KisaanSeva · Live Mandi Prices · Source: AGMARKNET",
    },
    "hi": {
        "mandi_title":"💰 लाइव मंडी भाव","mandi_sub":"स्थानीय मंडियों से वास्तविक समय फसल भाव",
        "mandi_search_ph":"फसल खोजें...","mandi_all_crops":"सभी फसलें",
        "mandi_prices_title":"आज के भाव","mandi_nearby_title":"नजदीकी मंडियाँ",
        "mandi_map_btn":"📍 मानचित्र","mandi_wa_btn":"📲 व्हाट्सएप",
        "mandi_modal_min":"न्यूनतम","mandi_modal_modal":"मोडल","mandi_modal_max":"अधिकतम",
        "mandi_share_wa":"📲 व्हाट्सएप पर शेयर करें","mandi_close":"बंद करें",
        "mandi_disclaimer":"⚠️ भाव AGMARKNET से हैं। बेचने से पहले स्थानीय APMC से सत्यापित करें।",
        "mandi_footer":"किसान सेवा · लाइव मंडी भाव · स्रोत: AGMARKNET",
    },
    "te": {
        "mandi_title":"💰 లైవ్ మండీ ధరలు","mandi_sub":"స్థానిక మండీల నుండి రియల్-టైమ్ పంట ధరలు",
        "mandi_search_ph":"పంటలు వెతకండి...","mandi_all_crops":"అన్ని పంటలు",
        "mandi_prices_title":"నేటి ధరలు","mandi_nearby_title":"దగ్గరలోని మండీలు",
        "mandi_map_btn":"📍 మ్యాప్","mandi_wa_btn":"📲 వాట్సాప్",
        "mandi_modal_min":"కనిష్ట","mandi_modal_modal":"మోడల్","mandi_modal_max":"గరిష్ట",
        "mandi_share_wa":"📲 వాట్సాప్‌లో షేర్ చేయండి","mandi_close":"మూసివేయి",
        "mandi_disclaimer":"⚠️ ధరలు AGMARKNET నుండి. అమ్మకానికి ముందు స్థానిక APMC తో నిర్ధారించుకోండి.",
        "mandi_footer":"కిసాన్ సేవ · లైవ్ మండీ ధరలు · మూలం: AGMARKNET",
    },
}
for _lang in ["ta","kn","mr","gu","pa","bn","ml"]:
    MANDI_UI[_lang] = MANDI_UI["en"].copy()

# ── Market UI strings ─────────────────────────────────────────────────────────
MARKET_UI = {
    "en": {
        "market_title":"🛒 Farmer Marketplace","market_sub":"Buy and sell crops, inputs, and equipment directly",
        "market_post_btn":"+ Post Listing","market_sell_btn":"View Selling","market_buy_btn":"View Buying",
        "market_filter_label":"Filter:","market_filter_all":"All","market_filter_selling":"Selling","market_filter_buying":"Buying",
        "market_filter_produce":"Produce","market_filter_input":"Inputs",
        "market_search_ph":"Search listings...","market_badge_selling":"🟢 SELLING","market_badge_buying":"🔵 BUYING",
        "market_badge_input":"Input","market_available":"available",
        "market_negotiable":"Negotiable","market_contact":"📲 WhatsApp","market_call":"📞 Call",
        "market_posted":"Posted","market_no_results":"No listings found.",
        "market_be_first":"Be the first to post!",
        "market_safety":"🛡️ Safety tip: Always meet sellers in a public place. Never pay in advance without seeing the product.",
        "market_footer":"KisaanSeva · Farmer Marketplace · Trade safely",
        "market_post_heading":"📝 Post a Listing",
        "market_post_type":"Type","market_post_type_sell":"Selling","market_post_type_buy":"Buying",
        "market_post_category":"Category","market_post_cat_produce":"Produce","market_post_cat_input":"Input / Supply",
        "market_post_crop":"Crop / Product Name","market_post_qty":"Quantity",
        "market_post_unit":"Unit (kg, bags, etc.)","market_post_price":"Price (₹)",
        "market_post_location":"Village / Town","market_post_district":"District",
        "market_post_state":"State","market_post_contact":"Mobile Number",
        "market_post_desc":"Description","market_post_image":"Photo (optional)",
        "market_post_submit":"🚀 Post Listing",
    },
    "hi": {
        "market_title":"🛒 किसान बाज़ार","market_sub":"फसल, इनपुट और उपकरण सीधे खरीदें-बेचें",
        "market_post_btn":"+ लिस्टिंग जोड़ें","market_sell_btn":"बेचने वाले","market_buy_btn":"खरीदने वाले",
        "market_filter_label":"फ़िल्टर:","market_filter_all":"सभी","market_filter_selling":"बेच रहे हैं","market_filter_buying":"खरीद रहे हैं",
        "market_filter_produce":"फसल","market_filter_input":"इनपुट",
        "market_search_ph":"खोजें...","market_badge_selling":"🟢 बेच रहे हैं","market_badge_buying":"🔵 खरीद रहे हैं",
        "market_badge_input":"इनपुट","market_available":"उपलब्ध",
        "market_negotiable":"बातचीत योग्य","market_contact":"📲 व्हाट्सएप","market_call":"📞 कॉल करें",
        "market_posted":"पोस्ट किया","market_no_results":"कोई लिस्टिंग नहीं मिली।",
        "market_be_first":"पहले पोस्ट करें!",
        "market_safety":"🛡️ सुरक्षा सुझाव: विक्रेता से सार्वजनिक स्थान पर मिलें।",
        "market_footer":"किसान सेवा · किसान बाज़ार",
        "market_post_heading":"📝 लिस्टिंग पोस्ट करें",
        "market_post_type":"प्रकार","market_post_type_sell":"बेचना","market_post_type_buy":"खरीदना",
        "market_post_category":"श्रेणी","market_post_cat_produce":"फसल","market_post_cat_input":"इनपुट",
        "market_post_crop":"फसल / उत्पाद का नाम","market_post_qty":"मात्रा",
        "market_post_unit":"इकाई (किग्रा, बैग आदि)","market_post_price":"मूल्य (₹)",
        "market_post_location":"गांव / कस्बा","market_post_district":"जिला",
        "market_post_state":"राज्य","market_post_contact":"मोबाइल नंबर",
        "market_post_desc":"विवरण","market_post_image":"फोटो (वैकल्पिक)",
        "market_post_submit":"🚀 पोस्ट करें",
    },
    "te": {
        "market_title":"🛒 రైతు మార్కెట్","market_sub":"పంటలు, ఇన్‌పుట్లు నేరుగా కొనండి-అమ్మండి",
        "market_post_btn":"+ లిస్టింగ్ జోడించు","market_sell_btn":"అమ్మేవారు","market_buy_btn":"కొనేవారు",
        "market_filter_label":"ఫిల్టర్:","market_filter_all":"అన్నీ","market_filter_selling":"అమ్ముతున్నారు","market_filter_buying":"కొంటున్నారు",
        "market_filter_produce":"పంట","market_filter_input":"ఇన్‌పుట్",
        "market_search_ph":"శోధించండి...","market_badge_selling":"🟢 అమ్ముతున్నారు","market_badge_buying":"🔵 కొంటున్నారు",
        "market_badge_input":"ఇన్‌పుట్","market_available":"అందుబాటులో",
        "market_negotiable":"చర్చనీయం","market_contact":"📲 వాట్సాప్","market_call":"📞 కాల్",
        "market_posted":"పోస్ట్ చేయబడింది","market_no_results":"లిస్టింగ్‌లు కనుగొనబడలేదు.",
        "market_be_first":"మొదటి పోస్ట్ చేయండి!",
        "market_safety":"🛡️ భద్రతా చిట్కా: విక్రేతను బహిరంగ ప్రదేశంలో కలవండి.",
        "market_footer":"కిసాన్ సేవ · రైతు మార్కెట్",
        "market_post_heading":"📝 లిస్టింగ్ పోస్ట్ చేయండి",
        "market_post_type":"రకం","market_post_type_sell":"అమ్మడం","market_post_type_buy":"కొనడం",
        "market_post_category":"వర్గం","market_post_cat_produce":"పంట","market_post_cat_input":"ఇన్‌పుట్",
        "market_post_crop":"పంట / ఉత్పత్తి పేరు","market_post_qty":"పరిమాణం",
        "market_post_unit":"యూనిట్","market_post_price":"ధర (₹)",
        "market_post_location":"గ్రామం / పట్టణం","market_post_district":"జిల్లా",
        "market_post_state":"రాష్ట్రం","market_post_contact":"మొబైల్ నంబర్",
        "market_post_desc":"వివరణ","market_post_image":"ఫోటో (ఐచ్ఛికం)",
        "market_post_submit":"🚀 పోస్ట్ చేయండి",
    },
}
for _lang in ["ta","kn","mr","gu","pa","bn","ml"]:
    MARKET_UI[_lang] = MARKET_UI["en"].copy()

# ── Doctor UI ─────────────────────────────────────────────────────────────────
DOCTOR_UI = {
    "en": {
        "doctor_badge":"AI Crop Disease Detection",
        "doctor_h1_green":"Diagnose your crop,","doctor_h1_gold":"Protect your harvest.",
        "doctor_sub":"Upload a photo of your diseased crop — get an instant AI diagnosis.",
        "doctor_temp":"Temp","doctor_humidity":"Humidity","doctor_weather":"Weather","doctor_season":"Season",
        "doctor_crop_label":"Crop Name","doctor_crop_ph":"e.g. Tomato, Rice, Cotton",
        "doctor_soil_label":"Soil Type","doctor_soil_ph":"Select soil type",
        "doctor_variety_label":"Variety","doctor_variety_ph":"e.g. Hybrid, IR-64",
        "doctor_days_label":"Days After Sowing","doctor_days_ph":"e.g. 45",
        "doctor_demo_label":"No photo? Try Demo Mode","doctor_demo_badge":"Offline",
        "doctor_demo_tomato":"Tomato","doctor_demo_rice":"Rice","doctor_demo_cotton":"Cotton",
        "doctor_upload_title":"Upload Crop Photo","doctor_upload_sub":"JPG, PNG or WEBP · Max 10 MB",
        "doctor_analyze_btn":"🔬 Analyze My Crop","doctor_analyzing":"Analyzing...",
        "doctor_report_title":"Crop Disease Report","doctor_report_sub":"AI-powered diagnosis",
        "doctor_confidence":"AI Confidence","doctor_conf_sub":"Based on image & field conditions",
        "doctor_severity":"Severity Level","doctor_sev_analyzing":"Analyzing...",
        "doctor_wa":"📲 Share on WhatsApp","doctor_listen":"🔊 Listen","doctor_copy":"📋 Copy Report",
        "doctor_check_price":"💰 Check Price",
        "doctor_mandi_label":"Current Mandi Price","doctor_mandi_source":"Source: AGMARKNET",
        "doctor_feat1_title":"Instant AI","doctor_feat1_sub":"Results in 10 seconds",
        "doctor_feat2_title":"10 Languages","doctor_feat2_sub":"Hindi, Telugu, Tamil & more",
        "doctor_feat3_title":"WhatsApp Ready","doctor_feat3_sub":"Share report instantly",
        "doctor_loading":"AI is analyzing your crop...","doctor_footer":"KisaanSeva · Crop Doctor",
    },
    "hi": {
        "doctor_badge":"AI फसल रोग पहचान",
        "doctor_h1_green":"अपनी फसल पहचानें,","doctor_h1_gold":"अपनी फसल बचाएं।",
        "doctor_sub":"रोगग्रस्त फसल की फोटो अपलोड करें — तुरंत AI निदान पाएं।",
        "doctor_temp":"तापमान","doctor_humidity":"नमी","doctor_weather":"मौसम","doctor_season":"ऋतु",
        "doctor_crop_label":"फसल का नाम","doctor_crop_ph":"उदा: टमाटर, चावल, कपास",
        "doctor_soil_label":"मिट्टी का प्रकार","doctor_soil_ph":"मिट्टी का प्रकार चुनें",
        "doctor_variety_label":"किस्म","doctor_variety_ph":"उदा: हाइब्रिड, IR-64",
        "doctor_days_label":"बुवाई के बाद दिन","doctor_days_ph":"उदा: 45",
        "doctor_demo_label":"फोटो नहीं? डेमो मोड आज़माएं","doctor_demo_badge":"ऑफलाइन",
        "doctor_demo_tomato":"टमाटर","doctor_demo_rice":"चावल","doctor_demo_cotton":"कपास",
        "doctor_upload_title":"फसल की फोटो अपलोड करें","doctor_upload_sub":"JPG, PNG या WEBP · अधिकतम 10 MB",
        "doctor_analyze_btn":"🔬 मेरी फसल का विश्लेषण करें","doctor_analyzing":"विश्लेषण हो रहा है...",
        "doctor_report_title":"फसल रोग रिपोर्ट","doctor_report_sub":"AI-संचालित निदान",
        "doctor_confidence":"AI विश्वसनीयता","doctor_conf_sub":"छवि और स्थितियों पर आधारित",
        "doctor_severity":"गंभीरता स्तर","doctor_sev_analyzing":"विश्लेषण हो रहा है...",
        "doctor_wa":"📲 व्हाट्सएप पर शेयर करें","doctor_listen":"🔊 सुनें","doctor_copy":"📋 रिपोर्ट कॉपी करें",
        "doctor_check_price":"💰 भाव जांचें",
        "doctor_mandi_label":"वर्तमान मंडी भाव","doctor_mandi_source":"स्रोत: AGMARKNET",
        "doctor_feat1_title":"तुरंत AI","doctor_feat1_sub":"10 सेकंड में परिणाम",
        "doctor_feat2_title":"10 भाषाएं","doctor_feat2_sub":"हिंदी, तेलुगू, तमिल और अधिक",
        "doctor_feat3_title":"व्हाट्सएप रेडी","doctor_feat3_sub":"तुरंत रिपोर्ट शेयर करें",
        "doctor_loading":"AI आपकी फसल का विश्लेषण कर रहा है...","doctor_footer":"किसान सेवा · फसल डॉक्टर",
    },
    "te": {
        "doctor_badge":"AI పంట వ్యాధి గుర్తింపు",
        "doctor_h1_green":"మీ పంటను నిర్ధారించండి,","doctor_h1_gold":"మీ పంటను కాపాడండి.",
        "doctor_sub":"రోగగ్రస్త పంట ఫోటో అప్‌లోడ్ చేయండి — తక్షణ AI నిర్ధారణ పొందండి.",
        "doctor_temp":"ఉష్ణోగ్రత","doctor_humidity":"తేమ","doctor_weather":"వాతావరణం","doctor_season":"సీజన్",
        "doctor_crop_label":"పంట పేరు","doctor_crop_ph":"ఉదా: టమాటో, వరి, పత్తి",
        "doctor_soil_label":"నేల రకం","doctor_soil_ph":"నేల రకం ఎంచుకోండి",
        "doctor_variety_label":"రకం","doctor_variety_ph":"ఉదా: హైబ్రిడ్, IR-64",
        "doctor_days_label":"నాటిన తర్వాత రోజులు","doctor_days_ph":"ఉదా: 45",
        "doctor_demo_label":"ఫోటో లేదా? డెమో మోడ్ ప్రయత్నించండి","doctor_demo_badge":"ఆఫ్‌లైన్",
        "doctor_demo_tomato":"టమాటో","doctor_demo_rice":"వరి","doctor_demo_cotton":"పత్తి",
        "doctor_upload_title":"పంట ఫోటో అప్‌లోడ్ చేయండి","doctor_upload_sub":"JPG, PNG లేదా WEBP · గరిష్ట 10 MB",
        "doctor_analyze_btn":"🔬 నా పంటను విశ్లేషించండి","doctor_analyzing":"విశ్లేషిస్తోంది...",
        "doctor_report_title":"పంట వ్యాధి నివేదిక","doctor_report_sub":"AI-ఆధారిత నిర్ధారణ",
        "doctor_confidence":"AI నమ్మకం","doctor_conf_sub":"చిత్రం & పరిస్థితులపై ఆధారంగా",
        "doctor_severity":"తీవ్రత స్థాయి","doctor_sev_analyzing":"విశ్లేషిస్తోంది...",
        "doctor_wa":"📲 వాట్సాప్‌లో షేర్ చేయండి","doctor_listen":"🔊 వినండి","doctor_copy":"📋 నివేదిక కాపీ చేయండి",
        "doctor_check_price":"💰 ధర తనిఖీ చేయండి",
        "doctor_mandi_label":"ప్రస్తుత మండీ ధర","doctor_mandi_source":"మూలం: AGMARKNET",
        "doctor_feat1_title":"తక్షణ AI","doctor_feat1_sub":"10 సెకన్లలో ఫలితాలు",
        "doctor_feat2_title":"10 భాషలు","doctor_feat2_sub":"తెలుగు, హిందీ, తమిళం & మరిన్ని",
        "doctor_feat3_title":"వాట్సాప్ రెడీ","doctor_feat3_sub":"తక్షణ నివేదిక షేర్ చేయండి",
        "doctor_loading":"AI మీ పంటను విశ్లేషిస్తోంది...","doctor_footer":"కిసాన్ సేవ · పంట డాక్టర్",
    },
}
for _lang in ["ta","kn","mr","gu","pa","bn","ml"]:
    DOCTOR_UI[_lang] = DOCTOR_UI["en"].copy()

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_lang():
    lang = request.args.get("lang") or request.form.get("lang") or session.get("lang","en")
    if lang not in LANGUAGES: lang = "en"
    session["lang"] = lang
    return lang

def get_platform_ui(lang):
    return PLATFORM_UI.get(lang, PLATFORM_UI["en"])

def get_client_location():
    """Get lat/lng from user's session (set during login) or IP geolocation."""
    user = current_user()
    if user and user["lat"] and user["lng"]:
        return user["lat"], user["lng"], user["district"] or "Your Location"

    # IP-based geolocation fallback
    try:
        ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        if ip in ("127.0.0.1", "::1"):
            # local dev — default to Hyderabad
            return 17.3850, 78.4867, "Hyderabad"
        geo = requests.get(f"http://ip-api.com/json/{ip}?fields=lat,lon,city,regionName", timeout=3).json()
        return geo.get("lat", 17.3850), geo.get("lon", 78.4867), geo.get("city", "Your Location")
    except:
        return 17.3850, 78.4867, "Hyderabad"

def get_weather(lat=None, lng=None, city=None):
    try:
        if lat and lng:
            url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lng}&appid={WEATHER_API_KEY}&units=metric"
        else:
            url = f"https://api.openweathermap.org/data/2.5/weather?q={city or 'Hyderabad'}&appid={WEATHER_API_KEY}&units=metric"
        data = requests.get(url, timeout=5).json()
        return round(data["main"]["temp"]), data["main"]["humidity"], data["weather"][0]["description"].title()
    except:
        return 32, 65, "Partly Cloudy"

def get_weather_forecast(lat=None, lng=None, city=None):
    try:
        if lat and lng:
            url = f"https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lng}&appid={WEATHER_API_KEY}&units=metric&cnt=40"
        else:
            url = f"https://api.openweathermap.org/data/2.5/forecast?q={city or 'Hyderabad'}&appid={WEATHER_API_KEY}&units=metric&cnt=40"
        data = requests.get(url, timeout=5).json()
        days = {}
        for item in data.get("list", []):
            date = item["dt_txt"][:10]
            if date not in days:
                days[date] = {
                    "date": date, "temp_max": item["main"]["temp_max"],
                    "temp_min": item["main"]["temp_min"],
                    "desc": item["weather"][0]["description"].title(),
                    "icon": item["weather"][0]["main"],
                    "humidity": item["main"]["humidity"],
                    "rain": item.get("rain", {}).get("3h", 0),
                }
            else:
                days[date]["temp_max"] = max(days[date]["temp_max"], item["main"]["temp_max"])
                days[date]["temp_min"] = min(days[date]["temp_min"], item["main"]["temp_min"])
                days[date]["rain"] += item.get("rain", {}).get("3h", 0)
        return list(days.values())[:5]
    except:
        return [{"date":(datetime.date.today()+datetime.timedelta(d)).isoformat(),
                 "temp_max":35,"temp_min":24,"desc":"Partly Cloudy","icon":"Clouds","humidity":68,"rain":0}
                for d in range(5)]

def get_season():
    m = datetime.datetime.now().month
    if m in [6,7,8,9]: return "Kharif"
    elif m in [10,11,12,1]: return "Rabi"
    return "Summer"

def get_mime_type(filename):
    ext = filename.rsplit(".",1)[-1].lower()
    return {"jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png","webp":"image/webp"}.get(ext,"image/jpeg")

def call_ai(prompt, image_path=None, original_filename="image.jpg", system_msg=None):
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    content = []
    if image_path and os.path.exists(image_path):
        with open(image_path, "rb") as f:
            img_data = base64.b64encode(f.read()).decode("utf-8")
        content.append({"type":"image_url","image_url":{"url":f"data:{get_mime_type(original_filename)};base64,{img_data}"}})
    content.append({"type":"text","text":prompt})
    system = system_msg or "You are an expert agricultural assistant for Indian farmers. Be concise and practical."
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role":"system","content":system},{"role":"user","content":content}],
        "max_tokens":1200,"temperature":0.3,
    }
    try:
        res = requests.post(GROQ_URL, headers=headers, json=payload, timeout=30)
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print("AI ERROR:", e)
        return None

def generate_gtts_audio(text, lang_code):
    """
    Generate TTS audio entirely in memory and return a base64 data URL.
    This avoids any disk writes — safe for Render's ephemeral filesystem.
    """
    try:
        from gtts import gTTS
        import io
        clean = re.sub(r'[\U00010000-\U0010ffff]', '', text)
        clean = re.sub(r'[\n\r]+', ' . ', clean).strip()[:2200]
        supported = ["en","hi","te","ta","kn","mr","gu","pa","bn","ml"]
        tts = gTTS(text=clean, lang=lang_code if lang_code in supported else "en", slow=False)
        buf = io.BytesIO()
        tts.write_to_fp(buf)
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode("utf-8")
        return "data:audio/mpeg;base64," + b64
    except Exception as e:
        print("gTTS ERROR:", e)
        return None

# ─── AGMARKNET live prices via data.gov.in API ────────────────────────────────
AGMARKNET_KEY = "579b464db66ec23d9b00000125fa3e3462d34f4deb75fae2f8e8b5fe"

def fetch_mandi_prices(state="Telangana", district=None):
    """
    Fetch real mandi prices from data.gov.in AGMARKNET API.
    Returns list of price dicts. Falls back to empty list on error.
    """
    try:
        params = {
            "api-key": AGMARKNET_KEY,
            "format": "json",
            "filters[State]": state,
            "limit": 100,
            "offset": 0,
        }
        if district:
            params["filters[District]"] = district
        url = "https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070"
        resp = requests.get(url, params=params, timeout=8)
        data = resp.json()
        records = data.get("records", [])
        prices = {}
        for r in records:
            crop = r.get("Commodity","").strip().lower()
            if not crop:
                continue
            try:
                modal = float(r.get("Modal_Price","0") or 0)
                mn    = float(r.get("Min_Price","0") or 0)
                mx    = float(r.get("Max_Price","0") or 0)
                if modal <= 0:
                    continue
                # convert from per quintal to per kg where price < 200
                unit = "₹/quintal" if modal > 200 else "₹/kg"
                if crop not in prices or modal > prices[crop]["modal"]:
                    prices[crop] = {
                        "modal": modal, "min": mn, "max": mx,
                        "unit": unit,
                        "market": r.get("Market","").strip(),
                        "district": r.get("District","").strip(),
                        "date": r.get("Arrival_Date",""),
                    }
            except:
                pass
        return prices
    except Exception as e:
        print("AGMARKNET ERROR:", e)
        return {}

def get_mandi_prices(state="Telangana", district=None, search=""):
    prices = fetch_mandi_prices(state, district)
    if search:
        prices = {k:v for k,v in prices.items() if search.lower() in k}
    return prices

# ══════════════════════════════════════════════════════════════════════════════
# GOVERNMENT SCHEMES DATA (real, no synthetic)
# ══════════════════════════════════════════════════════════════════════════════
SCHEMES = [
    {"name":"PM-Kisan Samman Nidhi","icon":"💰",
     "benefit":"₹6,000/year (₹2,000 × 3 installments) directly to bank account",
     "eligibility":"All land-holding farmer families","documents":"Aadhaar, Bank passbook, Land records (Patta)",
     "apply_url":"https://pmkisan.gov.in","helpline":"155261","category":"income_support"},
    {"name":"PM Fasal Bima Yojana (PMFBY)","icon":"🛡️",
     "benefit":"Crop insurance at 1.5–2% premium. Covers drought, flood, pest",
     "eligibility":"All farmers growing notified crops","documents":"Aadhaar, Bank account, Sowing certificate, Land records",
     "apply_url":"https://pmfby.gov.in","helpline":"14447","category":"insurance"},
    {"name":"Kisan Credit Card (KCC)","icon":"💳",
     "benefit":"Credit up to ₹3 lakh at 4% interest rate for crop loans",
     "eligibility":"All farmers, sharecroppers, tenant farmers","documents":"Aadhaar, Land records or rent agreement, Bank account",
     "apply_url":"https://www.nabard.org/kcc","helpline":"1800-180-1551","category":"credit"},
    {"name":"Soil Health Card Scheme","icon":"🌱",
     "benefit":"Free soil testing + personalized fertilizer recommendations every 2 years",
     "eligibility":"All farmers","documents":"Aadhaar, Land details (survey no., village)",
     "apply_url":"https://soilhealth.dac.gov.in","helpline":"1800-180-1551","category":"soil"},
    {"name":"PM Krishi Sinchayee Yojana","icon":"💧",
     "benefit":"Drip & sprinkler irrigation subsidy — up to 55% for small farmers",
     "eligibility":"All farmers with valid land records","documents":"Aadhaar, Land records, Bank account",
     "apply_url":"https://pmksy.gov.in","helpline":"1800-180-1551","category":"irrigation"},
    {"name":"eNAM — National Agriculture Market","icon":"📱",
     "benefit":"Sell directly to buyers pan-India. No middlemen.",
     "eligibility":"All farmers with produce to sell","documents":"Aadhaar, Bank account, Mandi registration",
     "apply_url":"https://enam.gov.in","helpline":"1800-270-0224","category":"market"},
    {"name":"Rashtriya Krishi Vikas Yojana (RKVY)","icon":"🏗️",
     "benefit":"Grants for farm infrastructure, cold storage, processing units",
     "eligibility":"Farmer groups, FPOs, cooperatives","documents":"Group registration, Project proposal, Bank details",
     "apply_url":"https://rkvy.nic.in","helpline":"011-23382651","category":"infrastructure"},
    {"name":"PM Kaushal Vikas Yojana (Agriculture)","icon":"🎓",
     "benefit":"Free skill training in modern farming, organic certification, agri-business",
     "eligibility":"Farmers and rural youth aged 18–35","documents":"Aadhaar, Class 10 certificate",
     "apply_url":"https://pmkvyofficial.org","helpline":"08800-55555","category":"training"},
]

# ══════════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/login", methods=["GET","POST"])
def login():
    lang = get_lang()
    if request.method == "POST":
        phone = request.form.get("phone","").strip()
        pw    = request.form.get("password","").strip()
        role  = request.form.get("role","producer")
        db    = get_db()
        user  = db.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        if not user:
            flash("Phone number not registered. Please sign up.", "error")
            return redirect(url_for("login", lang=lang))
        if user["password_hash"] != hash_password(pw):
            flash("Incorrect password.", "error")
            return redirect(url_for("login", lang=lang))
        session["user_id"] = user["id"]
        session["lang"] = lang
        next_url = request.args.get("next") or url_for("index", lang=lang)
        return redirect(next_url)
    return render_template("login.html",
        lang=lang, languages=LANGUAGES, platform_ui=get_platform_ui(lang))

@app.route("/register", methods=["GET","POST"])
def register():
    lang = get_lang()
    if request.method == "POST":
        name    = request.form.get("name","").strip()
        phone   = request.form.get("phone","").strip()
        pw      = request.form.get("password","").strip()
        role    = request.form.get("role","producer")
        village = request.form.get("village","").strip()
        district= request.form.get("district","").strip()
        state   = request.form.get("state","").strip()
        lat_s   = request.form.get("lat","").strip()
        lng_s   = request.form.get("lng","").strip()

        if not all([name, phone, pw, role]):
            flash("All fields are required.", "error")
            return redirect(url_for("register", lang=lang))
        if len(pw) < 6:
            flash("Password must be at least 6 characters.", "error")
            return redirect(url_for("register", lang=lang))

        db = get_db()
        existing = db.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone()
        if existing:
            flash("This phone number is already registered. Please login.", "error")
            return redirect(url_for("login", lang=lang))

        try:
            lat = float(lat_s) if lat_s else None
            lng = float(lng_s) if lng_s else None
        except:
            lat = lng = None

        db.execute("""INSERT INTO users (name,phone,password_hash,role,village,district,state,lat,lng)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (name, phone, hash_password(pw), role, village, district, state, lat, lng))
        db.commit()
        user = db.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        session["user_id"] = user["id"]
        flash(f"Welcome to KisaanSeva, {name}! 🌿", "success")
        return redirect(url_for("index", lang=lang))
    return render_template("register.html",
        lang=lang, languages=LANGUAGES, platform_ui=get_platform_ui(lang))

@app.route("/logout")
def logout():
    lang = get_lang()
    session.clear()
    return redirect(url_for("login", lang=lang))

@app.route("/profile")
def profile():
    lang = get_lang()
    user = current_user()
    if not user:
        flash("Please login to view your profile.", "warning")
        return redirect(url_for("login", lang=lang))
    db = get_db()
    try:
        listings = db.execute("SELECT * FROM market_listings WHERE user_id=? AND is_active=1 ORDER BY created_at DESC", (user["id"],)).fetchall()
    except Exception:
        listings = []
    try:
        posts = db.execute("SELECT * FROM community_posts WHERE user_id=? ORDER BY created_at DESC LIMIT 10", (user["id"],)).fetchall()
    except Exception:
        posts = []
    try:
        scans = db.execute("SELECT * FROM scan_history WHERE user_id=? ORDER BY created_at DESC LIMIT 5", (user["id"],)).fetchall()
    except Exception:
        scans = []
    try:
        return render_template("profile.html",
            lang=lang, languages=LANGUAGES, platform_ui=get_platform_ui(lang),
            user=user, listings=listings, posts=posts, scans=scans)
    except Exception:
        from flask import make_response
        html = f"""<!DOCTYPE html><html><head><title>Profile</title>
        <meta name="viewport" content="width=device-width,initial-scale=1"/>
        <style>body{{font-family:sans-serif;max-width:700px;margin:2rem auto;padding:1rem;background:#f9f6ef}}
        h2{{color:#1e6b12}}.card{{background:white;border:1px solid #c8e8c0;border-radius:12px;padding:1.25rem;margin-bottom:1rem}}
        .btn{{background:#1e6b12;color:white;padding:.6rem 1.2rem;border-radius:100px;text-decoration:none;font-weight:700;font-size:.85rem;display:inline-block;margin:.25rem}}
        .tag{{background:#edf7ea;color:#1e6b12;padding:3px 10px;border-radius:100px;font-size:.75rem;font-weight:700}}</style></head>
        <body><h2>🧑‍🌾 {user['name']}'s Profile</h2>
        <div class="card">
          <p><strong>📱 Phone:</strong> {user['phone']}</p>
          <p><strong>👤 Role:</strong> <span class="tag">{(user['role'] or 'producer').title()}</span></p>
          <p><strong>📍 Location:</strong> {user['village'] or ''} {user['district'] or ''} {user['state'] or ''}</p>
          <p><strong>📦 Active Listings:</strong> {len(listings)}</p>
          <p><strong>💬 Community Posts:</strong> {len(posts)}</p>
          <p><strong>🔬 Scans Done:</strong> {len(scans)}</p>
        </div>
        <a class="btn" href="/?lang={lang}">🏠 Home</a>
        <a class="btn" href="/market?lang={lang}" style="background:#993c1d">🛒 Market</a>
        <a class="btn" href="/community?lang={lang}" style="background:#534ab7">👥 Community</a>
        <a class="btn" href="/logout" style="background:#555">Logout</a>
        </body></html>"""
        return make_response(html, 200)

# ══════════════════════════════════════════════════════════════════════════════
# CROP DOCTOR (Module 1)
# ══════════════════════════════════════════════════════════════════════════════

DEMO_RESULTS = {
    "tomato":"""🔍 Disease:\nEarly Blight (Alternaria solani) — brown spots with yellow rings on lower leaves.\n🧪 Cause:\nFungal infection spreading in warm, humid weather. Spores travel by wind and water splash.\n⚠️ Severity:\nModerate — treat within 3–5 days to prevent further spread to upper leaves.\n💊 Treatment:\nSpray Mancozeb 75% WP (2.5g per litre of water) every 7 days. Remove and burn infected leaves.\n🌱 Fertilizer:\nApply NPK 19:19:19 @ 5g/litre foliar spray. Supplement with calcium nitrate for stronger cell walls.\n🛡️ Prevention:\nMaintain 60cm spacing. Use drip irrigation, avoid wetting leaves. Rotate crops every season.\n📅 Next Steps:\n1. Spray fungicide tomorrow morning. 2. Re-inspect in 7 days. 3. Contact local KVK if no improvement.""",
    "rice":"""🔍 Disease:\nRice Blast (Magnaporthe oryzae) — diamond-shaped grey lesions with brown borders.\n🧪 Cause:\nFungal spores spread by wind. Thrives in high humidity, cool nights, excessive nitrogen.\n⚠️ Severity:\nSevere — can destroy 30–50% of yield if not treated within 2 days.\n💊 Treatment:\nSpray Tricyclazole 75% WP (0.6g per litre) immediately. Alternatively Isoprothiolane 40% EC (1.5ml/litre).\n🌱 Fertilizer:\nReduce nitrogen now. Apply MOP (Muriate of Potash) @ 40kg/acre to strengthen plant immunity.\n🛡️ Prevention:\nUse blast-resistant varieties (IR-64, Samba Mahsuri). Avoid over-irrigation.\n📅 Next Steps:\n1. Drain field for 2 days. 2. Apply fungicide at sunrise. 3. Monitor every 3 days.""",
    "cotton":"""🔍 Disease:\nFusarium Wilt (Fusarium oxysporum) — yellowing and wilting from bottom leaves.\n🧪 Cause:\nSoil-borne fungus enters through roots. Spreads in waterlogged or compacted soil.\n⚠️ Severity:\nSevere — infected plants rarely recover. Priority is containment to save healthy plants.\n💊 Treatment:\nDrench soil with Carbendazim 50% WP (1g per litre). Remove and destroy wilted plants.\n🌱 Fertilizer:\nApply Trichoderma viride bio-fungicide (4kg/acre) mixed in FYM. Reduce irrigation.\n🛡️ Prevention:\nSolarize field soil before next planting. Use wilt-resistant Bt cotton varieties.\n📅 Next Steps:\n1. Mark and isolate all wilted zones. 2. Soil drench tomorrow. 3. Contact Agriculture Extension Officer.""",
}

def build_crop_prompt(crop, soil, variety, days, temp, humidity, weather, season, lang_code):
    lang_name = LANGUAGES.get(lang_code, {}).get("name","English")
    return f"""You are an expert crop doctor helping Indian farmers.

Crop: {crop} | Soil: {soil} | Variety: {variety or 'Unknown'} | Days After Sowing: {days or 'Unknown'}
Weather: {temp}°C, {humidity}% humidity, {weather}, {season} season

Analyze the image and provide:
🔍 Disease: [name and visual symptoms you see]
🧪 Cause: [biological/environmental cause]
⚠️ Severity: [Mild/Moderate/Severe and why]
💊 Treatment: [specific pesticide/fungicide with dose]
🌱 Fertilizer: [what to apply to help recovery]
🛡️ Prevention: [3 practical prevention tips]
📅 Next Steps: [numbered list of immediate actions]

If the image is NOT a crop/plant photo, respond only with: NOT_A_CROP_IMAGE

Respond ONLY in {lang_name}. Be specific with chemical names and doses."""

@app.route("/predict", methods=["POST"])
def predict():
    """Alias for the crop doctor — redirects POST to the index handler."""
    return index()

@app.route("/", methods=["GET","POST"])
def index():
    lang = get_lang()
    lat, lng, city = get_client_location()
    temp, humidity, weather = get_weather(lat, lng)
    season = get_season()
    ui     = DOCTOR_UI.get(lang, DOCTOR_UI["en"])
    user   = current_user()

    result = error = image = audio_url = None
    crop   = request.form.get("crop","").strip()
    demo_mode = request.args.get("demo") or request.form.get("demo_crop")

    if request.method == "POST" or demo_mode:
        soil     = request.form.get("soil","Loamy")
        variety  = request.form.get("variety","")
        days     = request.form.get("days","")
        demo_key = request.form.get("demo_crop","").lower()

        if demo_key and demo_key in DEMO_RESULTS:
            result = DEMO_RESULTS[demo_key]
            crop   = demo_key
        elif request.method == "POST":
            file = request.files.get("crop_image")
            if not file or not file.filename:
                error = "Please upload a crop image!"
                return render_template("crop_doctor.html",
                    lang=lang, languages=LANGUAGES, platform_ui=ui,
                    ui=ui, lang_bcp47=LANG_BCP47.get(lang,"en-IN"),
                    temp=temp, humidity=humidity, weather=weather, season=season,
                    soil_types=SOIL_TYPES, error=error, result=None, image=None,
                    audio_url=None, crop_name=crop, location_city=city, user=user)
            ext  = file.filename.rsplit(".",1)[-1].lower() if "." in file.filename else "jpg"
            path = os.path.join(UPLOAD_FOLDER, str(uuid.uuid4()) + "." + ext)
            file.save(path)
            image  = path
            prompt = build_crop_prompt(crop, soil, variety, days, temp, humidity, weather, season, lang)
            result = call_ai(prompt, path, file.filename) or "⚠️ AI unavailable. Please try Demo Mode."

            if result and result.strip().startswith("NOT_A_CROP_IMAGE"):
                error = "⚠️ The uploaded image does not appear to be a crop photo. Please upload a clear photo of your crop, leaf, or plant."
                try: os.remove(path)
                except: pass
                return render_template("crop_doctor.html",
                    lang=lang, languages=LANGUAGES, platform_ui=ui,
                    ui=ui, lang_bcp47=LANG_BCP47.get(lang,"en-IN"),
                    temp=temp, humidity=humidity, weather=weather, season=season,
                    soil_types=SOIL_TYPES, error=error, result=None, image=None,
                    audio_url=None, crop_name=crop, location_city=city, user=user)

        # Returns a base64 data URL directly — no disk file needed (safe for Render)
        audio_url = generate_gtts_audio(result, lang) if result else None

        db = get_db()
        db.execute("INSERT INTO scan_history (user_id,session_id,crop,result,image_path,lang) VALUES (?,?,?,?,?,?)",
                   (user["id"] if user else None, session.get("_id","anon"), crop, result,
                    image if not demo_mode else "", lang))
        db.commit()

    return render_template("crop_doctor.html",
        lang=lang, languages=LANGUAGES, platform_ui=ui,
        ui=ui, lang_bcp47=LANG_BCP47.get(lang,"en-IN"),
        temp=temp, humidity=humidity, weather=weather, season=season,
        soil_types=SOIL_TYPES, result=result, image=image, error=error,
        audio_url=audio_url, crop_name=crop, location_city=city, user=user)

# ══════════════════════════════════════════════════════════════════════════════
# MODULE 2 — MANDI PRICES (live from AGMARKNET)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/mandi")
def mandi():
    lang   = get_lang()
    lat, lng, city = get_client_location()
    temp, humidity, weather = get_weather(lat, lng)
    search = request.args.get("q","").lower().strip()
    user   = current_user()

    # Determine state from user profile or default
    state_name = "Telangana"
    district_name = None
    if user and user["state"]:
        state_name = user["state"]
    if user and user["district"]:
        district_name = user["district"]

    prices = get_mandi_prices(state_name, district_name, search)

    return render_template("mandi.html",
        lang=lang, languages=LANGUAGES, platform_ui=get_platform_ui(lang),
        ui=MANDI_UI.get(lang, MANDI_UI["en"]),
        lang_bcp47=LANG_BCP47.get(lang,"en-IN"),
        temp=temp, humidity=humidity, weather=weather, season=get_season(),
        prices=prices, search=search,
        today=datetime.date.today().strftime("%d %b %Y"),
        location_city=city, state_name=state_name, user=user)

@app.route("/api/mandi-price")
def api_mandi_price():
    crop = request.args.get("crop","").lower()
    prices = get_mandi_prices()
    data = prices.get(crop)
    if not data:
        for key, val in prices.items():
            if crop in key or key in crop:
                return jsonify({"found":True,"crop":key,**val})
    if data:
        return jsonify({"found":True,"crop":crop,**data})
    return jsonify({"found":False,"crop":crop,"modal":0})

# ══════════════════════════════════════════════════════════════════════════════
# MODULE 3 — MARKETPLACE
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/market")
def market():
    lang     = get_lang()
    db       = get_db()
    listing_type = request.args.get("type","all")
    category = request.args.get("category","all")
    search   = request.args.get("q","")
    user     = current_user()

    query  = "SELECT ml.*, u.name as seller_name, u.role as seller_role FROM market_listings ml LEFT JOIN users u ON ml.user_id=u.id WHERE ml.is_active=1"
    params = []

    # Consumers see all selling listings by default; producers see all
    if listing_type != "all":
        query += " AND ml.type=?"; params.append(listing_type)
    if category != "all":
        query += " AND ml.category=?"; params.append(category)
    if search:
        query += " AND (ml.crop LIKE ? OR ml.location LIKE ?)"; params += [f"%{search}%",f"%{search}%"]

    # Role-based view hint: consumers see sell listings by default (only when no specific type chosen)
    if user and (user["role"] or "producer") == "consumer" and listing_type == "all" and category == "all" and not search:
        query += " AND ml.type='sell'"
    query += " ORDER BY ml.created_at DESC"

    listings = db.execute(query, params).fetchall()
    return render_template("market.html",
        lang=lang, languages=LANGUAGES, platform_ui=get_platform_ui(lang),
        ui=MARKET_UI.get(lang, MARKET_UI["en"]),
        lang_bcp47=LANG_BCP47.get(lang,"en-IN"),
        listings=listings, listing_type=listing_type,
        category=category, search=search, user=user)

@app.route("/market/post", methods=["GET","POST"])
def market_post():
    lang = get_lang()
    user = current_user()
    if not user:
        flash("Please login to post a listing.", "warning")
        return redirect(url_for("login", lang=lang, next=request.url))

    if request.method == "POST":
        db   = get_db()
        file = request.files.get("image")
        img_path = ""
        if file and file.filename:
            ext  = file.filename.rsplit(".",1)[-1].lower() if "." in file.filename else "jpg"
            path = os.path.join(UPLOAD_FOLDER, str(uuid.uuid4()) + "." + ext)
            file.save(path)
            img_path = path

        db.execute("""INSERT INTO market_listings
            (user_id,type,category,crop,quantity,unit,price,location,district,state,contact,description,image_path,lang)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            user["id"],
            request.form.get("type","sell"),
            request.form.get("category","produce"),
            request.form.get("crop",""),
            float(request.form.get("quantity",0) or 0),
            request.form.get("unit","kg"),
            float(request.form.get("price",0) or 0),
            request.form.get("location", user["village"] or ""),
            request.form.get("district", user["district"] or ""),
            request.form.get("state", user["state"] or ""),
            request.form.get("contact", user["phone"] or ""),
            request.form.get("description",""),
            img_path, lang,
        ))
        db.commit()
        flash("Listing posted successfully! 🌾", "success")
        return redirect(url_for("market", lang=lang))
    return render_template("market_post.html",
        lang=lang, languages=LANGUAGES, platform_ui=get_platform_ui(lang),
        ui=MARKET_UI.get(lang, MARKET_UI["en"]),
        lang_bcp47=LANG_BCP47.get(lang,"en-IN"), user=user)

@app.route("/market/delete/<int:listing_id>", methods=["POST"])
def market_delete(listing_id):
    lang = get_lang()
    user = current_user()
    db   = get_db()
    if user:
        db.execute("UPDATE market_listings SET is_active=0 WHERE id=? AND user_id=?", (listing_id, user["id"]))
        db.commit()
    return redirect(url_for("market", lang=lang))

# ══════════════════════════════════════════════════════════════════════════════
# MODULE 4 — COMMUNITY FORUM
# ══════════════════════════════════════════════════════════════════════════════

COMMUNITY_CATEGORIES = ["general","disease","variety","scheme","market","water","soil","equipment"]

@app.route("/community")
def community():
    lang     = get_lang()
    db       = get_db()
    category = request.args.get("category","all")
    search   = request.args.get("q","")
    user     = current_user()

    query  = "SELECT * FROM community_posts"
    params = []
    wheres = []
    if category != "all":
        wheres.append("category=?"); params.append(category)
    if search:
        wheres.append("(title LIKE ? OR body LIKE ?)"); params += [f"%{search}%",f"%{search}%"]
    if wheres:
        query += " WHERE " + " AND ".join(wheres)
    query += " ORDER BY created_at DESC"

    posts = db.execute(query, params).fetchall()
    posts_with_counts = []
    for p in posts:
        cnt = db.execute("SELECT COUNT(*) FROM community_replies WHERE post_id=?", (p["id"],)).fetchone()[0]
        posts_with_counts.append({"post":p, "reply_count":cnt})

    return render_template("community.html",
        lang=lang, languages=LANGUAGES, platform_ui=get_platform_ui(lang),
        lang_bcp47=LANG_BCP47.get(lang,"en-IN"),
        posts=posts_with_counts, category=category, search=search,
        categories=COMMUNITY_CATEGORIES, user=user)

@app.route("/community/post/<int:post_id>", methods=["GET","POST"])
def community_post(post_id):
    lang  = get_lang()
    db    = get_db()
    post  = db.execute("SELECT * FROM community_posts WHERE id=?", (post_id,)).fetchone()
    user  = current_user()
    if not post:
        return redirect(url_for("community", lang=lang))

    if request.method == "POST":
        if not user:
            flash("Please login to reply.", "warning")
            return redirect(url_for("login", lang=lang))
        body        = request.form.get("body","").strip()
        author_name = user["name"] if user else request.form.get("author_name","Farmer")
        if body:
            db.execute("INSERT INTO community_replies (post_id,user_id,body,author_name) VALUES (?,?,?,?)",
                       (post_id, user["id"] if user else None, body, author_name))
            db.commit()
        return redirect(url_for("community_post", post_id=post_id, lang=lang))

    replies = db.execute("SELECT * FROM community_replies WHERE post_id=? ORDER BY created_at ASC", (post_id,)).fetchall()
    return render_template("community_post.html",
        lang=lang, languages=LANGUAGES, platform_ui=get_platform_ui(lang),
        lang_bcp47=LANG_BCP47.get(lang,"en-IN"),
        post=post, replies=replies, user=user)

@app.route("/community/new", methods=["GET","POST"])
def community_new():
    lang = get_lang()
    user = current_user()
    if not user:
        flash("Please login to ask a question.", "warning")
        return redirect(url_for("login", lang=lang, next=request.url))

    if request.method == "POST":
        db = get_db()
        db.execute("""INSERT INTO community_posts (user_id,title,body,category,author_name,location,lang)
            VALUES (?,?,?,?,?,?,?)""", (
            user["id"],
            request.form.get("title",""),
            request.form.get("body",""),
            request.form.get("category","general"),
            user["name"],
            user["village"] or request.form.get("location",""),
            lang,
        ))
        db.commit()
        return redirect(url_for("community", lang=lang))
    return render_template("community_new.html",
        lang=lang, languages=LANGUAGES, platform_ui=get_platform_ui(lang),
        lang_bcp47=LANG_BCP47.get(lang,"en-IN"),
        categories=COMMUNITY_CATEGORIES, user=user)

@app.route("/community/upvote/<int:post_id>", methods=["POST"])
def community_upvote(post_id):
    db = get_db()
    db.execute("UPDATE community_posts SET upvotes=upvotes+1 WHERE id=?", (post_id,))
    db.commit()
    return jsonify({"ok":True})

@app.route("/community/ask-ai", methods=["POST"])
def community_ask_ai():
    data     = request.get_json()
    question = data.get("question","")
    lang_code= data.get("lang","en")
    lang_name= LANGUAGES.get(lang_code,{}).get("name","English")
    prompt   = f"""A farmer asks: "{question}"
Respond in {lang_name} only. Give a practical, expert agricultural answer in 2-4 sentences.
Focus on: what the problem is, immediate action, and one preventive tip."""
    answer = call_ai(prompt, system_msg="You are an expert agricultural extension officer helping Indian farmers.") or "Sorry, AI is unavailable right now. Please consult your local KVK."
    return jsonify({"answer": answer})

# ══════════════════════════════════════════════════════════════════════════════
# MODULE 5 — GOVT SCHEMES + WEATHER FORECAST
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/schemes")
def schemes():
    lang     = get_lang()
    category = request.args.get("category","all")
    lat, lng, city = get_client_location()
    forecast = get_weather_forecast(lat, lng)
    temp, humidity, weather = get_weather(lat, lng)
    user     = current_user()

    filtered = SCHEMES if category == "all" else [s for s in SCHEMES if s["category"] == category]
    scheme_categories = list(set(s["category"] for s in SCHEMES))

    return render_template("schemes.html",
        lang=lang, languages=LANGUAGES, platform_ui=get_platform_ui(lang),
        lang_bcp47=LANG_BCP47.get(lang,"en-IN"),
        schemes=filtered, category=category, scheme_categories=scheme_categories,
        forecast=forecast, temp=temp, humidity=humidity, weather=weather,
        season=get_season(), today=datetime.date.today().strftime("%A, %d %b %Y"),
        city=city, user=user)

# ══════════════════════════════════════════════════════════════════════════════
# MISC
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/profile/update-location", methods=["POST"])
def profile_update_location():
    lang = get_lang()
    user = current_user()
    if not user:
        return redirect(url_for("login", lang=lang))
    db       = get_db()
    district = request.form.get("district","").strip()
    state    = request.form.get("state","").strip()
    lat_s    = request.form.get("lat","").strip()
    lng_s    = request.form.get("lng","").strip()
    try:
        lat = float(lat_s) if lat_s else None
        lng = float(lng_s) if lng_s else None
    except:
        lat = lng = None
    db.execute("UPDATE users SET district=?, state=?, lat=?, lng=? WHERE id=?",
               (district, state, lat, lng, user["id"]))
    db.commit()
    flash("Location updated! 📍", "success")
    return redirect(url_for("profile", lang=lang))

@app.route("/health")
def health():
    return {"status":"ok","app":"KisaanSeva","version":"2.0","modules":5}, 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
