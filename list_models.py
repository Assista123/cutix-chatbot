#!/usr/bin/env python3
"""Shows which Gemini models your key can use. Run: python list_models.py"""
import json
import urllib.request

import server

key = server.env("GEMINI_API_KEY")
if not key:
    raise SystemExit("Put your key after GEMINI_API_KEY= in the .env file first.")
base = server.env("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
req = urllib.request.Request(f"{base}/models?pageSize=200", headers={"x-goog-api-key": key})
try:
    data = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
except Exception as e:
    raise SystemExit(f"Could not list models: {e}")
print("Models that can generate text (copy the part after 'models/' into GEMINI_MODEL in .env):\n")
for m in data.get("models", []):
    if "generateContent" in m.get("supportedGenerationMethods", []):
        print(" ", m["name"].replace("models/", ""))
