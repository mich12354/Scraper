import hashlib
import json
import os
import random
import threading
import time
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# --- SERWER HEALTH CHECK DLA DARMOWEGO RENDER WEB SERVICE ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is active and running 24/7!")

    # Wyciszenie logów zapytań HTTP w konsoli
    def log_message(self, format, *args):
        return

def start_health_check_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"[i] Uruchomiono serwer Health Check na porcie {port}")
    server.serve_forever()

# Uruchomienie serwera HTTP w osobnym wątku
threading.Thread(target=start_health_check_server, daemon=True).start()

# Ustawienia ze zmiennych środowiskowych. PAGES i KEYWORDS podajemy po przecinku.
PAGES = [u.strip() for u in os.getenv("PAGES").split(",") if u.strip()]
KEYWORDS = [k.strip().lower() for k in os.getenv("KEYWORDS").split(",") if k.strip()]
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
INTERVAL = int(os.getenv("CHECK_INTERVAL", "1800"))
SEEN_FILE = os.getenv("SEEN_FILE", "seen_posts.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def load_seen():
    # słownik: adres strony -> zbiór skrótów już widzianych postów
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return {url: set(ids) for url, ids in json.load(f).items()}
    except (FileNotFoundError, ValueError):
        return {}


def save_seen():
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump({url: sorted(ids) for url, ids in seen.items()}, f)


def post_hash(text):
    # białe znaki normalizuję, żeby drobna zmiana formatowania nie dawała "nowego" posta
    clean = " ".join(text.split())
    return hashlib.md5(clean.encode("utf-8")).hexdigest()


seen = load_seen()


def notify(text, url, keyword):
    if not WEBHOOK_URL:
        print("Brak DISCORD_WEBHOOK_URL, nie mam gdzie wysłać powiadomienia")
        return

    data = {
        "embeds": [{
            "title": f"Znaleziono słowo: {keyword}",
            "description": f"Strona: {url}\n\n{text[:1000]}",
            "color": 5814783,
        }]
    }

    try:
        r = requests.post(WEBHOOK_URL, json=data, timeout=10)
        if r.status_code in (200, 204):
            print(f"Wysłano na Discorda ('{keyword}')")
        else:
            print(f"Discord zwrócił błąd {r.status_code}")
    except requests.RequestException as e:
        print(f"Nie udało się wysłać na Discorda: {e}")


def check_pages():
    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Sprawdzam stron: {len(PAGES)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(user_agent=USER_AGENT).new_page()

        for url in PAGES:
            print(f"Ładuję {url}")
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(3000)

                posts = page.query_selector_all('div[data-ad-preview="message"]')
                known = seen.setdefault(url, set())

                # pierwsze odwiedziny tej strony: zapamiętuję to, co już jest,
                # ale nic nie wysyłam, bo to są stare posty
                first_visit = len(known) == 0
                if first_visit and posts:
                    print(f"Pierwsze uruchomienie dla tej strony, zapamiętuję {len(posts)} istniejących postów")

                for post in posts:
                    text = post.inner_text()
                    pid = post_hash(text)
                    if pid in known:
                        continue
                    known.add(pid)

                    if first_visit:
                        continue

                    lowered = text.lower()
                    for kw in KEYWORDS:
                        if kw in lowered:
                            print(f"Nowy post ze słowem: {kw}")
                            notify(text, url, kw)
                            break

                save_seen()
            except Exception as e:
                print(f"Błąd przy {url}: {e}")

            time.sleep(3)

        browser.close()


if __name__ == "__main__":
    if not PAGES:
        print("Lista PAGES jest pusta, ustaw zmienną środowiskową.")
        raise SystemExit(1)

    print(f"Bot wystartował, pilnuję stron: {len(PAGES)}")
    while True:
        check_pages()
        print(f"Następne sprawdzenie za {INTERVAL} s")
        time.sleep(INTERVAL)
