"""Interactive Camoufox LiveView over WebDriver BiDi (frame streaming + mouse/key/wheel input).

Ported from flow2api-Camoufox `src/services/camoufox_login.py` so users can sign into Dola
(Google OAuth, Lark OAuth, QR scan) or solve captchas directly from the Web Admin console
even on headless Linux / Docker servers without VNC.
"""

from __future__ import annotations

import asyncio
from collections import deque
import os
import sys
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from .bidi_transport import BiDiClient


def has_display() -> bool:
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


WEBDRIVER_KEYS: Dict[str, str] = {
    "Cancel": "\uE001",
    "Help": "\uE002",
    "Backspace": "\uE003",
    "Tab": "\uE004",
    "Clear": "\uE005",
    "Enter": "\uE007",
    "Shift": "\uE008",
    "Control": "\uE009",
    "Alt": "\uE00A",
    "Pause": "\uE00B",
    "Escape": "\uE00C",
    "PageUp": "\uE00E",
    "PageDown": "\uE00F",
    "End": "\uE010",
    "Home": "\uE011",
    "ArrowLeft": "\uE012",
    "ArrowUp": "\uE013",
    "ArrowRight": "\uE014",
    "ArrowDown": "\uE015",
    "Insert": "\uE016",
    "Delete": "\uE017",
    "F1": "\uE031",
    "F2": "\uE032",
    "F3": "\uE033",
    "F4": "\uE034",
    "F5": "\uE035",
    "F6": "\uE036",
    "F7": "\uE037",
    "F8": "\uE038",
    "F9": "\uE039",
    "F10": "\uE03A",
    "F11": "\uE03B",
    "F12": "\uE03C",
    "Meta": "\uE03D",
    "ZenkakuHankaku": "\uE040",
}


