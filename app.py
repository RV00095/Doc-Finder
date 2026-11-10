import re
import csv
import io
import time
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify, Response
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
CORS(app)

BASE_URL = "http://doc.lk"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

MONTHS_REGEX = r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*'

def fetch_doc_lk_specialties():
    """Fetch live specialty options from doc.lk dropdown."""
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
    except Exception as e:
        print("Error fetching specialties:", e)
        return []

def parse_channel_page(channel_url, fallback_doc_name="Unknown Doctor"):
    """Scrape a doctor's channel page with bulletproof Active Appointment count extraction."""
    results = []
    doc_name = fallback_doc_name

    try:
        r = requests.get(channel_url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return doc_name, results

        soup = BeautifulSoup(r.text, 'html.parser')

        # 1. Doctor Name
        title_tag = soup.find('title')
        if title_tag:
            t_txt = title_tag.get_text()
            m = re.search(r'Channel\s+(.*?)\s+-', t_txt, re.I)
            if m:
                doc_name = m.group(1).strip()

        if doc_name == "Unknown Doctor" or not doc_name:
            h_tag = soup.find(['h1', 'h2', 'h3'])
            if h_tag:
                doc_name = h_tag.get_text(strip=True)

        # 2. Extract Session Cards/Blocks
        # Use space separator to avoid splitting labels and numbers across newlines
        full_text = soup.get_text(separator=" ", strip=True)
        clean_text = re.sub(r'\s+', ' ', full_text)

        # Look for containers (divs, trs, li) that hold individual session data
        containers = []
        for tag in soup.find_all(['div', 'tr', 'li', 'section']):
            t_text = tag.get_text(separator=" ", strip=True)
            if re.search(MONTHS_REGEX, t_text, re.I) and ("active" in t_text.lower() or "appointment" in t_text.lower()):
                if len(t_text) < 400: # Ensure localized session snippet
                    containers.append((tag, t_text))

        parsed_sessions = []

        if containers:
            for tag, c_text in containers:
                # Find hospital header
                hosp = "Unknown Hospital"
                curr = tag
                while curr and curr.parent:
                    prev_h = curr.find_previous(['h2', 'h3', 'h4', 'h5', 'strong', 'b'])
                    if prev_h:
                        p_txt = prev_h.get_text(strip=True)
                        if len(p_txt) < 80 and "active" not in p_txt.lower() and "doc" not in p_txt.lower():
                            hosp = p_txt.replace("Sessions", "").replace("sessions", "").strip()
                            break
                    curr = curr.parent

                # Extract Active Appointments count accurately
                act_m = re.search(r'Active\s*Appointments?\s*:?\s*(\d+)', c_text, re.I)
                active_count = int(act_m.group(1)) if act_m else 0

                # Extract Date & Time
                dt_m = re.search(r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}[^Active]*?)(?:Active|NOT AVAILABLE|CANCELLED|$)', c_text, re.I)
                dt_str = dt_m.group(1).strip() if dt_m else c_text[:60]

                status_str = "Active"
                if "NOT AVAILABLE" in c_text.upper():
                    status_str = "NOT AVAILABLE"
                elif "CANCELLED" in c_text.upper():
                    status_str = "CANCELLED"

                parsed_sessions.append({
                    'doctor_name': doc_name,
                    'hospital': hosp,
                    'date_time': dt_str,
                    'active_appointments': active_count,
                    'status': status_str,
                    'source_url': channel_url
                })

        # Regex fallback if container parsing returned empty
        if not parsed_sessions:
            matches = re.finditer(r'Active\s*Appointments?\s*:?\s*(\d+)', clean_text, re.I)
            for m in matches:
                act_count = int(m.group(1))
                snippet = clean_text[max(0, m.start() - 120): min(len(clean_text), m.end() + 80)]
                
                dt_m = re.search(r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}[^Active]*', snippet, re.I)
                dt_str = dt_m.group(0).strip() if dt_m else "Session Date Available"

                parsed_sessions.append({
                    'doctor_name': doc_name,
                    'hospital': "Hospital Session",
                    'date_time': dt_str,
                    'active_appointments': act_count,
                    'status': "Active",
                    'source_url': channel_url
                })

        # Remove duplicate sessions
        seen = set()
        unique_sessions = []
        for s in parsed_sessions:
            key = (s['doctor_name'], s['hospital'], s['date_time'], s['active_appointments'])
            if key not in seen:
                seen.add(key)
                unique_sessions.append(s)

        return doc_name, unique_sessions

    except Exception as e:
        print(f"Error scraping channel {channel_url}: {e}")
        return doc_name, results


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/get_specialties')
def get_specialties():
    specs = fetch_doc_lk_specialties()
    return jsonify({'specialties': specs})


