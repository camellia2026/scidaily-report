"""API 响应的 TTL 缓存与离线固件。

存在的三个理由，和原项目的 feed_cache 完全一致：
  1. 开发时不必每次真联网（--offline 只读缓存）
  2. 把一次真实抓取固化成 tests/fixtures，之后零网络依赖地跑测试
  3. TTL 内重复运行不重复打 API——这是礼貌，也是省钱

TTL 策略与原项目一致：
  本地 24 小时（开发时反复跑不重复打 API）
  CI   0 小时（每天必须拿当天的新数据，绝不读缓存）
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from common.console import warn

from ..settings import DATA_DIR, ROOT

CACHE_DIR = DATA_DIR / "api_cache"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "api"

TTL_HOURS_LOCAL = 24.0
TTL_HOURS_CI = 0.0

_SLUG = re.compile(r"[^A-Za-z0-9_.-]+")


def cache_ttl(env: str) -> float:
    return TTL_HOURS_CI if env == "ci" else TTL_HOURS_LOCAL


def cache_key(name: str, params: dict[str, Any] | None = None) -> str:
    """把 (名字, 参数) 变成一个稳定的文件名。

    参数按键排序后哈希，所以同样的一组参数永远落在同一个文件上。
    """
    slug = _SLUG.sub("_", name)[:60].strip("_")
    if not params:
        return slug
    blob = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
    return f"{slug}.{digest}"


def _path_for(key: str, base: Path) -> Path:
    return base / f"{key}.json"


def _is_fresh(path: Path, ttl_hours: float) -> bool:
    if ttl_hours <= 0:
        return False
    if not path.exists():
        return False
    age_hours = (time.time() - path.stat().st_mtime) / 3600.0
    return age_hours < ttl_hours


def read_cache(key: str, base: Path | None = None) -> Any | None:
    """读缓存。运行缓存没有时，回落到 tests/fixtures 里的固件。

    ★ 为什么要有这个回落：
      `--offline` 的承诺是「绝不联网」。但如果固件只能靠
      `--seed` 先联网生成，那「离线冒烟」就不是开箱可用的——
      新克隆仓库的人第一次跑 `--smoke` 会看到「所有源都失败」，
      从而以为项目坏了。回落到固件让 --smoke 真正零前提。
    """
    candidates = [_path_for(key, base or CACHE_DIR)]
    if base is None:
        candidates.append(_path_for(key, FIXTURE_DIR))
    for path in candidates:
        if not path.exists():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            warn(f"缓存 {path.name} 损坏（{e}），忽略它。")
    return None


def write_cache(key: str, payload: Any, base: Path | None = None) -> Path:
    base = base or CACHE_DIR
    base.mkdir(parents=True, exist_ok=True)
    path = _path_for(key, base)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def cached_json(
    key: str,
    fetch,
    *,
    ttl_hours: float = TTL_HOURS_LOCAL,
    offline: bool = False,
    quiet: bool = False,
):
    """读缓存 -> 否则调用 fetch() 取数并落盘。

    offline=True 时绝不联网：缓存里没有就抛，让调用方记成一个失败源。
    —— 一个「离线模式却偷偷联网」的实现会让本地调试结果不可信。
    """
    cached = read_cache(key)
    if cached is not None and (offline or _is_fresh(_path_for(key, CACHE_DIR), ttl_hours)):
        return cached
    if offline:
        raise FileNotFoundError(f"离线模式下缓存里没有 {key}，且不允许联网。")
    payload = fetch()
    write_cache(key, payload)
    if not quiet:
        pass
    return payload


def seed_fixtures(keys: list[str] | None = None, *, max_results: int = 20) -> list[Path]:
    """把缓存复制成测试固件。之后 --offline / --smoke 就有真实数据可用了。

    ★ 必须裁剪，不能原样复制。
      实测未裁剪的固件是 11MB（单个 OpenAlex 响应 4.1MB），
      提交进 git 会让仓库迅速变得难以克隆，而固件的用途只是
      「让离线冒烟有代表性数据可跑」——20 条足够覆盖所有解析分支
      （各种 venue_type、有无摘要、多 location、垃圾标题），
      再多只是在浪费时间下载。

    裁剪的是 results 数组的长度，不改字段——字段少了会让解析分支
    在离线时不执行，那固件就失去了意义。
    """
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    keys = keys or [p.stem for p in CACHE_DIR.glob("*.json")]

    for key in keys:
        src = _path_for(key, CACHE_DIR)
        if not src.exists():
            continue
        try:
            payload = json.loads(src.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        payload = _trim(payload, max_results)
        dst = _path_for(key, FIXTURE_DIR)
        dst.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        written.append(dst)
    return written


def _trim(payload, max_results: int):
    """只截断列表长度，不动结构。"""
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    if isinstance(out.get("results"), list):
        out["results"] = out["results"][:max_results]
    msg = out.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("items"), list):
        out["message"] = {**msg, "items": msg["items"][:max_results]}
    return out


def clear_fixtures() -> int:
    """删掉旧固件。改过 select 字段之后要重生成，否则固件与代码不一致。"""
    removed = 0
    for path in FIXTURE_DIR.glob("*.json"):
        path.unlink()
        removed += 1
    return removed


def fixture_keys() -> list[str]:
    return sorted(p.stem for p in FIXTURE_DIR.glob("*.json"))