class CamoufoxLiveView:
    """Streams live browser JPEG frames and forwards pointer/keyboard/wheel/dialog input over BiDi."""

    def __init__(
        self,
        *,
        on_interactive_change: Optional[Callable[[bool], Awaitable[None]]] = None,
    ) -> None:
        self.status: str = "idle"
        self.message: str = "Waiting for Camoufox browser..."
        self.frame_meta: Optional[Dict[str, Any]] = None
        self.frame_b64: str = ""
        self.frame_seq: int = 0
        self.dialog: Optional[Dict[str, Any]] = None
        self.dialog_seq: int = 0
        self.contexts: List[str] = []
        self._bidi: Optional[BiDiClient] = None
        self._inputs: deque[Dict[str, Any]] = deque(maxlen=256)
        self._subscribers: Set[Any] = set()
        self._last_frame_requested_at: float = 0.0
        self._input_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._closed = False
        self._tasks: List[asyncio.Task] = []
        self._on_interactive_change = on_interactive_change
        self._interactive_active: bool = False

    def add_subscriber(self, subscriber: Any = None) -> Any:
        token = subscriber if subscriber is not None else object()
        was_empty = len(self._subscribers) == 0
        self._subscribers.add(token)
        self._last_frame_requested_at = time.monotonic()
        self._wake_event.set()
        if was_empty and self._on_interactive_change and not self._interactive_active:
            self._interactive_active = True
            asyncio.create_task(self._notify_interactive(True))
        return token

    def remove_subscriber(self, subscriber: Any) -> None:
        self._subscribers.discard(subscriber)
        if not self._subscribers and self._on_interactive_change and self._interactive_active:
            self._interactive_active = False
            asyncio.create_task(self._notify_interactive(False))

    async def _notify_interactive(self, active: bool) -> None:
        if self._on_interactive_change:
            try:
                await self._on_interactive_change(active)
            except Exception:
                pass

    def snapshot(self, *, include_frame: bool = True) -> Dict[str, Any]:
        if include_frame:
            self._last_frame_requested_at = time.monotonic()
            self._wake_event.set()
        payload: Dict[str, Any] = {
            "status": self.status,
            "message": self.message,
            "frame_seq": self.frame_seq,
            "dialog": self.dialog,
            "dialog_seq": self.dialog_seq,
            "contexts": list(self.contexts),
            "active_context": self.active_context(),
        }
        if include_frame and self.frame_b64:
            payload["frame_b64"] = self.frame_b64
            payload["frame_meta"] = self.frame_meta
        return payload

    def set_status(self, status: str, message: str = "") -> None:
        self.status = status
        if message:
            self.message = message
        self._wake_event.set()

    async def attach(self, bidi: BiDiClient, root_context_id: str) -> None:
        await self.detach()
        self._closed = False
        self._bidi = bidi
        self.contexts = [root_context_id]
        self.status = "ready"
        self.message = "Connected to Camoufox BiDi"
        bidi.add_listener(self._on_bidi_event)
        try:
            await bidi.command(
                "session.subscribe",
                {
                    "events": [
                        "browsingContext.contextCreated",
                        "browsingContext.contextDestroyed",
                        "browsingContext.userPromptOpened",
                        "browsingContext.userPromptClosed",
                    ]
                },
                timeout=10.0,
            )
        except Exception:
            pass
        self._tasks = [
            asyncio.create_task(self._capture_loop()),
            asyncio.create_task(self._input_loop()),
        ]

    async def detach(self) -> None:
        self._closed = True
        self._wake_event.set()
        self._input_event.set()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._bidi is not None:
            self._bidi.remove_listener(self._on_bidi_event)
            self._bidi = None
        self.contexts.clear()

    async def close(self) -> None:
        await self.detach()
        self._subscribers.clear()
        self.status = "closed"

    def active_context(self) -> str:
        return self.contexts[-1] if self.contexts else ""

    def _on_bidi_event(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> None:
        params = params or {}
        ctx_id = str(params.get("context") or "")
        if method == "browsingContext.contextCreated":
            if params.get("parent") is None and ctx_id and ctx_id not in self.contexts:
                self.contexts.append(ctx_id)
                self._wake_event.set()
        elif method == "browsingContext.contextDestroyed":
            if ctx_id in self.contexts and len(self.contexts) > 1:
                self.contexts.remove(ctx_id)
                self._wake_event.set()
        elif method == "browsingContext.userPromptOpened":
            self.dialog = {
                "context": ctx_id,
                "kind": str(params.get("type") or "alert"),
                "message": str(params.get("message") or ""),
                "value": str(params.get("defaultValue") or ""),
            }
            self.dialog_seq += 1
            self._wake_event.set()
        elif method == "browsingContext.userPromptClosed":
            self.dialog = None
            self.dialog_seq += 1
            self._wake_event.set()

    def enqueue_input(self, item: Dict[str, Any]) -> None:
        if not isinstance(item, dict):
            return
        self._last_frame_requested_at = time.monotonic()
        itype = str(item.get("type") or "")
        if itype == "dialog":
            asyncio.create_task(
                self._answer_dialog(
                    bool(item.get("accept", True)), str(item.get("text") or "")
                )
            )
            return
        if itype == "navigate":
            url = str(item.get("url") or "").strip()
            if url:
                asyncio.create_task(self._navigate(url))
            return
        if itype == "reload":
            asyncio.create_task(self._reload())
            return
        if itype == "back":
            asyncio.create_task(self._history_step(-1))
            return
        if self._inputs:
            last = self._inputs[-1]
            if last.get("type") == itype:
                if (
                    itype == "pointer"
                    and item.get("action") == "move"
                    and last.get("action") == "move"
                ):
                    self._inputs[-1] = item
                    self._input_event.set()
                    return
                if (
                    itype == "wheel"
                    and last.get("x") == item.get("x")
                    and last.get("y") == item.get("y")
                ):
                    last["dx"] = float(last.get("dx") or 0) + float(item.get("dx") or 0)
                    last["dy"] = float(last.get("dy") or 0) + float(item.get("dy") or 0)
                    self._input_event.set()
                    return
        self._inputs.append(item)
        self._input_event.set()

    async def _navigate(self, url: str) -> None:
        bidi = self._bidi
        ctx_id = self.active_context()
        if not bidi or not ctx_id:
            return
        try:
            await bidi.command(
                "browsingContext.navigate",
                {"context": ctx_id, "url": url, "wait": "interactive"},
                timeout=20.0,
            )
            self._wake_event.set()
        except Exception:
            pass

    async def _reload(self) -> None:
        bidi = self._bidi
        ctx_id = self.active_context()
        if not bidi or not ctx_id:
            return
        try:
            await bidi.command(
                "browsingContext.reload",
                {"context": ctx_id, "wait": "interactive"},
                timeout=20.0,
            )
            self._wake_event.set()
        except Exception:
            pass

    async def _history_step(self, delta: int) -> None:
        bidi = self._bidi
        ctx_id = self.active_context()
        if not bidi or not ctx_id:
            return
        try:
            await bidi.command(
                "browsingContext.traverseHistory",
                {"context": ctx_id, "delta": delta},
                timeout=10.0,
            )
            self._wake_event.set()
        except Exception:
            pass

    async def _answer_dialog(self, accept: bool, text: str) -> None:
        dialog = self.dialog
        bidi = self._bidi
        if not dialog or not bidi:
            return
        params: Dict[str, Any] = {
            "context": dialog["context"],
            "accept": accept,
        }
        if dialog.get("kind") == "prompt" and accept:
            params["userText"] = text
        try:
            await bidi.command("browsingContext.handleUserPrompt", params, timeout=5.0)
        except Exception:
            pass

    async def _capture_loop(self) -> None:
        while not self._closed:
            now = time.monotonic()
            active_viewers = len(self._subscribers) > 0
            recent_poll = (now - self._last_frame_requested_at) <= 6.0
            if not active_viewers and not recent_poll:
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                continue
            started = now
            bidi = self._bidi
            ctx_id = self.active_context()
            if bidi and ctx_id and not bidi._closed:
                try:
                    shot = await bidi.command(
                        "browsingContext.captureScreenshot",
                        {
                            "context": ctx_id,
                            "format": {"type": "image/jpeg", "quality": 0.78},
                        },
                        timeout=4.0,
                    )
                    b64_data = str(shot.get("data") or "")
                    if b64_data and b64_data != self.frame_b64:
                        meta = await bidi.evaluate(
                            ctx_id,
                            "(() => [location.href, document.title || '', innerWidth || 1280, innerHeight || 800, devicePixelRatio || 1])()",
                            await_promise=False,
                            timeout=2.5,
                        )
                        if isinstance(meta, list) and len(meta) >= 5:
                            self.frame_meta = {
                                "type": "frame",
                                "context": ctx_id,
                                "url": str(meta[0] or ""),
                                "title": str(meta[1] or ""),
                                "width": float(meta[2] or 1280),
                                "height": float(meta[3] or 800),
                                "scale": float(meta[4] or 1),
                            }
                        elif self.frame_meta is None:
                            self.frame_meta = {
                                "type": "frame",
                                "context": ctx_id,
                                "url": "",
                                "title": "",
                                "width": 1280.0,
                                "height": 800.0,
                                "scale": 1.0,
                            }
                        self.frame_b64 = b64_data
                        self.frame_seq += 1
                        self._wake_event.set()
                except Exception:
                    pass
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.06, 0.16 - elapsed))

    async def _input_loop(self) -> None:
        while not self._closed:
            await self._input_event.wait()
            self._input_event.clear()
            if self._closed:
                break
            bidi = self._bidi
            if not bidi or bidi._closed:
                continue
            batch: List[Dict[str, Any]] = []
            while self._inputs:
                batch.append(self._inputs.popleft())
            for item in batch:
                ctx_id = str(item.get("context") or "") or self.active_context()
                if not ctx_id:
                    continue
                source = self._build_action_source(item)
                if not source:
                    continue
                try:
                    await bidi.command(
                        "input.performActions",
                        {"context": ctx_id, "actions": [source]},
                        timeout=5.0,
                    )
                except Exception:
                    pass

    def _build_action_source(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        itype = str(item.get("type") or "")
        meta = self.frame_meta or {}
        max_w = max(1, int(meta.get("width") or 1280))
        max_h = max(1, int(meta.get("height") or 800))
        x = min(max(int(round(float(item.get("x") or 0))), 0), max_w - 1)
        y = min(max(int(round(float(item.get("y") or 0))), 0), max_h - 1)
        action = str(item.get("action") or "")

        if itype == "pointer":
            move = {"type": "pointerMove", "x": x, "y": y}
            button = int(item.get("button") or 0)
            if action == "move":
                acts = [move]
            elif action == "down":
                acts = [move, {"type": "pointerDown", "button": button}]
            elif action == "up":
                acts = [move, {"type": "pointerUp", "button": button}]
            elif action == "click":
                acts = [
                    move,
                    {"type": "pointerDown", "button": button},
                    {"type": "pointerUp", "button": button},
                ]
            else:
                return None
            return {
                "type": "pointer",
                "id": "mouse",
                "parameters": {"pointerType": "mouse"},
                "actions": acts,
            }
        if itype == "wheel":
            dx = int(round(float(item.get("dx") or 0)))
            dy = int(round(float(item.get("dy") or 0)))
            return {
                "type": "wheel",
                "id": "wheel",
                "actions": [{"type": "scroll", "x": x, "y": y, "deltaX": dx, "deltaY": dy}],
            }
        if itype == "key":
            key_name = str(item.get("key") or "")
            key_val = WEBDRIVER_KEYS.get(key_name) or (
                key_name if len(key_name) == 1 else ""
            )
            if not key_val or action not in ("down", "up"):
                return None
            return {
                "type": "key",
                "id": "keyboard",
                "actions": [
                    {
                        "type": "keyDown" if action == "down" else "keyUp",
                        "value": key_val,
                    }
                ],
            }
        if itype == "text":
            text = str(item.get("text") or "")
            key_actions: List[Dict[str, Any]] = []
            for ch in text:
                if ch == "\n":
                    val = WEBDRIVER_KEYS["Enter"]
                elif ch == "\t":
                    val = WEBDRIVER_KEYS["Tab"]
                elif ord(ch) < 0x20 or ord(ch) == 0x7F:
                    continue
                else:
                    val = ch
                key_actions.append({"type": "keyDown", "value": val})
                key_actions.append({"type": "keyUp", "value": val})
            if not key_actions:
                return None
            return {"type": "key", "id": "keyboard", "actions": key_actions}
        return None
