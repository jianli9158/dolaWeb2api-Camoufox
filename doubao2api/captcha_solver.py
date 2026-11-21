"""Automated slider captcha solver with humanized mouse trajectory over WebDriver BiDi.

Designed for ByteDance SecSDK / TTVerifyCenter slider captchas on Dola web runtime.
Supports:
1. Captcha DOM & iframe detection across top-level and child browsing contexts via BiDi.
2. Gap distance calculation via PIL edge/shadow/column-variance analysis.
3. Humanized non-linear mouse trajectory generation (cubic ease, overshoot, micro-jitter).
4. Native WebDriver BiDi `input.performActions` drag execution (zero Playwright dependency).
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import math
import random
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("doubao2api.captcha_solver")


def generate_human_trajectory(
    start_x: float,
    start_y: float,
    distance: float,
    total_time: Optional[float] = None,
) -> List[Tuple[float, float, float]]:
    """Generate a realistic human-like mouse movement trajectory.

    Returns list of `(x, y, delay_seconds)` steps.
    """
    if total_time is None:
        total_time = random.uniform(0.65, 1.15)

    steps: List[Tuple[float, float, float]] = []
    overshoot = random.uniform(1.5, 4.5) if distance > 40 else 0.0
    effective_distance = distance + overshoot

    num_steps = random.randint(32, 52)
    step_duration = total_time / num_steps

    curr_x = start_x
    curr_y = start_y

    for i in range(1, num_steps + 1):
        t = i / num_steps
        if t < 0.25:
            ease = 4 * t * t * t
        else:
            ease = 1 - pow(-2 * t + 2, 3) / 2

        target_x = start_x + effective_distance * ease
        jitter_y = math.sin(t * math.pi * random.uniform(1.5, 3.0)) * random.uniform(0.3, 1.2)
        jitter_y += random.uniform(-0.8, 0.8)
        actual_y = curr_y + jitter_y * 0.25

        curr_x = target_x
        delay = step_duration * random.uniform(0.6, 1.45)
        if t > 0.85:
            delay *= random.uniform(1.1, 1.6)
        steps.append((round(curr_x, 2), round(actual_y, 2), round(delay, 4)))

    if overshoot > 0:
        back_steps = random.randint(4, 7)
        for i in range(1, back_steps + 1):
            frac = i / back_steps
            curr_x = start_x + effective_distance - overshoot * frac
            actual_y = start_y + random.uniform(-0.5, 0.5)
            delay = random.uniform(0.015, 0.04)
            steps.append((round(curr_x, 2), round(actual_y, 2), round(delay, 4)))

    return steps


def calculate_gap_offset(bg_bytes: bytes, slider_bytes: Optional[bytes] = None) -> Optional[int]:
    """Calculate the horizontal gap offset in the slider background image."""
    _ = slider_bytes
    try:
        from PIL import Image, ImageFilter

        img = Image.open(io.BytesIO(bg_bytes)).convert("L")
        width, height = img.size
        if width < 80 or height < 40:
            return None

        edges = img.filter(ImageFilter.FIND_EDGES)
        px = edges.load()

        start_x = int(width * 0.22)
        end_x = int(width * 0.88)
        y_top = int(height * 0.15)
        y_bot = int(height * 0.85)

        col_scores: List[float] = []
        for x in range(start_x, end_x):
            score = 0.0
            for y in range(y_top, y_bot):
                val = px[x, y]
                if val > 35:
                    score += val
            col_scores.append(score)

        if not col_scores:
            return None

        best_idx = max(range(len(col_scores)), key=lambda i: col_scores[i])
        gap_x = start_x + best_idx
        return max(30, min(gap_x, width - 30))
    except Exception as exc:
        log.warning("Failed to calculate gap offset from image: %s", exc)
        return None


_FIND_CAPTCHA_JS = r"""(() => {
    const containerSels = [
        '#captcha_container', '.captcha_verify_container',
        '.secsdk-captcha-wrapper', '[class*="captcha-modal"]', '[class*="captcha_verify"]'
    ];
    const handleSels = [
        '.secsdk-captcha-drag-icon', '.captcha_drag_icon', '.sec-captcha-drag-icon',
        '[class*="drag-icon"]', '[class*="drag_button"]', '[class*="secsdk-captcha-drag"]'
    ];
    const bgSels = [
        '#captcha-verify-image', '.captcha_verify_img_slide',
        'img[src*="captcha"]', 'img[src*="verify"]', 'canvas[class*="captcha"]'
    ];
    const visibleRect = (sels) => {
        for (const s of sels) {
            const el = document.querySelector(s);
            if (el) {
                const r = el.getBoundingClientRect();
                if (r.width > 5 && r.height > 5) {
                    return { x: r.x, y: r.y, width: r.width, height: r.height };
                }
            }
        }
        return null;
    };
    const container = visibleRect(containerSels);
    if (!container) return null;
    return {
        container,
        handle: visibleRect(handleSels),
        bg: visibleRect(bgSels),
    };
})()"""


class SliderCaptchaSolver:
    """Automated solver for ByteDance SecSDK slider captchas over WebDriver BiDi."""

    def __init__(self):
        self.max_attempts = 2

    @staticmethod
    def _collect_context_ids(nodes: List[Dict[str, Any]]) -> List[str]:
        ids: List[str] = []
        for node in nodes:
            cid = str(node.get("context") or "")
            if cid:
                ids.append(cid)
            children = node.get("children") or []
            if isinstance(children, list):
                ids.extend(SliderCaptchaSolver._collect_context_ids(children))
        return ids

    async def detect_and_solve(self, bidi: Any, context_id: str, timeout: float = 8.0) -> bool:
        """Detect a visible slider captcha and execute humanized drag via WebDriver BiDi."""
        _ = timeout
        if not bidi or not context_id:
            return False

        try:
            tree = await bidi.command("browsingContext.getTree", {"root": context_id}, timeout=8.0)
            candidate_contexts = self._collect_context_ids(tree.get("contexts") or [])
            if context_id not in candidate_contexts:
                candidate_contexts.append(context_id)

            target_ctx: Optional[str] = None
            boxes: Optional[Dict[str, Any]] = None
            for cid in candidate_contexts:
                try:
                    found = await bidi.evaluate(cid, _FIND_CAPTCHA_JS, await_promise=False, timeout=5.0)
                    if isinstance(found, dict) and found.get("handle"):
                        target_ctx = cid
                        boxes = found
                        break
                except Exception:
                    continue

            if not target_ctx or not boxes:
                log.debug("No visible slider captcha handle found in BiDi contexts")
                return False

            handle_box = boxes["handle"]
            bg_box = boxes.get("bg")
            distance: Optional[float] = None

            if isinstance(bg_box, dict) and bg_box.get("width", 0) > 20 and bg_box.get("height", 0) > 20:
                try:
                    shot = await bidi.command(
                        "browsingContext.captureScreenshot",
                        {
                            "context": target_ctx,
                            "clip": {
                                "type": "box",
                                "x": float(bg_box["x"]),
                                "y": float(bg_box["y"]),
                                "width": float(bg_box["width"]),
                                "height": float(bg_box["height"]),
                            },
                        },
                        timeout=6.0,
                    )
                    bg_bytes = base64.b64decode(shot.get("data", ""))
                    gap_x = calculate_gap_offset(bg_bytes)
                    if gap_x is not None:
                        distance = gap_x * (float(bg_box["width"]) / 340.0)
                except Exception as exc:
                    log.debug("Captcha bg screenshot analysis failed: %s", exc)

            if distance is None:
                distance = random.uniform(140.0, 190.0)
                log.info("Using estimated slider distance: %.1fpx", distance)

            start_x = float(handle_box["x"]) + float(handle_box["width"]) / 2.0
            start_y = float(handle_box["y"]) + float(handle_box["height"]) / 2.0
            trajectory = generate_human_trajectory(start_x, start_y, distance)

            log.info(
                "Executing BiDi humanized slider drag: start=(%.1f, %.1f), dist=%.1f, steps=%d",
                start_x,
                start_y,
                distance,
                len(trajectory),
            )

            await asyncio.sleep(random.uniform(0.15, 0.35))

            actions: List[Dict[str, Any]] = [
                {"type": "pointerMove", "x": int(round(start_x)), "y": int(round(start_y))},
                {"type": "pause", "duration": 50},
                {"type": "pointerDown", "button": 0},
            ]
            for px, py, delay in trajectory:
                actions.append(
                    {
                        "type": "pointerMove",
                        "x": int(round(px)),
                        "y": int(round(py)),
                        "duration": max(8, int(round(delay * 1000))),
                    }
                )
            actions.append({"type": "pause", "duration": random.randint(80, 180)})
            actions.append({"type": "pointerUp", "button": 0})

            await bidi.command(
                "input.performActions",
                {
                    "context": target_ctx,
                    "actions": [
                        {
                            "type": "pointer",
                            "id": "mouse",
                            "parameters": {"pointerType": "mouse"},
                            "actions": actions,
                        }
                    ],
                },
                timeout=15.0,
            )

            await asyncio.sleep(1.5)
            still = await bidi.evaluate(target_ctx, _FIND_CAPTCHA_JS, await_promise=False, timeout=5.0)
            if not still:
                log.info("Successfully solved slider captcha via BiDi!")
                return True
            log.warning("Slider captcha still visible after BiDi drag attempt")
            return False
        except Exception as exc:
            log.warning("Exception during BiDi captcha solving: %s", exc)
            return False
