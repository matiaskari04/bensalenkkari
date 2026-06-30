#!/usr/bin/env python3
"""
AutoHelper — car part lookup, replacement guides, and price comparison.
Supports: Anthropic (Claude) and Groq (Llama / Mixtral — free tier available)
"""

import sys
import os
import re
import json
import time
import threading
import webbrowser
import urllib.parse
import requests
from bs4 import BeautifulSoup
from colorama import init, Fore, Back, Style

init(autoreset=True)

# ─── 7zap OEM catalog scraper (uses Playwright — runs locally, not server-side) ──
def _scrape_7zap_oem(make: str, model: str, year: str, part_name: str, vin: str = "") -> list[str]:
    """
    Scrape 7zap.com for OEM part numbers.
    Uses Playwright to handle JS rendering. Returns list of OEM part numbers found.
    Falls back gracefully if Playwright not available or site unreachable.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    # Build search URL — 7zap has a VIN search and a part number search
    # The most reliable approach: search by part name after navigating to the brand
    make_slug = make.lower().replace(" ", "+")
    model_slug = model.lower().replace(" ", "+").replace("-", "+")

    # 7zap URL patterns:
    # Brand catalog: https://{make}.7zap.com/en/ (e.g. saab.7zap.com/en/)
    # OR: https://7zap.com/en/catalog/cars/{Make}/
    # VIN search:    https://7zap.com/en/vin-decoder/ (enter VIN)
    # Part search:   https://7zap.com/en/search/?q={part_number}

    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                locale="en-US"
            )
            page = context.new_page()

            # Strategy 1: Use 7zap VIN search if we have a VIN
            if vin and len(vin) == 17:
                try:
                    page.goto(f"https://7zap.com/en/vin/?vin={vin}", timeout=20000, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)
                    # Look for model/generation links that appeared after VIN decode
                    links = page.query_selector_all("a[href*='/catalog/']")
                    if links:
                        # Click first matching generation
                        links[0].click()
                        page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass

            # Strategy 2: Navigate to brand > model page
            try:
                brand_url = f"https://7zap.com/en/catalog/cars/{make}/"
                page.goto(brand_url, timeout=15000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)

                # Find the model in the list
                model_links = page.query_selector_all(f"a[href*='{model_slug}'], a[href*='{model.replace(' ','+')}']")
                if model_links:
                    model_links[0].click()
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                    page.wait_for_timeout(2000)

                    # Find generation matching year
                    gen_links = page.query_selector_all("a[href*='/catalog/']")
                    for link in gen_links:
                        text = link.inner_text()
                        if year and year[:4] in text:
                            link.click()
                            page.wait_for_load_state("domcontentloaded", timeout=10000)
                            page.wait_for_timeout(2000)
                            break
            except Exception:
                pass

            # Strategy 3: Use 7zap search for part name + make/model
            try:
                search_query = urllib.parse.quote_plus(f"{make} {model} {part_name}")
                page.goto(f"https://7zap.com/en/search/?q={search_query}", timeout=15000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
            except Exception:
                pass

            # Extract OEM part numbers from the current page
            # 7zap displays OEM numbers in elements with class containing "part-number", "oem", "article"
            page_content = page.content()
            browser.close()

            # Parse for OEM part numbers — they follow patterns like:
            # Saab: 5084871, 90537406 (8-digit numbers)
            # Toyota: 43310-XXXXX (with hyphens)
            # BMW: 31-12-1-139-453 (with hyphens)
            # VW/Audi: 8K0-407-151-F (alphanumeric with hyphens)
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(page_content, "html.parser")

            # Look for elements that typically contain OEM numbers on 7zap
            oem_candidates = set()
            for el in soup.find_all(class_=re.compile(r"part.?num|oem|article|catalog.?num", re.I)):
                text = el.get_text(strip=True)
                # Extract alphanumeric part number patterns
                nums = re.findall(r"([A-Z0-9]{4,}(?:[-\s]?[A-Z0-9]+){0,4})", text, re.I)
                for n in nums:
                    n = n.strip()
                    # Filter: must look like a part number, not a word
                    if re.search(r"\d", n) and len(n) >= 5 and len(n) <= 20:
                        if not any(w in n.lower() for w in ["saab", "toyota", "ford", "http", "www"]):
                            oem_candidates.add(n.upper())

            # Also search raw text for OEM number patterns
            raw_text = soup.get_text()
            # Common OEM number patterns
            patterns = [
                r"(\d{7,10})",           # Pure numeric: 5084871
                r"([A-Z]{1,3}[-\s]\d{4,}[A-Z0-9-]*)",  # BMW style: 31-12-1234
                r"(\d{5}-[A-Z0-9]{3,6})", # Toyota style: 43310-XXXXX
                r"([A-Z0-9]{2,5}-\d{3}-\d{3}[A-Z]?)",  # VW/Audi
            ]
            for pat in patterns:
                found = re.findall(pat, raw_text, re.I)
                for f in found:
                    f = f.strip()
                    if len(f) >= 5:
                        oem_candidates.add(f.upper())

            results = list(oem_candidates)[:5]

    except Exception as e:
        pass  # Silently fail — AI fallback will handle it

    return results

# ─── AI Provider Configuration ────────────────────────────────────────────────
#
#  Set AI_PROVIDER to "anthropic" or "groq"
#  Then set the matching API key as an environment variable, or paste it below.
#
#  Groq free tier: https://console.groq.com  (no credit card needed)
#  Anthropic:      https://console.anthropic.com
#
AI_PROVIDER = os.environ.get("AUTOHELPER_PROVIDER", "groq").lower()  # "anthropic" | "groq"

# API keys — set via env vars (recommended) or hard-code here for quick testing
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GROQ_API_KEY      = os.environ.get("GROQ_API_KEY", "")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL      = "gemini-2.0-flash"
_tuning_cache = {}
TUNING_CACHE_TTL = 86400
GEMINI_URL        = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

# Models
ANTHROPIC_MODEL = "claude-haiku-4-5"          # cheapest Anthropic model; swap to claude-sonnet-4-6 for best quality
GROQ_MODEL      = "llama-3.3-70b-versatile"   # free on Groq; alternatives: llama-3.1-8b-instant, mixtral-8x7b-32768

# Endpoints
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GROQ_URL      = "https://api.groq.com/openai/v1/chat/completions"


def _call_anthropic(prompt: str, system: str, max_tokens: int) -> str:
    if not ANTHROPIC_API_KEY:
        raise Exception("ANTHROPIC_API_KEY not set")
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system
    r = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=30)
    r.raise_for_status()
    return r.json()["content"][0]["text"].strip()


def _call_groq(prompt: str, system: str, max_tokens: int) -> str:
    if not GROQ_API_KEY:
        raise Exception("GROQ_API_KEY not set")
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {
        "model": GROQ_MODEL,
        "max_tokens": max_tokens,
        "messages": messages,
        "temperature": 0.3,
    }
    # Retry up to 3 times on connection errors
    for attempt in range(3):
        try:
            r = requests.post(GROQ_URL, headers=headers, json=body, timeout=45)
            if not r.ok:
                raise Exception(f"{r.status_code} {r.reason}: {r.text[:200]}")
            return r.json()["choices"][0]["message"]["content"].strip()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.Timeout) as e:
            if attempt < 2:
                time.sleep(2 ** attempt)  # 1s, 2s backoff
                continue
            raise Exception(f"Connection failed after 3 attempts: {e}")


def _call_gemini(prompt: str, system: str, max_tokens: int) -> str:
    if not GEMINI_API_KEY:
        raise Exception("GEMINI_API_KEY not set")
    url = GEMINI_URL.format(model=GEMINI_MODEL, key=GEMINI_API_KEY)
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0.3,
        },
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    for attempt in range(3):
        try:
            r = requests.post(url, json=body, timeout=45)
            if r.status_code == 429:
                raise Exception(f"429 Rate limit: {r.text[:100]}")
            if not r.ok:
                raise Exception(f"{r.status_code} {r.reason}: {r.text[:200]}")
            data = r.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise Exception(f"Gemini connection failed: {e}")


def ai(prompt: str, system: str = "", max_tokens: int = 1200) -> str:
    """
    Call the configured AI provider with automatic fallback.
    Primary: as configured (default Groq). On any error, retries with the other provider.
    """
    # Build provider chain: configured primary → fallbacks in order
    if AI_PROVIDER == "anthropic":
        chain = [
            (_call_anthropic, ANTHROPIC_API_KEY, "Anthropic"),
            (_call_groq,      GROQ_API_KEY,      "Groq"),
            (_call_gemini,    GEMINI_API_KEY,    "Gemini"),
        ]
    else:
        chain = [
            (_call_groq,      GROQ_API_KEY,      "Groq"),
            (_call_gemini,    GEMINI_API_KEY,    "Gemini"),
            (_call_anthropic, ANTHROPIC_API_KEY, "Anthropic"),
        ]

    last_err = None
    for fn, key, name in chain:
        if not key:
            continue
        try:
            result = fn(prompt, system, max_tokens)
            return result
        except Exception as err:
            last_err = err
            print(f"  {Fore.YELLOW}⚠  {name} failed ({str(err)[:60]}) — trying next provider...{Style.RESET_ALL}")
            continue

    providers_tried = [name for fn, key, name in chain if key]
    return f"[AI error: tried {', '.join(providers_tried)}. Last error: {last_err}]"


# Keep 'claude' as an alias so the rest of the code stays unchanged
claude = ai

# ─── UI helpers ───────────────────────────────────────────────────────────────
W = 72  # terminal width

def banner():
    print()
    print(Fore.CYAN + Style.BRIGHT + "═" * W)
    print(Fore.CYAN + Style.BRIGHT + "  🔧  AutoHelper")
    print(Fore.CYAN + "  Car part lookup · Replacement guides · Price comparison")
    print(Fore.CYAN + Style.BRIGHT + "═" * W)
    # Show active AI provider
    if AI_PROVIDER == "anthropic" and ANTHROPIC_API_KEY:
        provider_str = f"Anthropic ({ANTHROPIC_MODEL})"
        provider_color = Fore.MAGENTA
    elif AI_PROVIDER == "groq" and GROQ_API_KEY:
        provider_str = f"Groq ({GROQ_MODEL})"
        provider_color = Fore.GREEN
    elif GROQ_API_KEY:
        provider_str = f"Groq — fallback ({GROQ_MODEL})"
        provider_color = Fore.YELLOW
    elif ANTHROPIC_API_KEY:
        provider_str = f"Anthropic — fallback ({ANTHROPIC_MODEL})"
        provider_color = Fore.YELLOW
    else:
        provider_str = "NO API KEY SET"
        provider_color = Fore.RED
    print(f"  AI provider: {provider_color}{Style.BRIGHT}{provider_str}{Style.RESET_ALL}")
    # Show registry status
    reg_parts = []
    if TRAFICOM_DB.exists():
        size_mb = TRAFICOM_DB.stat().st_size / 1_048_576
        reg_parts.append(f"{Fore.GREEN}Traficom DB ({size_mb:.0f} MB, offline){Style.RESET_ALL}")
    else:
        reg_parts.append(f"{Fore.YELLOW}Traficom DB (not set up — run traficom_setup.py){Style.RESET_ALL}")
    reg_parts.append(f"{Fore.GREEN}NHTSA (VINs){Style.RESET_ALL}")
    reg_parts.append(f"{Fore.GREEN}Norway API{Style.RESET_ALL}")
    if CARSXE_API_KEY:
        reg_parts.append(f"{Fore.GREEN}CarsXE{Style.RESET_ALL}")
    print(f"  Registries:    {' · '.join(reg_parts)}")
    # Price comparison source
    if SERPER_API_KEY:
        price_src = f"{Fore.GREEN}Google Shopping via Serper (live prices){Style.RESET_ALL}"
    else:
        price_src = f"{Fore.YELLOW}No SERPER_API_KEY — showing shop links only (free at serper.dev){Style.RESET_ALL}"
    print(f"  Price data:    {price_src}")
    print()

def section(title: str):
    print()
    print(Fore.YELLOW + Style.BRIGHT + f"▌ {title}")
    print(Fore.YELLOW + "─" * W)

def info(label: str, value: str):
    print(f"  {Fore.WHITE}{Style.BRIGHT}{label:<22}{Style.RESET_ALL}{value}")

def bullet(text: str, color=Fore.WHITE):
    for line in text.strip().split("\n"):
        print(f"  {color}• {Style.RESET_ALL}{line}")

def numbered(items):
    for i, item in enumerate(items, 1):
        print(f"  {Fore.CYAN}{i}.{Style.RESET_ALL} {item}")

def hyperlink(url: str, text: str) -> str:
    """Return an ANSI OSC 8 clickable hyperlink (works in Windows Terminal, iTerm2, etc.)"""
    return f"]8;;{url}\\{text}]8;;\\"

def link(label: str, url: str):
    print(f"  {Fore.BLUE}{Style.BRIGHT}↗ {hyperlink(url, label)}{Style.RESET_ALL}")
    print(f"    {Fore.BLUE}{hyperlink(url, url)}{Style.RESET_ALL}")

def prompt_input(label: str) -> str:
    return input(f"\n{Fore.GREEN}▶ {Style.BRIGHT}{label}{Style.RESET_ALL} ").strip()

def spinner(label: str, fn, *args, **kwargs):
    """Run fn(*args, **kwargs) in a thread while showing a spinner."""
    result = [None]
    error = [None]
    done = threading.Event()

    def worker():
        try:
            result[0] = fn(*args, **kwargs)
        except Exception as e:
            error[0] = e
        finally:
            done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    i = 0
    while not done.is_set():
        print(f"\r  {Fore.CYAN}{frames[i % len(frames)]} {label}...{Style.RESET_ALL}", end="", flush=True)
        time.sleep(0.1)
        i += 1
    print(f"\r  {Fore.GREEN}✓ {label}{Style.RESET_ALL}" + " " * 20)
    if error[0]:
        raise error[0]
    return result[0]

def divider():
    print(Fore.CYAN + Style.DIM + "─" * W)

# ─── Car lookup ───────────────────────────────────────────────────────────────
COUNTRIES = {
    "FI": "Finland", "SE": "Sweden", "NO": "Norway", "DE": "Germany",
    "GB": "United Kingdom", "US": "United States", "FR": "France",
    "IT": "Italy", "ES": "Spain", "NL": "Netherlands", "PL": "Poland",
    "EE": "Estonia", "LV": "Latvia", "LT": "Lithuania", "DK": "Denmark",
}

# CarsXE API key — free tier: 100 lookups/month, supports FI/SE/NO/DE/GB/US etc.
# Sign up free at: https://carsxe.com
CARSXE_API_KEY = os.environ.get("CARSXE_API_KEY", "")

# Traficom SQLite database (built by traficom_setup.py)
import sqlite3
from pathlib import Path
TRAFICOM_DB = Path(__file__).parent / "traficom.db"


def _lookup_traficom_db(plate: str) -> dict | None:
    """Look up a Finnish plate in the local Traficom SQLite database."""
    if not TRAFICOM_DB.exists():
        return None
    try:
        plate_clean = plate.replace("-", "").replace(" ", "").upper()
        conn = sqlite3.connect(TRAFICOM_DB)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM vehicles WHERE rekisteritunnus = ? LIMIT 1",
            (plate_clean,)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None

        make  = row["merkkiSelvakielinen"] or ""
        model = row["mallimerkinta"] or row["kaupallinenNimi"] or ""
        date  = row["kayttoonottopvm"] or row["ensirekisterointipvm"] or ""
        year  = date[:4] if date else ""

        # Engine: displacement in cc → litres
        cc = row["iskutilavuus"] or ""
        try:
            engine = f"{int(cc)/1000:.1f}L" if cc and int(cc) > 0 else ""
        except Exception:
            engine = cc

        power_kw = row["suurinNettoteho"] or ""
        if power_kw:
            try:
                engine += f" {int(float(power_kw))}kW"
            except Exception:
                pass

        fuel_raw = (row["kayttovoima"] or "").upper()
        fuel_map = {
            "01": "Petrol", "02": "Diesel", "04": "Electric",
            "03": "LPG", "05": "Hybrid", "06": "Petrol/Electric",
            "BENSIINI": "Petrol", "DIESEL": "Diesel", "SÄHKÖ": "Electric",
        }
        fuel = fuel_map.get(fuel_raw, fuel_raw)

        trans_raw = (row["vaihteisto"] or "").upper()
        trans_map = {"01": "Manual", "02": "Automatic", "03": "Semi-auto",
                     "MANUAALI": "Manual", "AUTOMAATTI": "Automatic"}
        trans = trans_map.get(trans_raw, trans_raw)

        body_raw = (row["korityyppi"] or "")
        color_raw = (row["vari"] or "")

        return {
            "make":         make.title(),
            "model":        model,
            "year":         year,
            "engine":       engine.strip(),
            "fuel":         fuel,
            "transmission": trans,
            "body":         body_raw,
            "color":        color_raw,
            "municipality": row["kunta"] or "",
            "confidence":   "high",
            "note":         "Data from Traficom open data (offline, CC BY 4.0)"
        }
    except Exception as e:
        return None


def _lookup_vindecoder_eu(vin: str) -> dict | None:
    """vindecoder.eu — free, covers European + US vehicles including Saab, no key needed for basic decode."""
    try:
        url = f"https://vindecoder.eu/api/v2/decode_vin?vin={vin}"
        headers = {"Accept": "application/json"}
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("success"):
            return None
        attrs = data.get("decode", [])
        # Response is a list of {label, value} dicts
        info = {item["label"]: item["value"] for item in attrs if item.get("value")}
        make  = info.get("Make", "") or info.get("Manufacturer", "")
        model = info.get("Model", "")
        year  = info.get("Model Year", "") or info.get("Year", "")
        engine = info.get("Engine", "") or info.get("Displacement", "")
        fuel  = info.get("Fuel Type", "") or info.get("Engine Type", "")
        trans = info.get("Transmission", "")
        body  = info.get("Body Style", "") or info.get("Body Type", "")
        if not make:
            return None
        return {
            "make": make, "model": model, "year": str(year),
            "engine": engine, "fuel": fuel, "transmission": trans,
            "body": body, "confidence": "high",
            "note": "Data from vindecoder.eu (free, European + US vehicles)"
        }
    except Exception:
        return None


def _lookup_nhtsa_vin(vin: str) -> dict | None:
    """Free NHTSA VIN decoder — fallback, best for US/Canadian vehicles."""
    try:
        url = f"https://vpic.nhtsa.dot.gov/api/vehicles/decodevin/{vin}?format=json"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        results = {item["Variable"]: item["Value"] for item in r.json().get("Results", [])}
        make  = results.get("Make", "") or ""
        model = results.get("Model", "") or ""
        year  = results.get("Model Year", "") or ""
        engine_size   = results.get("Displacement (L)", "") or ""
        engine_config = results.get("Engine Configuration", "") or ""
        fuel  = results.get("Fuel Type - Primary", "") or ""
        trans = results.get("Transmission Style", "") or ""
        body  = results.get("Body Class", "") or ""
        engine = f"{engine_size}L {engine_config}".strip(" L")
        if not make or make == "Not Applicable":
            return None
        return {
            "make": make, "model": model, "year": year,
            "engine": engine, "fuel": fuel, "transmission": trans,
            "body": body, "confidence": "high",
            "note": "Data from NHTSA VIN database (free, US vehicles)"
        }
    except Exception:
        return None


def _lookup_norway_plate(plate: str) -> dict | None:
    """Norway Statens vegvesen open API — no key needed."""
    try:
        plate_clean = plate.replace("-", "").replace(" ", "").upper()
        url = f"https://www.vegvesen.no/ws/no/vegvesen/kjoretoy/felles/datautlevering/enkeltoppslag/kjoretoydata?kjennemerke={plate_clean}"
        headers = {"SVV-Authorization": "Ij0wMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDBiMGMwMjM=", "Accept": "application/json"}
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        kjoretoy = data.get("kjoretoydataListe", [{}])[0]
        godkjenning = kjoretoy.get("godkjenning", {})
        teknisk = godkjenning.get("tekniskGodkjenning", {}).get("tekniskeData", {})
        generelt = teknisk.get("generelt", {})
        motor_list = teknisk.get("motorOgDrivverk", {}).get("motor", [{}])
        motor = motor_list[0] if motor_list else {}

        make  = generelt.get("merke", [{}])[0].get("merke", "") if generelt.get("merke") else ""
        model = generelt.get("handelsbetegnelse", [{}])[0] if generelt.get("handelsbetegnelse") else ""
        if isinstance(model, dict):
            model = model.get("handelsbetegnelse", "")
        year_raw = godkjenning.get("forstegangsGodkjenning", {}).get("forstegangRegistrertDato", "")
        year = year_raw[:4] if year_raw else ""
        engine_cc = motor.get("slagvolum", "")
        engine = f"{int(engine_cc)/1000:.1f}L" if engine_cc else ""
        fuel_raw = motor.get("drivstoff", [{}])
        fuel = fuel_raw[0].get("drivstoffKode", {}).get("kodeBeskrivelse", "") if fuel_raw else ""

        if not make:
            return None
        return {
            "make": make, "model": model, "year": year,
            "engine": engine, "fuel": fuel, "transmission": "",
            "body": "", "confidence": "high",
            "note": "Data from Statens vegvesen (Norway official registry, free)"
        }
    except Exception:
        return None


def _lookup_carsxe(plate: str, country: str) -> dict | None:
    """CarsXE plate decoder — free tier 100/month, supports FI/SE/NO/DE/GB etc."""
    if not CARSXE_API_KEY:
        return None
    try:
        url = "https://api.carsxe.com/v2/platedecoder"
        params = {"key": CARSXE_API_KEY, "plate": plate, "country": country.upper()}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            return None
        attrs = data.get("attributes", {})
        return {
            "make":         attrs.get("make", ""),
            "model":        attrs.get("model", ""),
            "year":         str(attrs.get("year", attrs.get("registration_year", ""))),
            "engine":       attrs.get("engine", ""),
            "fuel":         attrs.get("fuel_type", ""),
            "transmission": attrs.get("transmission", ""),
            "body":         attrs.get("body_type", ""),
            "confidence":   "high",
            "note":         f"Data from CarsXE ({country.upper()} registry)"
        }
    except Exception:
        return None


def _lookup_ktype_from_vin(vin: str) -> str | None:
    """Get TecDoc K-Type ID from VIN via vindecoder.eu or free TecDoc VIN lookup."""
    try:
        # vindecoder.eu sometimes returns ktype
        url = f"https://vindecoder.eu/api/v2/decode_vin?vin={vin}"
        r = requests.get(url, headers={"Accept": "application/json"}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            attrs = {item["label"]: item["value"] for item in data.get("decode", []) if item.get("value")}
            ktype = attrs.get("KType") or attrs.get("TecDoc KType") or attrs.get("K-Type")
            if ktype:
                return str(ktype)
    except Exception:
        pass
    return None


def _lookup_ai_fallback(plate_or_vin: str, country: str) -> dict:
    """AI guess — used only when no registry data is available."""
    is_vin = len(plate_or_vin) == 17 and plate_or_vin.isalnum()
    id_type = "VIN code" if is_vin else "license plate number"
    country_name = COUNTRIES.get(country.upper(), country)
    prompt = f"""
