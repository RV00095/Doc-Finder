import re
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
CORS(app)

# Prevent Browser and API Caching to guarantee 100% LIVE data
@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '-1'
    return response

BASE_URL = "http://doc.lk"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

MONTHS_REGEX = r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*'

SPECIALTY_REJECT_PATTERN = re.compile(
    r'(rheumatolog|immunolog|cardiolog|neurolog|pediatric|paediatric|psychiatr|dermatolog|'
    r'radiolog|urolog|gynecolog|obstetric|surgeon|physician|oncolog|ent\b|eye\b|dental|'
    r'orthopaed|endocrinolog|gastro|nephrolog|hematolog|practitioner|consultant|patholog|'
    r'anaesthet|microbiolog|specialist|specialty|specialization)', re.I
)

def fetch_doc_lk_specialties():
    try:
        r = requests.get(BASE_URL, headers=HEADERS, timeout=8)
        soup = BeautifulSoup(r.text, 'html.parser')
        select = soup.find('select', {'name': re.compile(r'special', re.I)}) or \
                 soup.find('select', {'id': re.compile(r'special', re.I)})
        
        specialties = []
        if select:
            for opt in select.find_all('option'):
                val = opt.get('value', '').strip()
                txt = opt.get_text(strip=True)
                if val and val != "0" and txt and not re.search(r'select', txt, re.I):
                    specialties.append({'id': val, 'name': txt})
        return sorted(specialties, key=lambda x: x['name'])
    except:
        return []

def parse_channel_page(channel_url, fallback_doc_name="Unknown Doctor"):
    results = []
    doc_name = fallback_doc_name
    try:
        r = requests.get(channel_url, headers=HEADERS, timeout=10)
        if r.status_code != 200: return doc_name, results
        soup = BeautifulSoup(r.text, 'html.parser')

        hosp_name = "General Hospital"
        
        # 1. Extract Exact Hospital Name from Page Title
        # Title Format: "Channel Dr. BHATHIYA RANASINGHE - NAWALOKA HOSPITAL - COLOMBO 02 - Doc990"
        title_tag = soup.find('title')
        if title_tag:
            t_txt = title_tag.get_text()
            m_doc = re.search(r'Channel\s+(.*?)\s+-', t_txt, re.I)
            if m_doc: 
                doc_name = m_doc.group(1).strip()
            
            # Split by '-' and extract the middle parts for the Hospital Name
            parts = t_txt.split('-')
            if len(parts) >= 3:
                extracted_hosp = " - ".join([p.strip() for p in parts[1:-1]]).strip()
                if extracted_hosp and "Doc990" not in extracted_hosp:
                    hosp_name = extracted_hosp

        # Fallback if title extraction fails
        if hosp_name == "General Hospital":
            for tag in soup.find_all(['div', 'h4', 'strong', 'span']):
                tag_txt = tag.get_text(strip=True)
                if tag_txt.endswith('SESSIONS') and len(tag_txt) < 80:
                    potential = tag_txt.replace('SESSIONS', '').replace('-','').strip()
                    if potential and not SPECIALTY_REJECT_PATTERN.search(potential):
                        hosp_name = potential
                        break

        doc_name = re.sub(r'^Channel\s+', '', doc_name, flags=re.I).strip()

        # 2. Extract Sessions (Bulletproof extraction from <li> or <tr>)
        blocks = soup.find_all(['li', 'tr'])
        if not blocks:
            blocks = soup.find_all('div', class_=re.compile(r'row|list-group-item', re.I))

        for block in blocks:
            txt = " ".join(block.stripped_strings)
            # Find blocks containing both a month and "ACTIVE APPOINTMENT"
            if "ACTIVE APPOINTMENT" in txt.upper() and re.search(MONTHS_REGEX, txt, re.I):
                
                # Extract Bookings (Even if "SESSION FULL 30")
                act_m = re.search(r'ACTIVE\s*APPOINTMENTS?\W*(\d+)', txt, re.I)
                count = int(act_m.group(1)) if act_m else 0

                # Extract Date Time
                dt_m = re.search(r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}.*?(?:AM|PM|am|pm|\d{2}:\d{2}))', txt, re.I)
                if not dt_m: 
                    dt_m = re.search(r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})', txt, re.I)
                
                dt_str = dt_m.group(1).strip() if dt_m else "Session Date Available"
                # Clean up trailing texts
                dt_str = re.sub(r'(ACTIVE|APPOINTMENT|RS).*', '', dt_str, flags=re.I).strip()

                results.append({
                    'doctor_name': doc_name,
                    'hospital': hosp_name.upper(),
                    'date_time': dt_str,
                    'active_appointments': count,
                    'source_url': channel_url
                })

        # Deduplicate
        seen = set()
        unique_sessions = []
        for s in results:
            key = (s['doctor_name'], s['hospital'], s['date_time'], s['active_appointments'])
            if key not in seen:
                seen.add(key)
                unique_sessions.append(s)

        return doc_name, unique_sessions
    except Exception as e:
        return doc_name, results

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/get_specialties')
def get_specialties(): return jsonify({'specialties': fetch_doc_lk_specialties()})

