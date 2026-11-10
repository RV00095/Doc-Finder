from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re
from urllib.parse import quote, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
CORS(app)

BASE_URL = "http://doc.lk"
TARGET_KEYWORDS = ["asiri", "lanka", "nawaloka", "kdu", "royal", "delmon", "durdans", "ceymed", "winlanka", "vasan"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
SESSION_RE = re.compile(r'\b(' + '|'.join(MONTHS) + r')\s+(\d{1,2}),\s*(\d{4})\s+(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2}):(\d{2})\s*(am|pm)\b', re.I)

def clean(s):
    return re.sub(r'\s+', ' ', str(s or '')).strip()

def to_http_doc_lk(href):
    parsed = urlparse(href)
    path_q = parsed.path + (("?" + parsed.query) if parsed.query else "")
    if not path_q.startswith("/"): path_q = "/" + path_q
    return BASE_URL + path_q

# =============================================
# DOC.LK SCRAPING FUNCTIONS
# =============================================
def search_doc_lk_list(url_or_query, is_specialty=False):
    """Search pages (by Doctor name OR by Specialty ID)"""
    if is_specialty:
        search_url = f"{BASE_URL}/search?specialization={url_or_query}"
    else:
        search_url = f"{BASE_URL}/search?doctor={quote(url_or_query)}"
        
    r = requests.get(search_url, headers=HEADERS, timeout=20)
    if r.status_code != 200: return []

    soup = BeautifulSoup(r.text, "html.parser")
    items, seen_urls = [], set()
    current_hospital, latest_doc_name = "Hospital", ""

    for el in soup.find_all(["div", "h2", "h3", "h4", "span", "a", "p", "li"]):
        txt = clean(el.get_text())
        
        if el.name in ["div", "h2", "h3", "h4", "li"] and len(txt) < 90:
            hm = re.match(r'^([A-Z0-9][A-Z0-9 &\'.,\-()\/]{4,75}?)\s*\(\d+\)$', txt)
            if hm: current_hospital = clean(hm.group(1))

        if el.name != "a" or not el.has_attr("href"): continue
        href = el["href"]
        if re.match(r'^(Dr|Prof|Mrs|Mr|Ms|Miss)\b', txt, re.I): latest_doc_name = txt
        if not re.search(r'/channel/\d+', href): continue

        full_url = to_http_doc_lk(href)
        if full_url in seen_urls: continue
        seen_urls.add(full_url)

        # Skip non-target hospitals if doing specialty search (to save time)
        if is_specialty and not any(k in current_hospital.lower() for k in TARGET_KEYWORDS):
            continue

        doc_name, specialty = latest_doc_name, "Not Specified"
        node = el
        for _ in range(4):
            node = node.parent
            if not node: break
            lines = [clean(line) for line in node.get_text("\n").split("\n") if clean(line)]
            for idx, line in enumerate(lines):
                if re.match(r'^(Dr|Prof|Mrs|Mr|Ms|Miss)\b', line, re.I):
                    doc_name = line
                    if idx + 1 < len(lines) and not re.search(r'\bChannel\b', lines[idx + 1], re.I):
                        specialty = lines[idx + 1]
                    break
            if doc_name and specialty != "Not Specified": break

        items.append({
            "doctor": doc_name,
            "specialty": specialty,
            "hospital": current_hospital,
            "channel_url": full_url
        })
    return items

def fetch_channel_sessions(channel_url, doc_name=""):
    try:
        r = requests.get(channel_url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        
        title = clean(soup.title.string if soup.title else "")
        tm = re.match(r'^Channel\s+(.+?)\s+-\s+(.+?)\s+-\s+Doc990\b', title, re.I)
        if tm: doc_name = clean(tm.group(1))

        for tag in soup(["script", "style", "noscript", "template", "iframe"]): tag.decompose()
        text = clean(soup.get_text(separator=" "))
        
        session_header = re.search(r'\bSessions\b', text, re.I)
        if not session_header: return {"doctor": doc_name, "booked": 0, "sessions": []}

        section = text[session_header.end():]
        end_m = re.search(r'\bOther\s+Hospital\s+Sessions\b', section, re.I)
        if end_m: section = section[:end_m.start()]

        total_booked = 0
        sessions = []
        for j, m in enumerate(SESSION_RE.finditer(section)):
            chunk_end = len(section)
            chunk = section[m.end():chunk_end]
            
            counts = list(set(int(c.replace(",", "")) for c in re.findall(r'\bActive\s+Appointments?\s*[:\-–—]?\s*(\d[\d,]*)\b', chunk, re.I)))
            booked = counts[0] if len(counts) == 1 else 0
            total_booked += booked
            
            st = re.search(r'\b(Not\s+Available|Fully\s+Booked|Session\s+Full|Cancelled|Closed|Full|Available)\b', chunk, re.I)
            status = clean(st.group(0)) if st else "Available"
            
            sessions.append({
                "date": f"{m.group(3)}-{MONTHS.index(m.group(1).capitalize()) + 1:02d}-{int(m.group(2)):02d}",
                "time": f"{m.group(5)}:{m.group(6)} {m.group(7).upper()}",
                "booked": booked,
                "status": status
            })

        return {"doctor": doc_name, "booked": total_booked, "sessions": sessions}
    except:
        return {"doctor": doc_name, "booked": 0, "sessions": []}

# =============================================
# API ROUTES
# =============================================
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/get_specialties", methods=["GET"])
def api_get_specialties():
    try:
        r = requests.get(BASE_URL, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        select = soup.find("select", {"id": "specialization"})
        specs = []
        if select:
            for opt in select.find_all("option"):
                if opt.get("value") and opt.get("value") != "0":
                    specs.append({"id": opt.get("value"), "name": clean(opt.get_text())})
        return jsonify(sorted(specs, key=lambda x: x['name']))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.get_json() or {}
    query = data.get("doctor", "").strip()
    if len(query) < 2: return jsonify({"error": "Doctor name is too short"}), 400
    try:
        return jsonify({"items": search_doc_lk_list(query, is_specialty=False)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/channel", methods=["POST"])
def api_channel():
    data = request.get_json() or {}
    url = data.get("url", "")
    if not url: return jsonify({"error": "No URL"}), 400
    try:
        res = fetch_channel_sessions(url, doc_name=data.get("doctor", ""))
        return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e), "sessions": []}), 200

@app.route("/api/top_doctors", methods=["POST"])
def api_top_doctors():
    data = request.get_json() or {}
    spec_id = data.get("specialty_id", "")
    if not spec_id: return jsonify({"error": "Specialty ID required"}), 400

    try:
        # 1. Get all doctor links for this specialty
        items = search_doc_lk_list(spec_id, is_specialty=True)
        if not items: return jsonify({"doctors": []})

        # 2. Parallel fetch active appointments for top 30 links to save time
        items = items[:30] 
        doc_stats = {}

        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_item = {executor.submit(fetch_channel_sessions, item["channel_url"], item["doctor"]): item for item in items}
            
            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    res = future.result()
                    d_name = res["doctor"] or item["doctor"]
                    if d_name not in doc_stats:
                        doc_stats[d_name] = {"name": d_name, "specialty": item["specialty"], "total_booked": 0, "hospitals": set()}
                    
                    doc_stats[d_name]["total_booked"] += res["booked"]
                    doc_stats[d_name]["hospitals"].add(item["hospital"])
                except:
                    pass

        # 3. Sort by bookings
        sorted_docs = sorted(doc_stats.values(), key=lambda x: x["total_booked"], reverse=True)
        
        # Format output
        results = []
        for d in sorted_docs:
            if d["total_booked"] > 0: # Only show doctors with actual bookings
                d["hospitals"] = list(d["hospitals"])
                results.append(d)

        return jsonify({"doctors": results[:15]}) # Top 15

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)