The user has a car from {country_name} with {id_type}: {plate_or_vin.upper()}

{"Decode this VIN fully (make, model, year, engine, trim, country of manufacture)." if is_vin else "You cannot look up live registry data. Make your best estimate based on any patterns you know, but be honest about uncertainty."}

Respond ONLY with a JSON object (no markdown), keys: make, model, year, engine, fuel, transmission, body, confidence (high/medium/low), note.
"""
    raw = claude(prompt, max_tokens=400)
    try:
        raw = re.sub(r"```json|```", "", raw).strip()
        return json.loads(raw)
    except Exception:
        return {
            "make": "Unknown", "model": "Unknown", "year": "Unknown",
            "engine": "Unknown", "fuel": "Unknown", "transmission": "Unknown",
            "body": "Unknown", "confidence": "low",
            "note": "Could not retrieve vehicle data. Please correct manually."
        }


def lookup_vehicle(plate_or_vin: str, country: str) -> dict:
    """
    Multi-source vehicle lookup with automatic fallback chain:
      1. NHTSA (VINs, free, no key)
      2. Norway Statens vegvesen (NO plates, free, no key)
      3. CarsXE plate decoder (30+ countries incl. FI/SE, free tier, needs key)
      4. AI fallback (always works, low confidence for plates)
    """
    is_vin = len(plate_or_vin) == 17 and plate_or_vin.isalnum()
    country = country.upper()

    # 1. VIN decode — vindecoder.eu first (European + US), NHTSA as fallback
    if is_vin:
        result = _lookup_vindecoder_eu(plate_or_vin)
        if result:
            return result
        result = _lookup_nhtsa_vin(plate_or_vin)
        if result:
            return result

    # 2. Traficom offline DB (Finnish plates, built by traficom_setup.py)
    if country == "FI" and not is_vin:
        result = _lookup_traficom_db(plate_or_vin)
        if result:
            return result

    # 3. Norway official free API
    if country == "NO" and not is_vin:
        result = _lookup_norway_plate(plate_or_vin)
        if result:
            return result

    # 4. CarsXE (free tier, needs key, supports FI SE NO DE GB FR etc.)
    if not is_vin and CARSXE_API_KEY:
        result = _lookup_carsxe(plate_or_vin, country)
        if result:
            return result

    # 4. AI fallback
    result = _lookup_ai_fallback(plate_or_vin, country)

    # Add hint about CarsXE if no key set and it's a plate lookup
    if not is_vin and not CARSXE_API_KEY and result.get("confidence") != "high":
        result["note"] = (
            result.get("note", "") +
            " | Tip: set CARSXE_API_KEY for real registry data (free at carsxe.com)"
        )
    return result

# ─── OEM number web search ───────────────────────────────────────────────────

# OEM part number patterns — covers most manufacturers
OEM_PATTERNS = [
    r"(?<![0-9])(\d{7,10})(?![0-9])",
    r"(\d{5}-\d{5}-\d{2})",
    r"(\d{5}-\d{5})",
    r"([A-Z0-9]{2,5}-\d{3,6}-[A-Z0-9]{1,8})",
    r"([A-Z]{2,4}\d{5,8})",
]

def _extract_part_numbers(text: str) -> list[str]:
    """Extract OEM-looking part numbers from a block of text."""
    found = []
    # Filter words to exclude
    skip = {"HTTP", "HTTPS", "WWW", "HTML", "JSON", "NULL", "TRUE", "FALSE"}
    for pattern in OEM_PATTERNS:
        for m in re.findall(pattern, text, re.I):
            m = m.strip().upper()
            if (len(m) >= 5 and len(m) <= 20
                    and re.search(r"\d", m)           # must contain a digit
                    and not re.match(r"^(19|20)\d{2}$", m)  # not a bare year
                    and m not in skip
                    and m not in found):
                found.append(m)
    return found


def _search_oem_via_serper(make: str, model: str, year: str, engine: str, part: str, vin: str) -> list[str]:
    """Use Serper web search API to find OEM numbers (uses existing SERPER_API_KEY)."""
    if not SERPER_API_KEY:
        return []
    queries = [
        f"{year} {make} {model} {engine} {part} OEM part number",
    ]
    if vin:
        queries.append(f"{vin} {part} OEM number")
    found = []
    for q in queries[:2]:
        try:
            r = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                json={"q": q, "num": 5},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            # Collect text from organic results
            text = " ".join([
                item.get("snippet", "") + " " + item.get("title", "")
                for item in data.get("organic", [])
            ])
            for n in _extract_part_numbers(text):
                if n not in found:
                    found.append(n)
        except Exception:
            pass
    return found


def _search_oem_direct_sites(make: str, model: str, year: str, part: str) -> list[str]:
    """Scrape parts sites directly for OEM numbers."""
    found = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,*/*",
    }
    sites = [
        f"https://www.autodoc.fi/search?query={urllib.parse.quote_plus(f'{year} {make} {model} {part}')}",
        f"https://www.motonet.fi/fi/search?q={urllib.parse.quote_plus(f'{make} {model} {part}')}",
        f"https://www.ak24.fi/fi/search?term={urllib.parse.quote_plus(f'{make} {model} {part}')}",
        f"https://trodo.com/en/search?q={urllib.parse.quote_plus(f'{make} {model} {part}')}",
    ]
    for url in sites:
        try:
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code != 200:
                continue
            # Extract from JSON-LD structured data and OE fields
            text = " ".join([
                *re.findall(r'"mpn"\s*:\s*"([^"]+)"', r.text),
                *re.findall(r'"oeNumber[s]?"\s*:\s*"([^"]+)"', r.text),
                *re.findall(r'"articleNumber"\s*:\s*"([^"]+)"', r.text),
                *re.findall(r'data-oem="([^"]+)"', r.text),
            ])
            for n in _extract_part_numbers(text):
                if n not in found:
                    found.append(n)
            if found:
                break  # stop after first successful site
        except Exception:
            pass
    return found


def _search_oem_numbers(make: str, model: str, year: str, engine: str, part: str, vin: str = "") -> list[str]:
    """
    Find OEM part numbers via:
    1. Serper web search (if key available — best quality results)
    2. Direct scraping of parts sites
    Returns deduplicated list of candidate OEM numbers.
    """
    # Try Serper first (best results)
    found = _search_oem_via_serper(make, model, year, engine, part, vin)

    # Also try direct site scraping in parallel
    if len(found) < 3:
        direct = _search_oem_direct_sites(make, model, year, part)
        for n in direct:
            if n not in found:
                found.append(n)

    return found[:8]


# ─── Part info via Claude ─────────────────────────────────────────────────────
def _normalise_part_numbers(raw: list) -> list:
    """
    Normalise part_numbers to a consistent list of dicts:
    {"number": "5084871", "brand": "Saab", "type": "OEM"}
    Handles both old string format and new dict format.
    """
    result = []
    for i, item in enumerate(raw or []):
        if isinstance(item, dict):
            result.append({
                "number": str(item.get("number", item.get("part_number", ""))).strip(),
                "brand":  str(item.get("brand", item.get("manufacturer", ""))).strip(),
                "type":   str(item.get("type", "OEM" if i == 0 else "Aftermarket")).strip(),
            })
        elif isinstance(item, str):
            item = item.strip()
            if item:
                result.append({
                    "number": item,
                    "brand":  "",
                    "type":   "OEM" if i == 0 else "Aftermarket",
                })
    return [p for p in result if p["number"]]


def get_part_info(part: str, car: dict) -> dict:
    year   = car.get("year", "")
    make   = car.get("make", "")
    model  = car.get("model", "")
    engine = car.get("engine", "")
    vin    = car.get("vin", "")
    fuel   = car.get("fuel", "")
    body   = car.get("body", "")

    car_str = f"{year} {make} {model} {engine}".strip()
    vin_line = f"VIN: {vin}" if vin else ""
    extra = " | ".join(filter(None, [fuel, body]))

    # Run 7zap scrape + web search in parallel for speed
    import concurrent.futures
    zap_ref = [None]
    web_ref = [None]

    def _do_zap():
        zap_ref[0] = _scrape_7zap_oem(make, model, year, part, vin)

    def _do_web():
        web_ref[0] = _search_oem_numbers(make, model, year, engine, part, vin)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(_do_zap)
        f2 = ex.submit(_do_web)
        concurrent.futures.wait([f1, f2], timeout=15)

    zap_numbers = zap_ref[0] or []
    web_oem_numbers = web_ref[0] or []

    # Merge — web search first (more reliable), then 7zap
    all_found = []
    for n in web_oem_numbers + zap_numbers:
        if n not in all_found:
            all_found.append(n)

    zap_hint = ""
    if all_found:
        zap_hint = f"\nWeb search and catalog found these OEM part numbers: {', '.join(all_found[:6])}\nUse these as the basis for part_numbers — verify which are genuine OEM vs aftermarket."

    system = "You are an automotive parts specialist. You respond with valid JSON only. No markdown, no explanation. Just a raw JSON object starting with { and ending with }. Write description, replacement_summary and oem_note in Finnish (suomeksi). All other fields stay in English."

    prompt = f"""Vehicle: {car_str}
{vin_line}
Part: {part}{zap_hint}

