"""Send keyboard keys from controller buttons that have no gamepad slot.

In virtual Pro Controller mode the rear grip buttons (GL / GR) and the C button
cannot reach the host as gamepad buttons, because the original Pro Controller
protocol has no place for them. This exposes them as a small virtual keyboard,
so games can bind them in their own key settings.

    NGC_EXTRA_KEYS="GL=KEY_PAGEUP,GR=KEY_PAGEDOWN,C=KEY_SCROLLLOCK"

Key names are Linux input event names (KEY_*). Avoid KEY_F13..KEY_F24: common
keyboard layouts turn them into launcher keys that Windows games under Proton
do not see.
"""

from __future__ import annotations

import logging
from typing import Optional

from . import protocol as P

logger = logging.getLogger(__name__)

DEVICE_NAME = "Switch 2 controller extra buttons"


def parse_extra_keys(spec: str) -> list[tuple[int, int]]:
    """'GL=KEY_PAGEUP,GR=KEY_PAGEDOWN' -> [(button_mask, key_code), ...]."""
    from evdev import ecodes as e

    out: list[tuple[int, int]] = []
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        src, _, dst = item.partition("=")
        src, dst = src.strip().upper(), dst.strip().upper()
        code = getattr(e, dst, None) if dst.startswith("KEY_") else None
        if src in P.SWITCH_BUTTONS and isinstance(code, int):
            out.append((P.SWITCH_BUTTONS[src], code))
        else:
            logger.warning("ignoring extra key mapping %r (want BUTTON=KEY_NAME)", item)
    return out


class ExtraKeys:
    def __init__(self, spec: str, ui=None):
        self.mapping = parse_extra_keys(spec)
        self._state: dict[int, int] = {}
        self.ui = ui
        if self.ui is None and self.mapping:
            from evdev import UInput, ecodes as e

            codes = sorted({code for _, code in self.mapping})
            self.ui = UInput({e.EV_KEY: codes}, name=DEVICE_NAME, max_effects=0)
            logger.info("extra buttons send keys: %s", spec)

    def __bool__(self) -> bool:
        return bool(self.mapping) and self.ui is not None

    def update(self, buttons: int) -> None:
        if not self:
            return
        want: dict[int, int] = {}
        for mask, code in self.mapping:
            want[code] = want.get(code, 0) | (1 if buttons & mask else 0)
        changed = False
        for code, value in want.items():
            if self._state.get(code, 0) != value:
                self.ui.write(1, code, value)  # EV_KEY
                if value and code not in self._state:
                    logger.info("extra button sent key code %d for the first time", code)
                self._state[code] = value
                changed = True
        if changed:
            self.ui.syn()

    def release_all(self) -> None:
        self.update(0)

    def close(self) -> None:
        if self.ui is None:
            return
        try:
            self.release_all()
            self.ui.close()
        except Exception:  # noqa: BLE001
            pass
        self.ui = None


def from_env(spec: Optional[str]) -> Optional[ExtraKeys]:
    if not spec or not spec.strip():
        return None
    try:
        keys = ExtraKeys(spec)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not create extra-button keyboard: %s", exc)
        return None
    return keys if keys else None