@app.route('/api/search')
def search():
    query = request.args.get('query', '').strip()
    if not query:
        return jsonify({'error': 'Please provide a doctor name'}), 400

    search_url = f"{BASE_URL}/search?doctor={requests.utils.quote(query)}"
    
    try:
        r = requests.get(search_url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')

        channel_links = []
        for a in soup.find_all('a', href=re.compile(r'/channel/\d+')):
            href = a.get('href')
            full_url = href if href.startswith('http') else BASE_URL + href
            if full_url not in channel_links:
                channel_links.append(full_url)

        all_sessions = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(parse_channel_page, url) for url in channel_links[:12]]
            for future in as_completed(futures):
                _, sessions = future.result()
                all_sessions.extend(sessions)

        return jsonify({
            'query': query,
            'total_sessions': len(all_sessions),
            'sessions': all_sessions
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/top_doctors')
def top_doctors():
    spec_id = request.args.get('specialty_id', '').strip()
    spec_name = request.args.get('specialty_name', '').strip()

    if not spec_id and not spec_name:
        return jsonify({'error': 'Specialty required'}), 400

    search_urls = [
        f"{BASE_URL}/search?specialization={spec_id}",
        f"{BASE_URL}/search?speciality={spec_id}",
        f"{BASE_URL}/search?specialty={spec_id}",
        f"{BASE_URL}/search?doctor={requests.utils.quote(spec_name)}"
    ]

    channel_links = []
    seen = set()

    for s_url in search_urls:
        try:
            r = requests.get(s_url, headers=HEADERS, timeout=6)
            soup = BeautifulSoup(r.text, 'html.parser')
            for a in soup.find_all('a', href=re.compile(r'/channel/\d+')):
                href = a.get('href')
                full_url = href if href.startswith('http') else BASE_URL + href
                if full_url not in seen:
                    seen.add(full_url)
                    channel_links.append(full_url)
            if len(channel_links) >= 5:
                break
        except Exception:
            continue

    doctor_map = {}

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(parse_channel_page, url) for url in channel_links[:15]]
        for future in as_completed(futures):
            doc_name, sessions = future.result()
            if not sessions:
                continue

            if doc_name not in doctor_map:
                doctor_map[doc_name] = {
                    'doctor_name': doc_name,
                    'total_active_appointments': 0,
                    'sessions': [],
                    'hospitals': set(),
                    'channel_url': sessions[0]['source_url'] if sessions else ''
                }

            for s in sessions:
                doctor_map[doc_name]['sessions'].append(s)
                doctor_map[doc_name]['total_active_appointments'] += s['active_appointments']
                if s['hospital'] and s['hospital'] != "Unknown Hospital":
                    doctor_map[doc_name]['hospitals'].add(s['hospital'])

    ranked_doctors = []
    for d_name, d_data in doctor_map.items():
        d_data['hospitals'] = list(d_data['hospitals'])
        max_single = max([s['active_appointments'] for s in d_data['sessions']], default=0)
        d_data['high_demand'] = d_data['total_active_appointments'] >= 10 or max_single >= 10
        ranked_doctors.append(d_data)

    # Rank strictly from Highest Active Appointments to Lowest
    ranked_doctors.sort(key=lambda x: x['total_active_appointments'], reverse=True)

    return jsonify({
        'specialty': spec_name or spec_id,
        'doctors_found': len(ranked_doctors),
        'doctors': ranked_doctors
    })


if __name__ == '__main__':
    app.run(debug=True, port=5000)