Known example for reference: Saab 9-5 YS3E 2.3T front lower ball joint = OEM 5084871 / GM 90537406 / Moog ES80537.

Respond with this exact JSON structure:
{{
  "canonical_name": "Front Lower Left Ball Joint",
  "part_numbers": [
    {{"number": "5084871", "brand": "Saab", "type": "OEM", "quality": "Genuine OEM"}},
    {{"number": "90537406", "brand": "GM", "type": "OEM", "quality": "OEM equivalent"}},
    {{"number": "ES80537", "brand": "Moog", "type": "Aftermarket", "quality": "Premium"}},
    {{"number": "11025", "brand": "Febi Bilstein", "type": "Aftermarket", "quality": "Good"}},
    {{"number": "SB-8019", "brand": "TRW", "type": "Aftermarket", "quality": "Premium"}}
  ],
  "oem_note": "5084871/90537406 are genuine OEM. Moog and TRW are premium aftermarket. Febi is a good budget option.",
  "description": "Connects the control arm to the steering knuckle. Fails due to wear in the ball socket causing play and clunking.",
  "replacement_summary": "1. Safety first: engage parking brake, chock wheels, wear eye protection.\n2. Loosen wheel nuts while on ground.\n3. Jack up vehicle and support on axle stands at chassis jacking points.\n4. Remove wheel.\n5. [detailed steps specific to this part and car]\n...continue with all steps including torque specs",
  "difficulty": "Medium",
  "tools_needed": ["Ball joint press", "Torque wrench", "Floor jack", "Axle stands"],
  "avg_labor_hours": "1.5-2 hours",
  "search_keywords": "5084871,90537406,ES80537,11025"
}}

