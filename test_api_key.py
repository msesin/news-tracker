#!/usr/bin/env python3
"""Quick smoke-test for a Gemini API key.

Usage:
    python test_api_key.py                  # uses LLM_API_KEY from .env
    LLM_API_KEY=AIza... python test_api_key.py  # override inline
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()

api_key = os.environ.get("LLM_API_KEY", "")
if not api_key:
    sys.exit("LLM_API_KEY not set in .env or environment.")

print(f"Testing key: {api_key[:8]}…{api_key[-4:]}")
print(f"Model: gemini-3.1-flash-lite\n")

try:
    from google import genai
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-3.1-flash-lite",
        contents="Reply with exactly the word WORKING and nothing else.",
    )
    text = response.text.strip()
    print(f"Response: {text!r}")
    if "WORKING" in text.upper():
        print("\n✓  API key works — Gemini is responding correctly.")
    else:
        print("\n?  Got a response but unexpected content. Key likely works; check output above.")
except Exception as e:
    print(f"\n✗  Error: {e}")
    if "429" in str(e):
        print("   → Quota issue. This key/project is exhausted or billing is disabled.")
    elif "403" in str(e) or "invalid" in str(e).lower():
        print("   → Key rejected. Double-check you copied it correctly.")
    elif "404" in str(e):
        print("   → Model not found. The key may belong to a project that can't access gemini-3.1-flash-lite.")
