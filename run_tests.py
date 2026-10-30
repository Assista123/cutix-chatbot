#!/usr/bin/env python3
"""Run the test questions against the bot and write a results file to review.

Examples:
  python run_tests.py --showpieces        (only the 6 showpiece questions)
  python run_tests.py --only 1,5,16       (chosen questions)
  python run_tests.py                     (all questions)
"""
import argparse
import datetime
import re
import sys
import time
from pathlib import Path

import server

ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(.*?)\s*\|\s*(.+?)\s*\|\s*$")
CURRENCY = re.compile(r"₦|\bNGN\b|naira|\$|\bN\s?\d{2,}")
GONE = re.compile(r"former|left|stepped|until|previous|no longer|replaced|not the", re.I)


def load_questions():
    rows = []
    for line in (server.DATA / "test_questions.md").read_text(encoding="utf-8").splitlines():
        m = ROW.match(line)
        if m:
            rows.append({"n": int(m.group(1)), "q": m.group(2), "type": m.group(3), "exp": m.group(4)})
    return rows


def flags_for(answer):
    f = []
    if len(answer.split()) > 110:
        f.append("long (>110 words)")
    if CURRENCY.search(answer):
        f.append("mentions a price or currency")
    if "Oduonye" in answer and not GONE.search(answer):
        f.append("names Oduonye without saying she left")
    for m in server.PHONE_RE.findall(answer):
        if server.norm_phone(m) not in server.ALLOWED_NUMBERS and len(server.norm_phone(m)) >= 10:
            f.append("phone number not in knowledge base: " + m.strip())
    for e in server.EMAIL_RE.findall(answer):
        if e.lower() not in server.ALLOWED_EMAILS:
            f.append("email not in knowledge base: " + e)
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--showpieces", action="store_true")
    a = ap.parse_args()
    qs = load_questions()
    if a.only:
        want = {int(x) for x in a.only.split(",") if x.strip()}
        qs = [q for q in qs if q["n"] in want]
    if a.showpieces:
        qs = [q for q in qs if q["type"].strip() == "S"]
    provider = server.env("PROVIDER", "mock")
    gap = 0 if provider == "mock" else 60.0 / max(server.env_int("RPM_LIMIT", 12) - 2, 1)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    out = Path(f"test_results_{provider}_{stamp}.md")
    out.write_text(f"# Test results ({provider}, {stamp})\n\nFill in Pass or Fail for each row.\n\n", encoding="utf-8")
    flagged = 0
    for i, q in enumerate(qs):
        print(f"[{i+1}/{len(qs)}] Q{q['n']}: {q['q'][:60]}")
        reply, err = server.answer(q["q"], [], paced=False)
        fl = flags_for(reply)
        if err:
            fl.append("MODEL ERROR: " + err[:200])
        flagged += bool(fl)
        with out.open("a", encoding="utf-8") as f:
            f.write(f"## Q{q['n']}: {q['q']}\n\n**Expected:** {q['exp']}\n\n**Answer:**\n\n{reply}\n\n")
            f.write(f"**Auto flags:** {'; '.join(fl) if fl else 'none'}\n\n**Result (Pass/Fail):** \n\n---\n\n")
        if gap and i < len(qs) - 1:
            time.sleep(gap)
    print(f"\nDone. {len(qs)} questions, {flagged} with auto flags. Review: {out}")


if __name__ == "__main__":
    sys.exit(main())
