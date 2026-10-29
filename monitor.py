import hashlib
import json
import os
import random
import subprocess
import sys
import threading
import time
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv

# --- 1. WYMUSZENIE INSTALACJI CHROMIUM W FOLDERZE /tmp (BEZ DOCKERA I BEZ ROOT) ---
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/tmp/pw-browsers"

def ensure_playwright_browsers():
    browser_dir = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    if not os.path.exists(browser_dir) or not os.listdir(browser_dir):
        print("[i] Pobieram przeglądarkę Chromium do /tmp...")
        try:
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=True
            )
            print("[✓] Pobieranie Chromium zakończone sukcesem.")
        except Exception as e:
            print(f"[X] Błąd podczas instalacji Chromium: {e}")

ensure_playwright_browsers()

from playwright.sync_api import sync_playwright

# --- 2. SERWER HEALTH CHECK DLA DARMOWEGO RENDER WEB SERVICE ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is active and running!")

    def log_message(self, format, *args):
        return

def start_health_check_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"[i] Uruchomiono serwer Health Check na porcie {port}")
    server.serve_forever()

threading.Thread(target=start_health_check_server, daemon=True).start()

# Ustawienia ze zmiennych środowiskowych. PAGES i KEYWORDS podajemy po przecinku.
PAGES = [u.strip() for u in os.getenv("PAGES").split(",") if u.strip()]
KEYWORDS = [k.strip().lower() for k in os.getenv("KEYWORDS").split(",") if k.strip()]
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
INTERVAL = int(os.getenv("CHECK_INTERVAL", "1800"))
SEEN_FILE = os.getenv("SEEN_FILE", "seen_posts.json")

NOTIFY_EXISTING = False
SEEN_FILE = "seen_posts.json"
SCROLLS = 4  # ile razy przewinąć stronę, żeby Facebook doładował posty

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

POST_SELECTOR = 'div[data-ad-preview="message"], div[data-ad-comet-preview="message"]'

def keyword_variants(kw):
    # Zwykła fraza + wersje hashtagowe: #ai, #sztucznainteligencja, #sztuczna_inteligencja
    variants = [kw, "#" + kw.replace(" ", ""), "#" + kw.replace(" ", "_")]
    return list(dict.fromkeys(v.lower() for v in variants))


# Słowa kluczowe jako osobne słowa (żeby "ai" nie łapało "main", "said", "chain"...).
# Każdy wariant jest dopasowywany z granicami słów; wynik raportuje oryginalne słowo.
KEYWORD_PATTERNS = [
    (kw, re.compile(rf"(?<!\w){re.escape(variant)}(?!\w)", re.IGNORECASE))
    for kw in KEYWORDS
    for variant in keyword_variants(kw)
]


# --- Zapamiętane posty ------------------------------------------------------
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
    clean = " ".join(text.split())
    clean = re.sub(r"\s*(\.\.\.|…)?\s*(Wyświetl więcej|Wyświetl mniej|See more|See less)\s*$",
                   "", clean, flags=re.IGNORECASE)
    return hashlib.md5(clean[:300].encode("utf-8")).hexdigest()


seen = load_seen()


# --- Discord ----------------------------------------------------------------
def notify(text, url, keyword):
    data = {
        "embeds": [{
            "title": f"Znaleziono słowo: {keyword}",
            "description": f"Strona: {url}\n\n{text[:1000]}",
            "color": 5814783,
        }]
    }

    for attempt in range(3):
        try:
            r = requests.post(DISCORD_WEBHOOK_URL, json=data, timeout=10)
        except requests.RequestException as e:
            print(f"Nie udało się wysłać na Discorda: {e}")
            return

        if r.status_code in (200, 204):
            print(f"Wysłano na Discorda ('{keyword}')")
            return
        if r.status_code == 429:  # limit zapytań - czekam i próbuję ponownie
            try:
                wait = float(r.json().get("retry_after", 2))
            except ValueError:
                wait = 2
            time.sleep(wait + 0.5)
            continue

        print(f"Discord zwrócił błąd {r.status_code}")
        return


# --- Obsługa Facebooka ------------------------------------------------------
def dismiss_popups(page):
    # Okno logowania / cookies zasłania treść i blokuje przewijanie
    for label in ("Zamknij", "Close"):
        try:
            btn = page.query_selector(f'div[aria-label="{label}"][role="button"]')
            if btn:
                btn.click(timeout=2000)
                page.wait_for_timeout(500)
        except Exception:
            pass
    for text in ("Odrzuć opcjonalne pliki cookie", "Decline optional cookies"):
        try:
            btn = page.get_by_role("button", name=text)
            if btn.count():
                btn.first.click(timeout=2000)
                page.wait_for_timeout(500)
        except Exception:
            pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


