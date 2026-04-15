from flask import Flask, render_template, request, jsonify
import requests
from bs4 import BeautifulSoup
import base64
import sqlite3
import logging
import time
from functools import wraps
from collections import defaultdict

app = Flask(__name__)

# ─────────────────────────────────────────
#  Logging Setup
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('bteup_pro.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
#  In-Memory Rate Limiter (No extra lib)
# ─────────────────────────────────────────
request_counts = defaultdict(list)
RATE_LIMIT_PER_MIN = 6

def rate_limit(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        ip = request.remote_addr
        now = time.time()
        request_counts[ip] = [t for t in request_counts[ip] if now - t < 60]
        if len(request_counts[ip]) >= RATE_LIMIT_PER_MIN:
            wait = int(60 - (now - request_counts[ip][0]))
            logger.warning(f"Rate limit hit from IP: {ip}")
            return render_template(
                'error.html',
                message=f"Too many requests! Please wait {wait} seconds.",
                icon="⏱️"
            ), 429
        request_counts[ip].append(now)
        return f(*args, **kwargs)
    return decorated

# ─────────────────────────────────────────
#  Database Setup (Cache with Timestamps)
# ─────────────────────────────────────────
def init_db():
    conn = sqlite3.connect('bteup_pro.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS cache (
            key        TEXT PRIMARY KEY,
            html       TEXT,
            fetched_at INTEGER,
            fetch_count INTEGER DEFAULT 1
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS access_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            enroll     TEXT,
            ip         TEXT,
            source     TEXT,
            timestamp  INTEGER
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ─────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────
def to_base64(text):
    return base64.b64encode(text.encode()).decode()

def format_time(ts):
    return time.strftime('%d %b %Y, %I:%M %p', time.localtime(ts))

def fetch_with_retry(url, headers, retries=3, delay=2):
    """Retry logic with exponential backoff"""
    for attempt in range(retries):
        try:
            logger.info(f"Fetch attempt {attempt+1}/{retries} → {url[:60]}...")
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            return resp
        except requests.exceptions.Timeout:
            logger.warning(f"Attempt {attempt+1} timed out")
        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP error: {e}")
            break
        except requests.exceptions.ConnectionError:
            logger.warning(f"Connection error on attempt {attempt+1}")
        if attempt < retries - 1:
            time.sleep(delay * (attempt + 1))  # Exponential backoff
    return None

def log_access(enroll, source):
    try:
        conn = sqlite3.connect('bteup_pro.db')
        c = conn.cursor()
        c.execute(
            "INSERT INTO access_log (enroll, ip, source, timestamp) VALUES (?,?,?,?)",
            (enroll, request.remote_addr, source, int(time.time()))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Log access error: {e}")

# ─────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/generate', methods=['POST'])
@rate_limit
def generate():
    enroll = request.form.get('enrollment', '').strip()
    dob    = request.form.get('dob', '').strip()
    force  = request.form.get('refresh', 'false') == 'true'

    # Basic Validation
    if not enroll or not dob:
        return render_template('error.html', message="Enrollment number and Date of Birth are required.", icon="⚠️")
    if len(enroll) < 8:
        return render_template('error.html', message="Invalid enrollment number format.", icon="🚫")

    cache_key = f"{enroll}_{dob}"
    conn = sqlite3.connect('bteup_pro.db')
    c = conn.cursor()

    # ── Check Cache ──
    if not force:
        c.execute("SELECT html, fetched_at, fetch_count FROM cache WHERE key=?", (cache_key,))
        row = c.fetchone()
        if row:
            c.execute("UPDATE cache SET fetch_count=fetch_count+1 WHERE key=?", (cache_key,))
            conn.commit()
            conn.close()
            log_access(enroll, "cache")
            logger.info(f"[CACHE HIT] {enroll}")
            return render_template(
                'result.html',
                table_html   = row[0],
                enroll       = enroll,
                source       = "Local Cache",
                fetched_at   = format_time(row[1]),
                fetch_count  = row[2] + 1
            )

    conn.close()

    # ── Fetch from BTEUP ──
    enc_id  = to_base64(enroll)
    enc_id2 = to_base64(dob)
    url = f"https://result.bteexam.com/Odd_Semester/main/result.aspx?id={enc_id}&id2={enc_id2}"

    headers = {
        'User-Agent': 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,hi;q=0.8',
        'Connection': 'keep-alive',
    }

    response = fetch_with_retry(url, headers)

    if not response:
        return render_template('error.html', message="BTEUP server is unreachable after 3 attempts. Please try again later.", icon="🌐")

    soup   = BeautifulSoup(response.text, 'html.parser')
    tables = soup.find_all('table')

    if not tables:
        return render_template('error.html', message="Result not found! Please verify your Enrollment Number and Date of Birth.", icon="🔍")

    # Pick largest table (most likely the result table)
    result_table = max(tables, key=lambda t: len(t.find_all('tr')))
    html_data    = str(result_table)
    fetched_at   = int(time.time())

    # ── Save to Cache ──
    conn = sqlite3.connect('bteup_pro.db')
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO cache (key, html, fetched_at, fetch_count) VALUES (?,?,?,?)",
        (cache_key, html_data, fetched_at, 1)
    )
    conn.commit()
    conn.close()

    log_access(enroll, "live")
    logger.info(f"[LIVE FETCH] {enroll} → Success")

    return render_template(
        'result.html',
        table_html  = html_data,
        enroll      = enroll,
        source      = "BTEUP Live Server",
        fetched_at  = format_time(fetched_at),
        fetch_count = 1
    )


@app.route('/api/clear-cache', methods=['POST'])
def clear_cache():
    """API: Clear cache for a specific enrollment"""
    data   = request.get_json(silent=True) or {}
    enroll = data.get('enrollment', '').strip()
    dob    = data.get('dob', '').strip()
    if not enroll or not dob:
        return jsonify({"success": False, "message": "enrollment and dob required"}), 400
    cache_key = f"{enroll}_{dob}"
    conn = sqlite3.connect('bteup_pro.db')
    c = conn.cursor()
    c.execute("DELETE FROM cache WHERE key=?", (cache_key,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    logger.info(f"Cache cleared for {enroll}")
    return jsonify({"success": True, "deleted": deleted})


@app.route('/api/stats')
def stats():
    """API: Quick stats for admin"""
    conn = sqlite3.connect('bteup_pro.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM cache")
    cached = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM access_log")
    total_hits = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM access_log WHERE source='live'")
    live_hits = c.fetchone()[0]
    conn.close()
    return jsonify({
        "cached_results": cached,
        "total_requests": total_hits,
        "live_fetches": live_hits,
        "cache_hits": total_hits - live_hits
    })


# ─────────────────────────────────────────
#  Error Handlers
# ─────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', message="Page not found.", icon="🔭"), 404

@app.errorhandler(500)
def server_error(e):
    return render_template('error.html', message="Internal server error. Please try again.", icon="💥"), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
