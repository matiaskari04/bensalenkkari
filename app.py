"""
Bensalenkkari — web backend
Wraps autohelper.py logic as a Flask JSON API.
"""

import os, sys, json, threading, time, re
from flask import Flask, request, jsonify, render_template, send_from_directory

# ── Import all logic from autohelper ──────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "autohelper",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "autohelper.py")
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

lookup_vehicle        = _mod.lookup_vehicle
get_part_info         = _mod.get_part_info
fetch_prices          = _mod.fetch_prices
search_youtube        = _mod.search_youtube
get_exploded_view_images = _mod.get_exploded_view_images
AMBIGUOUS_PARTS       = None   # resolved at request time

app = Flask(__name__)

# ── My Cars — simple JSON file persistence ────────────────────────────────────
CARS_FILE = os.path.join(os.path.dirname(__file__), "my_cars.json")

def load_cars():
    try:
        with open(CARS_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def save_cars(cars):
    with open(CARS_FILE, "w") as f:
        json.dump(cars, f, indent=2)

def upsert_car(car: dict, vin: str):
    """Add or update car in saved list."""
    cars = load_cars()
    # Remove existing entry for this VIN
    cars = [c for c in cars if c.get("vin") != vin]
    cars.insert(0, {**car, "vin": vin, "saved_at": time.time()})
    cars = cars[:10]  # keep last 10
    save_cars(cars)

def add_recent_part(vin: str, part: str):
    """Track recent part searches per car."""
    cars = load_cars()
    for c in cars:
        if c.get("vin") == vin:
            parts = c.get("recent_parts", [])
            if part not in parts:
                parts.insert(0, part)
            c["recent_parts"] = parts[:5]
            break
    save_cars(cars)

# ── Part image search (clean product photos) ──────────────────────────────────
def search_part_image(part_name: str, make: str, model: str) -> str | None:
    """Search for a clean product image of the part using Serper Images."""
    serper_key = os.environ.get("SERPER_API_KEY", "")
    if not serper_key:
        return None
    try:
        import requests as req
        r = req.post(
            "https://google.serper.dev/images",
            headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
            json={"q": f"{part_name} auto part product photo white background", "num": 5},
            timeout=8,
        )
        if r.status_code == 200:
            images = r.json().get("images", [])
            for img in images:
                url = img.get("imageUrl", "")
                # Prefer clean product shots — avoid forum/blog images
                if url and any(url.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                    return url
    except Exception:
        pass
    return None

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/my-cars")
def api_my_cars():
    return jsonify(load_cars())

@app.route("/api/lookup-vin", methods=["POST"])
def api_lookup_vin():
    data = request.json or {}
    vin = (data.get("vin") or "").strip().upper()
    country = (data.get("country") or "FI").strip().upper()
    if not vin:
        return jsonify({"error": "VIN required"}), 400
    try:
        car = lookup_vehicle(vin, country)
        car["vin"] = vin
        car["country"] = country
        upsert_car(car, vin)
        return jsonify(car)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/part-info", methods=["POST"])
def api_part_info():
    data = request.json or {}
    part  = (data.get("part") or "").strip()
    car   = data.get("car") or {}
    country = (car.get("country") or data.get("country") or "FI").upper()
    if not part or not car:
        return jsonify({"error": "part and car required"}), 400
    try:
        info = get_part_info(part, car)
        add_recent_part(car.get("vin",""), part)
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/prices", methods=["POST"])
def api_prices():
    data    = request.json or {}
    part    = (data.get("part") or "").strip()
    car     = data.get("car") or {}
    country = (car.get("country") or data.get("country") or "FI").upper()
    part_info = data.get("part_info") or {}
    if not part or not car:
        return jsonify({"error": "part and car required"}), 400
    try:
        prices = fetch_prices(part, car, country, part_info=part_info)
        return jsonify(prices)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/youtube", methods=["POST"])
def api_youtube():
    data = request.json or {}
    car  = data.get("car") or {}
    part = (data.get("part") or "").strip()
    query = f"{car.get('year','')} {car.get('make','')} {car.get('model','')} {part} replacement how to"
    try:
        videos = search_youtube(query.strip())
        return jsonify(videos)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/diagrams", methods=["POST"])
def api_diagrams():
    data = request.json or {}
    car  = data.get("car") or {}
    part = (data.get("part") or "").strip()
    try:
        urls = get_exploded_view_images(part, car)
        return jsonify({"urls": urls})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/part-image", methods=["POST"])
def api_part_image():
    data = request.json or {}
    part = (data.get("part") or "").strip()
    car  = data.get("car") or {}
    try:
        url = search_part_image(part, car.get("make",""), car.get("model",""))
        return jsonify({"url": url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/delete-car", methods=["POST"])
def api_delete_car():
    data = request.json or {}
    vin = (data.get("vin") or "").strip().upper()
    cars = [c for c in load_cars() if c.get("vin") != vin]
    save_cars(cars)
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