@app.route('/api/search')
def search():
    query = request.args.get('query', '').strip()
    if not query: return jsonify({'error': 'No doctor name provided'}), 400

    search_url = f"{BASE_URL}/search?doctor={requests.utils.quote(query)}"
    try:
        r = requests.get(search_url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')

        channel_links = []
        for a in soup.find_all('a', href=re.compile(r'/channel/\d+')):
            href = a.get('href')
            full_url = href if href.startswith('http') else BASE_URL + href
            if full_url not in channel_links: channel_links.append(full_url)

        all_sessions = []
        # INCREASED LIMIT to 25 to catch all hospitals for busy doctors like Dr. Bhathiya
        with ThreadPoolExecutor(max_workers=15) as executor:
            futures = [executor.submit(parse_channel_page, url) for url in channel_links[:25]]
            for future in as_completed(futures):
                _, sessions = future.result()
                all_sessions.extend(sessions)

        return jsonify({'query': query, 'total_sessions': len(all_sessions), 'sessions': all_sessions})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/top_doctors')
def top_doctors():
    spec_id = request.args.get('specialty_id', '').strip()
    spec_name = request.args.get('specialty_name', '').strip()
    if not spec_id and not spec_name: return jsonify({'error': 'Specialty required'}), 400

    search_urls = [
        f"{BASE_URL}/search?specialization={spec_id}",
        f"{BASE_URL}/search?speciality={spec_id}",
        f"{BASE_URL}/search?doctor={requests.utils.quote(spec_name)}"
    ]

    channel_links = []
    seen = set()
    for s_url in search_urls:
        try:
            r = requests.get(s_url, headers=HEADERS, timeout=8)
            soup = BeautifulSoup(r.text, 'html.parser')
            for a in soup.find_all('a', href=re.compile(r'/channel/\d+')):
                href = a.get('href')
                full_url = href if href.startswith('http') else BASE_URL + href
                if full_url not in seen:
                    seen.add(full_url)
                    channel_links.append(full_url)
            if len(channel_links) >= 15: break # Gather more channels for leaderboard
        except: continue

    doctor_map = {}
    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = [executor.submit(parse_channel_page, url) for url in channel_links[:25]]
        for future in as_completed(futures):
            doc_name, sessions = future.result()
            if not sessions: continue

            if doc_name not in doctor_map:
                doctor_map[doc_name] = {
                    'doctor_name': doc_name,
                    'total_active_appointments': 0,
                    'sessions': [],
                    'channel_url': sessions[0]['source_url'] if sessions else ''
                }
            for s in sessions:
                doctor_map[doc_name]['sessions'].append(s)
                doctor_map[doc_name]['total_active_appointments'] += s['active_appointments']

    ranked_doctors = list(doctor_map.values())
    ranked_doctors.sort(key=lambda x: x['total_active_appointments'], reverse=True)
    return jsonify({'specialty': spec_name or spec_id, 'doctors_found': len(ranked_doctors), 'doctors': ranked_doctors})

if __name__ == '__main__': app.run(debug=True, port=5000)
