# Bensalenkkari 🔧

Auto parts lookup web app. Runs on Railway (free tier).

## Local development

```bash
cd bensalenkkari
pip install -r requirements.txt
export GROQ_API_KEY=your_key
export SERPER_API_KEY=your_key
python app.py
# Open http://localhost:5000
```

## Deploy to Railway (free, ~10 minutes)

1. Sign up at https://railway.app (free, GitHub login)
2. Create new project → "Deploy from GitHub repo"
   - Push this folder to a GitHub repo first, OR use Railway CLI
3. Set environment variables in Railway dashboard:
   - GROQ_API_KEY
   - SERPER_API_KEY
   - ANTHROPIC_API_KEY (optional backup)
4. Railway auto-detects Python + Procfile and deploys
5. Your app gets a URL like https://bensalenkkari-production.up.railway.app

## Railway CLI (fastest way)

```bash
npm install -g @railway/cli
railway login
cd bensalenkkari
railway init
railway up
railway variables set GROQ_API_KEY=your_key SERPER_API_KEY=your_key
```

## File structure

```
bensalenkkari/
  app.py              — Flask backend (API routes)
  requirements.txt    — Python dependencies
  Procfile            — Gunicorn start command
  my_cars.json        — Saved cars (auto-created)
  templates/
    index.html        — Full frontend (single file)
../autohelper.py      — Core logic (VIN lookup, OEM search, prices)
```
