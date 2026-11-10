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
TARGET_KEYWORDS = [
    "asiri", "lanka", "nawaloka", "kdu", "royal",
    "delmon", "durdans", "ceymed", "winlanka", "vasan",
    "hemas", "ninewells", "oasis", "lanka hospitals"
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "http://doc.lk/",
}

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]
SESSION_RE = re.compile(
    r'\b(' + '|'.join(MONTHS) + r')\s+(\d{1,2}),\s*(\d{4})\s+'
    r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2}):(\d{2})\s*(am|pm)\b',
    re.I
)

# If live specialty scrape fails, use NAME search (not fake IDs)
FALLBACK_SPECIALTY_NAMES = [
    "Cardiologist", "Dermatologist", "Neurologist", "General Physician",
    "Psychiatrist", "Orthopaedic Surgeon", "ENT Surgeon", "Eye Surgeon",
    "Dental Surgeon", "Paediatrician", "Gynaecologist", "VOG",
    "General Surgeon", "Urologist", "Gastroenterologist"
]


def clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def to_http_doc_lk(href):
    p = urlparse(href)
    path_q = p.path + (("?" + p.query) if p.query else "")
    if not path_q.startswith("/"):
        path_q = "/" + path_q
    return BASE_URL + path_q


def get_html(url, timeout=12):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text


def extract_items_from_search_html(html, prefer_target_hospitals=False):
    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()
    current_hospital, latest_doc = "Unknown Hospital", ""

    for el in soup.find_all(["div", "h2", "h3", "h4", "span", "a", "p", "li", "strong", "b"]):
        txt = clean(el.get_text())

        if el.name in ["div", "h2", "h3", "h4", "li", "strong", "b", "p"] and 5 < len(txt) < 110:
            hm = re.match(r"^([A-Z0-9][A-Z0-9 &'.,\-()/]{3,90}?)\s*\((\d+)\)$", txt)
            if hm:
                current_hospital = clean(hm.group(1))
                continue

        if el.name != "a" or not el.has_attr("href"):
            continue

        href = el.get("href", "")
        label = clean(el.get_text())

        if re.match(r"^(Dr|Prof|Mrs|Mr|Ms|Miss)\b", label, re.I):
            latest_doc = label

        if not re.search(r"/channel/\d+", href):
            continue

        full_url = to_http_doc_lk(href)
        if full_url in seen:
            continue
        seen.add(full_url)

        if prefer_target_hospitals:
            if not any(k in current_hospital.lower() for k in TARGET_KEYWORDS):
                # still keep some non-target if few results later
                pass

        doc_name, specialty = latest_doc or "Doctor", "Not Specified"
        node = el
        for _ in range(5):
            node = node.parent if node else None
            if not node:
                break
            lines = [clean(x) for x in node.get_text("\n").split("\n") if clean(x)]
            for i, line in enumerate(lines):
                if re.match(r"^(Dr|Prof|Mrs|Mr|Ms|Miss)\b", line, re.I):
                    doc_name = line
                    if i + 1 < len(lines) and "channel" not in lines[i + 1].lower():
                        specialty = lines[i + 1]
                    break
            if specialty != "Not Specified":
                break

        items.append({
            "doctor": doc_name,
            "specialty": specialty,
            "hospital": current_hospital,
            "channel_url": full_url,
        })

    return items


def search_by_doctor_name(name):
    html = get_html(f"{BASE_URL}/search?doctor={quote(name)}")
    return extract_items_from_search_html(html)