Fill in the actual values for: {car_str} — {part}
For part_numbers: include ALL numbers you know — genuine OEM first, then quality aftermarket. For each entry include a "quality" field: "Genuine OEM", "OEM equivalent", "Premium" (Moog, TRW, Lemförder, SKF, Sachs, Monroe, Bosch, NGK, Gates), "Good" (Febi, Meyle, Delphi, FAG), or "Budget". Aim for 4-8 entries total.
For replacement_summary: write a THOROUGH guide a beginner can follow. Include safety precautions, specific torque values in Nm (not ft-lbs), common mistakes to avoid, whether alignment is needed after, and any model-specific tips for this exact car. Minimum 10 detailed steps.
Only include part numbers you are reasonably confident about."""

    raw = claude(prompt, system=system, max_tokens=2000)
    try:
        # Strip markdown code fences and leading/trailing text
        clean = re.sub(r"```json|```", "", raw).strip()
        clean = "".join(ch for ch in clean if ord(ch) >= 32 or ch in "\n\t\r")
        # Extract the first complete JSON object from the response
        # Find outermost { } pair
        brace_start = clean.find("{")
        if brace_start == -1:
            raise ValueError("No JSON object found in response")
        depth = 0
        brace_end = -1
        for i, ch in enumerate(clean[brace_start:], brace_start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    brace_end = i + 1
                    break
        if brace_end == -1:
            raise ValueError("Unclosed JSON object")
        clean = clean[brace_start:brace_end]
        result = json.loads(clean)
        if os.environ.get("AUTOHELPER_DEBUG"):
            print(f"  {Fore.WHITE}{Style.DIM}[DEBUG] AI returned {len(result)} fields, part_numbers: {result.get('part_numbers')}{Style.RESET_ALL}")
        # Ensure required fields exist
        result.setdefault("canonical_name", part)
        result.setdefault("part_numbers", [])
        result.setdefault("description", "")
        result.setdefault("replacement_summary", "")
        result.setdefault("difficulty", "Unknown")
        result.setdefault("tools_needed", [])
        result.setdefault("avg_labor_hours", "Unknown")
        result.setdefault("search_keywords", part)
        # Normalise part_numbers — may be list of strings OR list of dicts
        result["part_numbers"] = _normalise_part_numbers(result["part_numbers"])
        return result
    except Exception as parse_err:
        if os.environ.get("AUTOHELPER_DEBUG"):
            print(f"  {Fore.RED}[DEBUG] JSON parse failed: {parse_err}{Style.RESET_ALL}")
            print(f"  {Fore.RED}[DEBUG] Raw AI response: {raw[:300]}{Style.RESET_ALL}")
        # Try to extract individual fields even if full JSON parse fails
        def extract_field(key, text):
            m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', text)
            return m.group(1) if m else ""
        return {
            "canonical_name": extract_field("canonical_name", raw) or part,
            "part_numbers":   re.findall(r'"([A-Z0-9]{5,})"', raw)[:3],
            "description":    extract_field("description", raw) or "Could not retrieve description.",
            "replacement_summary": extract_field("replacement_summary", raw) or "",
            "difficulty":     extract_field("difficulty", raw) or "Unknown",
            "tools_needed":   [],
            "avg_labor_hours":extract_field("avg_labor_hours", raw) or "Unknown",
            "search_keywords":extract_field("search_keywords", raw) or part,
        }

# ─── YouTube search ───────────────────────────────────────────────────────────
def search_youtube(query: str) -> list[dict]:
    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.youtube.com/results?search_query={encoded}"
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        ids = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', r.text)
        titles = re.findall(r'"title":\{"runs":\[\{"text":"([^"]+)"', r.text)
        seen = []
        results = []
        for vid_id, title in zip(ids, titles):
            if vid_id not in seen:
                seen.append(vid_id)
                results.append({
                    "title": title,
                    "url": f"https://www.youtube.com/watch?v={vid_id}"
                })
            if len(results) >= 4:
                break
        return results
    except Exception:
        return []

# ─── Exploded view / diagram search ──────────────────────────────────────────
def get_exploded_view_images(part: str, car: dict) -> list[dict]:
    """Fetch exploded view diagram images via Serper Images API.
    Returns list of {url, title} dicts. Uses specific queries to avoid wrong parts."""
    serper_key = os.environ.get("SERPER_API_KEY", "")
    make  = car.get("make", "")
    model = car.get("model", "")
    year  = car.get("year", "")

    # Use multiple targeted queries — car-specific first, then generic part diagram
    queries = [
        f"{make} {model} {year} {part} diagram",
        f"{make} {model} {part} exploded diagram",
        f"{part} exploded view parts diagram",
    ]

    results = []
    seen_urls = set()

    if not serper_key:
        return results

    for query in queries:
        if len(results) >= 6:
            break
        try:
            r = requests.post(
                "https://google.serper.dev/images",
                headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
                json={"q": query, "num": 6},
                timeout=10,
            )
            if r.status_code != 200:
                continue
            for item in r.json().get("images", []):
                url = item.get("imageUrl", "")
                title = item.get("title", query)
                # Filter out obviously wrong images — must relate to the part
                part_words = part.lower().split()
                title_low = title.lower()
                # Skip if title mentions a completely different major assembly
                skip_words = ["transmission", "gearbox", "engine block", "cylinder head"]
                if any(s in title_low for s in skip_words) and not any(p in title_low for p in part_words):
                    continue
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    results.append({"url": url, "title": title})
                if len(results) >= 6:
                    break
        except Exception:
            pass

    return results

# ─── Price comparison ────────────────────────────────────────────────────────
# Uses Google Shopping via Serper.dev (free: 2500 searches/month).
# Sign up free at https://serper.dev — then: export SERPER_API_KEY=your_key
# Without a key, generates direct shop search links instead.

SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")

# Amazon Associates affiliate tag — set via env var or paste here
AMAZON_TAG   = os.environ.get("AMAZON_TAG", "")
AUTODOC_AFF  = os.environ.get("AUTODOC_AFF", "")  # e.g. "bensalenkkari"

# English -> Finnish part name translations for Finnish shop searches
EN_TO_FI_PARTS = {
    "brake pads":           "jarrupalat",
    "brake discs":          "jarrulevyt",
    "brake rotors":         "jarrulevyt",
    "shock absorber":       "iskunvaimennin",
    "strut":                "iskunvaimennin",
    "control arm":          "tukivarsi",
    "ball joint":           "pallonivel",
    "tie rod":              "raidetanko",
    "wheel bearing":        "pyörälaakeri",
    "cv axle":              "vetoakseli",
    "cv joint":             "tasonivelakseli",
    "alternator":           "laturi",
    "starter motor":        "käynnistin",
    "water pump":           "vesipumppu",
    "timing belt":          "hammashihna",
    "drive belt":           "kiilahihna",
    "serpentine belt":      "kiilahihna",
    "thermostat":           "termostaatti",
    "radiator":             "jäähdytin",
    "oil filter":           "öljynsuodatin",
    "air filter":           "ilmansuodatin",
    "fuel filter":          "polttoainesuodatin",
    "cabin filter":         "sisätilasuodatin",
    "spark plugs":          "sytytystulpat",
    "spark plug":           "sytytystulppa",
    "clutch kit":           "kytkinsarja",
    "clutch":               "kytkin",
    "exhaust pipe":         "pakoputki",
    "catalytic converter":  "katalysaattori",
    "oxygen sensor":        "lambda-anturi",
    "lambda sensor":        "lambda-anturi",
    "fuel pump":            "polttoainepumppu",
    "wiper blades":         "pyyhkijänsulat",
    "wiper blade":          "pyyhkijänsulka",
    "battery":              "akku",
    "headlight":            "ajovalo",
    "tail light":           "takavalo",
    "mirror":               "peili",
    "side mirror":          "sivupeili",
    "lower control arm":    "alatukivarsi",
    "upper control arm":    "ylätukivarsi",
    "sway bar link":        "vakaajatanko",
    "stabilizer link":      "vakaajatanko",
    "track rod":            "raidetanko",
    "caliper":              "jarrusatula",
    "brake caliper":        "jarrusatula",
    "intercooler":          "välijäähdytin",
    "turbocharger":         "turboahdin",
    "power steering pump":  "ohjaustehostinpumppu",
    "gearbox oil":          "vaihteistoöljy",
    "engine oil":           "moottoriöljy",
    "coolant":              "jäähdytysneste",
    "antifreeze":           "jäähdytysneste",
    "brake fluid":          "jarruneste",
    "spring":               "jousi",
    "coil spring":          "kierrejousi",
    "wishbone":             "tukivarsi",
    "knuckle":              "ohjaussolkka",
    "hub":                  "pyörännapa",
    "driveshaft":           "vetoakseli",
    "prop shaft":           "kardaaniakseli",
    "exhaust manifold":     "pakosarjakaasutin",
    "intake manifold":      "imusarjakaasutin",
    "throttle body":        "kaasuläppäkotelo",
    "mass air flow":        "ilmamassavirtausanturi",
    "maf sensor":           "maf-anturi",
    "abs sensor":           "abs-anturi",
    "crankshaft sensor":    "kampiakselinanturi",
    "camshaft sensor":      "nokka-akselianturi",
    "engine mount":         "moottorin tuki",
    "gearbox mount":        "vaihteiston tuki",
}

def _translate_part_to_fi(part: str) -> str:
    """Translate English part name to Finnish for Finnish shop searches."""
    low = part.lower()
    best_key, best_len = None, 0
    for key, fi in EN_TO_FI_PARTS.items():
        if key in low and len(key) > best_len:
            best_key, best_len = key, len(key)
    if best_key:
        return part.lower().replace(best_key, EN_TO_FI_PARTS[best_key])
    return ""  # No translation found

def _add_amazon_tag(url: str) -> str:
    if not AMAZON_TAG or not url or "amazon." not in url:
        return url
    sep = "&" if "?" in url else "?"
    if f"tag={AMAZON_TAG}" in url:
        return url
    return f"{url}{sep}tag={AMAZON_TAG}"

def _add_autodoc_aff(url: str) -> str:
    """Add Autodoc affiliate parameter."""
    if not AUTODOC_AFF or not url or "autodoc." not in url:
        return url
    sep = "&" if "?" in url else "?"
    if "utm_source" in url:
        return url
    return f"{url}{sep}utm_source={AUTODOC_AFF}&utm_medium=referral"

def _clean_shop_url(url: str, source: str) -> str:
    """
    Fix Google Shopping redirect URLs by reconstructing direct shop URLs.
    Google Shopping Serper results often return ibp=oshop redirect links.
    """
    if not url or url == "#":
        return url

    # Detect Google Shopping redirect URLs
    is_google_redirect = ("ibp=oshop" in url or "prds=" in url or
                          ("google.com" in url and "shopping" in url.lower()))

    if not is_google_redirect:
        # Direct URL - apply affiliate tags and return
        if "amazon." in url.lower():
            url = _add_amazon_tag(url)
        if "autodoc." in url.lower():
            url = _add_autodoc_aff(url)
        return url

    # It's a Google redirect — build a direct search URL for the shop instead
    src = source.lower()
    # We don't have the product URL, so link to shop search
    # The query will be injected by the caller
    return None  # Signal to caller to use fallback  # e.g. "bensalenkkari-21"



COUNTRY_GOOGLE = {
    "FI": ("google.fi",   "EUR", "fi"),
    "SE": ("google.se",   "SEK", "sv"),
    "NO": ("google.no",   "NOK", "no"),
    "DE": ("google.de",   "EUR", "de"),
    "GB": ("google.co.uk","GBP", "en"),
    "US": ("google.com",  "USD", "en"),
    "FR": ("google.fr",   "EUR", "fr"),
    "EE": ("google.ee",   "EUR", "et"),
    "LV": ("google.lv",   "EUR", "lv"),
    "LT": ("google.lt",   "EUR", "lt"),
    "DK": ("google.dk",   "DKK", "da"),
    "PL": ("google.pl",   "PLN", "pl"),
    "NL": ("google.nl",   "EUR", "nl"),
    "IT": ("google.it",   "EUR", "it"),
    "ES": ("google.es",   "EUR", "es"),
}

PREFERRED_SHOPS = {
    "FI": ["motonet", "ak24", "autodoc", "biltema", "trodo", "raskone", "hankkija"],
    "SE": ["biltema", "mekonomen", "motonet", "autodoc", "trodo"],
    "NO": ["biltema", "mekonomen", "autodoc", "trodo"],
    "EE": ["ak24", "autodoc", "trodo", "biltema"],
    "DE": ["autodoc", "kfzteile24", "autoteile24", "trodo"],
    "GB": ["autodoc", "eurocarparts", "halfords"],
    "DEFAULT": ["autodoc", "ak24", "motonet", "trodo", "biltema"],
}


def _merge_price_results(primary: list, secondary: list) -> list:
    """Merge two price result lists, deduplicating by shop domain."""
    seen_shops = {}
    merged = []
    BOOSTED = {"motonet", "ak24", "autodoc", "biltema", "trodo"}
    for item in primary + secondary:
        shop = item.get("shop", "")
        key = re.sub(r"[^a-z].*", "", shop.lower())[:15]
        max_per = 3 if key in BOOSTED else 2
        count = seen_shops.get(key, 0)
        if count < max_per:
            seen_shops[key] = count + 1
            merged.append(item)
    return merged[:18]


def fetch_prices(part: str, car: dict, country: str, part_info: dict = None) -> list[dict]:
    """
    Search by OEM part number first (most accurate), fall back to name-based search.
    part_info: result from get_part_info, used to extract OEM part numbers.
    """
    make   = car.get("make", "")
    model  = car.get("model", "")
    year   = car.get("year", "")
    engine = car.get("engine", "")

    # Try OEM part numbers first
    oem_numbers = []
    if part_info:
        raw_numbers = part_info.get("part_numbers", [])
        keywords    = part_info.get("search_keywords", "")
        skip_words  = {make.lower(), model.lower(), year, "saab", "toyota", "ford", "volvo", "bmw", "audi"}
        # Handle both string and dict formats from normalised part_numbers
        for item in raw_numbers:
            n = item.get("number", "") if isinstance(item, dict) else str(item)
            n = n.strip()
            if n and len(n) >= 4 and not any(w in n.lower() for w in skip_words):
                oem_numbers.append(n)
        # Also parse search_keywords
        for kw in keywords.split(","):
            kw = kw.strip()
            if kw and len(kw) >= 4 and re.search(r"[A-Z0-9]{4,}", kw, re.I) and " " not in kw:
                if kw not in oem_numbers:
                    oem_numbers.append(kw)

    nordic_countries = {"FI", "SE", "NO", "EE"}
    use_bilingual = country.upper() in nordic_countries

    if oem_numbers:
        primary = oem_numbers[0]
        base_part = part.split(",")[0].strip()
        oem_query = f"{primary} {make} {model} {base_part}".strip()
        fallback_query = f"{year} {make} {model} {base_part}".strip()
        print(f"  {Fore.GREEN}✓ Searching by OEM part number: {primary}{Style.RESET_ALL}")
        if SERPER_API_KEY:
            results = _fetch_via_serper(oem_query, country, oem_numbers)
            # Also search with Finnish term for Nordic countries
            if use_bilingual:
                fi_part = _translate_part_to_fi(base_part)
                if fi_part:
                    fi_query = f"{primary} {make} {model} {fi_part}".strip()
                    fi_results = _fetch_via_serper(fi_query, country, oem_numbers)
                    results = _merge_price_results(results, fi_results)
            return results
        else:
            return _fetch_fallback_links(fallback_query, country, oem_numbers)
    else:
        canonical = (part_info or {}).get("canonical_name", "") or part
        base_part = canonical.split(",")[0].strip()
        for word in [make, model, year, engine]:
            if word and word.lower() in base_part.lower():
                base_part = re.sub(re.escape(word), "", base_part, flags=re.IGNORECASE).strip()
        query = " ".join(filter(None, [year, make, model, engine, base_part])).strip()
        print(f"  {Fore.YELLOW}⚠  No OEM number — searching by: {query[:60]}{Style.RESET_ALL}")
        if SERPER_API_KEY:
            results = _fetch_via_serper(query, country)
            # Also search with Finnish term for Nordic countries
            if use_bilingual:
                fi_part = _translate_part_to_fi(base_part)
                if fi_part:
                    fi_query = " ".join(filter(None, [year, make, model, engine, fi_part])).strip()
                    print(f"  {Fore.CYAN}🇫🇮 Also searching in Finnish: {fi_query[:60]}{Style.RESET_ALL}")
                    fi_results = _fetch_via_serper(fi_query, country)
                    results = _merge_price_results(results, fi_results)
            return results
        else:
            return _fetch_fallback_links(query, country)


# Rough shipping estimates by known shop domain
SHOP_SHIPPING_ESTIMATES = {
    "motonet":        "1-3 days (FI)",
    "biltema":        "2-4 days",
    "autodoc":        "3-7 days",
    "ak24":           "3-5 days",
    "trodo":          "4-7 days",
    "amazon":         "1-3 days (Prime)",
    "ebay":           "4-10 days",
    "eurocarparts":   "2-4 days",
    "oscaro":         "4-7 days",
    "mekonomen":      "2-4 days",
    "kfzteile24":     "3-5 days",
    "skruvat":        "3-5 days",
    "hankkija":       "2-4 days (FI)",
    "topautoosat":    "2-4 days (FI)",
    "autoexperten":   "3-5 days",
    "reservdelar":    "3-5 days",
    "bildelar":       "3-5 days",
}

def _parse_shipping(item: dict) -> str:
    """Extract shipping estimate from Serper result, fallback to known shop estimates."""
    # Check all fields Serper might use
    candidates = [
        item.get("delivery"),
        item.get("shipping"),
        item.get("shippingCost"),
        item.get("deliveryInfo"),
        item.get("deliveryDate"),
        (item.get("offers") or {}).get("delivery") if isinstance(item.get("offers"), dict) else None,
        next((e for e in (item.get("extensions") or []) if "ship" in e.lower() or "deliv" in e.lower() or "free" in e.lower()), None),
    ]
    delivery = next((str(c).strip() for c in candidates if c), None)

    if delivery:
        delivery = re.sub(r"free (shipping|delivery)", "Free shipping", delivery, flags=re.I)
        delivery = re.sub(r"ships? in ", "", delivery, flags=re.I)
        return delivery[:26]

    # Fallback: look up known shop
    source = (item.get("source") or item.get("seller") or "").lower()
    for shop_key, estimate in SHOP_SHIPPING_ESTIMATES.items():
        if shop_key in source:
            return f"~{estimate}"

    return "—"


def _fetch_via_serper(query: str, country: str, oem_numbers: list = None) -> list[dict]:
    _, currency, hl = COUNTRY_GOOGLE.get(country.upper(), ("google.com", "EUR", "en"))
    gl_code = country.lower() if country.upper() in COUNTRY_GOOGLE else "fi"
    try:
        r = requests.post(
            "https://google.serper.dev/shopping",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "gl": gl_code, "hl": hl, "num": 30},
            timeout=15,
        )
        r.raise_for_status()
        items = r.json().get("shopping", [])

        preferred = PREFERRED_SHOPS.get(country.upper(), PREFERRED_SHOPS["DEFAULT"])

        def sort_key(item):
            source = item.get("source", "").lower()
            pref = next((i for i, s in enumerate(preferred) if s in source), 999)
            try:
                price = float(re.sub(r"[^\d.]", "", str(item.get("price", "9999")).replace(",", ".")))
            except Exception:
                price = 9999
            return (pref, price)

        results = []
        seen = {}  # shop_key -> count
        # Major Finnish/Nordic shops get more slots
        BOOSTED = {'motonet', 'ak24', 'autodoc', 'biltema', 'trodo'}
        for item in sorted(items, key=sort_key):
            source = item.get("source", item.get("seller", "Unknown"))
            key = re.sub(r"[^a-z].*", "", source.lower().replace("ebay", "ebay"))
            key = key.split(".")[0][:15]
            max_per_shop = 3 if key in BOOSTED else 2
            if seen.get(key, 0) >= max_per_shop:
                continue
            seen[key] = seen.get(key, 0) + 1

            price_raw = item.get("price", "")
            price_num = None
            if price_raw:
                try:
                    clean = re.sub(r"[^\d.,]", "", str(price_raw))
                    if re.search(r",\d{2}$", clean):
                        clean = clean.replace(".", "").replace(",", ".")
                    else:
                        clean = clean.replace(",", "")
                    price_num = float(clean)
                except Exception:
                    pass

            raw_url = item.get("link", item.get("url", "#"))
            item_url = _clean_shop_url(raw_url, source)
            # If Google redirect URL, build a direct shop search link
            if item_url is None:
                src_low = source.lower()
                part_title = item.get("title", query)[:80]
                q = urllib.parse.quote_plus(part_title)
                if "ebay" in src_low:
                    item_url = f"https://www.ebay.de/sch/i.html?_nkw={q}"
                elif "autodoc" in src_low:
                    item_url = _add_autodoc_aff(f"https://www.autodoc.fi/search?query={q}")
                elif "motonet" in src_low:
                    item_url = f"https://www.motonet.fi/fi/search?q={q}"
                elif "amazon" in src_low:
                    item_url = _add_amazon_tag(f"https://www.amazon.de/s?k={q}")
                elif "biltema" in src_low:
                    item_url = f"https://www.biltema.fi/fi/search?query={q}"
                elif "trodo" in src_low:
                    item_url = f"https://trodo.com/en/search?q={q}"
                else:
                    item_url = f"https://www.google.com/search?tbm=shop&q={q}"
            if "autodoc." in (item_url or "").lower():
                item_url = _add_autodoc_aff(item_url)
            results.append({
                "shop": source,
                "part": item.get("title", "")[:60],
                "price": price_num,
                "currency": currency,
                "url": item_url,
                "shipping": _parse_shipping(item),
                "note": "" if price_num else "See site",
            })
            if len(results) >= 16:
                break

        return results or _fetch_fallback_links(query, country)
    except Exception:
        return _fetch_fallback_links(query, country)


def _fetch_fallback_links(query: str, country: str, oem_numbers: list = None) -> list[dict]:
    q   = urllib.parse.quote_plus(query)
    gl  = country.lower()
    # Google Shopping works better with descriptive queries than bare OEM numbers
    # Use the full query (which includes make/model/part name) for the Shopping link
    gsh = f"https://www.google.com/search?tbm=shop&q={q}&gl={gl}"
    return [
        {"shop": "Google Shopping",  "price": None, "url": gsh,                                                      "note": "Kaikki kaupat — klikkaa vertaillaksesi"},
        {"shop": "Autodoc",          "price": None, "url": _add_autodoc_aff(f"https://www.autodoc.fi/search?query={q}"), "note": "Katso sivustolta", "shipping": "3-7 days"},
        {"shop": "Motonet",          "price": None, "url": f"https://www.motonet.fi/fi/search?q={q}",                "note": "Katso sivustolta", "shipping": "1-3 days (FI)"},
        {"shop": "AK24",             "price": None, "url": f"https://www.ak24.fi/fi/search?term={q}",                "note": "Katso sivustolta", "shipping": "3-5 days"},
        {"shop": "Trodo",            "price": None, "url": f"https://trodo.com/en/search?q={q}",                     "note": "Katso sivustolta", "shipping": "3-7 days"},
        {"shop": "Biltema",          "price": None, "url": f"https://www.biltema.fi/fi/search?query={q}",            "note": "Katso sivustolta", "shipping": "1-3 days"},
        {"shop": "Amazon.de",        "price": None, "url": _add_amazon_tag(f"https://www.amazon.de/s?k={q}"),        "note": "Katso sivustolta", "shipping": "1-3 days"},
        {"shop": "eBay.de",          "price": None, "url": f"https://www.ebay.de/sch/i.html?_nkw={q}",              "note": "Katso sivustolta"},
        {"shop": "EuroCarParts",     "price": None, "url": f"https://www.eurocarparts.com/ecp/c/?q={q}",             "note": "Katso sivustolta", "shipping": "1-3 days"},
        {"shop": "Oscaro",           "price": None, "url": f"https://www.oscaro.com/search#/?q={q}",                 "note": "Katso sivustolta"},
    ]


def get_tuning_info(car: dict, lang: str = "fi") -> dict:
    """
    Get car-specific tuning information using AI + web search context.
    Returns structured tuning mods with HP/torque gains, costs and ratings.
    """
    make   = car.get("make", "")
    model  = car.get("model", "")
    year   = car.get("year", "")
    engine = car.get("engine", "")
    car_str = f"{year} {make} {model} {engine}".strip()

    # Check cache (24h TTL)
    import time as _tc
    _cache_key = f"{car_str.lower()}::{lang}"
    _hit = _tuning_cache.get(_cache_key)
    if _hit and (_tc.time() - _hit[0]) < TUNING_CACHE_TTL:
        print(f'  [tuning cache HIT] {car_str}')
        return _hit[1]


    # Step 1: Search for REAL stock specs first
    spec_context = ""
    if SERPER_API_KEY:
        try:
            spec_queries = [
                f"{year} {make} {model} {engine} horsepower torque specs",
                f"{make} {model} {engine} stock power output specifications",
            ]
            spec_snippets = []
            for q in spec_queries:
                r = requests.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                    json={"q": q, "num": 5},
                    timeout=10,
                )
                if r.status_code == 200:
                    data = r.json()
                    # Also check answer box and knowledge graph for specs
                    if data.get("answerBox"):
                        spec_snippets.append(str(data["answerBox"])[:300])
                    for item in data.get("organic", [])[:3]:
                        spec_snippets.append(item.get("snippet", ""))
            if spec_snippets:
                spec_context = "Real stock specs from web:\n" + "\n".join(spec_snippets[:5])
        except Exception:
            pass

    # Step 2: Search for real tuning data
    tune_context = ""
    if SERPER_API_KEY:
        try:
            tune_queries = [
                f"{make} {model} {engine} ECU remap stage 1 hp gain realistic",
                f"{make} {model} tuning modifications realistic power gains",
            ]
            tune_snippets = []
            for q in tune_queries:
                r = requests.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                    json={"q": q, "num": 4},
                    timeout=10,
                )
                if r.status_code == 200:
                    for item in r.json().get("organic", [])[:3]:
                        tune_snippets.append(item.get("snippet", ""))
            if tune_snippets:
                tune_context = "Real tuning data from web:\n" + "\n".join(tune_snippets[:4])
        except Exception:
            pass

    search_context = spec_context + ("\n\n" if spec_context and tune_context else "") + tune_context

    lang_instruction = (
        "Write ALL text fields in FINNISH only. Zero English anywhere in text fields."
        if lang == "fi" else
        "Write ALL text fields in ENGLISH only. Zero Finnish anywhere in text fields."
    )
    system = (
        "You are an automotive tuning expert. Respond with valid JSON only. No markdown. "
        "Be conservative and realistic — do not exaggerate power gains. "
        + lang_instruction
    )

    prompt = (
        f"Car: {car_str}\n"
        f"{search_context}\n\n"
        "TASK: Return tuning and cosmetics data for this exact car.\n\n"
        "CRITICAL: Use the REAL stock specs from the web data above. Do not guess or invent specs.\n"
        "CRITICAL: Be REALISTIC and CONSERVATIVE with power gains:\n"
        "  - A Stage 1 remap on a naturally aspirated engine gives 5-15 hp MAX, often less.\n"
        "  - A Stage 1 remap on a turbo engine gives 15-40 hp depending on the engine.\n"
        "  - Do not claim 50+ hp from a simple remap unless the web data confirms it.\n"
        "  - hp_gain is the gain from THAT MOD ALONE, not cumulative total.\n"
        "  - total_hp in each level = stock_hp + sum of all hp_gains in that level + previous levels.\n"
        "CRITICAL: All text fields must be in language: " + lang + " ONLY.\n\n"
        "JSON structure:\n"
        "{ \"stock_hp\": int, \"stock_torque\": int, \"summary\": \"string\",\n"
        "  \"cosmetics\": [ { \"name\": \"str\", \"category\": \"str\", \"description\": \"str\","
        " \"price_eur\": int, \"price_range\": \"str\", \"worth_score\": int, \"effect\": \"str\", \"popular_brands\": [] } ],\n"
        "  \"levels\": [ { \"level\": 1, \"name\": \"str\", \"description\": \"str\","
        " \"total_hp\": int, \"total_torque\": int, \"total_cost_eur\": int,\n"
        "    \"mods\": [ { \"name\": \"str\", \"category\": \"str\", \"description\": \"str\","
        " \"hp_gain\": int, \"torque_gain\": int,\n"
        "      \"price_eur\": int, \"price_range\": \"str\", \"difficulty\": \"str\","
        " \"reversible\": bool, \"worth_score\": int, \"notes\": \"str\",\n"
        "      \"requires\": [], \"effects\": { \"power_hp\": int, \"torque_nm\": int,"
        " \"handling\": int, \"fuel_l100km\": float, \"reliability\": int, \"daily_usability\": int } } ] } ] }\n\n"
        "REQUIRED: Return EXACTLY 3 levels in the levels array. Every level must have mods.\n"
        "Stage 1 (bolt-on): remap, sport air filter, exhaust. 2-3 mods.\n"
        "Stage 2 (hardware): downpipe, intercooler, sport suspension, brakes. 2-3 mods.\n"
        "Stage 3 (major): turbo upgrade, fueling, internals, or advanced chassis. 2-3 mods even if modest.\n"
        "worth_score 1-5 realistic. Prices EUR Finnish market.\n"
    )
    print(f"  [tuning] calling AI, GROQ_KEY={'set' if GROQ_API_KEY else 'MISSING'}, GEMINI_KEY={'set' if GEMINI_API_KEY else 'MISSING'}")
    raw = claude(prompt, system=system, max_tokens=2500)
    print(f"  [tuning] raw response start: {repr(raw[:150])}")
    # Check for API errors before parsing
    if raw.startswith("[AI error:") or raw.startswith("[GROQ") or raw.startswith("[GEMINI"):
        is_rate_limit = "429" in raw or "rate limit" in raw.lower() or "Too Many" in raw
        msg = raw[:300]
        return {"error": msg, "levels": [], "cosmetics": [], "summary": ""}
    try:
        import re as _re, json as _json
        s = _re.sub(r'```json|```', '', raw).strip()
        s = _re.sub(r'[\x00-\x1f\x7f]', ' ', s)
        # Fix single-quoted strings
        def _fq(txt):
            out, i = [], 0
            while i < len(txt):
                if txt[i] == "'" and (i == 0 or txt[i-1] in ',:[ \t\n{('):
                    out.append('"'); i += 1
                    while i < len(txt) and txt[i] != "'":
                        if txt[i] == '"': out.append('\\"')
                        else: out.append(txt[i])
                        i += 1
                    out.append('"'); i += 1
                else:
                    out.append(txt[i]); i += 1
            return ''.join(out)
        s = _fq(s)
        # Fix unquoted property names
        s = _re.sub(r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1"\2":', s)
        # Fix trailing commas
        s = _re.sub(r',\s*([}\]])', r'\1', s)
        idx = s.find('{')
        if idx == -1: raise ValueError('No JSON')
        result = _json.loads(s[idx:])
        result.setdefault('stock_hp', 0)
        result.setdefault('stock_torque', 0)
        result.setdefault('summary', '')
        result.setdefault('levels', [])
        result.setdefault('cosmetics', [])
        # Store in cache
        import time as _t
        _tuning_cache[_cache_key] = (_t.time(), result)
        if len(_tuning_cache) > 200:
            oldest_key = min(_tuning_cache, key=lambda k: _tuning_cache[k][0])
            del _tuning_cache[oldest_key]
        return result
    except Exception as e:
        return {'error': str(e), 'levels': [], 'cosmetics': [], 'summary': 'Parse error - try again'}
def recommend_prices(prices: list[dict]) -> dict:
    """Pick cheapest, best quality, fastest shipping, and happy medium."""
    priced = [p for p in prices if p.get("price") is not None]

    cheapest = min(priced, key=lambda x: x["price"]) if priced else None

    # Quality: prefer known reputable auto parts specialists over general marketplaces
    quality_order = ["Autodoc", "Motonet", "Trodo", "AK24", "Biltema",
                     "EuroCarParts", "Oscaro", "kfzteile", "mekonomen", "halfords"]
    quality = next(
        (p for q in quality_order for p in priced if q.lower() in p.get("shop", "").lower()),
        cheapest
    )

    # Fastest: prefer shops with fast shipping notes
    fast_keywords = ["prime", "1-3", "next day", "same day", "motonet", "biltema", "amazon"]
    fastest = next(
        (p for p in priced if any(k in (p.get("shipping") or "").lower() for k in fast_keywords)),
        priced[0] if priced else None
    )

    # Happy medium: middle of sorted prices
    medium = None
    if len(priced) > 2:
        sorted_p = sorted(priced, key=lambda x: x["price"])
        medium = sorted_p[len(sorted_p) // 2]
    elif len(priced) == 2:
        medium = priced[1]  # second cheapest
    elif priced:
        medium = priced[0]

    return {"cheapest": cheapest, "quality": quality, "fastest": fastest, "medium": medium}

# ─── Display part results ─────────────────────────────────────────────────────
def display_car(car: dict, plate_or_vin: str, country: str):
    section("Vehicle Information")
    confidence_color = {"high": Fore.GREEN, "medium": Fore.YELLOW, "low": Fore.RED}.get(
        car.get("confidence", "low"), Fore.WHITE)
    info("ID:", plate_or_vin.upper())
    info("Country:", COUNTRIES.get(country.upper(), country))
    info("Make:", car.get("make", "—"))
    info("Model:", car.get("model", "—"))
    info("Year:", car.get("year", "—"))
    info("Engine:", car.get("engine", "—"))
    info("Fuel:", car.get("fuel", "—"))
    info("Transmission:", car.get("transmission", "—"))
    info("Body:", car.get("body", "—"))
    if car.get("color"):
        info("Color:", car["color"])
    if car.get("municipality"):
        info("Municipality:", car["municipality"])
    print(f"  {'Confidence:':<22}{confidence_color}{car.get('confidence','—').upper()}{Style.RESET_ALL}")
    if car.get("note"):
        print(f"  {Fore.WHITE}{Style.DIM}ℹ  {car['note']}{Style.RESET_ALL}")

def display_part_info(part_info: dict, car: dict, country: str):
    section(f"Part: {part_info.get('canonical_name', '—')}")
    info("Description:", part_info.get("description", "—"))
    if part_info.get("part_numbers"):
        nums = part_info["part_numbers"]
        print(f"  {Fore.WHITE}{Style.BRIGHT}{'OEM Part #:':<22}{Style.RESET_ALL}", end="")
        for i, n in enumerate(nums):
            color = Fore.GREEN if i == 0 else Fore.WHITE
            print(f"{color}{n}{Style.RESET_ALL}", end="  ")
        print()
        if part_info.get("oem_note"):
            print(f"  {Fore.WHITE}{Style.DIM}  ℹ {part_info['oem_note']}{Style.RESET_ALL}")
    else:
        print(f"  {Fore.YELLOW}⚠  No OEM part number found — prices may be less accurate{Style.RESET_ALL}")
    info("Difficulty:", part_info.get("difficulty", "—"))
    info("Labor time:", part_info.get("avg_labor_hours", "—"))
    if part_info.get("tools_needed"):
        info("Tools needed:", ", ".join(part_info["tools_needed"]))

def display_replacement_guide(part_info: dict):
    section("Replacement Guide")
    steps = part_info.get("replacement_summary", "")
    if not steps:
        print(f"  {Fore.YELLOW}No replacement guide available.{Style.RESET_ALL}")
        return
    if isinstance(steps, list):
        steps = "\n".join(steps)
    for line in steps.strip().split("\n"):
        line = line.strip()
        if not line:
            print()
            continue
        # Main numbered steps get cyan numbers
        m = re.match(r"^(\d+)\.(.*)", line)
        if m:
            num = m.group(1)
            text = m.group(2).strip()
            print(f"  {Fore.CYAN}{Style.BRIGHT}{num}.{Style.RESET_ALL} {Fore.WHITE}{text}{Style.RESET_ALL}")
        # Sub-steps (a., b., -, *)
        elif re.match(r"^[a-z]\.|^[-*•]", line):
            print(f"     {Fore.WHITE}{Style.DIM}{line}{Style.RESET_ALL}")
        # Section headers (ALL CAPS or ending in :)
        elif line.isupper() or line.endswith(":"):
            print(f"  {Fore.YELLOW}{Style.BRIGHT}{line}{Style.RESET_ALL}")
        else:
            print(f"  {Fore.WHITE}{line}{Style.RESET_ALL}")

def _detect_image_protocol() -> str:
    """Detect which inline image protocol the terminal supports."""
    term = os.environ.get("TERM", "")
    term_program = os.environ.get("TERM_PROGRAM", "")
    wt_session = os.environ.get("WT_SESSION", "")  # Windows Terminal

    if "kitty" in term:
        return "kitty"
    if "iterm" in term_program.lower():
        return "iterm2"
    if wt_session:
        return "iterm2"  # Windows Terminal 1.22+ supports iTerm2 protocol
    if os.environ.get("TERM_PROGRAM") == "WezTerm":
        return "iterm2"
    return "browser"  # fallback


def _render_image_iterm2(image_data: bytes, width: int = 80) -> None:
    """Render image inline using iTerm2 protocol (Windows Terminal, WezTerm, iTerm2)."""
    import base64
    b64 = base64.b64encode(image_data).decode()
    # iTerm2 inline image escape sequence
    payload = f"]1337;File=inline=1;width={width};preserveAspectRatio=1:{b64}"
    sys.stdout.write(payload)
    sys.stdout.flush()
    print()  # newline after image


def _render_image_chafa(image_data: bytes) -> bool:
    """Render image using chafa (terminal image renderer). Returns True if successful."""
    import subprocess, tempfile
    try:
        # Write image to temp file
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(image_data)
            tmp_path = f.name
        result = subprocess.run(
            ["chafa", "--size=80x24", tmp_path],
            capture_output=True, text=True, timeout=10
        )
        os.unlink(tmp_path)
        if result.returncode == 0:
            print(result.stdout)
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        pass
    return False


def _fetch_and_render_image(url: str, protocol: str, index: int) -> bool:
    """Fetch an image URL and render it inline. Returns True if successful."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124"}
        r = requests.get(url, headers=headers, timeout=10, stream=True)
        if r.status_code != 200:
            return False
        # Only accept image content types
        ct = r.headers.get("content-type", "")
        if "image" not in ct:
            return False
        data = r.content
        if len(data) < 1000:  # too small, probably an error page
            return False

        print(f"  {Fore.WHITE}{Style.DIM}Image {index}:{Style.RESET_ALL}")
        if protocol in ("iterm2", "kitty"):
            _render_image_iterm2(data)
            return True
        elif _render_image_chafa(data):
            return True
        return False
    except Exception:
        return False


