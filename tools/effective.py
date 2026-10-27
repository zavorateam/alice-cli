"""Effective mode из VinsResponse.

Реально встречены формы (A5):
  - payload.effective_alice_2_settings.mode              (top-level)
  - payload.response.alice_2_effective_settings.effective_settings.mode (nested)
  - payload.alice_2_effective_settings.mode              (legacy)
Приоритет: top → nested → legacy → inline.

Если ни одна не сработала — mode=None, degraded=False. Отсутствие данных
не превращаем в "Base по умолчанию".
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class EffectiveMode:
    requested_mode: Optional[str] = None
    effective_mode: Optional[str] = None
    effective_preset: Optional[str] = None
    trial_left: Optional[int] = None
    trial_limit: Optional[int] = None
    source_path: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        if not self.requested_mode or not self.effective_mode:
            return False
        return self.requested_mode.strip().lower() != self.effective_mode.strip().lower()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested_mode": self.requested_mode,
            "effective_mode": self.effective_mode,
            "effective_preset": self.effective_preset,
            "trial_left": self.trial_left,
            "trial_limit": self.trial_limit,
            "source_path": self.source_path,
            "degraded": self.degraded,
        }


def _as_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _has_content(block: Dict[str, Any]) -> bool:
    if not isinstance(block, dict):
        return False
    if block.get("mode") or block.get("preset") or block.get("trial_state"):
        return True
    inner = block.get("effective_settings")
    if isinstance(inner, dict) and (inner.get("mode") or inner.get("preset")):
        return True
    return False


def _pick_candidate(payload: Any) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    if not isinstance(payload, dict):
        return None, None

    top = payload.get("effective_alice_2_settings")
    if _has_content(top):
        return "payload.effective_alice_2_settings", top

    resp = payload.get("response")
    if isinstance(resp, dict):
        nested = resp.get("alice_2_effective_settings")
        if _has_content(nested):
            return "payload.response.alice_2_effective_settings", nested

    legacy = payload.get("alice_2_effective_settings")
    if _has_content(legacy):
        return "payload.alice_2_effective_settings", legacy

    inline = payload.get("effective_settings")
    if _has_content(inline):
        return "payload.effective_settings", inline

    return None, None


def normalize_effective_mode(payload: Any, requested_mode: Optional[str] = None) -> EffectiveMode:
    path, block = _pick_candidate(payload)
    if block is None:
        return EffectiveMode(requested_mode=requested_mode)

    inner = block.get("effective_settings") if isinstance(block.get("effective_settings"), dict) else block
    mode = inner.get("mode")
    preset = inner.get("preset")

    trial: Dict[str, Any] = {}
    if isinstance(block.get("trial_state"), dict):
        trial = block["trial_state"]
    elif isinstance(inner.get("trial_state"), dict):
        trial = inner["trial_state"]

    return EffectiveMode(
        requested_mode=requested_mode,
        effective_mode=mode if isinstance(mode, str) else None,
        effective_preset=preset if isinstance(preset, str) else None,
        trial_left=_as_int(trial.get("left_count")),
        trial_limit=_as_int(trial.get("limit")),
        source_path=path,
        raw=block,
    )
