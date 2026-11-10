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
    """Scrape a doctor's channel page for sessions, active bookings, and hospital names."""
    results = []
    doc_name = fallback_doc_name

    try:
        r = requests.get(channel_url, headers=HEADERS, timeout=8)
        if r.status_code != 200:
            return doc_name, results

        soup = BeautifulSoup(r.text, 'html.parser')

        # 1. Doctor Name
        title_tag = soup.find('title')
        if title_tag:
            t_txt = title_tag.get_text()
            m = re.search(r'Channel\s+(.*?)\s+-', t_txt)
            if m:
                doc_name = m.group(1).strip()

        if doc_name == "Unknown Doctor":
            h_tag = soup.find(['h1', 'h2', 'h3'])
            if h_tag:
                doc_name = h_tag.get_text(strip=True)

        page_text = soup.get_text(separator="\n")
        lines = [line.strip() for line in page_text.split("\n") if line.strip()]

        current_hospital = "Unknown Hospital"
        
        for i, line in enumerate(lines):
            # Detect hospital header
            if "hospital" in line.lower() and len(line) < 70 and not "active" in line.lower():
                current_hospital = line.replace("Sessions", "").replace("sessions", "").strip()

            # Detect session date line
            if re.search(MONTHS_REGEX, line, re.I) and re.search(r'\d{4}', line):
                date_time_str = line
                active_count = 0
                status_str = "Active"

                # Look forward for Active Appointments
                for j in range(i + 1, min(i + 12, len(lines))):
                    next_line = lines[j]
                    act_m = re.search(r'Active\s*Appointments\s*:\s*(\d+)', next_line, re.I)
                    if act_m:
                        active_count = int(act_m.group(1))
                        status_str = next_line
                        break
                    if "NOT AVAILABLE" in next_line.upper() or "CANCELLED" in next_line.upper():
                        status_str = next_line
                        break

                results.append({
                    'doctor_name': doc_name,
                    'hospital': current_hospital,
                    'date_time': date_time_str,
                    'active_appointments': active_count,
                    'status': status_str,
                    'source_url': channel_url
                })

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
        # Multi-threaded fetch for fast response
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

    # Search URL variations for doc.lk
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

    # Scrape channels in parallel
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

    # Format doctors list
    ranked_doctors = []
    for d_name, d_data in doctor_map.items():
        d_data['hospitals'] = list(d_data['hospitals'])
        # Identify if high demand (> 10 appointments in total or single session)
        max_single = max([s['active_appointments'] for s in d_data['sessions']], default=0)
        d_data['high_demand'] = d_data['total_active_appointments'] >= 10 or max_single >= 10
        ranked_doctors.append(d_data)

    # Sort strictly from Highest Active Bookings to Lowest
    ranked_doctors.sort(key=lambda x: x['total_active_appointments'], reverse=True)

    return jsonify({
        'specialty': spec_name or spec_id,
        'doctors_found': len(ranked_doctors),
        'doctors': ranked_doctors
    })


if __name__ == '__main__':
    app.run(debug=True, port=5000)