def display_exploded_views(image_urls: list[str], part: str, car: dict):
    section("Exploded Views & Diagrams")
    car_str = f"{car.get('make','')} {car.get('model','')} {car.get('year','')}".strip()
    protocol = _detect_image_protocol()
    query = urllib.parse.quote_plus(f"{car_str} {part} exploded view diagram")
    fallback_url = f"https://www.google.com/search?tbm=isch&q={query}"

    rendered = 0
    if image_urls and protocol != "browser":
        print(f"  {Fore.WHITE}{Style.DIM}Rendering diagrams inline...{Style.RESET_ALL}")
        for i, url in enumerate(image_urls[:3], 1):
            if _fetch_and_render_image(url, protocol, i):
                rendered += 1

    if rendered == 0:
        if image_urls:
            # Open images in browser
            print(f"  {Fore.GREEN}Opening {len(image_urls)} diagram(s) in browser...{Style.RESET_ALL}")
            for url in image_urls[:2]:
                webbrowser.open(url)
                time.sleep(0.3)
        else:
            print(f"  {Fore.YELLOW}No direct images found — opening Google Images search.{Style.RESET_ALL}")
            webbrowser.open(fallback_url)
        print(f"  {Fore.BLUE}{hyperlink(fallback_url, 'Search Google Images for more diagrams')}{Style.RESET_ALL}")

