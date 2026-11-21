"""Isolated Camoufox browser login & per-account profile management sessions (flow2api-Camoufox architecture).

Provides two strictly isolated session modes:
- `mode="add"`: Launches a brand-new ephemeral Camoufox profile in `<data_dir>/_staging/login_<sid>/`
  with its own fresh Windows Firefox fingerprint and explicit per-session proxy (via `LocalSocks5Relay`
  when auth/SOCKS5 is used). Zero cookies or history are shared with existing accounts. Once Dola
  login succeeds (auto-detected or manually confirmed), the isolated profile + fingerprint + startupCache
  are persisted into `<data_dir>/accounts/<new_account_id>/` and registered in `AccountManager`.
- `mode="manage"`: Releases any runtime browser holding the target account's profile lock, then opens
  that account's OWN isolated directory `<data_dir>/accounts/<account_id>/` using that account's own
  proxy & fingerprint so the user can inspect history, re-login, or solve captcha without affecting any
  other account. Closing or clicking Sync persists updated cookies and clears captcha/invalid states.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import shutil
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional
import uuid

from .bidi_transport import BiDiClient, LocalSocks5Relay, wait_for_bidi_endpoint
from .camoufox_fingerprint import (
    _start_stdout_drain,
    build_camoufox_env,
    ensure_camoufox_executable,
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
    DOLA_DEFAULT_LOCALE,
    DOLA_DEFAULT_TIMEZONE,
    DOLA_TEA_CACHE_KEY,
    get_env,
)
from .proxy_utils import mask_proxy_url, normalize_proxy_url, proxy_needs_relay
from .redaction import redact_sensitive_text

log = logging.getLogger("doubao2api.camoufox_login")


DOLA_EXTRACT_IDENTITY_SCRIPT = f"""() => {{
    let deviceId = '', webId = '', region = '', lang = '', displayName = '';
    try {{
        const sam = JSON.parse(localStorage.getItem('samantha_web_web_id') || '{{}}');
        if (sam.web_id) {{ deviceId = String(sam.web_id); webId = String(sam.web_id); }}
    }} catch (e) {{}}
    try {{
        const tea = JSON.parse(localStorage.getItem('{DOLA_TEA_CACHE_KEY}') || '{{}}');
        if (tea.web_id) webId = String(tea.web_id);
        if (tea.user_unique_id && !deviceId) deviceId = String(tea.user_unique_id);
    }} catch (e) {{}}
    try {{
        const mc = document.cookie.match(/(?:^|;\\s*)flow_user_country=([^;]+)/);
        if (mc) region = decodeURIComponent(mc[1]).trim().toUpperCase();
        const ml = document.cookie.match(/(?:^|;\\s*)i18next=([^;]+)/);
        if (ml) lang = decodeURIComponent(ml[1]).trim();
    }} catch (e) {{}}
    try {{
        // Best-effort account display name from sidebar footer or avatar alt
        const imgs = Array.from(document.querySelectorAll('img[alt]'));
        for (const img of imgs) {{
            const alt = (img.getAttribute('alt') || '').trim();
            if (alt && alt.length >= 2 && alt.length <= 40 && !/logo|dola|icon|banner/i.test(alt)) {{
                displayName = alt;
                break;
            }}
        }}
    }} catch (e) {{}}
    return {{
        url: String(location.href || ''),
        device_id: deviceId,
        web_id: webId,
        region: region,
        lang: lang,
        display_name: displayName,
    }};
}}"""


def persist_isolated_profile_artifacts(source_account_dir: Path, target_account_dir: Path) -> None:
    """Copy a freshly logged-in isolated profile (firefox-profile + fingerprint + startupCache) to target."""
    target_account_dir.mkdir(parents=True, exist_ok=True)

    src_profile = source_account_dir / "firefox-profile"
    dst_profile = target_account_dir / "firefox-profile"
    if src_profile.exists():
        clear_stale_firefox_profile_locks(src_profile)
        prune_camoufox_profile_bloat(src_profile)
        if dst_profile.exists():
            shutil.rmtree(dst_profile, ignore_errors=True)
        shutil.copytree(src_profile, dst_profile, symlinks=False, ignore_dangling_symlinks=True)

    for fname in ("camoufox-fingerprint.json",):
        src_f = source_account_dir / fname
        if src_f.exists():
            shutil.copy2(src_f, target_account_dir / fname)

    src_startup = source_account_dir / "profile-startup"
    dst_startup = target_account_dir / "profile-startup"
    if src_startup.exists():
        if dst_startup.exists():
            shutil.rmtree(dst_startup, ignore_errors=True)
        shutil.copytree(src_startup, dst_startup, symlinks=False, ignore_dangling_symlinks=True)


class DolaLoginSession:
    """One isolated Camoufox browser session for either adding a brand-new account or managing an existing one."""

    def __init__(
        self,
        session_id: str,
        *,
        user_data_root: Path,
        mode: str = "add",  # "add" | "manage"
        account_id: Optional[str] = None,
        account_name: Optional[str] = None,
        proxy_url: Optional[str] = None,
        headless: bool = True,
        timeout_seconds: float = 600.0,
        on_complete: Optional[Callable[["DolaLoginSession", Dict[str, Any]], Awaitable[Dict[str, Any]]]] = None,
        on_release_runtime: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        self.session_id = session_id
        self.user_data_root = Path(user_data_root)
        self.mode = "manage" if mode == "manage" else "add"
        self.account_id = account_id
        self.account_name = (account_name or "").strip()
        self.proxy_url = normalize_proxy_url(proxy_url) if (proxy_url and proxy_url.strip()) else None
        self.headless = True if (headless or not has_display()) else False
        self.timeout_seconds = max(60.0, float(timeout_seconds or 600.0))
        self.on_complete = on_complete
        self.on_release_runtime = on_release_runtime

        self.liveview = CamoufoxLiveView()
        self.status: str = "starting"  # starting | waiting | saving | completed | cancelled | error
        self.message: str = "正在启动隔离 Camoufox 浏览器..."
        self.error: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None
        self.logged_in_detected: bool = False
        self.created_at: float = time.time()
        self.finished_at: Optional[float] = None

        self._work_dir: Optional[Path] = None
        self._profile_dir: Optional[Path] = None
        self._is_staging: bool = self.mode == "add"
        self._lease: Optional[ProfileLease] = None
        self._relay: Optional[LocalSocks5Relay] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._stdout_drain_task: Optional[asyncio.Task] = None
        self._bidi: Optional[BiDiClient] = None
        self._context_id: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._cancel_event = asyncio.Event()
        self._save_event = asyncio.Event()
        self._closed = False

    def set_state(self, status: str, message: str, error: Optional[str] = None) -> None:
        self.status = status
        self.message = message
        if error is not None:
            self.error = error
        if status in ("completed", "cancelled", "error"):
            self.finished_at = time.time()
        self.liveview.set_status(status, message)

    def to_dict(self, *, include_frame: bool = False) -> Dict[str, Any]:
        snap = self.liveview.snapshot(include_frame=include_frame)
        return {
            "session_id": self.session_id,
            "mode": self.mode,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "proxy_masked": mask_proxy_url(self.proxy_url),
            "headless": self.headless,
            "status": self.status,
            "message": self.message,
            "error": self.error,
            "logged_in_detected": self.logged_in_detected,
            "result": self.result,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "liveview": snap,
        }

    def start_background(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())
        return self._task

    def request_save(self) -> None:
        """Signal the session to extract cookies & persist immediately (used in manage mode or manual save)."""
        self._save_event.set()

    def request_cancel(self) -> None:
        self._cancel_event.set()

    async def _extract_dola_cookies(self) -> Dict[str, str]:
        if not self._bidi:
            return {}
        try:
            res = await self._bidi.command("storage.getCookies", {}, timeout=10.0)
        except Exception:
            return {}
        out: Dict[str, str] = {}
        for item in res.get("cookies") or []:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or "")
            if "dola.com" not in domain:
                continue
            name = str(item.get("name") or "")
            val = item.get("value")
            if isinstance(val, dict):
                val = val.get("value", "")
            if name:
                out[name] = str(val or "")
        return out

    async def _extract_identity(self) -> Dict[str, Any]:
        if not self._bidi or not self._context_id:
            return {}
        try:
            val = await self._bidi.call_function(
                self._context_id,
                DOLA_EXTRACT_IDENTITY_SCRIPT,
                await_promise=False,
                timeout=8.0,
            )
            return val if isinstance(val, dict) else {}
        except Exception:
            return {}

    async def _close_browser(self, *, save_cache: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.liveview.close()
        except Exception:
            pass
        if self._bidi:
            try:
                await self._bidi.close()
            except Exception:
                pass
            self._bidi = None
        if self._process:
            proc = self._process
            self._process = None
            try:
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await asyncio.wait_for(proc.wait(), timeout=3.0)
            except Exception:
                pass
        if self._stdout_drain_task and not self._stdout_drain_task.done():
            self._stdout_drain_task.cancel()
            await asyncio.gather(self._stdout_drain_task, return_exceptions=True)
            self._stdout_drain_task = None
        if self._relay:
            try:
                await self._relay.close()
            except Exception:
                pass
            self._relay = None
        if save_cache and self._work_dir and self._profile_dir and self._profile_dir.exists():
            try:
                clear_stale_firefox_profile_locks(self._profile_dir)
                save_startup_cache(self._work_dir, self._profile_dir)
                prune_camoufox_profile_bloat(self._profile_dir)
            except Exception:
                pass
        if self._lease:
            try:
                self._lease.release()
            except Exception:
                pass
            self._lease = None

    async def run(self) -> None:
        try:
            if self.mode == "manage":
                if not self.account_id:
                    raise ValueError("account_id is required for manage mode")
                if self.on_release_runtime:
                    await self.on_release_runtime(self.account_id)
                work_dir = self.user_data_root / "accounts" / self.account_id
                self._is_staging = False
            else:
                work_dir = self.user_data_root / "_staging" / f"login_{self.session_id}"
                if work_dir.exists():
                    shutil.rmtree(work_dir, ignore_errors=True)
                self._is_staging = True

            work_dir.mkdir(parents=True, exist_ok=True)
            profile_dir = work_dir / "firefox-profile"
            profile_dir.mkdir(parents=True, exist_ok=True)
            self._work_dir = work_dir
            self._profile_dir = profile_dir

            self._lease = acquire_profile_lease(
                work_dir, holder=f"dola-login:{self.session_id}", wait_seconds=5.0
            )
            clear_stale_firefox_profile_locks(profile_dir)
            restore_startup_cache(work_dir, profile_dir)

            exe = await ensure_camoufox_executable()
            locale = get_env("DOLA_LOCALE", None, DOLA_DEFAULT_LOCALE)
            tz = get_env("DOLA_TIMEZONE", None, DOLA_DEFAULT_TIMEZONE)
            fp_cfg = load_or_create_account_camoufox_config(work_dir, locale=locale, timezone=tz)

            relay_addr = None
            if self.proxy_url and proxy_needs_relay(self.proxy_url):
                self._relay = LocalSocks5Relay(self.proxy_url)
                relay_addr = await self._relay.start()
                log.info(
                    "LoginSession %s started LocalSocks5Relay %s:%d -> %s",
                    self.session_id,
                    relay_addr[0],
                    relay_addr[1],
                    mask_proxy_url(self.proxy_url),
                )

            write_camoufox_user_js(
                profile_dir,
                fp_cfg,
                proxy_url=None if relay_addr else self.proxy_url,
                relay_addr=relay_addr,
                headless=self.headless,
                live_view=True,
                idle_frame_divisor=1,
                executable_path=exe,
            )
            env = build_camoufox_env(
                fp_cfg,
                headless=self.headless,
                live_view=True,
                profile_dir=profile_dir,
                executable_path=exe,
            )

            args = [exe]
            if self.headless:
                args.append("--headless")
            args.extend(["--remote-debugging-port=0", "-wait-for-browser", "-profile", str(profile_dir)])

            log.info(
                "Starting isolated DolaLoginSession %s (mode=%s, headless=%s, dir=%s, proxy=%s)",
                self.session_id,
                self.mode,
                self.headless,
                work_dir,
                mask_proxy_url(self.proxy_url) or "direct",
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
            await self._bidi.command("session.subscribe", {"events": ["script.message"]}, timeout=10.0)

            tree = await self._bidi.command("browsingContext.getTree", {}, timeout=15.0)
            contexts = tree.get("contexts") or []
            if not contexts:
                created = await self._bidi.command("browsingContext.create", {"type": "tab"}, timeout=15.0)
                self._context_id = str(created.get("context") or "")
            else:
                self._context_id = str(contexts[0].get("context") or "")

            await self.liveview.attach(self._bidi, self._context_id)
            self.set_state(
                "waiting",
                "正在打开 Dola 国际版..." if self.mode == "add" else "正在打开该账号独立浏览器资料...",
            )

            await self._bidi.command(
                "browsingContext.navigate",
                {"context": self._context_id, "url": DOLA_CHAT_URL, "wait": "complete"},
                timeout=60.0,
            )

            if self.mode == "add":
                self.set_state("waiting", "请在下方纯净隔离浏览器中完成 Dola 登录（登录成功后自动入池）")
            else:
                self.set_state("waiting", "已打开该账号独立隔离资料，可管理登录/过验证，完成后点击「同步并关闭」")

            deadline = time.monotonic() + self.timeout_seconds
            stable_login_hits = 0

            while time.monotonic() < deadline:
                if self._cancel_event.is_set():
                    self.set_state("cancelled", "用户已取消浏览器会话")
                    await self._close_browser(save_cache=(self.mode == "manage"))
                    if self._is_staging and self._work_dir and self._work_dir.exists():
                        shutil.rmtree(self._work_dir, ignore_errors=True)
                    return

                if self._process and self._process.returncode is not None:
                    self.set_state("cancelled", "浏览器窗口已关闭")
                    await self._close_browser(save_cache=(self.mode == "manage"))
                    return

                cookies = await self._extract_dola_cookies()
                has_session = any(
                    bool(cookies.get(k)) for k in ("sessionid", "sessionid_ss", "sid_tt")
                )
                self.logged_in_detected = has_session

                should_save = self._save_event.is_set()
                if self.mode == "add" and has_session:
                    stable_login_hits += 1
                    # Wait 2 consecutive checks (~3s) so all Dola cookies & localStorage settle
                    if stable_login_hits >= 2:
                        should_save = True
                else:
                    stable_login_hits = 0

                if should_save:
                    if not has_session and self.mode == "add":
                        self._save_event.clear()
                        self.set_state("waiting", "尚未检测到 Dola 登录 Cookie，请先完成登录再保存")
                    else:
                        self.set_state("saving", "正在同步隔离资料与凭证...")
                        identity = await self._extract_identity()
                        exported = {
                            "cookies": cookies,
                            "identity": identity,
                            "source_work_dir": str(work_dir),
                            "is_staging": self._is_staging,
                            "proxy_url": self.proxy_url,
                        }
                        # Close browser BEFORE copying profile so SQLite/cache files flush cleanly!
                        await self._close_browser(save_cache=True)
                        saved_info: Dict[str, Any] = {}
                        if self.on_complete:
                            saved_info = await self.on_complete(self, exported)
                        if self._is_staging and work_dir.exists():
                            shutil.rmtree(work_dir, ignore_errors=True)
                        self.result = saved_info
                        self.set_state(
                            "completed",
                            "新账号登录并完成隔离资料保存！" if self.mode == "add" else "账号资料与凭证已同步保存！",
                        )
                        return

                await asyncio.sleep(1.5)

            self.set_state("error", "浏览器会话超时", error="timeout")
            await self._close_browser(save_cache=(self.mode == "manage"))
            if self._is_staging and work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)

        except asyncio.CancelledError:
            await self._close_browser(save_cache=False)
            raise
        except Exception as exc:
            safe = redact_sensitive_text(str(exc))
            log.error("DolaLoginSession %s failed: %s", self.session_id, safe)
            self.set_state("error", f"浏览器会话异常: {safe}", error=safe)
            await self._close_browser(save_cache=False)
            if self._is_staging and self._work_dir and self._work_dir.exists():
                shutil.rmtree(self._work_dir, ignore_errors=True)


class DolaLoginManager:
    """Manages active isolated browser login / account-profile management sessions."""

    def __init__(self) -> None:
        self._sessions: Dict[str, DolaLoginSession] = {}

    def create_session(self, **kwargs: Any) -> DolaLoginSession:
        self._prune_old()
        sid = f"sess_{uuid.uuid4().hex[:10]}"
        sess = DolaLoginSession(sid, **kwargs)
        self._sessions[sid] = sess
        sess.start_background()
        return sess

    def get(self, session_id: str) -> Optional[DolaLoginSession]:
        return self._sessions.get(session_id)

    def list_active(self) -> List[Dict[str, Any]]:
        self._prune_old()
        return [
            s.to_dict(include_frame=False)
            for s in self._sessions.values()
            if s.status in ("starting", "waiting", "saving")
        ]

    async def close_all(self) -> None:
        for sess in list(self._sessions.values()):
            sess.request_cancel()
            if sess._task and not sess._task.done():
                try:
                    await asyncio.wait_for(sess._task, timeout=5.0)
                except Exception:
                    sess._task.cancel()

    def _prune_old(self) -> None:
        now = time.time()
        for sid, sess in list(self._sessions.items()):
            if sess.finished_at and (now - sess.finished_at) > 600:
                self._sessions.pop(sid, None)