EXPAND_JS = """
() => {
  const labels = ['wyświetl więcej', 'see more'];
  let n = 0;
  document.querySelectorAll('div[role="button"], span[role="button"]').forEach(el => {
    const t = (el.innerText || '').replace(/[.…]/g, '').trim().toLowerCase();
    if (t.length < 25 && labels.some(l => t === l)) { el.click(); n++; }
  });
  return n;
}
"""

MORE_TAIL = re.compile(r"\s*(\.\.\.|…)?\s*(Wyświetl więcej|See more)\s*$", re.IGNORECASE)

MORE_BUTTON = re.compile(r"^\s*[.…]*\s*(Wyświetl więcej|See more)\s*$", re.IGNORECASE)


def expand_posts(page):
    for _ in range(2):
        try:
            clicked = page.evaluate(EXPAND_JS)
        except Exception:
            clicked = 0
        page.wait_for_timeout(800)
        if not clicked:
            break

    # Zapas: wymuszony klik Playwrighta na tym, co jeszcze zostało
    try:
        for btn in page.locator('div[role="button"]', has_text=MORE_BUTTON).all():
            try:
                btn.click(timeout=1000, force=True)
            except Exception:
                pass
    except Exception:
        pass


def load_posts(page, url):
    # "networkidle" na Facebooku prawie nigdy nie następuje - czekam na DOM
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    dismiss_popups(page)

    # Facebook usuwa z DOM posty, które wyszły poza ekran, więc zbieram teksty
    # po każdym przewinięciu, a nie dopiero na końcu.
    collected = {}  # skrót -> tekst

    def collect():
        expand_posts(page)
        page.wait_for_timeout(500)
        for post in page.query_selector_all(POST_SELECTOR):
            try:
                text = post.inner_text()
            except Exception:
                continue  # element zniknął z DOM w trakcie odczytu
            if text.strip():
                collected.setdefault(post_hash(text), text)

    collect()
    for _ in range(SCROLLS):
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(1500)
        dismiss_popups(page)
        collect()

    # Jeśli ten sam post zebrałem raz uciętego, a raz rozwiniętego, zostawiam rozwinięty
    texts = list(collected.values())
    cleaned = [clean_text(t) for t in texts]
    result = []
    for t, c in zip(texts, cleaned):
        if MORE_TAIL.search(c):
            stem = MORE_TAIL.sub("", c).rstrip(". …")
            if any(o != c and o.startswith(stem) for o in cleaned):
                continue
        result.append(t)
    return result


def clean_text(text):
    # twarde spacje, znaki zerowej szerokości itp. -> zwykłe spacje / nic
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad]", "", text)
    return " ".join(text.split())


def find_keyword(text):
    text = clean_text(text)
    for kw, pattern in KEYWORD_PATTERNS:
        if pattern.search(text):
            return kw
    return None


def check_pages():
    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Sprawdzam stron: {len(PAGES)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="pl-PL",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        try:
            for url in PAGES:
                print(f"Ładuję {url}")
                try:
                    texts = load_posts(page, url)
                    print(f"Znaleziono postów: {len(texts)}")

                    if not texts:
                        print("Nie znaleziono żadnych postów (Facebook mógł zablokować "
                              "dostęp albo zmienił strukturę strony)")
                        continue

                    known = seen.setdefault(url, set())

                    # pierwsze odwiedziny tej strony: zapamiętuję to, co już jest,
                    # ale nic nie wysyłam, bo to są stare posty
                    first_visit = len(known) == 0
                    if first_visit:
                        print(f"Pierwsze uruchomienie dla tej strony, zapamiętuję {len(texts)} istniejących postów")

                    for text in texts:
                        pid = post_hash(text)
                        preview = clean_text(text)[:60]
                        if re.search(r"(Wyświetl więcej|See more)\s*$", clean_text(text), re.IGNORECASE):
                            preview += "  <-- UWAGA: wciąż ucięty"
                        if pid in known:
                            print(f"  [już znany] {preview}")
                            continue
                        known.add(pid)

                        if first_visit and not NOTIFY_EXISTING:
                            print(f"  [zapamiętany przy 1. uruchomieniu] {preview}")
                            continue

                        kw = find_keyword(text)
                        if kw:
                            print(f"  [NOWY, słowo '{kw}'] {preview}")
                            notify(text, url, kw)
                        else:
                            print(f"  [nowy, bez słowa kluczowego] {preview}")

                    save_seen()
                except Exception as e:
                    print(f"Błąd przy {url}: {e}")

                time.sleep(3)
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    print(f"Bot wystartował, pilnuję stron: {len(PAGES)}")
    while True:
        try:
            check_pages()
        except Exception as e:
            print(f"Błąd w cyklu sprawdzania: {e}")
        print(f"Następne sprawdzenie za {INTERVAL} s")
        time.sleep(INTERVAL)