def display_youtube(videos: list[dict], query: str):
    section("YouTube Tutorials")
    if not videos:
        print(f"  {Fore.YELLOW}No results found. Try searching manually:{Style.RESET_ALL}")
        print(f"  https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query)}")
        return
    for v in videos:
        print(f"  {Fore.CYAN}▶ {Style.RESET_ALL}{hyperlink(v['url'], v['title'])}")
        print()

def display_prices(prices: list[dict], recs: dict):
    has_prices = any(p.get("price") is not None for p in prices)
    section("Price Comparison" + (" — Live from Google Shopping" if has_prices else " — Shop Links"))

    if not has_prices:
        print(f"  {Fore.YELLOW}ℹ  No live prices — set SERPER_API_KEY for real-time prices (free: serper.dev){Style.RESET_ALL}")
        print()
        # Open Google Shopping automatically for the user
        gsh = next((p["url"] for p in prices if "google" in p.get("shop","").lower()), None)
        if gsh:
            print(f"  {Fore.GREEN}Opening Google Shopping in browser...{Style.RESET_ALL}")
            webbrowser.open(gsh)

    col_w = [28, 10, 6, 26]
    header = (
        f"  {Fore.WHITE}{Style.BRIGHT}"
        f"{'Shop':<{col_w[0]}}{'Price':>{col_w[1]}}  {'Cur':<{col_w[2]}}  {'Shipping':<{col_w[3]}}"
        f"{Style.RESET_ALL}"
    )
    print(header)
    print("  " + "─" * (sum(col_w) + 6))

    for p in prices:
        shop     = p.get("shop", "?")
        price    = p.get("price")
        currency = p.get("currency", "")
        shipping = p.get("shipping") or p.get("note") or "—"
        shipping = shipping[:26]
        price_str = f"{price:.2f}" if price is not None else "see site"

        tag = ""
        color = Fore.WHITE
        if recs.get("cheapest") and shop == recs["cheapest"].get("shop"):
            tag = " ★"
            color = Fore.GREEN
        elif recs.get("quality") and shop == recs["quality"].get("shop") and shop != recs.get("cheapest", {}).get("shop"):
            tag = " ✦"
            color = Fore.CYAN
        elif recs.get("fastest") and shop == recs["fastest"].get("shop") and not tag:
            tag = " ⚡"
            color = Fore.YELLOW

        shop_disp = (shop + tag)[:col_w[0]]
        shop_linked = hyperlink(p.get("url",""), shop_disp) if p.get("url") else shop_disp
        row = (
            f"  {color}"
            f"{shop_linked:<{col_w[0]}}{price_str:>{col_w[1]}}  {currency:<{col_w[2]}}  {shipping:<{col_w[3]}}"
            f"{Style.RESET_ALL}"
        )
        print(row)
        # Show part name from Google Shopping if available
        part_name = p.get("part", "")
        if part_name and price is not None:
            print(f"    {Fore.WHITE}{Style.DIM}{part_name}{Style.RESET_ALL}")

    print()
    if has_prices:
        print(f"  {Style.BRIGHT}Our picks:{Style.RESET_ALL}")
        picks = [
            ("💰 Cheapest",        recs.get("cheapest")),
            ("🏆 Best quality",    recs.get("quality")),
            ("🚀 Fastest shipping",recs.get("fastest")),
            ("⚖️  Happy medium",   recs.get("medium")),
        ]
        for label, pick in picks:
            if pick:
                price_str = f" — {pick['price']:.2f} {pick.get('currency','')}" if pick.get("price") else ""
                shop_link = hyperlink(pick.get("url",""), pick["shop"]) if pick.get("url") else pick["shop"]
                print(f"  {label}: {Fore.CYAN}{shop_link}{Style.RESET_ALL}{price_str}")
    else:
        print(f"  {Style.BRIGHT}Search links (click to open):{Style.RESET_ALL}")
        for p in prices[:5]:
            linked = hyperlink(p["url"], p["shop"]) if p.get("url") else p["shop"]
            print(f"  {Fore.CYAN}{linked}{Style.RESET_ALL}")