def search_by_specialty(spec_id=None, spec_name=None):
    """Try multiple URL patterns used by doc.lk"""
    candidates = []
    if spec_id:
        candidates += [
            f"{BASE_URL}/search?specialization={quote(str(spec_id))}",
            f"{BASE_URL}/search?speciality={quote(str(spec_id))}",
            f"{BASE_URL}/search?specialty={quote(str(spec_id))}",
            f"{BASE_URL}/search?Specialization={quote(str(spec_id))}",
        ]
    if spec_name:
        candidates += [
            f"{BASE_URL}/search?specialization={quote(spec_name)}",
            f"{BASE_URL}/search?speciality={quote(spec_name)}",
            f"{BASE_URL}/search?specialty={quote(spec_name)}",
            f"{BASE_URL}/search?doctor={quote(spec_name)}",  # fallback
        ]

    all_items = []
    seen = set()
    errors = []

    for url in candidates:
        try:
            html = get_html(url, timeout=10)
            items = extract_items_from_search_html(html)
            for it in items:
                if it["channel_url"] not in seen:
                    seen.add(it["channel_url"])
                    all_items.append(it)
            # if we already have decent amount, stop
            if len(all_items) >= 8:
                break
        except Exception as e:
            errors.append(f"{url} -> {e}")

    return all_items, errors


def fetch_channel_sessions(channel_url, doc_name="", hosp_fallback="", spec_fallback=""):
    out = {
        "doctor": doc_name,
        "hospital": hosp_fallback or "Unknown Hospital",
        "specialty": spec_fallback or "Not Specified",
        "booked": 0,
        "sessions": [],
    }
    try:
        html = get_html(channel_url, timeout=8)
        soup = BeautifulSoup(html, "html.parser")

        title = clean(soup.title.string if soup.title else "")
        tm = re.match(r"^Channel\s+(.+?)\s+-\s+(.+?)\s+-\s+Doc", title, re.I)
        if tm:
            out["doctor"] = clean(tm.group(1))
            out["hospital"] = clean(tm.group(2))

        for tag in soup(["script", "style", "noscript", "template", "iframe"]):
            tag.decompose()
        text = clean(soup.get_text(" "))

        if out["specialty"] in ("", "Not Specified"):
            sm = re.search(r"Speciali[sz]ation\s*[:\-]?\s*([A-Za-z0-9 ,&'/.\\-]{3,60})", text, re.I)
            if sm:
                out["specialty"] = re.sub(r"\s+(Hospital|Sessions?|Book|Channel).*$", "", clean(sm.group(1)), flags=re.I)

        sec = re.search(re.escape(out["hospital"]) + r"\s+Sessions\b", text, re.I)
        if not sec:
            sec = re.search(r"\bSessions\b", text, re.I)
        if not sec:
            return out

        section = text[sec.end():]
        endm = re.search(r"\bOther\s+Hospital\s+Sessions\b", section, re.I)
        if endm:
            section = section[:endm.start()]

        matches = list(SESSION_RE.finditer(section))
        total = 0
        for j, m in enumerate(matches):
            start = m.end()
            end = matches[j + 1].start() if j + 1 < len(matches) else len(section)
            chunk = section[start:end]
            counts = list(set(int(c.replace(",", "")) for c in re.findall(
                r"\bActive\s+Appointments?\s*[:\-–—]?\s*(\d[\d,]*)\b", chunk, re.I)))
            booked = counts[0] if len(counts) == 1 else 0
            total += booked
            st = re.search(
                r"\b(Not\s+Available|Fully\s+Booked|Session\s+Full|Cancelled|Canceled|Closed|Full|Available)\b",
                chunk, re.I
            )
            out["sessions"].append({
                "date": f"{m.group(3)}-{MONTHS.index(m.group(1).capitalize())+1:02d}-{int(m.group(2)):02d}",
                "day": m.group(4),
                "time": f"{m.group(5)}:{m.group(6)} {m.group(7).upper()}",
                "booked": booked,
                "status": clean(st.group(0)) if st else "Available",
                "hospital": out["hospital"],
            })
        out["booked"] = total
        return out
    except Exception:
        return out


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/get_specialties", methods=["GET"])
def api_get_specialties():
    specs = []
    try:
        html = get_html(BASE_URL, timeout=12)
        soup = BeautifulSoup(html, "html.parser")
        for sel in soup.find_all("select"):
            meta = ((sel.get("id") or "") + " " + (sel.get("name") or "")).lower()
            opts = sel.find_all("option")
            texts = [clean(o.get_text()) for o in opts]
            looks = ("special" in meta) or any(
                re.search(r"cardio|derma|physician|paediat|gynaec|ortho|psych|dental|ent|eye", t, re.I)
                for t in texts
            )
            if not looks:
                continue
            for o in opts:
                val = str(o.get("value") or "").strip()
                name = clean(o.get_text())
                if not val or val in ("0", "all") or len(name) < 3:
                    continue
                if re.match(r"^(select|any|all|--|special)", name, re.I):
                    continue
                specs.append({"id": val, "name": name})
            if specs:
                break
    except Exception:
        pass

    if not specs:
        # name-only fallback (id = name)
        specs = [{"id": n, "name": n} for n in FALLBACK_SPECIALTY_NAMES]

    uniq = {}
    for s in specs:
        uniq[s["name"].lower()] = s
    return jsonify(sorted(uniq.values(), key=lambda x: x["name"]))


