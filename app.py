import re
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
CORS(app)

BASE_URL = "http://doc.lk"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

MONTHS_REGEX = r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*'

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

        # 1. Get Doctor Name
        title_tag = soup.find('title')
        if title_tag:
            m = re.search(r'Channel\s+(.*?)\s+-', title_tag.get_text(), re.I)
            if m: doc_name = m.group(1).strip()

        # 2. Get Sessions and Exact Hospital Names
        panels = soup.find_all('div', class_=re.compile(r'panel', re.I))
        
        for panel in panels:
            title_elem = panel.find(class_=re.compile(r'panel-title', re.I))
            if not title_elem: continue
            
            # Clean Hospital Name
            hosp_name = title_elem.get_text(strip=True)
            hosp_name = re.sub(r'\s*Sessions?$', '', hosp_name, flags=re.I).strip()
            
            # Avoid specialties leaking
            if re.search(r'(specialist|surgeon|physician|cardiologist|neurologist|mbbs|md)', hosp_name, re.I):
                continue

            for row in panel.find_all(['tr', 'li']):
                r_txt = " ".join(row.stripped_strings)
                if re.search(MONTHS_REGEX, r_txt, re.I) and re.search(r'Active', r_txt, re.I):
                    act_m = re.search(r'Active\s*Appointments?\s*:?\s*(\d+)', r_txt, re.I)
                    count = int(act_m.group(1)) if act_m else 0
                    
                    dt_m = re.search(r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}.*?(?:AM|PM|am|pm|\d{2}:\d{2}))', r_txt, re.I)
                    if not dt_m:
                        dt_m = re.search(r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})', r_txt, re.I)
                    
                    dt_str = dt_m.group(1).strip() if dt_m else "Date & Time Available"
                    dt_str = re.sub(r'(Active|Appointment|Rs).*', '', dt_str, flags=re.I).strip()
                    
                    results.append({
                        'doctor_name': doc_name,
                        'hospital': hosp_name,
                        'date_time': dt_str,
                        'active_appointments': count,
                        'status': 'Active',
                        'source_url': channel_url
                    })

        # Fallback if no panels found
        if not results:
            lines = [line.strip() for line in soup.get_text(separator="\n").split('\n') if line.strip()]
            current_hosp = "General Hospital"
            hosp_keywords = r'(hospital|medicare|laboratory|clinic|asiri|nawaloka|lanka|durdans|kdu|royal|ceymed|winlanka|vasan|hemachandra|suwasewana|singhe)'
            
            for i, line in enumerate(lines):
                if re.search(hosp_keywords, line, re.I) and len(line) < 60 and not re.search(r'active|appointment|doctor|surgeon|physician', line, re.I):
                    current_hosp = re.sub(r'\s*Sessions?$', '', line, flags=re.I).strip()
                    
                if re.search(MONTHS_REGEX, line, re.I) and re.search(r'\d{4}', line):
                    dt_str = line
                    for j in range(i, min(i+10, len(lines))):
                        act_m = re.search(r'Active\s*Appointments?\s*:?\s*(\d+)', lines[j], re.I)
                        if act_m:
                            results.append({
                                'doctor_name': doc_name,
                                'hospital': current_hosp,
                                'date_time': dt_str,
                                'active_appointments': int(act_m.group(1)),
                                'status': 'Active',
                                'source_url': channel_url
                            })
                            break

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
        print(f"Error scraping {channel_url}: {e}")
        return doc_name, results

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/get_specialties')
def get_specialties(): return jsonify({'specialties': fetch_doc_lk_specialties()})

@app.route('/api/search')
def search():
    query = request.args.get('query', '').strip()
    if not query: return jsonify({'error': 'No query'}), 400

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
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(parse_channel_page, url) for url in channel_links[:12]]
            for future in as_completed(futures):
                _, sessions = future.result()
                all_sessions.extend(sessions)

        return jsonify({'query': query, 'total_sessions': len(all_sessions), 'sessions': all_sessions})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
            r = requests.get(s_url, headers=HEADERS, timeout=6)
            soup = BeautifulSoup(r.text, 'html.parser')
            for a in soup.find_all('a', href=re.compile(r'/channel/\d+')):
                href = a.get('href')
                full_url = href if href.startswith('http') else BASE_URL + href
                if full_url not in seen:
                    seen.add(full_url)
                    channel_links.append(full_url)
            if len(channel_links) >= 5: break
        except: continue

    doctor_map = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(parse_channel_page, url) for url in channel_links[:15]]
        for future in as_completed(futures):
            doc_name, sessions = future.result()
            if not sessions: continue

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
                doctor_map[doc_name]['hospitals'].add(s['hospital'])

    ranked_doctors = []
    for d_name, d_data in doctor_map.items():
        d_data['hospitals'] = list(d_data['hospitals'])
        max_single = max([s['active_appointments'] for s in d_data['sessions']], default=0)
        d_data['high_demand'] = d_data['total_active_appointments'] >= 10 or max_single >= 10
        ranked_doctors.append(d_data)

    ranked_doctors.sort(key=lambda x: x['total_active_appointments'], reverse=True)
    return jsonify({'specialty': spec_name or spec_id, 'doctors_found': len(ranked_doctors), 'doctors': ranked_doctors})

if __name__ == '__main__': app.run(debug=True, port=5000)