def open_links_prompt(links: list[str]):
    ans = input(f"\n{Fore.GREEN}▶{Style.RESET_ALL} Open diagram links in browser? [y/N] ").strip().lower()
    if ans == "y":
        for url in links[:2]:
            webbrowser.open(url)

# ─── Main flow ────────────────────────────────────────────────────────────────
def main():
    banner()

    if not ANTHROPIC_API_KEY and not GROQ_API_KEY:
        print(f"  {Fore.RED}⚠  No AI key found! Part lookup features will not work.{Style.RESET_ALL}")
        print(f"  {Fore.WHITE}  Groq (free):     export GROQ_API_KEY=your_key{Style.RESET_ALL}")
        print(f"  {Fore.WHITE}  Anthropic:       export ANTHROPIC_API_KEY=your_key{Style.RESET_ALL}")
        print(f"  {Fore.WHITE}  Switch provider: export AUTOHELPER_PROVIDER=groq  (or anthropic){Style.RESET_ALL}")
        print()
    if not CARSXE_API_KEY:
        print(f"  {Fore.YELLOW}ℹ  No CARSXE_API_KEY — Finnish/Swedish/German plate lookup uses AI fallback.{Style.RESET_ALL}")
        print(f"  {Fore.WHITE}  Get a free key (100 lookups/month) at: https://carsxe.com{Style.RESET_ALL}")
        print(f"  {Fore.WHITE}  Then: export CARSXE_API_KEY=your_key{Style.RESET_ALL}")
        print()

    # Step 1: VIN
    print(f"  {Fore.WHITE}{Style.DIM}Enter your 17-character VIN code (found on dashboard, door frame, or registration document).{Style.RESET_ALL}")
    print(f"  {Fore.WHITE}{Style.DIM}Finnish/Swedish/European VINs start with YS3 (Saab), W0L (Opel), W0L (Opel), etc.{Style.RESET_ALL}")
    plate_or_vin = prompt_input("VIN:").upper()
    if not plate_or_vin:
        print(f"{Fore.RED}No input provided. Exiting.{Style.RESET_ALL}")
        sys.exit(1)

    # Step 2: Country (for price search region — optional, defaults to FI)
    print(f"  {Fore.WHITE}{Style.DIM}Country sets your price search region and currency. Press Enter to use FI (Finland).{Style.RESET_ALL}")
    print(f"  {Fore.WHITE}{Style.DIM}Options: FI SE NO DE GB US FR EE{Style.RESET_ALL}")
    country_input = prompt_input("Country code [FI]:").upper().strip()
    country = country_input if country_input in COUNTRIES else "FI"
    if country_input and country_input not in COUNTRIES:
        print(f"  {Fore.YELLOW}Unknown country code — defaulting to FI{Style.RESET_ALL}")
    print(f"  {Fore.WHITE}{Style.DIM}Region: {COUNTRIES.get(country, country)}{Style.RESET_ALL}")

    # Step 3: Look up vehicle
    car = spinner("Looking up vehicle", lookup_vehicle, plate_or_vin, country)
    # Store VIN/plate in car dict so part lookup can use it
    is_vin_input = len(plate_or_vin) == 17 and plate_or_vin.isalnum()
    if is_vin_input:
        car["vin"] = plate_or_vin.upper()
    display_car(car, plate_or_vin, country)

    # Confirm car or allow manual override
    print()
    override = prompt_input("Is this correct? Press Enter to continue, or type a correction (e.g. '2018 Toyota Yaris 1.5'):")
    if override:
        parts = override.split()
        car["year"] = parts[0] if parts else car["year"]
        car["make"] = parts[1] if len(parts) > 1 else car["make"]
        car["model"] = parts[2] if len(parts) > 2 else car["model"]
        car["engine"] = " ".join(parts[3:]) if len(parts) > 3 else car["engine"]
        print(f"  {Fore.GREEN}✓ Updated to: {car['year']} {car['make']} {car['model']} {car['engine']}{Style.RESET_ALL}")



    # Parts that need clarification on which side/axle
    AMBIGUOUS_PARTS = {
        "brake pad": "Which axle? (front/rear)",
        "brake pads": "Which axle? (front/rear)",
        "brake disc": "Which axle? (front/rear)",
        "brake discs": "Which axle? (front/rear)",
        "brake rotor": "Which axle? (front/rear)",
        "shock absorber": "Which corner? (front-left/front-right/rear-left/rear-right)",
        "shock": "Which corner? (front-left/front-right/rear-left/rear-right)",
        "strut": "Which corner? (front-left/front-right/rear-left/rear-right)",
        "control arm": "Which one? (front-left/front-right/rear-left/rear-right, upper/lower)",
        "ball joint": "Which one? (front-left/front-right/rear-left/rear-right, upper/lower)",
        "wheel bearing": "Which corner? (front-left/front-right/rear-left/rear-right)",
        "hub bearing": "Which corner? (front-left/front-right/rear-left/rear-right)",
        "cv axle": "Which side? (left/right)",
        "cv joint": "Which side? (left/right)",
        "tie rod": "Which side? (left/right, inner/outer)",
        "caliper": "Which corner? (front-left/front-right/rear-left/rear-right)",
        "spring": "Which corner? (front-left/front-right/rear-left/rear-right)",
        "headlight": "Which side? (left/right)",
        "taillight": "Which side? (left/right)",
        "mirror": "Which side? (left/right)",
        "door": "Which door? (front-left/front-right/rear-left/rear-right)",
        "window regulator": "Which door? (front-left/front-right/rear-left/rear-right)",
        "wiper blade": "Which? (driver/passenger/rear)",
        "oxygen sensor": "Which? (upstream/downstream, bank 1/bank 2)",
        "o2 sensor": "Which? (upstream/downstream, bank 1/bank 2)",
    }

    # Main loop: search for parts
    while True:
        print()
        divider()
        part_query = prompt_input("Part to search (or 'quit' to exit):")
        if part_query.lower() in ("quit", "exit", "q", ""):
            print(f"\n  {Fore.CYAN}Thanks for using AutoHelper. Drive safe! 🚗{Style.RESET_ALL}\n")
            break

        # Check if the part needs clarification
        part_lower = part_query.lower().strip()
        for ambiguous, question in AMBIGUOUS_PARTS.items():
            if ambiguous in part_lower and not any(
                word in part_lower for word in ["front", "rear", "left", "right", "upper", "lower", "inner", "outer", "driver", "passenger", "upstream", "downstream"]
            ):
                clarification = prompt_input(f"  {Fore.YELLOW}ℹ  {question}{Style.RESET_ALL}").strip()
                if clarification:
                    part_query = f"{clarification} {part_query}"
                break

        car_str = f"{car.get('year','')} {car.get('make','')} {car.get('model','')}".strip()
        yt_query = f"{car_str} {part_query} replacement how to"

        # Parallel fetch
        part_info_ref = [None]
        youtube_ref = [None]
        prices_ref = [None]

        images_ref = [None]

        def fetch_all():
            # Step A: OEM lookup + part info (sequential — OEM feeds into AI prompt)
            part_info_ref[0] = get_part_info(part_query, car)
            # Step B: YouTube + prices + images in parallel
            yt_ref2    = [None]
            price_ref2 = [None]
            img_ref2   = [None]
            import concurrent.futures as cf2
            def _yt():   yt_ref2[0]    = search_youtube(yt_query)
            def _pr():   price_ref2[0] = fetch_prices(part_info_ref[0].get("search_keywords", part_query), car, country, part_info=part_info_ref[0])
            def _img():  img_ref2[0]   = get_exploded_view_images(part_query, car)
            with cf2.ThreadPoolExecutor(max_workers=3) as ex2:
                cf2.wait([ex2.submit(_yt), ex2.submit(_pr), ex2.submit(_img)], timeout=20)
            youtube_ref[0] = yt_ref2[0] or []
            prices_ref[0]  = price_ref2[0] or []
            images_ref[0]  = img_ref2[0] or []

        spinner(f"Searching OEM numbers & researching {part_query}", fetch_all)

        part_info = part_info_ref[0]
        videos = youtube_ref[0]
        prices = prices_ref[0]
        recs = recommend_prices(prices)
        image_urls = images_ref[0] or []

        # Display all sections
        display_part_info(part_info, car, country)
        display_replacement_guide(part_info)
        display_exploded_views(image_urls, part_query, car)
        display_youtube(videos, yt_query)
        display_prices(prices, recs)

        print()
        another = prompt_input("Search another part for this car? [Y/n]:")
        if another.lower() == "n":
            print(f"\n  {Fore.CYAN}Thanks for using AutoHelper. Drive safe! 🚗{Style.RESET_ALL}\n")
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n  {Fore.CYAN}Goodbye! 🚗{Style.RESET_ALL}\n")
