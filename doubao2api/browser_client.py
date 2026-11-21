"""Native Camoufox + WebDriver BiDi client for Dola International (https://www.dola.com/).

Architecture adapted from flow2api-Camoufox:
- Zero Playwright / Chromium dependency: native Camoufox binary + pure asyncio WebDriver BiDi.
- Persistent per-profile Windows Firefox fingerprint (`camoufox-fingerprint.json` + `CAMOU_CONFIG_*`).
- OS kernel profile lock (`fcntl.flock` / `msvcrt.locking`) + startup cache save/restore.
- Local unauthenticated SOCKS5 relay (`LocalSocks5Relay`) for authenticated upstream HTTP/SOCKS5 proxies.
- Native `BiDiChannel` (`script.message`) SSE streaming from in-browser `fetch()` (with `bdms` signing).
- Built-in `CamoufoxLiveView` attachment for remote Web interactive login (Google / Lark / QR) and captcha solving.
"""

from __future__ import annotations

import asyncio
import base64
from functools import wraps
import hashlib
import hmac as hmac_mod
import inspect
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Union
from urllib.parse import parse_qs, quote as url_quote, urlencode, urlparse
import uuid
import zlib
from datetime import datetime, timezone

import httpx

from .bidi_transport import (
    BiDiClient,
    LocalSocks5Relay,
    _to_bidi_local_value,
    wait_for_bidi_endpoint,
)
from .browser_operation import BrowserOperationGate
from .camoufox_fingerprint import (
    _start_stdout_drain,
    build_camoufox_env,
    ensure_camoufox_executable,
    find_camoufox_executable,
    load_or_create_account_camoufox_config,
    write_camoufox_user_js,
)
from .camoufox_liveview import CamoufoxLiveView, has_display
from .camoufox_profile import (
    ProfileLease,
    acquire_profile_lease,
    clear_stale_firefox_profile_locks,
    prune_camoufox_profile_bloat,
    restore_startup_cache,
    save_startup_cache,
)
from .dola_config import (
    DOLA_CHAT_URL,
    DOLA_COMPLETION_URL,
    DOLA_COOKIE_DOMAIN,
    DOLA_DEFAULT_BOT_ID,
    DOLA_DEFAULT_LANGUAGE,
    DOLA_DEFAULT_LOCALE,
    DOLA_DEFAULT_REGION,
    DOLA_DEFAULT_TIMEZONE,
    DOLA_IMAGEX_REGION,
    DOLA_IMAGEX_SERVICE,
    DOLA_PC_VERSION,
    DOLA_SAMANTHA_COMPLETION_URL,
    DOLA_TEA_CACHE_KEY,
    DOLA_TOS_FALLBACK_HOST,
    DOLA_URL,
    DOLA_VERSION_CODE,
    DOLA_WEB_AID,
    get_configured_proxy,
    get_default_profile_dir,
    get_env,
    resolve_dola_image_model,
    resolve_dola_video_model,
)
from .proxy_utils import mask_proxy_url, normalize_proxy_url, proxy_needs_relay
from .redaction import redact_sensitive_text

log = logging.getLogger("doubao2api.browser_client")

# Exported constants (Dola International)
CHAT_URL = DOLA_CHAT_URL
COMPLETION_URL = DOLA_COMPLETION_URL
SAMANTHA_COMPLETION_URL = DOLA_SAMANTHA_COMPLETION_URL
DEFAULT_BOT_ID = DOLA_DEFAULT_BOT_ID


class VideoQuotaExceeded(RuntimeError):
    """The active account has reached its daily video generation quota."""


def _serialized_browser_operation(method):
    if inspect.isasyncgenfunction(method):
        @wraps(method)
        async def _gen(self, *args, **kwargs):
            gate = getattr(self, "operation_gate", None)
            if gate is None:
                async for item in method(self, *args, **kwargs):
                    yield item
                return
            async with gate.lease():
                async for item in method(self, *args, **kwargs):
                    yield item

        return _gen

    @wraps(method)
    async def _call(self, *args, **kwargs):
        gate = getattr(self, "operation_gate", None)
        if gate is None:
            return await method(self, *args, **kwargs)
        async with gate.lease():
            return await method(self, *args, **kwargs)

    return _call


def resolve_headless_mode(val: Optional[Union[bool, str]] = None) -> bool:
    """Resolve headless mode from bool, string, or DOLA_HEADLESS / DOUBAO_HEADLESS env var.

    Defaults to headless (`True`) so the server never steals focus or auto-pops a GUI window;
    interactive login & per-account profile management use Web LiveView (or explicit `headless=False`).
    """
    if isinstance(val, bool):
        return val
    if val is None:
        raw = get_env("DOLA_HEADLESS", "DOUBAO_HEADLESS", "true").strip().lower()
    else:
        raw = str(val).strip().lower()

    if raw in ("false", "0", "no"):
        return False
    return True

def resolve_memory_type(
    memory_type: Optional[int] = None,
    save_history: Optional[bool] = None,
) -> int:
    """Resolve ByteDance memory_type: 0 = save to history, 2 = ephemeral (no history)."""
    if memory_type is not None:
        return int(memory_type)
    if save_history is not None:
        return 0 if save_history else 2
    env_save = (os.environ.get("SAVE_CHAT_HISTORY") or get_env("DOLA_SAVE_HISTORY", "DOUBAO_SAVE_HISTORY", "")).strip().lower()
    if env_save in ("true", "1", "yes"):
        return 0
    if env_save in ("false", "0", "no"):
        return 2
    auto_del = get_env("DOLA_AUTO_DELETE_CONV", "DOUBAO_AUTO_DELETE_CONV", "true").strip().lower()
    if auto_del in ("false", "0", "no"):
        return 0
    return 2


