"""
checkpoint.py - 全市场扫描的断点续传管理器

支持：
  1) 记录某次"全市场扫描任务"哪些 ticker 已成功拉取 info / history
  2) 跨进程、跨天持久化（JSON）
  3) 区分不同阶段（info_light / history / score）
  4) 提供"失败重试冷却"机制（最近10分钟失败过的，暂不重试）

文件格式：cache/checkpoints/<task_name>.json
{
  "task": "universe_scan_tech_resource",
  "started_at": "...",
  "updated_at": "...",
  "stages": {
    "info_light": {
      "done":   ["AAPL", "MSFT", ...],    # 已成功
      "empty":  ["XYZ", ...],              # 确认无数据（非限流）
      "failed": {"ABC": {"at": "...", "reason": "rate_limit"}, ...}
    },
    "history":   {...},
    "score":     {...}
  }
}
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHECKPOINT_DIR = PROJECT_ROOT / "cache" / "checkpoints"
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)


class Checkpoint:
    """
    线程安全的断点续传管理器。
    用法：
        ckpt = Checkpoint("universe_scan_tech_resource")
        todo = ckpt.filter_todo("info_light", candidates)  # 过滤掉已完成的
        ...
        ckpt.mark_done("info_light", "AAPL")
        ckpt.flush()  # 定期持久化
    """

    DEFAULT_COOLDOWN_MIN = 30  # 失败后 30min 内不重试

    def __init__(self, task_name: str, max_age_hours: float = 48.0):
        self.task = task_name
        self.file = CHECKPOINT_DIR / f"{task_name}.json"
        self._lock = threading.Lock()
        self._data = self._load(max_age_hours)
        self._dirty = False
        self._last_flush = time.time()

    def _load(self, max_age_hours: float) -> dict:
        if not self.file.exists():
            return self._new_data()
        try:
            with self.file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            # 过期则丢弃
            upd = data.get("updated_at")
            if upd:
                age = (datetime.now() - datetime.fromisoformat(upd)).total_seconds() / 3600
                if age > max_age_hours:
                    logger.info(f"[checkpoint] {self.task} 过期 ({age:.1f}h), 重置")
                    return self._new_data()
            logger.info(f"[checkpoint] {self.task} 已加载，"
                        f"info_light.done={len(data.get('stages', {}).get('info_light', {}).get('done', []))}, "
                        f"history.done={len(data.get('stages', {}).get('history', {}).get('done', []))}")
            return data
        except Exception as e:
            logger.warning(f"[checkpoint] load failed, 重建: {e}")
            return self._new_data()

    def _new_data(self) -> dict:
        now = datetime.now().isoformat(timespec="seconds")
        return {
            "task": self.task,
            "started_at": now,
            "updated_at": now,
            "stages": {
                "info_light": {"done": [], "empty": [], "failed": {}},
                "history":    {"done": [], "empty": [], "failed": {}},
                "score":      {"done": [], "empty": [], "failed": {}},
            },
        }

    # -------- 读 --------
    def _stage(self, stage: str) -> dict:
        return self._data["stages"].setdefault(
            stage, {"done": [], "empty": [], "failed": {}}
        )

    def is_done(self, stage: str, symbol: str) -> bool:
        s = self._stage(stage)
        return symbol in s["done"] or symbol in s["empty"]

    def is_in_cooldown(self, stage: str, symbol: str,
                       cooldown_min: Optional[int] = None) -> bool:
        """失败冷却期内返回 True（暂不重试）"""
        cd = cooldown_min or self.DEFAULT_COOLDOWN_MIN
        s = self._stage(stage)
        info = s["failed"].get(symbol)
        if not info:
            return False
        try:
            t = datetime.fromisoformat(info["at"])
            return datetime.now() - t < timedelta(minutes=cd)
        except Exception:
            return False

    def filter_todo(self, stage: str, symbols: list,
                    respect_cooldown: bool = True,
                    cooldown_min: Optional[int] = None) -> list:
        """
        过滤出需要本次执行的 symbols（剔除已完成；可选剔除冷却中）。
        """
        out = []
        for s in symbols:
            if self.is_done(stage, s):
                continue
            if respect_cooldown and self.is_in_cooldown(stage, s, cooldown_min):
                continue
            out.append(s)
        return out

    def stats(self, stage: str) -> dict:
        s = self._stage(stage)
        return {
            "done": len(s["done"]),
            "empty": len(s["empty"]),
            "failed": len(s["failed"]),
        }

    # -------- 写 --------
    def mark_done(self, stage: str, symbol: str) -> None:
        with self._lock:
            s = self._stage(stage)
            if symbol not in s["done"]:
                s["done"].append(symbol)
            s["failed"].pop(symbol, None)
            self._dirty = True

    def mark_empty(self, stage: str, symbol: str) -> None:
        """标记为"确认无数据"（非限流），不再重试"""
        with self._lock:
            s = self._stage(stage)
            if symbol not in s["empty"]:
                s["empty"].append(symbol)
            s["failed"].pop(symbol, None)
            self._dirty = True

    def mark_failed(self, stage: str, symbol: str, reason: str = "") -> None:
        """失败（限流等），进入冷却期"""
        with self._lock:
            s = self._stage(stage)
            s["failed"][symbol] = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "reason": reason[:120],
            }
            self._dirty = True

    def flush(self, force: bool = False, min_interval_sec: int = 5) -> None:
        """持久化到磁盘。高频调用时有最小间隔保护。"""
        with self._lock:
            if not self._dirty and not force:
                return
            if not force and (time.time() - self._last_flush) < min_interval_sec:
                return
            self._data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            tmp = self.file.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            tmp.replace(self.file)
            self._dirty = False
            self._last_flush = time.time()

    def reset(self) -> None:
        with self._lock:
            self._data = self._new_data()
            self._dirty = True
        self.flush(force=True)

    def reset_stage(self, stage: str) -> None:
        with self._lock:
            self._data["stages"][stage] = {"done": [], "empty": [], "failed": {}}
            self._dirty = True
        self.flush(force=True)
