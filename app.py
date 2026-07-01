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
get_tuning_info          = _mod.get_tuning_info
get_tuning_level         = _mod.get_tuning_level
AMBIGUOUS_PARTS       = None   # resolved at request time

app = Flask(__name__)

@app.after_request
def add_headers(response):
    """Prevent browser and CDN caching of HTML to always serve fresh JS."""
    if response.content_type and 'text/html' in response.content_type:
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

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
    lang  = data.get("lang", "fi")
    country = (car.get("country") or data.get("country") or "FI").upper()
    if not part or not car:
        return jsonify({"error": "part and car required"}), 400
    try:
        info = get_part_info(part, car, lang=lang)
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

@app.route("/api/prices-test", methods=["GET"])
def api_prices_test():
    """Debug: call fetch_prices directly and show full result + traceback"""
    import traceback as _tb
    car = {"make": "Skoda", "model": "Octavia", "year": "2010", "engine": "1.9 TDI"}
    part_info = {
        "canonical_name": "Lower Ball Joint",
        "part_numbers": [{"number": "4835709", "brand": "Delphi", "type": "Aftermarket"}],
        "search_keywords": "4835709"
    }
    try:
        result = fetch_prices("lower ball joint", car, "FI", part_info=part_info)
        return jsonify({
            "ok": True,
            "count": len(result),
            "items": result,
            "serper_key_set": bool(os.environ.get("SERPER_API_KEY"))
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
            "type": type(e).__name__,
            "traceback": _tb.format_exc()
        })

@app.route("/api/serper-test", methods=["GET"])
def api_serper_test():
    """Quick raw Serper test — GET /api/serper-test?q=4835709+Skoda+Octavia"""
    import os, requests as req
    key = os.environ.get("SERPER_API_KEY","")
    q   = request.args.get("q","4835709 Skoda Octavia ball joint")
    try:
        r = req.post("https://google.serper.dev/shopping",
            headers={"X-API-KEY": key,"Content-Type":"application/json"},
            json={"q": q,"gl":"fi","hl":"fi","num":10}, timeout=12)
        return jsonify({"status": r.status_code, "key_set": bool(key),
                        "items": r.json().get("shopping",[])[:5]})
    except Exception as e:
        return jsonify({"error": str(e)})

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
        images = get_exploded_view_images(part, car)
        # Handle both old list[str] and new list[dict] formats
        if images and isinstance(images[0], str):
            images = [{"url": u, "title": ""} for u in images]
        return jsonify({"images": images})
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

@app.route("/api/save-car", methods=["POST"])
def api_save_car():
    """Save a manually entered car."""
    car = request.json or {}
    vin = car.get("vin", "").strip()
    if not vin:
        return jsonify({"error": "vin required"}), 400
    upsert_car(car, vin)
    return jsonify({"ok": True})

@app.route("/api/tuning", methods=["POST"])
def api_tuning():
    data = request.json or {}
    car  = data.get("car") or {}
    lang = data.get("lang", "fi")
    try:
        info = get_tuning_info(car, lang=lang)
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/tuning-level", methods=["POST"])
def api_tuning_level():
    data      = request.json or {}
    car       = data.get("car") or {}
    level     = int(data.get("level", 1))
    lang      = data.get("lang", "fi")
    stock_hp  = int(data.get("stock_hp", 0))
    stock_tq  = int(data.get("stock_torque", 0))
    try:
        result = get_tuning_level(car, level, lang=lang,
                                  stock_hp=stock_hp, stock_torque=stock_tq)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/delete-car", methods=["POST"])
def api_delete_car():
    data = request.json or {}
    vin = (data.get("vin") or "").strip().upper()
    cars = [c for c in load_cars() if c.get("vin") != vin]
    save_cars(cars)
    return jsonify({"ok": True})

@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json",
                               mimetype="application/manifest+json")

@app.route("/sw.js")
def service_worker():
    resp = send_from_directory("static", "sw.js",
                               mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp

@app.route("/icon-192.png")
def icon192():
    return send_from_directory("static", "icon-192.png", mimetype="image/png")

@app.route("/icon-512.png")
def icon512():
    return send_from_directory("static", "icon-512.png", mimetype="image/png")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
