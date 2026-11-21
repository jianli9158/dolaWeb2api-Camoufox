"""Account pool manager for Dola multi-account support, proxy bindings, and per-capability cooldowns.

Manages account persistence (`accounts.json`), daily quota tracking, per-capability cooldown backoff
(adapted from flow2api-Camoufox `load_balancer.py`), and failover / round-robin scheduling.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional
import uuid

from .dola_config import get_default_profile_dir, get_env
from .proxy_utils import mask_proxy_url, normalize_proxy_url

log = logging.getLogger("doubao2api.account_manager")


@dataclass
class Account:
    id: str
    name: str
    cookies: Dict[str, str] = field(default_factory=dict)
    status: str = "active"  # "active", "quota_exceeded", "disabled", "invalid", "captcha"
    video_quota_exceeded: bool = False
    quota_reset_date: str = field(default_factory=lambda: date.today().isoformat())
    last_used: float = 0.0
    created_at: float = field(default_factory=time.time)
    proxy_url: Optional[str] = None
    cooldowns: Dict[str, float] = field(default_factory=dict)  # feature -> unix timestamp
    profile_dir: str = ""  # isolated relative profile dir, e.g. "accounts/acc_xxxx"
    region: str = ""
    device_id: str = ""
    def is_in_cooldown(self, feature: str = "chat", now: Optional[float] = None) -> bool:
        ts = time.time() if now is None else now
        for key in ("global", feature):
            until = float(self.cooldowns.get(key) or 0.0)
            if until > ts:
                return True
        return False

    def active_cooldowns(self, now: Optional[float] = None) -> Dict[str, int]:
        ts = time.time() if now is None else now
        return {
            k: int(round(until - ts))
            for k, until in self.cooldowns.items()
            if float(until or 0.0) > ts
        }

    def to_dict(self, mask_cookies: bool = False) -> Dict[str, Any]:
        data = asdict(self)
        data["cooldown_remaining"] = self.active_cooldowns()
        data["proxy_masked"] = mask_proxy_url(self.proxy_url)
        if mask_cookies:
            masked = {}
            for k, v in self.cookies.items():
                if len(v) > 8:
                    masked[k] = v[:3] + "..." + v[-3:]
                else:
                    masked[k] = "***"
            data["cookies"] = masked
            data["has_sessionid"] = any(
                k in self.cookies and bool(self.cookies[k])
                for k in ("sessionid", "sessionid_ss", "sid_tt")
            )
            if self.proxy_url:
                data["proxy_url"] = data["proxy_masked"]
        return data


class AccountManager:
    """Manages a Dola account pool stored in `accounts.json`."""

    def __init__(
        self,
        storage_dir: Optional[str] = None,
        filepath: Optional[str] = None,
        strategy: str = "failover",
    ):
        if filepath:
            self.filepath = filepath
        elif storage_dir:
            self.filepath = os.path.join(storage_dir, "accounts.json")
        else:
            self.filepath = os.path.join(get_default_profile_dir(), "accounts.json")

        self.strategy = get_env("DOLA_ACCOUNT_STRATEGY", "DOUBAO_ACCOUNT_STRATEGY", strategy).lower()
        if self.strategy not in ("failover", "round_robin"):
            self.strategy = "failover"

        self.active_account_id: Optional[str] = None
        self.accounts: Dict[str, Account] = {}
        self._round_robin_last_id: Optional[str] = None

        self.load()

    def load(self) -> None:
        if not os.path.exists(self.filepath):
            return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.strategy = data.get("strategy", self.strategy)
            self.active_account_id = data.get("active_account_id")

            self.accounts.clear()
            for item in data.get("accounts", []):
                raw_proxy = item.get("proxy_url")
                norm_proxy = None
                if raw_proxy:
                    try:
                        norm_proxy = normalize_proxy_url(raw_proxy)
                    except ValueError:
                        norm_proxy = None
                raw_cd = item.get("cooldowns")
                cooldowns = (
                    {str(k): float(v) for k, v in raw_cd.items()}
                    if isinstance(raw_cd, dict)
                    else {}
                )
                acc_id = item.get("id", str(uuid.uuid4())[:8])
                acc = Account(
                    id=acc_id,
                    name=item.get("name", "Unnamed Account"),
                    cookies=item.get("cookies", {}),
                    status=item.get("status", "active"),
                    video_quota_exceeded=item.get("video_quota_exceeded", False),
                    quota_reset_date=item.get("quota_reset_date", date.today().isoformat()),
                    last_used=item.get("last_used", 0.0),
                    created_at=item.get("created_at", time.time()),
                    proxy_url=norm_proxy,
                    cooldowns=cooldowns,
                    profile_dir=str(item.get("profile_dir") or f"accounts/{acc_id}"),
                    region=str(item.get("region") or ""),
                    device_id=str(item.get("device_id") or ""),
                )
                self.accounts[acc.id] = acc

            self.reset_daily_quotas_if_needed()

            if self.accounts and (not self.active_account_id or self.active_account_id not in self.accounts):
                self.active_account_id = next(iter(self.accounts.keys()))

            log.info(
                "Loaded %d Dola accounts from %s (active=%s, strategy=%s)",
                len(self.accounts),
                self.filepath,
                self.active_account_id,
                self.strategy,
            )
        except Exception as exc:
            log.error("Failed to load accounts from %s: %s", self.filepath, exc)

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.filepath)), exist_ok=True)
            data = {
                "active_account_id": self.active_account_id,
                "strategy": self.strategy,
                "accounts": [asdict(acc) for acc in self.accounts.values()],
            }
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            log.error("Failed to save accounts to %s: %s", self.filepath, exc)

    def reset_daily_quotas_if_needed(self, today: Optional[date] = None) -> None:
        today_str = (today or date.today()).isoformat()
        changed = False
        now = time.time()
        for acc in self.accounts.values():
            # Prune expired cooldowns lazily
            expired = [k for k, v in acc.cooldowns.items() if float(v or 0.0) <= now]
            for k in expired:
                acc.cooldowns.pop(k, None)
                changed = True
            if acc.quota_reset_date != today_str:
                if acc.video_quota_exceeded or acc.status == "quota_exceeded":
                    acc.video_quota_exceeded = False
                    if acc.status == "quota_exceeded":
                        acc.status = "active"
                    log.info("Auto-reset daily quota for account %s (%s)", acc.id, acc.name)
                    changed = True
                acc.quota_reset_date = today_str
                changed = True
        if changed:
            self.save()

    def resolve_account_dir(self, account_id: str) -> str:
        """Return the absolute isolated browser data directory for an account."""
        base_dir = os.path.dirname(os.path.abspath(self.filepath))
        acc = self.accounts.get(account_id)
        rel = (acc.profile_dir if acc and acc.profile_dir else f"accounts/{account_id}").strip()
        if os.path.isabs(rel):
            return rel
        return os.path.join(base_dir, rel)

    def add_or_update_account(
        self,
        name: str,
        cookies: Dict[str, str],
        account_id: Optional[str] = None,
        proxy_url: Optional[str] = None,
        profile_dir: Optional[str] = None,
        region: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> Account:
        self.reset_daily_quotas_if_needed()
        norm_proxy = normalize_proxy_url(proxy_url) if proxy_url else None

        if account_id and account_id in self.accounts:
            acc = self.accounts[account_id]
            acc.name = name or acc.name
            if cookies:
                acc.cookies = cookies
                acc.status = "active"
                acc.video_quota_exceeded = False
                acc.cooldowns.clear()
            if proxy_url is not None:
                acc.proxy_url = norm_proxy
            if profile_dir:
                acc.profile_dir = profile_dir
            if region:
                acc.region = region
            if device_id:
                acc.device_id = device_id
        else:
            acc_id = account_id or f"acc_{uuid.uuid4().hex[:8]}"
            acc = Account(
                id=acc_id,
                name=name or f"Account-{len(self.accounts) + 1}",
                cookies=cookies,
                status="active",
                proxy_url=norm_proxy,
                profile_dir=profile_dir or f"accounts/{acc_id}",
                region=region or "",
                device_id=device_id or "",
            )
            self.accounts[acc.id] = acc

        if not self.active_account_id or self.active_account_id not in self.accounts:
            self.active_account_id = acc.id

        self.save()
        return acc

    def update_account(
        self,
        account_id: str,
        *,
        name: Optional[str] = None,
        proxy_url: Optional[str] = None,
        clear_proxy: bool = False,
        region: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> Optional[Account]:
        acc = self.accounts.get(account_id)
        if not acc:
            return None
        if name is not None and name.strip():
            acc.name = name.strip()
        if clear_proxy:
            acc.proxy_url = None
        elif proxy_url is not None:
            acc.proxy_url = normalize_proxy_url(proxy_url) if proxy_url.strip() else None
        if region is not None:
            acc.region = region
        if device_id is not None:
            acc.device_id = device_id
        self.save()
        return acc
    def set_account_proxy(self, account_id: str, proxy_url: Optional[str]) -> bool:
        if account_id not in self.accounts:
            return False
        self.accounts[account_id].proxy_url = normalize_proxy_url(proxy_url) if proxy_url else None
        self.save()
        return True

    def get_account(self, account_id: str) -> Optional[Account]:
        self.reset_daily_quotas_if_needed()
        return self.accounts.get(account_id)

    def get_active_account(self) -> Optional[Account]:
        self.reset_daily_quotas_if_needed()
        return self.accounts.get(self.active_account_id) if self.active_account_id else None

    def set_active_account(self, account_id: str) -> bool:
        if account_id in self.accounts:
            self.active_account_id = account_id
            self.accounts[account_id].last_used = time.time()
            self._round_robin_last_id = account_id
            self.save()
            return True
        return False

    def delete_account(self, account_id: str, delete_profile: bool = False) -> bool:
        if account_id in self.accounts:
            profile_path = self.resolve_account_dir(account_id) if delete_profile else None
            del self.accounts[account_id]
            if self.active_account_id == account_id:
                self.active_account_id = next(iter(self.accounts.keys()), None)
            if self._round_robin_last_id == account_id:
                self._round_robin_last_id = None
            self.save()
            if profile_path and os.path.isdir(profile_path):
                import shutil
                try:
                    shutil.rmtree(profile_path, ignore_errors=True)
                except Exception:
                    pass
            return True
        return False

    def rename_account(self, account_id: str, new_name: str) -> bool:
        if account_id in self.accounts and new_name.strip():
            self.accounts[account_id].name = new_name.strip()
            self.save()
            return True
        return False

    def update_account_status(self, account_id: str, status: str) -> bool:
        if account_id in self.accounts:
            self.accounts[account_id].status = status
            self.save()
            return True
        return False

    def mark_cooldown(
        self,
        account_id: Optional[str] = None,
        feature: str = "chat",
        seconds: float = 60.0,
    ) -> None:
        """Apply per-capability cooldown backoff (e.g. 'chat', 'image', 'video', 'global')."""
        target_id = account_id or self.active_account_id
        if not target_id or target_id not in self.accounts:
            return
        acc = self.accounts[target_id]
        until = time.time() + max(1.0, float(seconds))
        acc.cooldowns[feature] = max(float(acc.cooldowns.get(feature) or 0.0), until)
        log.warning(
            "Account %s (%s) placed on %s cooldown for %.0fs",
            acc.id,
            acc.name,
            feature,
            seconds,
        )
        self.save()

    def clear_cooldowns(self, account_id: str) -> bool:
        if account_id not in self.accounts:
            return False
        self.accounts[account_id].cooldowns.clear()
        self.save()
        return True

    def mark_quota_exceeded(self, account_id: Optional[str] = None, feature: str = "video") -> None:
        target_id = account_id or self.active_account_id
        if not target_id or target_id not in self.accounts:
            return
        acc = self.accounts[target_id]
        if feature == "video":
            acc.video_quota_exceeded = True
        acc.quota_reset_date = date.today().isoformat()
        log.warning("Account %s (%s) marked as quota exceeded for %s", acc.id, acc.name, feature)
        self.save()

    def mark_captcha_required(self, account_id: Optional[str] = None) -> None:
        target_id = account_id or self.active_account_id
        if not target_id or target_id not in self.accounts:
            return
        acc = self.accounts[target_id]
        acc.status = "captcha"
        log.warning("Account %s (%s) marked as status='captcha'", acc.id, acc.name)
        self.save()

    def clear_captcha_status(self, account_id: str) -> bool:
        if account_id in self.accounts:
            acc = self.accounts[account_id]
            if acc.status == "captcha":
                acc.status = "active"
                acc.cooldowns.pop("global", None)
                log.info("Account %s (%s) captcha status cleared, restored to active", acc.id, acc.name)
                self.save()
                return True
        return False

    def reset_quota(self, account_id: str) -> bool:
        if account_id in self.accounts:
            acc = self.accounts[account_id]
            acc.video_quota_exceeded = False
            acc.cooldowns.clear()
            if acc.status == "quota_exceeded":
                acc.status = "active"
            self.save()
            return True
        return False

    def has_alternative_accounts(self, feature: str = "video") -> bool:
        return any(acc.id != self.active_account_id for acc in self.get_available_accounts(feature))

    def get_available_accounts(
        self,
        feature: str = "chat",
        exclude_ids: Optional[set[str]] = None,
    ) -> List[Account]:
        """Return eligible accounts in scheduling order without changing manager state."""
        self.reset_daily_quotas_if_needed()
        excluded = exclude_ids or set()
        now = time.time()
        available = [
            acc
            for acc in self.accounts.values()
            if acc.id not in excluded
            and acc.status not in ("disabled", "invalid", "captcha")
            and acc.status in ("active", "quota_exceeded")
            and not acc.is_in_cooldown(feature, now=now)
            and not (feature == "video" and (acc.video_quota_exceeded or acc.status == "quota_exceeded"))
        ]
        if self.strategy != "round_robin" or not available:
            return available

        cursor_id = self._round_robin_last_id
        if cursor_id is None and self.active_account_id in {acc.id for acc in available}:
            active_index = next(i for i, acc in enumerate(available) if acc.id == self.active_account_id)
            return available[active_index:] + available[:active_index]
        cursor_index = next((i for i, acc in enumerate(available) if acc.id == cursor_id), None)
        if cursor_index is None:
            return available
        next_index = (cursor_index + 1) % len(available)
        return available[next_index:] + available[:next_index]

    def get_next_available_account(
        self,
        feature: str = "chat",
        exclude_ids: Optional[set[str]] = None,
    ) -> Optional[Account]:
        available = self.get_available_accounts(feature, exclude_ids)
        if not available:
            return None
        if self.strategy == "failover":
            active = self.get_active_account()
            if active and active in available:
                return active
        return available[0]

    def list_accounts(self, mask_cookies: bool = True) -> List[Dict[str, Any]]:
        self.reset_daily_quotas_if_needed()
        result = []
        for acc in self.accounts.values():
            d = acc.to_dict(mask_cookies=mask_cookies)
            d["is_active"] = acc.id == self.active_account_id
            result.append(d)
        return result