class BrowserClient:
    """Manages native Camoufox via WebDriver BiDi for Dola auth, signing, and in-browser streaming."""

    def __init__(
        self,
        headless: Optional[Union[bool, str]] = None,
        user_data_dir: Optional[str] = None,
        proxy_url: Optional[str] = None,
    ):
        self._raw_headless = headless
        self.headless = resolve_headless_mode(headless)
        self.operation_gate = BrowserOperationGate()
        self.user_data_dir = os.path.expanduser(user_data_dir) if user_data_dir else get_default_profile_dir()

        raw_proxy = proxy_url if proxy_url is not None else get_configured_proxy()
        self._proxy_url: Optional[str] = normalize_proxy_url(raw_proxy) if raw_proxy else None

        # Native Camoufox + BiDi runtime handles
        self._executable_path: Optional[str] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._stdout_drain_task: Optional[asyncio.Task] = None
        self._bidi: Optional[BiDiClient] = None
        self._context_id: Optional[str] = None
        self._relay: Optional[LocalSocks5Relay] = None
        self._profile_lease: Optional[ProfileLease] = None
        self._account_dir: Optional[Path] = None
        self._profile_dir: Optional[Path] = None
        self._fingerprint_config: Dict[str, Any] = {}

        self._http: Optional[httpx.AsyncClient] = None
        self._ready = False
        self._device_id: Optional[str] = None
        self._web_id: Optional[str] = None
        self._fp: Optional[str] = None
        self._region: str = DOLA_DEFAULT_REGION
        self._language: str = DOLA_DEFAULT_LANGUAGE
        self._tz_name: str = DOLA_DEFAULT_TIMEZONE
        self._ms_token: str = ""

        # Robustness / failure tracking
        self._consecutive_failures: int = 0
        self._last_error_code: int = 0
        self._last_error_category: str = ""
        self._needs_captcha: bool = False
        self._mode_lock: asyncio.Lock = asyncio.Lock()
        self._bridge_ready: bool = False
        self._user_agent: str = ""
        self._browser_name: str = "Camoufox (WebDriver BiDi)"

        # LiveView instance for Admin Web interactive browser control
        self.liveview: CamoufoxLiveView = CamoufoxLiveView()

        from .account_manager import AccountManager
        self.account_manager = AccountManager(storage_dir=self.user_data_dir)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def browser_name(self) -> str:
        return self._browser_name

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def proxy_url(self) -> Optional[str]:
        return self._proxy_url

    @property
    def bidi(self) -> Optional[BiDiClient]:
        return self._bidi

    @property
    def context_id(self) -> Optional[str]:
        return self._context_id

    @property
    def needs_captcha(self) -> bool:
        return self._needs_captcha

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def last_error_code(self) -> int:
        return self._last_error_code

    @property
    def last_error_category(self) -> str:
        return self._last_error_category

    # ------------------------------------------------------------------
    # Status / Failure Tracking
    # ------------------------------------------------------------------

    def record_success(self):
        """Reset failure counters on successful request."""
        self._consecutive_failures = 0
        self._last_error_code = 0
        self._last_error_category = ""
        self._needs_captcha = False
        if self.account_manager:
            active = self.account_manager.get_active_account()
            if active:
                self.account_manager.clear_captcha_status(active.id)

    def clear_captcha(self):
        """Manually or automatically clear the needs_captcha flag."""
        self._needs_captcha = False
        self._consecutive_failures = 0
        self._last_error_code = 0
        self._last_error_category = ""
        log.info("Captcha flag cleared")

    def record_failure(
        self,
        error_code: int = 0,
        *,
        verification_required: Optional[bool] = None,
        error_category: Optional[str] = None,
    ):
        """Track failures and mark verification only when classified as such."""
        self._consecutive_failures += 1
        self._last_error_code = error_code
        is_verify = verification_required is True or (
            verification_required is None and error_code == 710022002
        )
        self._last_error_category = error_category or ("verification" if is_verify else "")
        if is_verify:
            self._needs_captcha = True
            log.warning("Verification flag marked on error code %s", error_code)
        if self._consecutive_failures >= 10 and not self._needs_captcha:
            log.error("10 consecutive failures (non-captcha) - marking not ready")
            self._ready = False

    # ------------------------------------------------------------------
    # Low-level BiDi helpers
    # ------------------------------------------------------------------

    async def is_alive(self) -> bool:
        """Return True if Camoufox process and WebDriver BiDi connection are healthy."""
        if (
            self._process is None
            or self._process.returncode is not None
            or self._bidi is None
            or self._bidi._closed
            or not self._context_id
        ):
            return False
        try:
            res = await self._bidi.evaluate(
                self._context_id, "1 + 1", await_promise=False, timeout=5.0
            )
            return res == 2
        except Exception:
            return False

    async def evaluate(self, expression: str, *, await_promise: bool = True, timeout: float = 30.0) -> Any:
        """Evaluate a JS expression in the active Dola browser context."""
        if not self._bidi or not self._context_id:
            raise RuntimeError("Camoufox browser not running")
        return await self._bidi.evaluate(
            self._context_id, expression, await_promise=await_promise, timeout=timeout
        )

    async def get_page_url(self) -> str:
        """Return current browser URL."""
        if not self._bidi or not self._context_id:
            return ""
        try:
            return str(await self._bidi.evaluate(self._context_id, "location.href", await_promise=False, timeout=5.0) or "")
        except Exception:
            return ""

    @_serialized_browser_operation
    async def screenshot(self, *, image_format: str = "png", quality: float = 0.85) -> bytes:
        """Capture a screenshot of the active Dola page via WebDriver BiDi."""
        if not self._bidi or not self._context_id:
            raise RuntimeError("Camoufox browser not running")
        fmt: Dict[str, Any] = {"type": f"image/{image_format}"}
        if image_format == "jpeg":
            fmt["quality"] = quality
        shot = await self._bidi.command(
            "browsingContext.captureScreenshot",
            {"context": self._context_id, "format": fmt},
            timeout=10.0,
        )
        return base64.b64decode(shot.get("data", ""))

    # ------------------------------------------------------------------
    # Captcha Detection & Auto-Solving
    # ------------------------------------------------------------------

    @_serialized_browser_operation
    async def is_captcha_visible(self) -> bool:
        """Check whether a ByteDance / Dola captcha modal/slider/iframe is visible in DOM."""
        if not self._bidi or not self._context_id:
            return False
        try:
            visible = await self._bidi.evaluate(
                self._context_id,
                """(() => {
                    const frames = Array.from(document.querySelectorAll('iframe'));
                    for (const f of frames) {
                        const s = (f.src || '').toLowerCase();
                        if (
                            s.includes('verify') || s.includes('captcha') ||
                            s.includes('zijieapi') || s.includes('byteoversea') ||
                            s.includes('bytedance.com') || s.includes('ttwstatic')
                        ) {
                            const r = f.getBoundingClientRect();
                            if (r.width > 10 && r.height > 10) return true;
                        }
                    }
                    const sels = [
                        '#captcha_container', '.captcha_verify_container',
                        '#captcha-verify-image', '.secsdk-captcha-wrapper',
                        '.verify-bar-close', '.secsdk-captcha-drag-icon',
                        '[class*="captcha-modal"]', '[class*="captcha_verify"]'
                    ];
                    for (const sel of sels) {
                        const el = document.querySelector(sel);
                        if (el) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 10 && r.height > 10) return true;
                        }
                    }
                    return false;
                })()""",
                await_promise=False,
                timeout=8.0,
            )
            return bool(visible)
        except Exception:
            return False

    @_serialized_browser_operation
    async def auto_popup_for_captcha(self) -> bool:
        """If captcha is detected and a desktop GUI is present, switch to windowed mode."""
        if not has_display():
            log.info("Cannot auto-popup browser window: no GUI display (use Web LiveView in /admin instead)")
            return False
        try:
            return await self.switch_mode(headless=False)
        except Exception as exc:
            log.error("Failed to switch Camoufox to windowed mode: %s", exc)
            return False

    @_serialized_browser_operation
    async def try_auto_solve_captcha(self) -> bool:
        """Attempt automated slider captcha solving via WebDriver BiDi input actions."""
        if not self._bidi or not self._context_id:
            return False
        try:
            from .captcha_solver import SliderCaptchaSolver

            solver = SliderCaptchaSolver()
            solved = await solver.detect_and_solve(self._bidi, self._context_id)
            if solved:
                self._ready = True
                self._needs_captcha = False
                log.info("Auto-solved slider captcha via Camoufox BiDi!")
                return True
            return False
        except Exception as exc:
            log.warning("Automated captcha solver error: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Lifecycle (Camoufox + BiDi + LocalSocks5Relay + ProfileLease)
    # ------------------------------------------------------------------

    def _active_proxy_url(self) -> Optional[str]:
        """Resolve effective proxy: active account proxy overrides global proxy."""
        if self.account_manager:
            active = self.account_manager.get_active_account()
            if active and getattr(active, "proxy_url", None):
                return active.proxy_url
        return self._proxy_url

    def _build_httpx_client(self, proxy_url: Optional[str]) -> httpx.AsyncClient:
        kwargs: Dict[str, Any] = {
            "timeout": httpx.Timeout(120.0, connect=20.0),
            "follow_redirects": True,
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return httpx.AsyncClient(**kwargs)

    def _resolve_active_account_dir(self) -> Path:
        """Resolve the strictly isolated profile directory for the active account (`accounts/<id>/`)."""
        base = Path(self.user_data_dir)
        acc = self.account_manager.get_active_account() if self.account_manager else None
        if acc:
            target = Path(self.account_manager.resolve_account_dir(acc.id))
            # One-time migration from legacy shared `<base>/firefox-profile` into isolated `accounts/<id>/`
            legacy_profile = base / "firefox-profile"
            target_profile = target / "firefox-profile"
            if legacy_profile.exists() and not target_profile.exists():
                import shutil
                target.mkdir(parents=True, exist_ok=True)
                try:
                    clear_stale_firefox_profile_locks(legacy_profile)
                    shutil.copytree(legacy_profile, target_profile, symlinks=False, ignore_dangling_symlinks=True)
                    legacy_fp = base / "camoufox-fingerprint.json"
                    if legacy_fp.exists() and not (target / "camoufox-fingerprint.json").exists():
                        shutil.copy2(legacy_fp, target / "camoufox-fingerprint.json")
                    log.info("Migrated legacy shared profile into isolated account directory %s", target)
                except Exception as exc:
                    log.warning("Legacy profile migration failed (%s); starting fresh isolated profile", exc)
            return target
        return base / "accounts" / "_default"

    async def release_if_account(self, account_id: Optional[str] = None) -> None:
        """Close runtime Camoufox if holding the specified account's isolated profile (or any if None)."""
        async with self.operation_gate.lease():
            active_id = getattr(self, "_active_profile_account_id", None)
            if account_id is None or active_id == account_id or self._process is not None:
                if account_id is None or active_id == account_id or active_id is None:
                    await self.close()

    async def start(self):
        """Launch native Camoufox with WebDriver BiDi inside the active account's isolated profile."""
        if self._raw_headless in (None, "auto"):
            env_mode = get_env("DOLA_HEADLESS", "DOUBAO_HEADLESS", "true").strip().lower()
            self.headless = resolve_headless_mode(env_mode)

        active_acc = self.account_manager.get_active_account() if self.account_manager else None
        self._active_profile_account_id = active_acc.id if active_acc else None
        account_dir = self._resolve_active_account_dir()
        account_dir.mkdir(parents=True, exist_ok=True)
        profile_dir = account_dir / "firefox-profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._account_dir = account_dir
        self._profile_dir = profile_dir

        # Acquire cross-process OS kernel lock (flow2api-Camoufox pattern)
        if self._profile_lease is None or not self._profile_lease.active:
            self._profile_lease = acquire_profile_lease(
                account_dir, holder=f"dola2api:{os.getpid()}", wait_seconds=3.0
            )

        clear_stale_firefox_profile_locks(profile_dir)
        restore_startup_cache(account_dir, profile_dir)

        # Ensure native Camoufox executable (auto-downloads from GitHub Release if absent)
        exe = await ensure_camoufox_executable()
        self._executable_path = exe

        # Load or generate locked per-profile Windows Firefox fingerprint
        locale = get_env("DOLA_LOCALE", None, DOLA_DEFAULT_LOCALE)
        tz = get_env("DOLA_TIMEZONE", None, DOLA_DEFAULT_TIMEZONE)
        self._tz_name = tz
        fp_cfg = load_or_create_account_camoufox_config(
            account_dir, locale=locale, timezone=tz
        )
        self._fingerprint_config = fp_cfg
        self._user_agent = str(fp_cfg.get("navigator.userAgent") or "")

        # Configure upstream proxy & optional LocalSocks5Relay
        effective_proxy = self._active_proxy_url()
        relay_addr = None
        if effective_proxy and proxy_needs_relay(effective_proxy):
            self._relay = LocalSocks5Relay(effective_proxy)
            relay_addr = await self._relay.start()
            log.info(
                "Started LocalSocks5Relay on %s:%d -> %s",
                relay_addr[0],
                relay_addr[1],
                mask_proxy_url(effective_proxy),
            )
        elif effective_proxy:
            log.info("Configured direct proxy: %s", mask_proxy_url(effective_proxy))

        write_camoufox_user_js(
            profile_dir,
            fp_cfg,
            proxy_url=None if relay_addr else effective_proxy,
            relay_addr=relay_addr,
            headless=self.headless,
            live_view=False,
            idle_frame_divisor=1,
            executable_path=exe,
        )

        env = build_camoufox_env(
            fp_cfg,
            headless=self.headless,
            live_view=False,
            profile_dir=profile_dir,
            executable_path=exe,
        )

        args = [exe]
        if self.headless:
            args.append("--headless")
        args.extend(
            ["--remote-debugging-port=0", "-wait-for-browser", "-profile", str(profile_dir)]
        )

        log.info(
            "Launching Camoufox (headless=%s, profile=%s, proxy=%s)",
            self.headless,
            profile_dir,
            mask_proxy_url(effective_proxy) or "direct",
        )
        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        ws_url = await wait_for_bidi_endpoint(self._process, timeout=45.0)
        self._stdout_drain_task = _start_stdout_drain(self._process)
        self._bidi = await BiDiClient.connect(ws_url, timeout=20.0)
        await self._bidi.command("session.new", {"capabilities": {}}, timeout=15.0)
        await self._bidi.command(
            "session.subscribe", {"events": ["script.message"]}, timeout=10.0
        )
        tree = await self._bidi.command("browsingContext.getTree", {}, timeout=15.0)
        contexts = tree.get("contexts") or []
        if not contexts:
            created = await self._bidi.command(
                "browsingContext.create", {"type": "tab"}, timeout=15.0
            )
            self._context_id = str(created.get("context") or "")
        else:
            self._context_id = str(contexts[0].get("context") or "")

        if not self._context_id:
            raise RuntimeError("Camoufox BiDi returned no browsing context")

        self._http = self._build_httpx_client(effective_proxy)
        self._bridge_ready = True

        # Attach LiveView so /admin can view & interact with this Camoufox instance on demand
        await self.liveview.attach(self._bidi, self._context_id)

        log.info("Navigating Camoufox to %s ...", CHAT_URL)
        await self._bidi.command(
            "browsingContext.navigate",
            {"context": self._context_id, "url": CHAT_URL, "wait": "complete"},
            timeout=60.0,
        )
        await asyncio.sleep(3)

        # Re-hydrate active account cookies if available
        if self.account_manager and not await self._has_session_cookie():
            active = self.account_manager.get_active_account()
            if active and active.cookies:
                log.info("Restoring saved cookies for active account '%s'", active.name)
                await self.inject_cookies_and_reload(active.cookies)
                return

        await self._check_login_state()

    async def close(self):
        """Gracefully close BiDi session, Camoufox process, SOCKS5 relay, HTTP client, and profile lock."""
        self._ready = False
        self._bridge_ready = False

        try:
            await self.liveview.detach()
        except Exception:
            pass

        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

        if self._bidi is not None:
            try:
                await self._bidi.close()
            except Exception:
                pass
            self._bidi = None

        if self._stdout_drain_task is not None and not self._stdout_drain_task.done():
            self._stdout_drain_task.cancel()
            try:
                await self._stdout_drain_task
            except BaseException:
                pass
            self._stdout_drain_task = None

        if self._process is not None:
            if self._process.returncode is None:
                try:
                    self._process.terminate()
                    await asyncio.wait_for(self._process.wait(), timeout=6.0)
                except asyncio.TimeoutError:
                    try:
                        self._process.kill()
                        await asyncio.wait_for(self._process.wait(), timeout=3.0)
                    except Exception:
                        pass
                except Exception:
                    pass
            self._process = None

        self._context_id = None

        if self._relay is not None:
            try:
                await self._relay.close()
            except Exception:
                pass
            self._relay = None

        if self._profile_dir and self._account_dir:
            save_startup_cache(self._profile_dir, self._account_dir)
            prune_camoufox_profile_bloat(self._profile_dir)
            clear_stale_firefox_profile_locks(self._profile_dir)

        if self._profile_lease is not None:
            self._profile_lease.release()
            self._profile_lease = None

    async def restart(self):
        """Close and relaunch Camoufox."""
        log.info("Restarting Camoufox browser...")
        await self.close()
        await asyncio.sleep(0.8)
        await self.start()

    @_serialized_browser_operation
    async def switch_mode(self, headless: bool) -> bool:
        """Dynamically switch Camoufox between headless and desktop windowed mode."""
        async with self._mode_lock:
            if self.headless == headless and await self.is_alive():
                return True
            if not headless and not has_display():
                log.warning("Cannot switch to windowed mode without GUI display; use Web LiveView")
                return False
            self.headless = headless
            self._raw_headless = headless
            await self.restart()
            return await self.is_alive()

    @_serialized_browser_operation
    async def set_proxy(self, proxy_url: Optional[str]) -> Optional[str]:
        """Update global upstream proxy and restart Camoufox if already running."""
        normalized = normalize_proxy_url(proxy_url) if proxy_url else None
        changed = normalized != self._proxy_url
        self._proxy_url = normalized
        if changed and self._process is not None:
            await self.restart()
        return self._proxy_url

    # ------------------------------------------------------------------
    # Cookie Operations (WebDriver BiDi storage.*)
    # ------------------------------------------------------------------

    async def get_cookies(self, domain: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieve cookies from Camoufox via BiDi `storage.getCookies`."""
        if not self._bidi or self._bidi._closed:
            return []
        params: Dict[str, Any] = {}
        if domain:
            params["filter"] = {"domain": domain}
        res = await self._bidi.command("storage.getCookies", params, timeout=10.0)
        raw_cookies = res.get("cookies") or []
        out: List[Dict[str, Any]] = []
        for c in raw_cookies:
            c_domain = str(c.get("domain") or "")
            if domain and domain.lstrip(".") not in c_domain.lstrip("."):
                continue
            val_obj = c.get("value")
            if isinstance(val_obj, dict):
                val = str(val_obj.get("value") or "")
            else:
                val = str(val_obj or "")
            out.append(
                {
                    "name": str(c.get("name") or ""),
                    "value": val,
                    "domain": c_domain,
                    "path": str(c.get("path") or "/"),
                    "expires": c.get("expiry", -1),
                    "httpOnly": bool(c.get("httpOnly", False)),
                    "secure": bool(c.get("secure", False)),
                }
            )
        return out

    async def clear_cookies(self, domain: Optional[str] = None) -> None:
        """Delete cookies via BiDi `storage.deleteCookies`."""
        if not self._bidi or self._bidi._closed:
            return
        params: Dict[str, Any] = {}
        if domain:
            params["filter"] = {"domain": domain}
        await self._bidi.command("storage.deleteCookies", params, timeout=10.0)

    async def set_cookies(self, cookies: Dict[str, str], domain: str = DOLA_COOKIE_DOMAIN) -> int:
        """Inject cookie key/value pairs into Camoufox via BiDi `storage.setCookie`."""
        if not self._bidi or self._bidi._closed:
            raise RuntimeError("Camoufox BiDi is not connected")
        count = 0
        for name, value in cookies.items():
            if not name:
                continue
            await self._bidi.command(
                "storage.setCookie",
                {
                    "cookie": {
                        "name": str(name),
                        "value": {"type": "string", "value": str(value)},
                        "domain": domain,
                        "path": "/",
                        "secure": True,
                        "httpOnly": False,
                    }
                },
                timeout=10.0,
            )
            count += 1
        return count

    async def _has_session_cookie(self) -> bool:
        cookies = await self.get_cookies()
        return any(
            c["name"] in ("sessionid", "sessionid_ss", "sid_tt")
            and bool(c.get("value"))
            and "dola.com" in c.get("domain", "")
            for c in cookies
        )

    # ------------------------------------------------------------------
    # Login Detection & Account Switching
    # ------------------------------------------------------------------

    @_serialized_browser_operation
    async def _check_login_state(self):
        """Check if logged into Dola by inspecting session cookies and DOM indicators."""
        if not self._bidi or not self._context_id:
            self._ready = False
            return

        current_url = await self.get_page_url()
        if "from_logout=1" in current_url:
            log.info("Page is on from_logout=1, navigating back to %s", CHAT_URL)
            try:
                await self._bidi.command(
                    "browsingContext.navigate",
                    {"context": self._context_id, "url": CHAT_URL, "wait": "complete"},
                    timeout=30.0,
                )
                await asyncio.sleep(2)
            except Exception as exc:
                log.warning("Navigation back to CHAT_URL failed: %s", exc)

        try:
            cookies = await self.get_cookies()
        except Exception as exc:
            log.warning("Failed to inspect Camoufox cookies: %s", exc)
            self._ready = False
            return

        dola_cookies = [c for c in cookies if "dola.com" in c.get("domain", "")]
        has_session = any(
            c["name"] in ("sessionid", "sessionid_ss", "sid_tt") and bool(c.get("value"))
            for c in dola_cookies
        )

        dom_state = {"has_login_btn": False, "has_avatar": False}
        try:
            dom_state = await self._bidi.evaluate(
                self._context_id,
                """(() => {
                    const btns = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                    let hasLogin = false;
                    for (const b of btns) {
                        const txt = (b.innerText || '').trim().toLowerCase();
                        if (txt === 'log in' || txt === 'sign in' || txt === '登录') {
                            const r = b.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) { hasLogin = true; break; }
                        }
                    }
                    const avatar = document.querySelector('img[class*="avatar"], div[class*="avatar"], [data-testid*="avatar"]');
                    return { has_login_btn: hasLogin, has_avatar: Boolean(avatar) };
                })()""",
                await_promise=False,
                timeout=8.0,
            ) or dom_state
        except Exception:
            pass

        has_login_btn = bool(dom_state.get("has_login_btn"))
        has_avatar = bool(dom_state.get("has_avatar"))
        is_logged_in = (has_session and not has_login_btn) or (has_session and has_avatar)
        log.info(
            "Dola login check: has_session=%s, has_avatar=%s, has_login_btn=%s -> logged_in=%s",
            has_session,
            has_avatar,
            has_login_btn,
            is_logged_in,
        )

        if not is_logged_in:
            self._ready = False
            flag_file = os.path.join(self.user_data_dir, ".login_success")
            if os.path.exists(flag_file):
                try:
                    os.remove(flag_file)
                except Exception:
                    pass
            return

        self._ready = True
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            with open(os.path.join(self.user_data_dir, ".login_success"), "w", encoding="utf-8") as f:
                f.write(str(int(time.time())))
        except Exception:
            pass

        await self._extract_params()
        await self._seed_ms_token()
        await self._verify_fetch_hook()
        await self._wait_for_signing()
        log.info(
            "Dola Camoufox client ready! device_id=%s, region=%s, lang=%s",
            self._device_id,
            self._region,
            self._language,
        )

        # Auto-register default account into pool if empty
        if self.account_manager and len(self.account_manager.accounts) == 0:
            cookie_dict = {c["name"]: c["value"] for c in dola_cookies if c.get("value")}
            if any(k in cookie_dict for k in ("sessionid", "sessionid_ss", "sid_tt")):
                self.account_manager.add_or_update_account(
                    name="Default Dola Account",
                    cookies=cookie_dict,
                    account_id="acc_default",
                )
                log.info("Auto-registered current Dola session as 'acc_default' in account pool")

    async def _extract_params(self):
        """Extract device_id, web_id, fp, region, and language from Dola localStorage & cookies."""
        for _ in range(5):
            async with self.operation_gate.lease():
                if not self._bidi or not self._context_id:
                    return
                params = await self._bidi.evaluate(
                    self._context_id,
                    f"""(() => {{
                        const result = {{}};
                        try {{
                            const samWeb = JSON.parse(localStorage.getItem('samantha_web_web_id') || '{{}}');
                            result.device_id = samWeb.web_id || '';
                        }} catch(e) {{}}
                        try {{
                            const tea = JSON.parse(localStorage.getItem('{DOLA_TEA_CACHE_KEY}') || '{{}}');
                            result.web_id = tea.web_id || '';
                        }} catch(e) {{}}
                        const cookieMap = {{}};
                        for (const part of document.cookie.split(';')) {{
                            const idx = part.indexOf('=');
                            if (idx > 0) {{
                                cookieMap[part.slice(0, idx).trim()] = part.slice(idx + 1).trim();
                            }}
                        }}
                        result.fp = cookieMap['s_v_web_id'] || '';
                        result.region = cookieMap['flow_user_country'] || localStorage.getItem('flow_user_country') || '';
                        result.language = cookieMap['i18next'] || localStorage.getItem('i18nextLng') || '';
                        return result;
                    }})()""",
                    await_promise=False,
                    timeout=10.0,
                ) or {}
            self._device_id = str(params.get("device_id") or "")
            self._web_id = str(params.get("web_id") or self._device_id or "")
            self._fp = str(params.get("fp") or "")
            if params.get("region"):
                self._region = str(params["region"]).upper()
            if params.get("language"):
                self._language = str(params["language"]).split("-", 1)[0].lower()
            if self._device_id:
                break
            await asyncio.sleep(1)

    async def _wait_for_signing(self):
        """Wait for window.bdms.frontierSign to become available."""
        for i in range(12):
            async with self.operation_gate.lease():
                if not self._bidi or not self._context_id:
                    return
                has_sign = await self._bidi.evaluate(
                    self._context_id,
                    "typeof window.bdms?.frontierSign === 'function'",
                    await_promise=False,
                    timeout=8.0,
                )
            if has_sign:
                log.info("bdms.frontierSign ready after %ds", (i + 1) * 2)
                return
            await asyncio.sleep(2)
        log.warning("bdms.frontierSign not detected after 24s - upload signing may fail")

    async def _verify_fetch_hook(self) -> bool:
        """Verify ByteDance's in-page fetch interceptor is active (auto-injects msToken & a_bogus)."""
        for i in range(10):
            async with self.operation_gate.lease():
                if not self._bidi or not self._context_id:
                    return False
                hooked = await self._bidi.evaluate(
                    self._context_id,
                    "(() => { try { return !window.fetch.toString().includes('native code'); } catch(e) { return false; } })()",
                    await_promise=False,
                    timeout=8.0,
                )
            if hooked:
                log.info("Dola fetch hook active after %ds", (i + 1) * 2)
                return True
            await asyncio.sleep(2)
        log.warning("Dola fetch hook not detected after 20s")
        return False

    async def wait_for_login(self, timeout: int = 180) -> bool:
        """Poll until the user completes login (Google, Lark, or QR scan) in Camoufox / LiveView."""
        await self._trigger_login_dialog()
        log.info("Waiting for Dola login (timeout=%ds)...", timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(1.5)
            await self._check_login_state()
            if self._ready:
                return True
        return False

    async def _trigger_login_dialog(self):
        """Click 'Log In' button on Dola if modal is not already shown."""
        if not self._bidi or not self._context_id:
            return
        try:
            async with self.operation_gate.lease():
                await self._bidi.evaluate(
                    self._context_id,
                    """(() => {
                        const modal = document.querySelector('[role="dialog"], .semi-modal');
                        if (modal) return false;
                        const btns = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                        const target = btns.find(b => {
                            const t = (b.innerText || '').trim().toLowerCase();
                            return t === 'log in' || t === 'sign in' || t === '登录';
                        });
                        if (target) { target.click(); return true; }
                        return false;
                    })()""",
                    await_promise=False,
                    timeout=8.0,
                )
        except Exception:
            pass

    @_serialized_browser_operation
    async def inject_cookies_and_reload(self, cookies: Dict[str, str]) -> bool:
        """Inject Dola cookies into Camoufox, reload, and verify authenticated state."""
        if not await self.is_alive():
            log.info("inject_cookies: Camoufox not active, restarting...")
            await self.restart()
        if not self._bidi or not self._context_id:
            return False

        count = await self.set_cookies(cookies, domain=DOLA_COOKIE_DOMAIN)
        log.info("Injected %d cookies into Camoufox for %s", count, DOLA_COOKIE_DOMAIN)

        await self._bidi.command(
            "browsingContext.navigate",
            {"context": self._context_id, "url": CHAT_URL, "wait": "complete"},
            timeout=45.0,
        )
        await asyncio.sleep(3)
        await self._check_login_state()
        return self._ready

    @_serialized_browser_operation
    async def switch_to_account(self, account_id: str) -> bool:
        """Switch Camoufox session to the specified account using its strictly isolated profile & proxy."""
        acc = self.account_manager.get_account(account_id)
        if not acc or not acc.cookies:
            log.error("switch_to_account: account %s missing or has no cookies", account_id)
            return False

        previous = self.account_manager.get_active_account()
        previous_cookies = dict(previous.cookies) if previous else {}
        previous_proxy = previous.proxy_url if previous else self._proxy_url
        target_proxy = acc.proxy_url or self._proxy_url

        # Use strict isolated per-account profile restart whenever switching real Camoufox profiles or proxies
        target_profile_dir = Path(self.account_manager.resolve_account_dir(acc.id)) / "firefox-profile"
        cur_profile_acc = getattr(self, "_active_profile_account_id", None)
        need_isolated_restart = (
            target_proxy != previous_proxy
            or (cur_profile_acc is not None and cur_profile_acc != acc.id)
            or target_profile_dir.exists()
        )
        if need_isolated_restart:
            self.account_manager.set_active_account(acc.id)
            try:
                await self.restart()
                if self._ready:
                    self._consecutive_failures = 0
                    self._last_error_code = 0
                    self._last_error_category = ""
                    self._needs_captcha = False
                    log.info(
                        "Switched to isolated Camoufox profile for Dola account %s (%s)",
                        acc.id,
                        acc.name,
                    )
                    return True
            except asyncio.CancelledError:
                if previous:
                    self.account_manager.set_active_account(previous.id)
                self._ready = False
                raise
            except Exception as exc:
                log.warning("Isolated account switch failed (%s)", type(exc).__name__)
            if previous:
                self.account_manager.set_active_account(previous.id)
                try:
                    await self.restart()
                except Exception:
                    self._ready = False
            return False

        if not await self.is_alive():
            try:
                await self.restart()
            except Exception as exc:
                log.warning("Camoufox restart failed during account switch (%s)", type(exc).__name__)
                self._ready = False
                return False
        async def _restore_previous() -> bool:
            if not previous_cookies:
                return False
            try:
                await self.clear_cookies()
                return await self.inject_cookies_and_reload(previous_cookies)
            except Exception:
                return False

        self._ready = False
        try:
            await self.clear_cookies()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("clear_cookies failed before account switch (%s)", type(exc).__name__)
            return False

        try:
            ok = await self.inject_cookies_and_reload(acc.cookies)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Account switch failed (%s)", type(exc).__name__)
            ok = False

        if ok:
            self.account_manager.set_active_account(acc.id)
            self._consecutive_failures = 0
            self._last_error_code = 0
            self._last_error_category = ""
            self._needs_captcha = False
            log.info("Switched Camoufox session to Dola account %s (%s)", acc.id, acc.name)
            return True

        restored = await _restore_previous()
        self._ready = restored
        return False

    # ------------------------------------------------------------------
    # Signing, Cookies & Dola Query Params
    # ------------------------------------------------------------------

    @_serialized_browser_operation
    async def _get_cookies_string(self) -> str:
        cookies = await self.get_cookies()
        dola_cookies = [c for c in cookies if "dola.com" in c.get("domain", "")]
        return "; ".join(f"{c['name']}={c['value']}" for c in dola_cookies if c.get("name"))

    @_serialized_browser_operation
    async def _get_csrf_token(self) -> str:
        cookies = await self.get_cookies()
        for c in cookies:
            if c["name"] in ("passport_csrf_token", "passport_csrf_token_default") and c.get("value"):
                return c["value"]
        return ""

    @_serialized_browser_operation
    async def _seed_ms_token(self):
        cookies = await self.get_cookies()
        for c in cookies:
            if c["name"] == "msToken" and c.get("value"):
                self._ms_token = c["value"]
                log.info("Seeded msToken from Camoufox cookies (%d chars)", len(self._ms_token))
                return

    @_serialized_browser_operation
    async def _sign_url(self, base_url: str, params: Dict[str, str]) -> str:
        """Sign a URL using Dola's in-page `window.bdms.frontierSign`."""
        sorted_params = dict(sorted(params.items()))
        query_string = urlencode(sorted_params)

        if not self._bidi or not self._context_id:
            raise RuntimeError("Camoufox BiDi not connected")

        last_error = None
        for attempt in range(3):
            try:
                sig = await self._bidi.call_function(
                    self._context_id,
                    "(qs) => window.bdms && typeof window.bdms.frontierSign === 'function' ? window.bdms.frontierSign(qs) : null",
                    [_to_bidi_local_value(query_string)],
                    await_promise=False,
                    timeout=10.0,
                )
                if isinstance(sig, dict):
                    if "a_bogus" in sig:
                        return f"{base_url}?{query_string}&a_bogus={sig['a_bogus']}"
                    if "X-Bogus" in sig:
                        return f"{base_url}?{query_string}&X-Bogus={sig['X-Bogus']}"
                    if sig:
                        k, v = next(iter(sig.items()))
                        return f"{base_url}?{query_string}&{k}={v}"
                elif isinstance(sig, str) and sig:
                    return f"{base_url}?{query_string}&X-Bogus={sig}"
                last_error = "empty signature"
            except Exception as exc:
                last_error = f"signing failed ({type(exc).__name__})"
                log.warning("frontierSign attempt %d failed (%s)", attempt + 1, type(exc).__name__)
            if attempt < 2:
                await asyncio.sleep(1)

        raise RuntimeError(f"Failed to generate bdms signature: {last_error}")

    def _build_query_params(self) -> Dict[str, str]:
        """Build standard Dola International web query parameters."""
        region = self._region or DOLA_DEFAULT_REGION
        lang = self._language or DOLA_DEFAULT_LANGUAGE
        params = {
            "aid": str(DOLA_WEB_AID),
            "real_aid": str(DOLA_WEB_AID),
            "device_id": self._device_id or "",
            "device_platform": "web",
            "doubao_device_platform": "web",
            "web_platform": "browser",
            "fp": self._fp or "",
            "language": lang,
            "pc_version": DOLA_PC_VERSION,
            "doubao_pc_version": DOLA_PC_VERSION,
            "pkg_type": "release_version",
            "region": region,
            "sys_region": region,
            "samantha_web": "1",
            "tea_uuid": self._web_id or "",
            "tz_name": self._tz_name or DOLA_DEFAULT_TIMEZONE,
            "use-olympus-account": "1",
            "version_code": DOLA_VERSION_CODE,
            "web_id": self._web_id or "",
            "web_tab_id": str(uuid.uuid4()),
        }
        if self._ms_token:
            params["msToken"] = self._ms_token
        return params

    def _build_headers(self, cookie_str: str, csrf_token: str = "") -> Dict[str, str]:
        """Build Dola HTTP headers for out-of-browser TOS/upload calls."""
        if not csrf_token:
            for part in cookie_str.split("; "):
                if part.startswith("passport_csrf_token="):
                    csrf_token = part.split("=", 1)[1]
                    break
        ua = self._user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0"
        )
        headers = {
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Cookie": cookie_str,
            "Origin": DOLA_URL,
            "Referer": CHAT_URL,
            "User-Agent": ua,
            "agw-js-conv": "str, str",
        }
        if csrf_token:
            headers["x-tt-passport-csrf-token"] = csrf_token
        return headers

    # ------------------------------------------------------------------
    # Chat Completion (Real-time SSE Streaming via WebDriver BiDiChannel)
    # ------------------------------------------------------------------

    @_serialized_browser_operation
    async def chat_completion(
        self,
        text: str,
        conversation_id: Optional[str] = None,
        bot_id: Optional[str] = None,
        use_deep_think: int = 0,
        file_attachments: Optional[List[Dict[str, Any]]] = None,
        image_attachments: Optional[List[Dict[str, Any]]] = None,
        chat_ability: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Send a chat message on Dola and stream SSE events over a WebDriver BiDiChannel."""
        if not self._ready or not self._bidi or not self._context_id:
            raise RuntimeError("Camoufox browser not ready - login to Dola first")

        effective_bot_id = bot_id or DEFAULT_BOT_ID
        need_create = not bool(conversation_id)
        local_conv_id = f"local_{uuid.uuid4().int % 10**16}"
        msg_uuid = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        now_sec = int(time.time())

        content_blocks: List[Dict[str, Any]] = []

        if file_attachments:
            att_list = [
                {
                    "type": 3,
                    "identifier": str(uuid.uuid4()),
                    "file": {
                        "uri": f.get("uri", ""),
                        "url": "",
                        "file_type": 0,
                        "name": f.get("name", "file.txt"),
                        "size": int(f.get("size") or 0),
                    },
                    "parse_state": 1,
                    "review_state": 1,
                    "upload_status": 1,
                    "progress": 100,
                    "src": "",
                }
                for f in file_attachments
            ]
            content_blocks.append(
                {
                    "block_type": 10052,
                    "content": {
                        "attachment_block": {"attachments": att_list},
                        "pc_event_block": "",
                    },
                    "block_id": str(uuid.uuid4()),
                    "parent_id": "",
                    "meta_info": [],
                    "append_fields": [],
                }
            )

        if image_attachments:
            for img in image_attachments:
                content_blocks.append(
                    {
                        "block_type": 10052,
                        "content": {
                            "attachment_block": {
                                "attachments": [
                                    {
                                        "type": 1,
                                        "identifier": str(uuid.uuid4()),
                                        "image": {
                                            "name": img.get("name", "image.png"),
                                            "uri": img.get("uri", ""),
                                            "image_ori": {
                                                "url": img.get("cdn_url", ""),
                                                "width": int(img.get("width") or 1024),
                                                "height": int(img.get("height") or 1024),
                                                "format": img.get("format", "png"),
                                                "url_formats": {},
                                            },
                                        },
                                        "parse_state": 1,
                                        "review_state": 1,
                                        "upload_status": 1,
                                        "progress": 100,
                                        "src": img.get("cdn_url", ""),
                                    }
                                ]
                            },
                            "pc_event_block": "",
                        },
                        "block_id": str(uuid.uuid4()),
                        "parent_id": "",
                        "meta_info": [],
                        "append_fields": [],
                        "is_finish": True,
                        "patch_type": 2,
                    }
                )

        content_blocks.append(
            {
                "block_type": 10000,
                "content": {
                    "text_block": {"text": text, "icon_url": "", "icon_url_dark": "", "summary": ""},
                    "pc_event_block": "",
                },
                "block_id": str(uuid.uuid4()),
                "parent_id": "",
                "meta_info": [],
                "append_fields": [],
            }
        )

        payload = {
            "client_meta": {
                "local_conversation_id": local_conv_id if need_create else "",
                "conversation_id": conversation_id or "",
                "bot_id": effective_bot_id,
                "last_section_id": "",
                "last_message_index": None,
                "local_permissions": [
                    {"permission_name": "ACCESS_COARSE_LOCATION", "status": 3},
                    {"permission_name": "ACCESS_FINE_LOCATION", "status": 3},
                    {"permission_name": "ACCESS_BACKGROUND_LOCATION", "status": 3},
                ],
            },
            "messages": [
                {
                    "local_message_id": msg_uuid,
                    "content_block": content_blocks,
                    "message_status": 0,
                }
            ],
            "option": {
                "send_message_scene": "",
                "create_time_ms": now_ms,
                "collect_id": "",
                "is_audio": False,
                "answer_with_suggest": False,
                "agent_mode": 2,
                "tts_switch": False,
                "need_deep_think": use_deep_think,
                "click_clear_context": False,
                "from_suggest": False,
                "is_regen": False,
                "is_replace": False,
                "is_from_click_option": False,
                "is_from_click_softlink": False,
                "disable_sse_cache": False,
                "select_text_action": "",
                "is_select_text": False,
                "resend_for_regen": False,
                "scene_type": 0,
                "unique_key": str(uuid.uuid4()),
                "start_seq": 0,
                "need_create_conversation": need_create,
                "conversation_init_option": {"need_ack_conversation": True},
                "conversation_init_ext": {
                    "model_item_key": "0",
                    "reasoning_effort": str(use_deep_think) if use_deep_think else "0",
                    "mode_id": "1",
                },
                "regen_query_id": [],
                "edit_query_id": [],
                "regen_instruction": "",
                "no_replace_for_regen": False,
                "message_from": 0,
                "shared_app_name": "",
                "shared_app_id": "",
                "sse_recv_event_options": {"support_chunk_delta": True},
                "is_ai_playground": False,
                "is_old_user": True,
                "recovery_option": {
                    "is_recovery": False,
                    "req_create_time_sec": now_sec,
                    "append_sse_event_scene": 0,
                },
                "message_storage_type": 0,
                "related_deleted_message_ids": {},
                "connector_info_list": [],
                "model_config": {
                    "model_item_key": "0",
                    "model_extra_params": {},
                    "reasoning_effort": use_deep_think,
                },
                "aggregate_params": {
                    "conversation_mode": "1",
                    "mode_id": "1",
                    "model_item_key": "0",
                    "agent_mode": "2",
                    "reasoning_effort": str(use_deep_think) if use_deep_think else "0",
                    "provider_id": "",
                },
                "conversation_mode": 1,
            },
            "user_context": [],
            "ext": {
                "agent_mode": "2",
                "use_deep_think": str(use_deep_think),
                "sub_conv_firstmet_type": "1" if need_create else "0",
                "collection_id": "",
                "is_finish": "1",
                "conversation_init_option": json.dumps({"need_ack_conversation": True}),
                "commerce_credit_config_enable": "0",
            },
        }
        if chat_ability:
            payload["chat_ability"] = chat_ability

        query_params = self._build_query_params()
        url = await self._sign_url("/chat/completion", query_params)

        request_id = f"req_{uuid.uuid4().hex[:16]}"
        channel = self._bidi.open_channel(f"dola_stream_{request_id}")

        log.info(
            "POST %s (conv=%s, deep_think=%s) [Camoufox BiDiChannel]",
            url.split("?")[0],
            conversation_id or "new",
            use_deep_think,
        )

        eval_task = asyncio.create_task(
            self._browser_fetch_stream(url, payload, request_id, channel.name)
        )

        def _on_eval_done(task: asyncio.Task):
            try:
                exc = task.exception()
                if exc:
                    log.error("BiDi stream eval crashed for %s (%s)", request_id, type(exc).__name__)
                    channel.push({"type": "string", "value": f"__ERROR__:{redact_sensitive_text(str(exc))}"})
            except (asyncio.CancelledError, Exception):
                pass

        eval_task.add_done_callback(_on_eval_done)

        stream_complete = False
        try:
            while True:
                chunk_json = await channel.next_text(timeout=180.0)
                if chunk_json == "__DONE__":
                    stream_complete = True
                    break
                if chunk_json.startswith("__ERROR__:"):
                    yield {"error": True, "status": 0, "body": chunk_json[10:]}
                    break
                if chunk_json.startswith("__HTTP_ERROR__:"):
                    rest = chunk_json[15:]
                    status = int(rest.split(":", 1)[0])
                    body = rest.split(":", 1)[1] if ":" in rest else ""
                    log.error("Dola upstream HTTP error: status=%d", status)
                    yield {"error": True, "status": status, "body": body}
                    break
                try:
                    data = json.loads(chunk_json)
                    yield data
                except json.JSONDecodeError:
                    continue
        except asyncio.TimeoutError:
            log.error("Stream timeout (180s) for request %s", request_id)
            yield {"error": True, "status": 0, "body": "Stream timeout"}
        finally:
            channel.close()
            if stream_complete and not eval_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(eval_task), timeout=2.0)
                except (asyncio.TimeoutError, Exception):
                    pass
            if not eval_task.done():
                if self._bidi and not self._bidi._closed and self._context_id:
                    try:
                        await asyncio.wait_for(
                            self._bidi.call_function(
                                self._context_id,
                                """(requestId) => {
                                    const m = window.__dolaStreamAbortControllers;
                                    const c = m && m.get(requestId);
                                    if (c) c.abort();
                                }""",
                                [_to_bidi_local_value(request_id)],
                                await_promise=False,
                                timeout=2.0,
                            ),
                            timeout=2.5,
                        )
                    except Exception:
                        pass
                eval_task.cancel()
                await asyncio.gather(eval_task, return_exceptions=True)
            else:
                try:
                    eval_task.result()
                except Exception:
                    pass

    async def _browser_fetch_stream(
        self, url: str, payload: Dict[str, Any], request_id: str, channel_name: str
    ):
        """Run in-page `fetch()` on Dola and emit parsed SSE chunks over WebDriver BiDi channel."""
        if not self._bidi or not self._context_id:
            raise RuntimeError("Camoufox BiDi not connected")

        js_fn = r"""async (url, payloadJson, requestId, sendChunk) => {
            const controllers = window.__dolaStreamAbortControllers ||
                (window.__dolaStreamAbortControllers = new Map());
            const controller = new AbortController();
            controllers.set(requestId, controller);
            let reader = null;
            try {
                const csrf = document.cookie.match(/passport_csrf_token=([^;]+)/);
                const csrfToken = csrf ? csrf[1] : '';
                const headers = {
                    'Content-Type': 'application/json',
                    'agw-js-conv': 'str',
                };
                if (csrfToken) {
                    headers['x-tt-passport-csrf-token'] = csrfToken;
                }
                const res = await fetch(url, {
                    method: 'POST',
                    headers: headers,
                    body: payloadJson,
                    credentials: 'include',
                    signal: controller.signal,
                });
                if (!res.ok) {
                    const errBody = await res.text();
                    sendChunk('__HTTP_ERROR__:' + res.status + ':' + errBody.slice(0, 500));
                    return;
                }
                reader = res.body.getReader();
                const decoder = new TextDecoder();
                let currentEvent = '';
                let buffer = '';
                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    buffer = lines.pop();
                    for (const line of lines) {
                        const trimmed = line.trim();
                        if (!trimmed) continue;
                        if (trimmed.startsWith('event: ')) {
                            currentEvent = trimmed.slice(7);
                            continue;
                        }
                        if (trimmed.startsWith('id: ')) continue;
                        if (!trimmed.startsWith('data: ')) continue;
                        const dataStr = trimmed.slice(6);
                        if (!dataStr || dataStr === '{}') continue;
                        try {
                            const obj = JSON.parse(dataStr);
                            obj._event = currentEvent;
                            sendChunk(JSON.stringify(obj));
                        } catch (e) {}
                    }
                }
                sendChunk('__DONE__');
            } catch (e) {
                if (e && e.name === 'AbortError') {
                    sendChunk('__DONE__');
                } else {
                    sendChunk('__ERROR__:' + (e && e.message ? e.message : String(e)));
                }
            } finally {
                if (reader) {
                    try { await reader.cancel(); } catch (e) {}
                }
                controllers.delete(requestId);
            }
        }"""

        payload_json = json.dumps(payload, ensure_ascii=False)
        await self._bidi.call_function(
            self._context_id,
            js_fn,
            [
                _to_bidi_local_value(url),
                _to_bidi_local_value(payload_json),
                _to_bidi_local_value(request_id),
                {"type": "channel", "value": {"channel": channel_name}},
            ],
            await_promise=True,
            timeout=200.0,
        )

    # ------------------------------------------------------------------
    # High-level Chat Helper & SSE Parsing
    # ------------------------------------------------------------------

    @_serialized_browser_operation
    async def chat(
        self,
        text: str,
        conversation_id: Optional[str] = None,
        bot_id: Optional[str] = None,
        use_deep_think: int = 0,
    ) -> Dict[str, Any]:
        """Send message, collect full response. Returns {text, conversation_id}."""
        full_text = ""
        result_conv_id = conversation_id
        async for event in self.chat_completion(
            text,
            conversation_id=conversation_id,
            bot_id=bot_id,
            use_deep_think=use_deep_think,
        ):
            if event.get("error"):
                raise RuntimeError(
                    f"API error {event.get('status')}: {redact_sensitive_text(str(event.get('body', ''))[:200])}"
                )
            if not result_conv_id:
                cid = self.extract_conversation_id(event)
                if cid and cid != "0":
                    result_conv_id = cid
            full_text += self._extract_text(event)
        return {"text": full_text, "conversation_id": result_conv_id}

    @staticmethod
    def _extract_text(event: Dict[str, Any]) -> str:
        event_type = event.get("_event", "")
        if event_type == "CHUNK_DELTA" and "text" in event:
            return event["text"]
        if "patch_op" in event:
            for op in event["patch_op"]:
                pv = op.get("patch_value", {})
                for block in pv.get("content_block", []):
                    tb = block.get("content", {}).get("text_block", {})
                    if tb.get("text"):
                        return tb["text"]
                if op.get("patch_object") == 102:
                    raw = pv.get("content", "")
                    if raw:
                        try:
                            parsed = json.loads(raw)
                            if parsed.get("text"):
                                return parsed["text"]
                        except (json.JSONDecodeError, TypeError):
                            pass
        if event_type == "STREAM_MSG_NOTIFY":
            content = event.get("content", {})
            if isinstance(content, dict):
                for block in content.get("content_block", []):
                    tb = block.get("content", {}).get("text_block", {})
                    if tb.get("text"):
                        return tb["text"]
        return ""

    @staticmethod
    def extract_conversation_id(event: Dict[str, Any]) -> Optional[str]:
        ack = event.get("ack_client_meta", {})
        if ack.get("conversation_id"):
            return ack["conversation_id"]
        meta = event.get("meta", {})
        if meta.get("conversation_id"):
            return meta["conversation_id"]
        return None

    @_serialized_browser_operation
    async def delete_conversation(self, conversation_id: str) -> bool:
        """Delete an ephemeral conversation on Dola via `/im/conversation/batch_del_user_conv`."""
        if not conversation_id or str(conversation_id).strip() in ("0", ""):
            return False
        if not self._ready or not self._bidi or not self._context_id:
            return False

        query_params = self._build_query_params()
        try:
            signed_url = await self._sign_url("/im/conversation/batch_del_user_conv", query_params)
        except Exception as exc:
            log.warning("Failed to sign delete url for %s: %s", conversation_id, exc)
            return False

        payload = {
            "cmd": 4171,
            "uplink_body": {
                "batch_delete_user_conversation_uplink_body": {
                    "conversation_id": [str(conversation_id)],
                    "delete_all": False,
                    "conversation_type": 3,
                }
            },
            "sequence_id": str(uuid.uuid4()),
            "channel": 2,
            "version": "1",
        }

        try:
            res = await self._bidi.call_function(
                self._context_id,
                """async (url, payloadJson) => {
                    const csrfMatch = document.cookie.match(/passport_csrf_token=([^;]+)/);
                    const csrf = csrfMatch ? csrfMatch[1] : "";
                    try {
                        const resp = await fetch(url, {
                            method: "POST",
                            headers: {
                                "Content-Type": "application/json; encoding=utf-8",
                                "x-tt-passport-csrf-token": csrf,
                                "agw-js-conv": "str",
                            },
                            body: payloadJson,
                            credentials: "include",
                        });
                        return { status: resp.status, body: await resp.text() };
                    } catch (e) {
                        return { error: e.message };
                    }
                }""",
                [_to_bidi_local_value(signed_url), _to_bidi_local_value(json.dumps(payload))],
                await_promise=True,
                timeout=20.0,
            ) or {}
            if res.get("error"):
                return False
            data = json.loads(res.get("body", "{}"))
            if data.get("status_code") == 0:
                log.info("Deleted ephemeral Dola conversation %s", conversation_id)
                return True
            return False
        except Exception as exc:
            log.warning("delete_conversation %s exception: %s", conversation_id, exc)
            return False

    # ------------------------------------------------------------------
    # Samantha Endpoint (Image / Video / Music Generation)
    # ------------------------------------------------------------------

    @_serialized_browser_operation
    async def _samantha_request(self, payload: Dict[str, Any], timeout: float = 120) -> str:
        """Send a request to Dola `/samantha/chat/completion` via in-browser `fetch()`."""
        if not self._ready or not self._bidi or not self._context_id:
            raise RuntimeError("Camoufox browser not ready - login to Dola first")

        query_params = self._build_query_params()
        url = await self._sign_url("/samantha/chat/completion", query_params)
        payload_json = json.dumps(payload, ensure_ascii=False)
        timeout_ms = int(timeout * 1000)

        log.info("POST %s [Camoufox fetch, timeout=%ds]", url.split("?")[0], timeout)
        result = await self._bidi.call_function(
            self._context_id,
            """async (url, payloadJson, timeoutMs) => {
                const csrf = document.cookie.match(/passport_csrf_token=([^;]+)/);
                const csrfToken = csrf ? csrf[1] : '';
                const headers = { 'Content-Type': 'application/json', 'agw-js-conv': 'str' };
                if (csrfToken) headers['x-tt-passport-csrf-token'] = csrfToken;
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeoutMs);
                try {
                    const res = await fetch(url, {
                        method: 'POST',
                        headers: headers,
                        body: payloadJson,
                        credentials: 'include',
                        signal: controller.signal,
                    });
                    clearTimeout(timer);
                    if (!res.ok) {
                        return { error: true, status: res.status, body: (await res.text()).slice(0, 500) };
                    }
                    return { error: false, body: await res.text() };
                } catch (e) {
                    clearTimeout(timer);
                    return { error: true, status: 0, body: e.message };
                }
            }""",
            [
                _to_bidi_local_value(url),
                _to_bidi_local_value(payload_json),
                _to_bidi_local_value(timeout_ms),
            ],
            await_promise=True,
            timeout=timeout + 15.0,
        ) or {"error": True, "status": 0, "body": "empty BiDi response"}

        if result.get("error"):
            raise RuntimeError(
                f"samantha/chat/completion failed ({result.get('status', 0)}): "
                f"{redact_sensitive_text(str(result.get('body', ''))[:500])}"
            )

        body = str(result.get("body") or "")
        if body.lstrip().startswith("{"):
            try:
                err = json.loads(body)
                if isinstance(err, dict) and "code" in err:
                    raise RuntimeError(
                        f"samantha auth error: code={err.get('code')} "
                        f"msg={err.get('msg') or err.get('message', '')}"
                    )
            except json.JSONDecodeError:
                pass
        return body

    @staticmethod
    def _parse_samantha_sse(raw: str) -> List[Dict[str, Any]]:
        events = []
        for block in raw.split("\n\n"):
            if not block.strip():
                continue
            data_str = ""
            for line in block.strip().split("\n"):
                if line.startswith("data:"):
                    data_str = line[5:].strip()
            if not data_str:
                continue
            try:
                events.append(json.loads(data_str))
            except json.JSONDecodeError:
                continue
        return events

    @staticmethod
    def _extract_images_from_event(event: Any) -> List[Dict[str, Any]]:
        """Recursively extract generated image descriptors from Dola `/chat/completion` SSE events."""
        found: List[Dict[str, Any]] = []
        seen_urls: set = set()

        def _walk(node: Any) -> None:
            if isinstance(node, list):
                for item in node:
                    _walk(item)
                return
            if isinstance(node, str):
                if node.startswith("{") or node.startswith("["):
                    try:
                        _walk(json.loads(node))
                    except Exception:
                        pass
                return
            if not isinstance(node, dict):
                return

            ori = node.get("image_ori")
            raw_img = node.get("image_raw")
            preview = node.get("image_preview") or node.get("image_thumb")
            if isinstance(ori, dict) or isinstance(raw_img, dict):
                ori_d = ori if isinstance(ori, dict) else {}
                raw_d = raw_img if isinstance(raw_img, dict) else {}
                prev_d = preview if isinstance(preview, dict) else {}
                url = str(ori_d.get("url") or raw_d.get("url") or prev_d.get("url") or "").strip()
                if url and url.startswith("http") and url not in seen_urls:
                    seen_urls.add(url)
                    found.append(
                        {
                            "key": str(node.get("key") or node.get("uri") or ""),
                            "url": url,
                            "raw_url": str(raw_d.get("url") or url),
                            "preview_url": str(prev_d.get("url") or ""),
                            "width": int(ori_d.get("width") or raw_d.get("width") or 0),
                            "height": int(ori_d.get("height") or raw_d.get("height") or 0),
                            "format": str(ori_d.get("format") or raw_d.get("format") or "png"),
                        }
                    )
                return

            for val in node.values():
                _walk(val)

        _walk(event)
        return found

    @_serialized_browser_operation
    async def generate_image(
        self,
        prompt: str,
        ratio: Optional[str] = None,
        ref_image_key: Optional[str] = None,
        ref_image_keys: Optional[List[str]] = None,
        memory_type: Optional[int] = None,
        save_history: Optional[bool] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate or edit images on Dola via `/chat/completion` using native `chat_ability` (Seedream 4.5 / 5.0 Pro)."""
        dola_model = resolve_dola_image_model(model)
        eff_ratio = (ratio or "auto").strip()

        keys: List[str] = []
        if ref_image_keys:
            if isinstance(ref_image_keys, str):
                keys.append(ref_image_keys)
            elif isinstance(ref_image_keys, (list, tuple)):
                keys.extend([k for k in ref_image_keys if k])
        if ref_image_key and ref_image_key not in keys:
            keys.insert(0, ref_image_key)

        image_attachments: List[Dict[str, Any]] = []
        for k in keys:
            cdn_url = ""
            try:
                if self._http is not None:
                    file_url = await self._sign_url(f"{DOLA_URL}/alice/message/get_file_url", self._build_query_params())
                    headers = self._build_headers(await self._get_cookies_string())
                    resp = await self._http.post(
                        file_url,
                        headers=headers,
                        json={"uris": [k], "type": "image", "format": "png", "expire_second": 3600},
                        timeout=20,
                    )
                    if resp.status_code == 200:
                        furls = resp.json().get("data", {}).get("file_urls", [])
                        if furls:
                            cdn_url = furls[0].get("main_url") or furls[0].get("url", "")
            except Exception:
                pass
            image_attachments.append(
                {
                    "uri": k,
                    "cdn_url": cdn_url,
                    "name": "ref_image.png",
                    "format": "png",
                    "width": 1024,
                    "height": 1024,
                }
            )

        clean_prompt = prompt.strip()
        full_prompt = f"生成图片：{clean_prompt}"
        if ratio:
            full_prompt = f"{full_prompt} ({eff_ratio})"

        ability_param = json.dumps(
            {
                "ability_param": {
                    "model": dola_model,
                    "ratio": eff_ratio,
                    "input_box_content": {
                        "user_input_content": clean_prompt,
                        "reply_message_format": "生成图片：%s",
                    },
                },
                "ability_type": 1,
            },
            ensure_ascii=False,
        )
        chat_ability = {"ability_type": 3, "ability_param": ability_param}

        eff_mem_type = resolve_memory_type(memory_type, save_history)
        images: List[Dict[str, Any]] = []
        seen_urls: set = set()
        conv_id: Optional[str] = None
        input_uris = set(keys)
        input_urls = {a["cdn_url"] for a in image_attachments if a.get("cdn_url")}

        async for event in self.chat_completion(
            text=full_prompt,
            image_attachments=image_attachments or None,
            use_deep_think=0,
            chat_ability=chat_ability,
        ):
            if event.get("error"):
                raise RuntimeError(
                    f"generate_image error ({event.get('status', 0)}): {str(event.get('body', ''))[:500]}"
                )
            cid = self.extract_conversation_id(event)
            if cid:
                conv_id = cid
            for img in self._extract_images_from_event(event):
                if img.get("key") in input_uris or img.get("url") in input_urls:
                    continue
                if img["url"] not in seen_urls:
                    seen_urls.add(img["url"])
                    images.append(img)

        if conv_id and eff_mem_type == 2:
            try:
                await self.delete_conversation(conv_id)
            except Exception:
                pass

        return {"images": images, "prompt": prompt, "model": dola_model}

    @_serialized_browser_operation
    async def generate_music(
        self,
        prompt: str,
        lyric: Optional[str] = None,
        genre: Optional[str] = None,
        memory_type: Optional[int] = None,
        save_history: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Generate music via Dola `/samantha/chat/completion`."""
        content_data: Dict[str, Any] = {"text": prompt}
        if lyric:
            content_data["lyric"] = lyric
        if genre:
            content_data["genre"] = genre

        message: Dict[str, Any] = {
            "content": json.dumps(content_data, ensure_ascii=False),
            "content_type": 2005,
            "attachments": [],
            "references": [],
            "skill": {
                "skill_type": 9,
                "skill_type_no_default": 9,
                "skill_id": "9",
                "skill_id_no_default": "9",
            },
        }
        eff_mem_type = resolve_memory_type(memory_type, save_history)
        payload = {
            "messages": [message],
            "completion_option": {
                "is_regen": False,
                "with_suggest": True,
                "need_create_conversation": True,
                "launch_stage": 1,
                "is_replace": False,
                "is_delete": False,
                "is_ai_playground": False,
                "memory_type": eff_mem_type,
                "message_from": 0,
                "use_deep_think": False,
                "use_auto_cot": False,
                "resend_for_regen": False,
                "enable_commerce_credit": False,
                "action_bar_skill_id": 9,
            },
            "evaluate_option": {"web_ab_params": ""},
            "local_conversation_id": str(uuid.uuid4()),
            "local_message_id": str(uuid.uuid4()),
        }

        raw = await self._samantha_request(payload, timeout=300)
        final_content = None
        for data in self._parse_samantha_sse(raw):
            et = data.get("event_type")
            if et == 2005:
                raise RuntimeError(f"generate_music error: {str(data.get('event_data', ''))[:500]}")
            if et != 2001:
                continue
            ed = data.get("event_data", {})
            if isinstance(ed, str):
                try:
                    ed = json.loads(ed)
                except json.JSONDecodeError:
                    continue
            msg = ed.get("message", {})
            if isinstance(msg, str):
                try:
                    msg = json.loads(msg)
                except json.JSONDecodeError:
                    continue
            if msg.get("content_type") not in (2006, 2004):
                continue
            content_raw = msg.get("content", "")
            final_content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw

        if not final_content:
            return {"tracks": [], "prompt": prompt}

        tasks = final_content.get("tasks", {})
        tasks_list = list(tasks.values()) if isinstance(tasks, dict) else (tasks if isinstance(tasks, list) else [])
        tracks = []
        for task in tasks_list:
            if not isinstance(task, dict):
                continue
            audio_url = ""
            duration = 0.0
            vm_str = task.get("video_model", "")
            if vm_str:
                try:
                    vm = json.loads(vm_str) if isinstance(vm_str, str) else vm_str
                    duration = vm.get("video_duration", 0.0)
                    for _q, vinfo in (vm.get("video_list") or {}).items():
                        b64_url = vinfo.get("main_url", "")
                        if b64_url:
                            audio_url = base64.b64decode(b64_url).decode("utf-8", errors="replace")
                            break
                except Exception:
                    pass
            cover = task.get("cover", {})
            cover_url = (cover.get("image_ori") or {}).get("url", "") if isinstance(cover, dict) else ""
            if audio_url or task.get("title"):
                tracks.append(
                    {
                        "audio_url": audio_url,
                        "title": task.get("title", ""),
                        "lyrics": task.get("lyric", ""),
                        "duration": duration,
                        "cover_url": cover_url,
                    }
                )
        return {"tracks": tracks, "prompt": prompt}

    @staticmethod
    def _extract_videos_from_event(event: Any) -> List[Dict[str, Any]]:
        """Recursively extract generated video descriptors from Dola `/chat/completion` SSE events."""
        found: List[Dict[str, Any]] = []
        seen_urls: set = set()

        def _walk(node: Any) -> None:
            if isinstance(node, list):
                for item in node:
                    _walk(item)
                return
            if isinstance(node, str):
                if node.startswith("{") or node.startswith("["):
                    try:
                        _walk(json.loads(node))
                    except Exception:
                        pass
                return
            if not isinstance(node, dict):
                return

            video_url = ""
            for k in ("video_url", "play_url", "download_url"):
                v = node.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    video_url = v
                    break
            if not video_url and node.get("video_model"):
                try:
                    vm = (
                        json.loads(node["video_model"])
                        if isinstance(node["video_model"], str)
                        else node["video_model"]
                    )
                    for _q, vinfo in (vm.get("video_list") or {}).items():
                        if isinstance(vinfo, dict) and vinfo.get("main_url"):
                            raw_u = str(vinfo["main_url"])
                            video_url = (
                                raw_u
                                if raw_u.startswith("http")
                                else base64.b64decode(raw_u).decode("utf-8", errors="replace")
                            )
                            break
                except Exception:
                    pass
            if not video_url and isinstance(node.get("video"), dict):
                vobj = node["video"]
                for k in ("video_url", "play_url", "url", "main_url"):
                    val = vobj.get(k)
                    if isinstance(val, str) and val.startswith("http"):
                        video_url = val
                        break
            if video_url and video_url not in seen_urls:
                seen_urls.add(video_url)
                cover_url = str(
                    node.get("cover_url")
                    or (node.get("cover") or {}).get("url")
                    or (node.get("poster") or {}).get("url")
                    or ""
                )
                found.append(
                    {
                        "video_url": video_url,
                        "cover_url": cover_url,
                        "width": int(node.get("width") or 0),
                        "height": int(node.get("height") or 0),
                        "duration": float(node.get("duration") or 0.0),
                    }
                )
                return

            for val in node.values():
                _walk(val)

        _walk(event)
        return found

    @_serialized_browser_operation
    async def generate_video(
        self,
        prompt: str,
        ratio: Optional[str] = None,
        ref_image_key: Optional[str] = None,
        ref_image_keys: Optional[List[str]] = None,
        memory_type: Optional[int] = None,
        save_history: Optional[bool] = None,
        model: Optional[str] = None,
        duration: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Generate video on Dola via `/chat/completion` using native Seedance `chat_ability` (`ability_type=17`)."""
        dola_model = resolve_dola_video_model(model)
        eff_ratio = (ratio or "auto").strip()
        eff_duration = int(duration or 10)
        if eff_duration not in (5, 10):
            eff_duration = 10 if eff_duration >= 8 else 5

        keys: List[str] = []
        if ref_image_keys:
            if isinstance(ref_image_keys, str):
                keys.append(ref_image_keys)
            elif isinstance(ref_image_keys, (list, tuple)):
                keys.extend([k for k in ref_image_keys if k])
        if ref_image_key and ref_image_key not in keys:
            keys.insert(0, ref_image_key)

        image_attachments: List[Dict[str, Any]] = []
        for k in keys:
            cdn_url = ""
            try:
                if self._http is not None:
                    file_url = await self._sign_url(f"{DOLA_URL}/alice/message/get_file_url", self._build_query_params())
                    headers = self._build_headers(await self._get_cookies_string())
                    resp = await self._http.post(
                        file_url,
                        headers=headers,
                        json={"uris": [k], "type": "image", "format": "png", "expire_second": 3600},
                        timeout=20,
                    )
                    if resp.status_code == 200:
                        furls = resp.json().get("data", {}).get("file_urls", [])
                        if furls:
                            cdn_url = furls[0].get("main_url") or furls[0].get("url", "")
            except Exception:
                pass
            image_attachments.append(
                {
                    "uri": k,
                    "cdn_url": cdn_url,
                    "name": "ref_image.png",
                    "format": "png",
                    "width": 1024,
                    "height": 1024,
                }
            )

        clean_prompt = prompt.strip()
        full_prompt = f"生成视频：{clean_prompt}，{eff_duration}s"
        ability_param = json.dumps(
            {
                "ratio": eff_ratio,
                "model": dola_model,
                "duration": eff_duration,
                "input_box_content": {
                    "user_input_content": clean_prompt,
                    "reply_message_format": "生成视频：%s",
                },
            },
            ensure_ascii=False,
        )
        chat_ability = {"ability_type": 17, "ability_param": ability_param}

        quota_markers = (
            "daily limit",
            "quota exceeded",
            "limit reached",
            "out of credits",
            "upgrade",
            "subscribe",
            "今日生成次数已达上限",
            "次数已达上限",
            "额度已用尽",
            "达到今日上限",
            "次数超限",
        )

        eff_mem_type = resolve_memory_type(memory_type, save_history)
        videos: List[Dict[str, Any]] = []
        seen_urls: set = set()
        text_parts: List[str] = []
        conv_id: Optional[str] = None
        task_id: Optional[str] = None

        def _find_task_id(node: Any) -> Optional[str]:
            if isinstance(node, dict):
                at = node.get("async_task")
                if isinstance(at, dict) and at.get("id"):
                    return str(at["id"])
                if node.get("task_id") and isinstance(node.get("task_id"), (str, int)):
                    return str(node["task_id"])
                for v in node.values():
                    found = _find_task_id(v)
                    if found:
                        return found
            elif isinstance(node, list):
                for item in node:
                    found = _find_task_id(item)
                    if found:
                        return found
            return None

        async for event in self.chat_completion(
            text=full_prompt,
            image_attachments=image_attachments or None,
            use_deep_think=0,
            chat_ability=chat_ability,
        ):
            if event.get("error"):
                detail = str(event.get("body", ""))
                if any(k in detail.lower() for k in quota_markers):
                    if self.account_manager:
                        self.account_manager.mark_quota_exceeded(
                            account_id=self.account_manager.active_account_id,
                            feature="video",
                        )
                    raise VideoQuotaExceeded("Video generation quota exceeded for active account")
                raise RuntimeError(f"generate_video error ({event.get('status', 0)}): {detail[:500]}")
            cid = self.extract_conversation_id(event)
            if cid:
                conv_id = cid
            if not task_id:
                task_id = _find_task_id(event)
            txt = self._extract_text(event)
            if txt:
                text_parts.append(txt)
            for v in self._extract_videos_from_event(event):
                if v["video_url"] not in seen_urls:
                    seen_urls.add(v["video_url"])
                    videos.append(v)

        full_text = "".join(text_parts)
        if not videos and any(k in full_text.lower() for k in quota_markers):
            if self.account_manager:
                self.account_manager.mark_quota_exceeded(
                    account_id=self.account_manager.active_account_id,
                    feature="video",
                )
            raise VideoQuotaExceeded("Video generation quota exceeded for active account")

        # If Dola queued an async video task (1-3 min), stream `/chat/async/chunk_stream` until completion
        if not videos and task_id and self._bidi and self._context_id:
            log.info("Dola Seedance video queued async task_id=%s; streaming async result...", task_id)
            polled = await self._poll_async_video_stream(task_id, conv_id=conv_id, timeout=240.0)
            for v in polled:
                if v["video_url"] not in seen_urls:
                    seen_urls.add(v["video_url"])
                    videos.append(v)

        if conv_id and eff_mem_type == 2 and (videos or not task_id):
            try:
                await self.delete_conversation(conv_id)
            except Exception:
                pass

        return {
            "videos": videos,
            "prompt": prompt,
            "model": dola_model,
            "task_id": task_id,
            "conversation_id": conv_id,
            "message": full_text,
        }

    async def _poll_async_video_stream(
        self, task_id: str, *, conv_id: Optional[str] = None, timeout: float = 240.0
    ) -> List[Dict[str, Any]]:
        """Stream Dola async video generation progress over signed `/chat/async/chunk_stream`."""
        if not self._bidi or not self._context_id:
            return []
        videos: List[Dict[str, Any]] = []
        seen: set = set()
        for endpoint in ("/chat/async/chunk_stream", "/samantha/chat/async/stream"):
            try:
                signed_url = await self._sign_url(endpoint, self._build_query_params())
                req_id = f"vid_{uuid.uuid4().hex[:12]}"
                channel = self._bidi.open_channel(f"dola_vid_{req_id}")
                payload: Dict[str, Any] = {"task_id": task_id, "event_id": 0}
                if conv_id:
                    payload["conversation_id"] = conv_id
                eval_task = asyncio.create_task(
                    self._browser_fetch_stream(signed_url, payload, req_id, channel.name)
                )
                try:
                    while True:
                        chunk_json = await channel.next_text(timeout=timeout)
                        if chunk_json == "__DONE__" or chunk_json.startswith(("__ERROR__:", "__HTTP_ERROR__:")):
                            break
                        try:
                            ev = json.loads(chunk_json)
                        except Exception:
                            continue
                        for v in self._extract_videos_from_event(ev):
                            if v["video_url"] not in seen:
                                seen.add(v["video_url"])
                                videos.append(v)
                finally:
                    channel.close()
                    if not eval_task.done():
                        eval_task.cancel()
                    await asyncio.gather(eval_task, return_exceptions=True)
                if videos:
                    return videos
            except Exception as exc:
                log.warning("Async video stream %s failed: %s", endpoint, redact_sensitive_text(str(exc)))
        return videos

    @_serialized_browser_operation
    async def _poll_video_result(
        self, task_id: str, prompt: str, timeout: float = 300
    ) -> Dict[str, Any]:
        raw = await self._samantha_request({"task_id": task_id, "event_id": 0}, timeout=timeout)
        videos = []
        for data in self._parse_samantha_sse(raw):
            if data.get("event_type") != 2001:
                continue
            ed = data.get("event_data", {})
            if isinstance(ed, str):
                try:
                    ed = json.loads(ed)
                except json.JSONDecodeError:
                    continue
            msg = ed.get("message", {})
            if isinstance(msg, str):
                try:
                    msg = json.loads(msg)
                except json.JSONDecodeError:
                    continue
            if msg.get("content_type") != 2021:
                continue
            content_raw = msg.get("content", "")
            content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
            for item in content.get("data", [content]):
                if not isinstance(item, dict):
                    continue
                video_url = item.get("video_url", "") or item.get("url", "")
                if not video_url and item.get("video_model"):
                    try:
                        vm = json.loads(item["video_model"]) if isinstance(item["video_model"], str) else item["video_model"]
                        for _q, vinfo in (vm.get("video_list") or {}).items():
                            if vinfo.get("main_url"):
                                video_url = base64.b64decode(vinfo["main_url"]).decode("utf-8", errors="replace")
                                break
                    except Exception:
                        pass
                cover_url = item.get("cover_url", "") or (item.get("cover") or {}).get("url", "")
                if video_url:
                    videos.append(
                        {
                            "video_url": video_url,
                            "cover_url": cover_url,
                            "width": item.get("width", 0),
                            "height": item.get("height", 0),
                            "duration": item.get("duration", 0.0),
                        }
                    )
        return {"videos": videos, "prompt": prompt}

    # ------------------------------------------------------------------
    # File & Image Upload (Dola ByteDance Overseas TOS / ImageX)
    # ------------------------------------------------------------------

    @_serialized_browser_operation
    async def upload_file(
        self,
        file_data: bytes,
        filename: str = "document.txt",
    ) -> Dict[str, Any]:
        """Upload a file to Dola storage (ByteDance TOS via ImageX proxy)."""
        if not self._ready or self._http is None:
            raise RuntimeError("Camoufox browser not ready - login to Dola first")

        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        file_size = len(file_data)
        crc32 = format(zlib.crc32(file_data) & 0xFFFFFFFF, "08x")

        query_params = self._build_query_params()
        signed_url = await self._sign_url(f"{DOLA_URL}/alice/resource/prepare_upload", query_params)
        cookie_str = await self._get_cookies_string()
        headers = self._build_headers(cookie_str)

        resp = await self._http.post(
            signed_url,
            headers=headers,
            json={"tenant_id": "5", "scene_id": "5", "resource_type": 1},
            timeout=30,
        )
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"prepare_upload failed: {body.get('msg', body)}")

        data = body["data"]
        service_id = data["service_id"]
        auth_token = data["upload_auth_token"]
        ak = auth_token["access_key"]
        sk = auth_token["secret_key"]
        st = auth_token["session_token"]
        # Use dynamic region from prepare_upload if returned, else overseas Singapore default
        aws_region = (
            str(data.get("region") or data.get("upload_region") or auth_token.get("region") or "").strip()
            or DOLA_IMAGEX_REGION
        )

        def _aws_sign_v4(method: str, url: str, req_body: Union[str, bytes]) -> Dict[str, str]:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            path = parsed.path or "/"
            now = datetime.now(timezone.utc)
            amz_date = now.strftime("%Y%m%dT%H%M%SZ")
            date_stamp = now.strftime("%Y%m%d")
            qparams = parse_qs(parsed.query, keep_blank_values=True)
            sorted_qp = sorted((k, v[0] if v else "") for k, v in qparams.items())
            canonical_qs = "&".join(
                f"{url_quote(k, safe='~')}={url_quote(v, safe='~')}" for k, v in sorted_qp
            )
            h2s = {"host": host, "x-amz-date": amz_date}
            if st:
                h2s["x-amz-security-token"] = st
            signed_h = ";".join(sorted(h2s.keys()))
            canonical_h = "".join(f"{k}:{v}\n" for k, v in sorted(h2s.items()))
            body_b = req_body if isinstance(req_body, bytes) else req_body.encode()
            payload_hash = hashlib.sha256(body_b).hexdigest()
            cr = f"{method}\n{path}\n{canonical_qs}\n{canonical_h}\n{signed_h}\n{payload_hash}"
            scope = f"{date_stamp}/{aws_region}/{DOLA_IMAGEX_SERVICE}/aws4_request"
            cr_hash = hashlib.sha256(cr.encode()).hexdigest()
            sts = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{cr_hash}"

            def _s(key: bytes, msg: str) -> bytes:
                return hmac_mod.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

            k_d = _s(f"AWS4{sk}".encode("utf-8"), date_stamp)
            k_r = _s(k_d, aws_region)
            k_sv = _s(k_r, DOLA_IMAGEX_SERVICE)
            k_sg = _s(k_sv, "aws4_request")
            sig = hmac_mod.new(k_sg, sts.encode("utf-8"), hashlib.sha256).hexdigest()
            result = {
                "Authorization": f"AWS4-HMAC-SHA256 Credential={ak}/{scope}, SignedHeaders={signed_h}, Signature={sig}",
                "x-amz-date": amz_date,
                "x-amz-content-sha256": payload_hash,
            }
            if st:
                result["x-amz-security-token"] = st
            return result

        file_ext = f".{ext}" if ext else ""
        apply_url = (
            f"{DOLA_URL}/top/v1?"
            f"Action=ApplyImageUpload&Version=2018-08-01"
            f"&ServiceId={service_id}&NeedFallback=true"
            f"&FileSize={file_size}&FileExtension={file_ext}&s=jdnfglwfkl"
        )
        sign_h = _aws_sign_v4("GET", apply_url, "")
        sign_h["Cookie"] = cookie_str
        resp = await self._http.get(apply_url, headers=sign_h, timeout=30)
        result_data = resp.json().get("Result")
        if not result_data:
            raise RuntimeError(f"ApplyImageUpload failed: {resp.json()}")

        upload_addr = result_data["UploadAddress"]
        store_info = upload_addr["StoreInfos"][0]
        store_uri = store_info["StoreUri"]
        tos_auth = store_info["Auth"]
        session_key = upload_addr["SessionKey"]
        upload_hosts = upload_addr.get("UploadHosts", [])

        tos_host = upload_hosts[0] if upload_hosts else DOLA_TOS_FALLBACK_HOST
        upload_url = f"https://{tos_host}/upload/v1/{store_uri}"
        resp = await self._http.post(
            upload_url,
            content=file_data,
            headers={"Authorization": tos_auth, "Content-CRC32": crc32},
            timeout=120,
        )
        tos_resp = resp.json()
        if tos_resp.get("code") != 2000:
            raise RuntimeError(f"TOS upload failed: {tos_resp}")

        commit_url = f"{DOLA_URL}/top/v1?Action=CommitImageUpload&Version=2018-08-01&ServiceId={service_id}"
        commit_body = json.dumps({"SessionKey": session_key})
        sign_h2 = _aws_sign_v4("POST", commit_url, commit_body)
        sign_h2["Content-Type"] = "application/json"
        sign_h2["Cookie"] = cookie_str
        resp = await self._http.post(commit_url, content=commit_body, headers=sign_h2, timeout=30)
        body = resp.json()
        results = body.get("Result", {}).get("Results", [])
        if not results or results[0].get("UriStatus") != 2000:
            raise RuntimeError(f"CommitImageUpload failed: {body}")

        log.info("File uploaded to Dola TOS: %s -> %s", filename, store_uri)
        return {"uri": store_uri, "name": filename, "size": file_size, "file_type": ext}

    @_serialized_browser_operation
    async def get_file_download_url(self, uri: str, expire_seconds: int = 3600) -> str:
        if not self._ready or self._http is None:
            raise RuntimeError("Camoufox browser not ready - login to Dola first")
        signed_url = await self._sign_url(f"{DOLA_URL}/alice/message/get_file_url", self._build_query_params())
        headers = self._build_headers(await self._get_cookies_string())
        ext = uri.rsplit(".", 1)[-1] if "." in uri else ""
        resp = await self._http.post(
            signed_url,
            headers=headers,
            json={"uris": [uri], "type": "file", "format": ext, "expire_second": expire_seconds},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"get_file_url failed ({resp.status_code})")
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"get_file_url error: {body.get('msg', body)}")
        file_urls = body.get("data", {}).get("file_urls", [])
        if not file_urls:
            raise RuntimeError("get_file_url returned no file_urls")
        return file_urls[0].get("main_url", "")

    @_serialized_browser_operation
    async def upload_image(self, image_bytes: bytes, filename: str = "image.png") -> Dict[str, Any]:
        """Upload an image to Dola and resolve CDN metadata."""
        if not self._ready or self._http is None:
            raise RuntimeError("Camoufox browser not ready - login to Dola first")
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
        signed_url = await self._sign_url(f"{DOLA_URL}/samantha/pages/upload_image", self._build_query_params())
        headers = self._build_headers(await self._get_cookies_string())
        headers.pop("Content-Type", None)
        files = {
            "data": (filename, image_bytes, f"image/{ext}"),
            "file_type": (None, ext),
        }
        resp = await self._http.post(signed_url, headers=headers, files=files, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"Image upload failed ({resp.status_code})")
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"Image upload error: {body.get('msg', body)}")
        uri = body.get("data", {}).get("uri", "")
        if not uri:
            raise RuntimeError(f"Image upload returned no uri: {body}")

        file_url = await self._sign_url(f"{DOLA_URL}/alice/message/get_file_url", self._build_query_params())
        headers2 = self._build_headers(await self._get_cookies_string())
        resp2 = await self._http.post(
            file_url,
            headers=headers2,
            json={"uris": [uri], "type": "image", "format": ext, "expire_second": 3600},
            timeout=30,
        )
        if resp2.status_code != 200:
            raise RuntimeError(f"get_file_url failed ({resp2.status_code})")
        body2 = resp2.json()
        if body2.get("code") != 0:
            raise RuntimeError(f"get_file_url error: {body2.get('msg', body2)}")
        file_urls = body2.get("data", {}).get("file_urls", [])
        if not file_urls:
            raise RuntimeError("get_file_url returned no file_urls")
        info = file_urls[0]

        img_w, img_h = 1024, 1024
        try:
            import io
            from PIL import Image

            with Image.open(io.BytesIO(image_bytes)) as pil_img:
                img_w, img_h = pil_img.size
                if pil_img.format:
                    ext = pil_img.format.lower()
        except Exception:
            pass

        return {
            "uri": info.get("uri", uri),
            "cdn_url": info.get("main_url") or info.get("url", ""),
            "name": filename,
            "format": ext,
            "width": int(info.get("width") or img_w),
            "height": int(info.get("height") or img_h),
        }

    @_serialized_browser_operation
    async def chat_with_file(
        self,
        text: str,
        file_uri: Union[str, List[Dict[str, Any]]],
        file_name: str = "file.txt",
        file_size: int = 0,
        use_deep_think: int = 0,
    ) -> Dict[str, Any]:
        """Chat on Dola with attached files or images."""
        file_refs = file_uri if isinstance(file_uri, list) else [
            {"uri": file_uri, "name": file_name, "size": file_size}
        ]
        file_atts: List[Dict[str, Any]] = []
        image_atts: List[Dict[str, Any]] = []
        for ref in file_refs:
            fname = str(ref.get("name", "")).lower()
            fext = fname.rsplit(".", 1)[-1] if "." in fname else ""
            if fext in ("png", "jpg", "jpeg", "webp", "gif", "bmp") or ref.get("is_image"):
                image_atts.append(ref)
            else:
                file_atts.append(ref)

        full_text = ""
        conv_id = None
        async for event in self.chat_completion(
            text,
            use_deep_think=use_deep_think,
            file_attachments=file_atts or None,
            image_attachments=image_atts or None,
        ):
            if event.get("error"):
                raise RuntimeError(
                    f"chat_with_file error {event.get('status')}: {redact_sensitive_text(str(event.get('body', ''))[:200])}"
                )
            full_text += self._extract_text(event)
            if not conv_id:
                cid = self.extract_conversation_id(event)
                if cid and cid != "0":
                    conv_id = cid
        return {"text": full_text, "conversation_id": conv_id}