@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.get_json(silent=True) or {}
    q = clean(data.get("doctor", ""))
    if len(q) < 2:
        return jsonify({"error": "Doctor name too short", "items": []}), 400
    try:
        items = search_by_doctor_name(q)
        return jsonify({"items": items, "count": len(items)})
    except Exception as e:
        return jsonify({"error": str(e), "items": []}), 500


@app.route("/api/channel", methods=["POST"])
def api_channel():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "")
    if not url:
        return jsonify({"error": "No URL", "sessions": []}), 400
    res = fetch_channel_sessions(
        url,
        doc_name=data.get("doctor", ""),
        hosp_fallback=data.get("hospital", ""),
        spec_fallback=data.get("specialty", ""),
    )
    return jsonify(res)


@app.route("/api/top_doctors", methods=["POST"])
def api_top_doctors():
    data = request.get_json(silent=True) or {}
    spec_id = clean(data.get("specialty_id", ""))
    spec_name = clean(data.get("specialty_name", ""))

    if not spec_id and not spec_name:
        return jsonify({"error": "specialty required", "doctors": []}), 400

    try:
        items, errors = search_by_specialty(spec_id=spec_id or None, spec_name=spec_name or None)

        # Prefer major hospitals first, but don't drop all others if empty
        preferred = [i for i in items if any(k in i["hospital"].lower() for k in TARGET_KEYWORDS)]
        pool = preferred if len(preferred) >= 3 else items
        pool = pool[:15]

        if not pool:
            return jsonify({
                "doctors": [],
                "checked": 0,
                "found_links": 0,
                "message": "No doctor links found for this specialty from doc.lk",
                "debug": errors[:3],
            })

        stats = {}
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {
                ex.submit(
                    fetch_channel_sessions,
                    it["channel_url"], it["doctor"], it["hospital"], it["specialty"]
                ): it for it in pool
            }
            for fut in as_completed(futs):
                it = futs[fut]
                try:
                    res = fut.result()
                except Exception:
                    continue
                name = res.get("doctor") or it["doctor"]
                if name not in stats:
                    stats[name] = {
                        "name": name,
                        "specialty": res.get("specialty") or it.get("specialty") or spec_name or "Specialty",
                        "total_booked": 0,
                        "session_count": 0,
                        "hospitals": set(),
                    }
                stats[name]["total_booked"] += int(res.get("booked") or 0)
                stats[name]["session_count"] += len(res.get("sessions") or [])
                stats[name]["hospitals"].add(res.get("hospital") or it["hospital"])

        rows = []
        for d in stats.values():
            d["hospitals"] = sorted([h for h in d["hospitals"] if h])
            # keep doctors who have sessions OR bookings
            if d["session_count"] > 0 or d["total_booked"] > 0:
                rows.append(d)

        rows.sort(key=lambda x: (x["total_booked"], x["session_count"]), reverse=True)

        return jsonify({
            "doctors": rows[:20],
            "checked": len(pool),
            "found_links": len(items),
            "message": "ok" if rows else "Links found but no parseable sessions right now",
        })
    except Exception as e:
        return jsonify({"error": str(e), "doctors": []}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
