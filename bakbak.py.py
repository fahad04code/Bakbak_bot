# app.py
import streamlit as st
import sqlite3
import os
import random
import re
import uuid
import time
from datetime import datetime
from dotenv import load_dotenv
from passlib.hash import bcrypt

load_dotenv()

# Optional AssemblyAI key for audio transcription (set in .env)
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")

# -----------------------
# Paths & helpers
# -----------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bakbak_bot.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

def safe_filename(name: str) -> str:
    name = name.strip().replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# -----------------------
# Database initialization
# -----------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()

    # Users: phone is unique identifier
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        phone TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        age INTEGER NOT NULL,
        gender TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    """)

    # Activities: stores every user action (truth answer, dare upload, meme upload, twister upload)
    c.execute("""
    CREATE TABLE IF NOT EXISTS activities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT NOT NULL,
        activity_type TEXT NOT NULL, -- Truth / Dare / Meme / TongueTwister
        prompt TEXT,                 -- The question/dare/twister text given to user
        response_text TEXT,          -- For text answers
        file_path TEXT,              -- For uploaded file path (if any)
        timestamp TEXT NOT NULL,
        FOREIGN KEY (phone) REFERENCES users (phone)
    );
    """)

    # History of prompts assigned to users (so we can avoid repeats)
    c.execute("""
    CREATE TABLE IF NOT EXISTS truth_dare_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT NOT NULL,
        kind TEXT NOT NULL, -- 'truth' or 'dare' or 'twister'
        prompt TEXT NOT NULL,
        assigned_at TEXT NOT NULL,
        FOREIGN KEY (phone) REFERENCES users (phone)
    );
    """)

    conn.commit()
    conn.close()

init_db()

# -----------------------
# Transcription (optional)
# -----------------------
import requests
def transcribe_with_assemblyai(file_path: str, api_key: str):
    if not api_key:
        return "AssemblyAI API key not configured."
    try:
        headers = {"authorization": api_key}
        with open(file_path, "rb") as f:
            r = requests.post("https://api.assemblyai.com/v2/upload", headers=headers, data=f)
        if r.status_code != 200:
            return f"Upload failed ({r.status_code})"
        upload_url = r.json().get("upload_url")
        if not upload_url:
            return "Upload URL not returned."
        # request transcript
        r2 = requests.post("https://api.assemblyai.com/v2/transcript", headers=headers, json={"audio_url": upload_url})
        if r2.status_code not in (200, 201):
            return f"Transcription request failed ({r2.status_code})"
        tid = r2.json().get("id")
        if not tid:
            return "Transcript id missing."
        # poll for completion (short loop)
        for _ in range(30):
            r3 = requests.get(f"https://api.assemblyai.com/v2/transcript/{tid}", headers=headers)
            if r3.status_code == 200:
                stat = r3.json().get("status")
                if stat == "completed":
                    return r3.json().get("text", "")
                elif stat == "failed":
                    return "Transcription failed."
            time.sleep(1)
        return "Transcription timed out."
    except Exception as e:
        return f"Transcription error: {e}"

# -----------------------
# Prompt generation (infinite feel)
# -----------------------
# Use template lists and random fillers to generate near-infinite unique prompts.
TRUTH_TEMPLATES = [
    "What's a time you felt {emotion} about {object}? Explain.",
    "Tell us about the most {adjective} thing you did at {place}.",
    "When did you last {action} and how did it go?",
    "What's a secret about your {category} you can share?",
    "Describe a moment you felt very {emotion}."
]

DARE_TEMPLATES = [
    "Record a short video of you {action} for 10-20 seconds.",
    "Record your voice {action} and upload it.",
    "Upload a video showing {object} in action.",
    "Record a voice message describing a {adjective} {category}.",
    "Make a short clip of you {action} in {place}."
]

TWISTER_TEMPLATES = [
    "Say this tongue twister: '{twister}'.",
    "Record yourself saying: '{twister}' three times fast.",
    "Try this: '{twister}' in a {adjective} voice and upload it."
]

ADJECTIVES = ["embarrassing", "exciting", "funny", "scary", "weird", "silly", "proud"]
OBJECTS = ["your phone", "your pet", "your last meal", "a book you love", "your first car"]
EMOTIONS = ["jealous", "happy", "angry", "nervous", "excited", "embarrassed"]
ACTIONS = ["dancing", "singing", "jumping", "laughing", "shouting", "whistling"]
PLACES = ["school", "park", "kitchen", "party", "beach"]
CATEGORIES = ["family", "friendship", "hobby", "job", "dream"]
TWISTERS = [
    "She sells seashells by the seashore.",
    "Peter Piper picked a peck of pickled peppers.",
    "How much wood would a woodchuck chuck?",
    "Betty Botter bought some butter.",
    "Six slippery snails slid silently."
]

def _random_fill(template: str):
    return template.format(
        adjective=random.choice(ADJECTIVES),
        object=random.choice(OBJECTS),
        emotion=random.choice(EMOTIONS),
        action=random.choice(ACTIONS),
        place=random.choice(PLACES),
        category=random.choice(CATEGORIES),
        twister=random.choice(TWISTERS)
    )

def generate_unique_prompt(phone: str, kind: str, max_attempts=200):
    """
    kind: 'truth', 'dare', 'twister'
    This function generates a prompt not present in truth_dare_history for this user.
    """
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT prompt FROM truth_dare_history WHERE phone = ? AND kind = ?", (phone, kind))
    used = set(row["prompt"] for row in c.fetchall())
    conn.close()

    templates = TRUTH_TEMPLATES if kind == "truth" else (DARE_TEMPLATES if kind == "dare" else TWISTER_TEMPLATES)
    for _ in range(max_attempts):
        raw = _random_fill(random.choice(templates))
        # append a tiny unique token sometimes to make unique (but readable)
        # ensure readability: we append nothing most of times; occasionally append a short token
        if random.random() < 0.05:
            raw = f"{raw} (#{uuid.uuid4().hex[:5]})"
        if raw not in used:
            # store to history immediately (so another request won't repeat before user completes)
            conn = get_conn()
            c = conn.cursor()
            c.execute("INSERT INTO truth_dare_history (phone, kind, prompt, assigned_at) VALUES (?, ?, ?, ?)",
                      (phone, kind, raw, now_str()))
            conn.commit()
            conn.close()
            return raw
    # fallback: unique UUID appended
    fallback = f"{_random_fill(random.choice(templates))} (#{uuid.uuid4().hex})"
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO truth_dare_history (phone, kind, prompt, assigned_at) VALUES (?, ?, ?, ?)",
              (phone, kind, fallback, now_str()))
    conn.commit()
    conn.close()
    return fallback

# -----------------------
# DB operations for users & activities
# -----------------------
def create_user(phone: str, name: str, age: int, gender: str, is_admin: bool):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (phone, name, age, gender, is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)",
              (phone, name, age, gender, 1 if is_admin else 0, now_str()))
    conn.commit()
    conn.close()

def user_exists(phone: str) -> bool:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM users WHERE phone = ?", (phone,))
    res = c.fetchone() is not None
    conn.close()
    return res

def get_user(phone: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE phone = ?", (phone,))
    r = c.fetchone()
    conn.close()
    return r

def save_activity(phone: str, activity_type: str, prompt: str = None, response_text: str = None, file_path: str = None):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO activities (phone, activity_type, prompt, response_text, file_path, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (phone, activity_type, prompt, response_text, file_path, now_str()))
    conn.commit()
    conn.close()

def get_activities_for(phone: str, is_admin: bool):
    conn = get_conn()
    c = conn.cursor()
    if is_admin:
        c.execute("SELECT a.*, u.name FROM activities a JOIN users u ON a.phone = u.phone ORDER BY timestamp DESC")
        rows = c.fetchall()
    else:
        c.execute("SELECT a.*, u.name FROM activities a JOIN users u ON a.phone = u.phone WHERE a.phone = ? ORDER BY timestamp DESC", (phone,))
        rows = c.fetchall()
    conn.close()
    return rows

# -----------------------
# Streamlit UI
# -----------------------
st.set_page_config(page_title="BakBak Bot", layout="centered", page_icon="😄")
st.title("😄 BakBak Bot")

# Initialize session state
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "phone" not in st.session_state:
    st.session_state.phone = ""
if "is_admin" not in st.session_state:
    st.session_state.is_admin = False
if "assigned_prompt" not in st.session_state:
    st.session_state.assigned_prompt = None
if "assigned_kind" not in st.session_state:
    st.session_state.assigned_kind = None

# --- Login / Register ---
if not st.session_state.logged_in:
    st.header("Login / Register")
    with st.form("login_form", clear_on_submit=False):
        name = st.text_input("Full name", placeholder="Your full name")
        phone = st.text_input("Phone number (this will be your login id)", placeholder="e.g. +911234567890")
        age = st.number_input("Age", min_value=5, max_value=120, value=18)
        gender = st.selectbox("Gender", ["Male", "Female", "Other"])
        admin_password = st.text_input("Admin password (leave empty if not admin)", type="password", placeholder="Enter admin password if admin")
        submitted = st.form_submit_button("Login / Register")

    if submitted:
        if not (name and phone and age and gender):
            st.error("Please fill all required fields.")
        else:
            is_admin = (admin_password == "FFSVA")
            try:
                create_user(phone, name, age, gender, is_admin)
                st.session_state.logged_in = True
                st.session_state.phone = phone
                st.session_state.is_admin = is_admin
                st.success(f"Welcome, {'ADMIN ' if is_admin else ''}{name}!")
                st.rerun()
            except Exception as e:
                st.error(f"Error creating user: {e}")

else:
    # Logged in UI
    user = get_user(st.session_state.phone)
    st.sidebar.markdown(f"**Logged in as:** {user['name']}  \n**Phone:** {user['phone']}  \n**Role:** {'Admin' if st.session_state.is_admin else 'User'}")
    action = st.sidebar.radio("Choose activity", ["Truth & Dare", "Meme Creation", "Tongue Twister", "View Data", "Logout"])

    # ---------- Truth & Dare ----------
    if action == "Truth & Dare":
        st.header("🎲 Truth & Dare")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Get Truth"):
                prompt = generate_unique_prompt(st.session_state.phone, "truth")
                st.session_state.assigned_prompt = prompt
                st.session_state.assigned_kind = "truth"
        with col2:
            if st.button("Get Dare"):
                prompt = generate_unique_prompt(st.session_state.phone, "dare")
                st.session_state.assigned_prompt = prompt
                st.session_state.assigned_kind = "dare"

        if st.session_state.assigned_prompt:
            st.subheader("Task")
            st.write(st.session_state.assigned_prompt)
            if st.session_state.assigned_kind == "truth":
                ans = st.text_area("Your answer")
                if st.button("Submit Truth Answer"):
                    if not ans.strip():
                        st.error("Please write something before submitting.")
                    else:
                        save_activity(st.session_state.phone, "Truth", prompt=st.session_state.assigned_prompt, response_text=ans.strip())
                        st.success("Answer saved!")
                        st.session_state.assigned_prompt = None
                        st.session_state.assigned_kind = None
                        st.rerun()
            else:  # dare: expect file (video/voice/image)
                uploaded = st.file_uploader("Upload proof (video/audio/image)", type=["mp4", "mov", "wav", "mp3", "png", "jpg", "jpeg"])
                if uploaded:
                    if uploaded.size > 100 * 1024 * 1024:
                        st.error("File size must be under 100 MB.")
                    else:
                        if st.button("Submit Dare Proof"):
                            fname = f"{uuid.uuid4().hex}_{safe_filename(uploaded.name)}"
                            fpath = os.path.join(UPLOAD_DIR, fname)
                            with open(fpath, "wb") as f:
                                f.write(uploaded.getbuffer())
                            # optional transcription for audio/video if API key set
                            transcript = None
                            if ASSEMBLYAI_API_KEY and uploaded.type.startswith(("audio", "video")):
                                transcript = transcribe_with_assemblyai(fpath, ASSEMBLYAI_API_KEY)
                            save_activity(st.session_state.phone, "Dare", prompt=st.session_state.assigned_prompt, file_path=fpath, response_text=(transcript or None))
                            st.success("Dare proof uploaded and saved!")
                            st.session_state.assigned_prompt = None
                            st.session_state.assigned_kind = None
                            st.rerun()

    # ---------- Meme Creation ----------
    elif action == "Meme Creation":
        st.header("📸 Meme Creation")
        st.write("Upload an image or short video (<100 MB). Add an optional caption.")
        uploaded = st.file_uploader("Upload image or video", type=["png", "jpg", "jpeg", "mp4", "mov"])
        caption = st.text_input("Caption (optional)")
        if uploaded:
            if uploaded.size > 100 * 1024 * 1024:
                st.error("File too large. Must be under 100 MB.")
            else:
                if st.button("Upload Meme"):
                    fname = f"{uuid.uuid4().hex}_{safe_filename(uploaded.name)}"
                    fpath = os.path.join(UPLOAD_DIR, fname)
                    with open(fpath, "wb") as f:
                        f.write(uploaded.getbuffer())
                    save_activity(st.session_state.phone, "Meme", prompt="Meme upload", file_path=fpath, response_text=(caption.strip() or None))
                    st.success("Meme uploaded and saved!")

    # ---------- Tongue Twister ----------
    elif action == "Tongue Twister":
        st.header("👅 Tongue Twister")
        if st.button("Get Tongue Twister"):
            prompt = generate_unique_prompt(st.session_state.phone, "twister")
            st.session_state.assigned_prompt = prompt
            st.session_state.assigned_kind = "twister"
        if st.session_state.assigned_prompt:
            st.write(st.session_state.assigned_prompt)
            uploaded = st.file_uploader("Upload your voice recording (mp3/wav)", type=["mp3", "wav"])
            if uploaded:
                if uploaded.size > 100 * 1024 * 1024:
                    st.error("File too large. Must be under 100 MB.")
                else:
                    if st.button("Submit Tongue Twister Recording"):
                        fname = f"{uuid.uuid4().hex}_{safe_filename(uploaded.name)}"
                        fpath = os.path.join(UPLOAD_DIR, fname)
                        with open(fpath, "wb") as f:
                            f.write(uploaded.getbuffer())
                        transcript = None
                        if ASSEMBLYAI_API_KEY:
                            transcript = transcribe_with_assemblyai(fpath, ASSEMBLYAI_API_KEY)
                        save_activity(st.session_state.phone, "TongueTwister", prompt=st.session_state.assigned_prompt, file_path=fpath, response_text=(transcript or None))
                        st.success("Recording saved!")
                        st.session_state.assigned_prompt = None
                        st.session_state.assigned_kind = None
                        st.rerun()

    # ---------- View Data ----------
    elif action == "View Data":
        st.header("📂 View Data")
        rows = get_activities_for(st.session_state.phone, st.session_state.is_admin)
        if not rows:
            st.info("No data found.")
        else:
            for r in rows:
                # r includes columns: id, phone, activity_type, prompt, response_text, file_path, timestamp, name (from join)
                user_name = r["name"]
                st.markdown(f"**User:** {user_name}  \n**Type:** {r['activity_type']}  \n**When:** {r['timestamp']}")
                if r["prompt"]:
                    st.write(f"**Prompt:** {r['prompt']}")
                if r["response_text"]:
                    st.write(f"**Response / Transcription:** {r['response_text']}")
                if r["file_path"]:
                    fp = r["file_path"]
                    ext = fp.split(".")[-1].lower()
                    try:
                        if ext in ("png", "jpg", "jpeg"):
                            st.image(fp, width=300)
                        elif ext in ("mp4", "mov"):
                            st.video(fp)
                        elif ext in ("mp3", "wav"):
                            st.audio(fp)
                        else:
                            st.write(f"File: {fp}")
                        # show download link
                        st.markdown(f"[Download file]({fp})")
                    except Exception as e:
                        st.write(f"File: {fp} (can't preview: {e})")
                st.write("---")

    # ---------- Logout ----------
    elif action == "Logout":
        st.session_state.clear()
        st.rerun()
