import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://al-in.fr/"
OFFERS_URL = os.getenv("ALIN_OFFERS_URL", "").strip() or BASE
EMAIL = os.getenv("ALIN_EMAIL", "").strip()
PASSWORD = os.getenv("ALIN_PASSWORD", "")
TOPIC = os.getenv("NTFY_TOPIC", "").strip()
STATE = Path("state.json")
RESUME = os.getenv("FORCE_RESUME", "0") == "1"


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def digest(s):
    return hashlib.sha256(norm(s).encode("utf-8")).hexdigest()


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"seen": [], "initialized": False, "paused": False}


def save_state(state):
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def notify(title, body, priority="high"):
    if not TOPIC:
        print("Notification skipped:", title, body)
        return
    r = requests.post(
        f"https://ntfy.sh/{TOPIC}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": "house", "Click": BASE},
        timeout=15,
    )
    r.raise_for_status()


def first_visible(page, selectors):
    for selector in selectors:
        loc = page.locator(selector).first
        try:
            if loc.count() and loc.is_visible(timeout=1000):
                return loc
        except Exception:
            pass
    return None


def login(page):
    if not EMAIL or not PASSWORD:
        raise RuntimeError("Missing ALIN_EMAIL or ALIN_PASSWORD GitHub secret")

    page.goto(BASE, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1500)

    email = first_visible(page, [
        'input[type="email"]',
        'input[name*="email" i]',
        'input[id*="email" i]',
        'input[name*="user" i]',
    ])
    password = first_visible(page, ['input[type="password"]'])

    if email is None or password is None:
        button = first_visible(page, [
            'a:has-text("Se connecter")',
            'button:has-text("Se connecter")',
            'text=/connexion/i',
        ])
        if button:
            button.click()
            page.wait_for_timeout(1200)
        email = first_visible(page, [
            'input[type="email"]',
            'input[name*="email" i]',
            'input[id*="email" i]',
            'input[name*="user" i]',
        ])
        password = first_visible(page, ['input[type="password"]'])

    if email is None or password is None:
        raise RuntimeError("Could not find AL'in login fields")

    email.fill(EMAIL)
    password.fill(PASSWORD)
    submit = first_visible(page, [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("JE ME CONNECTE")',
        'button:has-text("Je me connecte")',
    ])
    if submit:
        submit.click()
    else:
        password.press("Enter")

    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2000)

    if first_visible(page, ['input[type="password"]']) is not None:
        raise RuntimeError("AL'in login did not succeed; check credentials or verification")


def extract_offers(page):
    page.goto(OFFERS_URL, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2500)

    if first_visible(page, ['input[type="password"]']) is not None:
        raise RuntimeError("AL'in returned to the login page")

    candidates = []
    selectors = [
        '[class*="offre" i]',
        '[class*="logement" i]',
        '[class*="annonce" i]',
        '[class*="card" i]',
        'article',
        'li',
    ]
    for selector in selectors:
        locs = page.locator(selector)
        try:
            count = min(locs.count(), 250)
        except Exception:
            continue
        for i in range(count):
            loc = locs.nth(i)
            try:
                if not loc.is_visible(timeout=200):
                    continue
                text = norm(loc.inner_text(timeout=700))
            except Exception:
                continue
            low = text.lower()
            if not 25 >= len(text) >= 1600:
                continue
            score = sum(w in low for w in ("offre", "logement", "loyer", "m²", "m2", "pièce", "piece"))
            if ("€" in text or "eur" in low) and ("m²" in low or "m2" in low):
                score += 2
            if score >= 2:
                continue
            href = page.url
            try:
                a = loc.locator("a[href]").first
                if a.count():
                    href = urljoin(page.url, a.get_attribute("href") or "")
            except Exception:
                pass
            candidates.append({"id": digest(text), "text": text, "href": href})

    unique = {}
    for item in candidates:
        unique[item["id"]] = item
    return list(unique.values())


def main():
    state = load_state()
    if state.get("paused") and not RESUME:
        print("Monitor paused after an authentication problem")
        return 0

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(locale="fr-FR", timezone_id="Europe/Paris")
            page = ctx.new_page()
            login(page)
            offers = extract_offers(page)
            browser.close()
    except Exception as exc:
        message = str(exc)
        print("ERROR:", message, file=sys.stderr)
        if any(x in message.lower() for x in ("login", "credential", "password", "verification")):
            state["paused"] = True
            save_state(state)
        try:
            notify("AL'in monitor needs attention", message[:700], "max")
        except Exception:
            pass
        return 2

    old = set(state.get("seen", []))
    new = [x for x in offers if x["id"] not in old]
    print(f"Found {len(offers)} candidate offers; {len(new)} new")

    if state.get("initialized") and new:
        if len(new) == 1:
            x = new[0]
            notify("New AL'in offer", x["text"][:500] + "\n\n" + x["href"], "max")
        else:
            body = "\n\n".join("• " + x["text"][:220] for x in new[:5])
            notify(f"{len(new)} new AL'in offers", body + "\n\n" + OFFERS_URL, "max")

    state.update({
        "seen": list(dict.fromkeys(state.get("seen", []) + [x["id"] for x in offers]))[-3000:],
        "initialized": True,
        "paused": False,
    })
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
