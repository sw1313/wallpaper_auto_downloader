# -*- coding: utf-8 -*-
r"""
we_auto_fetch.py — Web API(steam api_key) 拉已排序/筛选列表 → 每轮只下 1 个（失败自动换下一个）
→ steamcmd 下载(实时进度)（隐藏黑窗） → 镜像到官方 Workshop → 复制到 WE projects\backup → 强制应用
→ one_per_run 清理（不重写日志，长期去重）

依赖：pip install requests

改动要点（2025-09-30）：
- 年龄分级以普通 tags 为主（Everyone / Questionable / Mature），KV 仅兜底
- types 也是普通 tag（Scene / Video / Web / Application / …）
- 当 age 或 types 只配置单个值时，推入 WebAPI/HTML 的 requiredtags 做服务端筛选；
  多选时在客户端用 OR 过滤，避免服务端 AND 筛成 0。

改动要点（2025-10-01）：
- 新增 RUN_NOW 命名事件：允许托盘/外部唤醒本进程，立刻执行一轮 run_once（不重启 worker）。
- 新增命令行 --once：读取配置并仅执行一轮 run_once 后退出。
"""

from __future__ import annotations
import configparser, json, os, re, shutil, subprocess, sys, time, io, ctypes, hashlib
from ctypes import wintypes
from pathlib import Path
from typing import Dict, List, Optional, Tuple

APPID_WE = 431960

# =========================
# 配置与路径解析
# =========================
def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

HERE = _app_root()
CONF_PATH = None

def _candidate_config_paths() -> list[Path]:
    names = ("config", "config.ini")
    out: list[Path] = []
    env_p = os.environ.get("WE_CONFIG") or os.environ.get("WE_CONF")
    if env_p:
        out.append(Path(env_p))
    base = _app_root()
    out += [base / n for n in names]
    cwd = Path.cwd()
    if cwd != base:
        out += [cwd / n for n in names]
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        m = Path(mei)
        out += [m / n for n in names]
    seen, uniq = set(), []
    for p in out:
        rp = p.resolve()
        if rp not in seen:
            uniq.append(rp); seen.add(rp)
    return uniq

def read_conf() -> configparser.ConfigParser:
    candidates = _candidate_config_paths()
    for p in candidates:
        if p.exists():
            cfg = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",))
            cfg.read(p, encoding="utf-8")
            print(f"[config] 使用配置：{p}")
            return cfg
    tried = "\n  - " + "\n  - ".join(str(p) for p in candidates)
    raise RuntimeError("未找到配置文件；已尝试：" + tried)

# ---------- 小工具 ----------
def expand(p: str) -> str:
    return os.path.expandvars((p or "").strip())

def parse_csv(s: str) -> List[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]

def parse_interval(s: str) -> int:
    if not s: return 0
    s = s.strip().lower()
    total = 0
    for num, unit in re.findall(r"(\d+)\s*([hms])", s):
        v = int(num)
        total += v * (3600 if unit == 'h' else 60 if unit == 'm' else 1)
    return total

def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

def _safe_int(x, default=0) -> int:
    try:
        if isinstance(x, bool): return int(x)
        if isinstance(x, (int, float)): return int(x)
        s = str(x).strip()
        return int(s) if s else default
    except Exception:
        return default

def _cfg_int(cfg: configparser.ConfigParser, section: str, option: str, fallback: int) -> int:
    try:
        if not cfg.has_option(section, option): return fallback
        raw = cfg.get(section, option)
        s = "" if raw is None else str(raw).strip()
        return int(s) if s else fallback
    except Exception:
        return fallback

def _cfg_bool(cfg: configparser.ConfigParser, section: str, option: str, fallback: bool) -> bool:
    truthy = {"1","true","yes","y","on"}
    falsy  = {"0","false","no","n","off"}
    try:
        if not cfg.has_option(section, option): return fallback
        raw = cfg.get(section, option)
        if raw is None: return fallback
        s = str(raw).strip().lower()
        if not s: return fallback
        if s in truthy: return True
        if s in falsy:  return False
        return fallback
    except Exception:
        return fallback

def _win_hidden_popen_kwargs():
    if os.name != "nt":
        return {}
    CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        return {"startupinfo": si, "creationflags": CREATE_NO_WINDOW}
    except Exception:
        return {"creationflags": CREATE_NO_WINDOW}

# ---------- Steam Workshop 目录发现 ----------
def _reg_str(root, subkey, name) -> Optional[str]:
    try:
        import winreg
        with winreg.OpenKey(root, subkey) as k:
            v, _ = winreg.QueryValueEx(k, name)
            return v if isinstance(v, str) else None
    except Exception:
        return None

def _drive_ready(p: Path) -> bool:
    try:
        if os.name != "nt": return True
        d = p.drive
        return (not d) or Path(d + "\\").exists()
    except Exception:
        return False

def _path_ready(p: Path) -> bool:
    return _drive_ready(p) and p.exists()

def _ensure_dir_ready(p: Path) -> Optional[Path]:
    if not _drive_ready(p):
        return None
    try:
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
        return p.resolve()
    except Exception:
        try:
            return p.resolve() if p.exists() else None
        except Exception:
            return None

def find_all_workshop_roots() -> List[Path]:
    roots: List[Path] = []
    try:
        import winreg
        sr = _reg_str(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath")
        if sr and Path(sr).exists():
            srp = Path(sr)
            libs = [srp]
            vdf = srp / "steamapps" / "libraryfolders.vdf"
            if vdf.exists():
                text = vdf.read_text(encoding="utf-8", errors="ignore")
                for m in re.finditer(r'"\d+"\s*\{\s*"path"\s*"([^"]+)"', text):
                    libs.append(Path(m.group(1)))
            for lib in libs:
                p = lib / "steamapps" / "workshop" / "content" / str(APPID_WE)
                rp = _ensure_dir_ready(p)
                if rp:
                    roots.append(rp)
    except Exception:
        pass

    for cand in [r"%ProgramFiles(x86)%\Steam\steamapps\workshop\content\431960",
                 r"%ProgramFiles%\Steam\steamapps\workshop\content\431960"]:
        p = Path(expand(cand))
        rp = _ensure_dir_ready(p)
        if rp and rp not in roots:
            roots.append(rp)

    uniq, seen = [], set()
    for r in roots:
        if r not in seen:
            uniq.append(r); seen.add(r)
    return uniq

def _candidate_we_exes_from_cfg(cfg: configparser.ConfigParser) -> List[Path]:
    raw = expand(cfg.get("paths","we_exe",fallback="")).strip()
    if not raw: return []
    p = Path(raw)
    cands = []
    if p.suffix.lower() == ".exe":
        cands.append(p)
    else:
        cands += [p / "wallpaper64.exe", p / "wallpaper32.exe", p / "wallpaper_engine.exe"]
    return cands

def _candidate_we_exes_from_system() -> List[Path]:
    cands: List[Path] = []
    try:
        import winreg
        sp = _reg_str(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath")
        if sp:
            base = Path(sp) / "steamapps" / "common" / "wallpaper_engine"
            cands += [base / "wallpaper64.exe", base / "wallpaper32.exe", base / "wallpaper_engine.exe"]
    except Exception:
        pass
    for env in ("%ProgramFiles(x86)%", "%ProgramFiles%"):
        base = Path(expand(env)) / "Steam" / "steamapps" / "common" / "wallpaper_engine"
        cands += [base / "wallpaper64.exe", base / "wallpaper32.exe", base / "wallpaper_engine.exe"]
    return cands

def locate_we_exe(cfg: configparser.ConfigParser) -> Optional[Path]:
    seen = set()
    for cand in _candidate_we_exes_from_cfg(cfg) + _candidate_we_exes_from_system():
        try:
            c = cand.resolve()
        except Exception:
            c = cand
        if c in seen: continue
        seen.add(c)
        if _path_ready(c):
            return c
    return None

def locate_workshop_root(cfg: configparser.ConfigParser) -> Optional[Path]:
    ws_root_cfg = expand(cfg.get("paths","workshop_root",fallback="")).strip()
    if ws_root_cfg:
        p = Path(ws_root_cfg)
        if not _drive_ready(p):
            return None
        if not p.exists():
            try:
                p.mkdir(parents=True, exist_ok=True)
                print(f"[workshop] 已创建配置指定的根目录：{p}")
            except Exception:
                return None
        return p.resolve()
    roots = find_all_workshop_roots()
    for r in roots:
        if _path_ready(r):
            return r
    return None

# ---------- HTTP ----------
def _make_session(https_proxy: str=""):
    try:
        import requests
    except ImportError as e:
        raise RuntimeError("缺少依赖：requests。请先执行 pip install requests") from e
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    s = requests.Session()
    retry = Retry(total=6, connect=6, read=6, backoff_factor=1.2,
                  status_forcelist=(429,500,502,503,504),
                  allowed_methods=frozenset({"GET","POST"}),
                  respect_retry_after_header=True, raise_on_status=False)
    ad = HTTPAdapter(max_retries=retry)
    s.mount("https://", ad); s.mount("http://", ad)
    if https_proxy:
        s.proxies.update({"https": https_proxy, "http": os.environ.get("http_proxy")})
    s.headers.update({
        "User-Agent": "we-auto-fetch/steamcmd-webapi-1.7 (+requests)",
        "Accept": "application/json, text/html,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s

# ---------- Web API 映射 & 调用 ----------
def map_sort_to_query(sort_name: str) -> Tuple[int,int]:
    s = (sort_name or "").lower()
    if s == "most recent": return 1, 0
    if s in ("top rated","most up votes"): return 11, 0
    if s in ("most subscriptions","most subscribed"): return 9, 0
    if s.startswith("most popular"):
        if "year" in s:  return 3, 365
        if "month" in s: return 3, 30
        if "week" in s:  return 3, 7
        if "day" in s or "today" in s: return 3, 1
        return 3, 7
    return 3, 7

# === 年龄映射/提取（以 tags 为主） ===
_AGE_TAG_TO_CANON = {
    "everyone": "G",
    "questionable": "PG13",
    "mature": "R",
}
_CANON_TO_WORKSHOP_TAG = {
    "G": "Everyone",
    "PG13": "Questionable",
    "R": "Mature",
}
# 一些 KV 可能写法
_AGE_NORMALIZE_KV = {
    "everyone": "G", "g": "G",
    "questionable": "PG13", "pg13": "PG13", "pg-13": "PG13",
    "mature": "R", "r": "R", "r18": "R", "adult": "R", "adultonly": "R", "adult only": "R",
}

def _kv_lookup(item: dict) -> Dict[str, str]:
    kv = {}
    for kvp in (item.get("kv_tags") or []):
        k_raw = kvp.get("key", "")
        v_raw = kvp.get("value", "")
        k = (k_raw or "").strip().lower()
        v = (v_raw or "").strip()
        if k and (k not in kv):
            kv[k] = v
    return kv

def _extract_normalized_age(item: dict) -> Optional[str]:
    # 1) 优先：从 tags 取 Everyone/Questionable/Mature
    for t in (item.get("tags") or []):
        tag = (t.get("tag") or "").strip().lower()
        if tag in _AGE_TAG_TO_CANON:
            return _AGE_TAG_TO_CANON[tag]
    # 2) 兜底：从 KV 的 Age Rating 相近键取
    kv = _kv_lookup(item)
    v = (kv.get("age rating") or kv.get("agerating") or kv.get("age_rating") or "").strip().lower()
    if not v:
        # 极少数把 "Age Rating: XXX" 塞入普通标签文本里
        for t in (item.get("tags") or []):
            tx = (t.get("tag") or "").strip().lower()
            if "age rating" in tx:
                for sep in (":", "-", "—", "–"):
                    if sep in tx:
                        v = tx.split(sep, 1)[1].strip().lower()
                        break
                if v: break
    v = v.replace("　", " ").replace("/", " ").strip()
    if not v: return None
    return _AGE_NORMALIZE_KV.get(v)

def _age_single_workshop_tag_from_csv(ages_csv: str) -> Optional[str]:
    """若 age 仅配置单一等级，则返回对应的 Workshop tag（Everyone/Questionable/Mature）。"""
    wanted = [x.upper() for x in parse_csv(ages_csv)]
    uniq = sorted(set(wanted))
    if len(uniq) != 1:
        return None
    return _CANON_TO_WORKSHOP_TAG.get(uniq[0])

def _age_match_any(item: dict, ages_csv: str) -> bool:
    wanted = {x.upper() for x in parse_csv(ages_csv)}
    if not wanted:
        return True  # 未配置 age 不过滤
    norm = _extract_normalized_age(item)
    if norm is None:
        # WE 基本都有年龄 tag；稳妥：不识别则不通过
        return False
    return norm in wanted
# === 结束：年龄映射/提取 ===

def query_files_webapi(cfg: configparser.ConfigParser) -> Tuple[List[int], Dict[int, dict], str]:
    key = (cfg.get("steam","api_key",fallback="") or "").strip()
    if not key:
        return [], {}, "no_key"

    https_proxy = cfg.get("network","https_proxy",fallback="").strip()
    sess = _make_session(https_proxy)

    sort_name = cfg.get("sort","method",fallback="Most Popular (Week)")
    qtype, days = map_sort_to_query(sort_name)
    pages = _cfg_int(cfg, "fallback", "pages", 3)
    page_size = _cfg_int(cfg, "filters", "numperpage", _cfg_int(cfg, "fallback", "page_size", 40))

    req_tags = [t for t in parse_csv(cfg.get("filters","show_only",fallback="")) if t.lower()!="approved"]
    req_tags += parse_csv(cfg.get("filters","tags",fallback=""))
    exc_tags = parse_csv(cfg.get("filters","exclude",fallback=""))

    # ★ 若 age= 单值，则把对应 Workshop Tag 放入 requiredtags（服务端筛选）
    age_tag = _age_single_workshop_tag_from_csv(cfg.get("filters","age",fallback=""))
    if age_tag:
        req_tags = list(dict.fromkeys(req_tags + [age_tag]))
    # ★ 若 types= 单值，同理
    type_tag = _single_type_workshop_tag_from_csv(cfg.get("filters","types",fallback=""))
    if type_tag:
        req_tags = list(dict.fromkeys(req_tags + [type_tag]))

    base_url = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
    ids: List[int] = []
    det: Dict[int, dict] = {}
    dbg: List[str] = []
    if req_tags:
        dbg.append(f"requiredtags={req_tags!r}")

    cursor = "*"
    for p in range(1, pages+1):
        payload = {
            "query_type": qtype,
            "appid": APPID_WE,
            "numperpage": page_size,
            "return_kv_tags": True,
            "return_tags": True,
            "return_children": False,
            "return_previews": False,
            "match_all_tags": True,
            "filetype": 0,
            "mature_content": True,
            "include_mature": True,
            "cache_max_age_seconds": 60,
        }
        if qtype == 3 and days:
            payload["days"] = days
            payload["include_recent_votes_only"] = False
        if req_tags:
            payload["requiredtags"] = req_tags
        if exc_tags:
            payload["excludedtags"] = exc_tags
        if cursor:
            payload["cursor"] = cursor
        else:
            payload["page"] = p

        params = {"key": key, "input_json": json.dumps(payload, separators=(",", ":"))}

        try:
            r = sess.get(base_url, params=params, timeout=(8, 25))
            dbg.append(f"p{p}: GET qtype={qtype}, days={days}, npp={page_size} -> HTTP {r.status_code}")
            if not r.ok:
                t = (r.text or "")[:200].replace("\n"," ")
                dbg.append(f"body: {t}")
                break

            resp = r.json().get("response", {}) or {}
            items = resp.get("publishedfiledetails") or resp.get("files") or resp.get("items") or []
            if not items:
                dbg.append(f"p{p}: empty items")
                break

            for it in items:
                fid = int(str(it.get("publishedfileid", "0")))
                if not fid: continue
                if fid not in det:
                    det[fid] = it
                    ids.append(fid)

            cursor = resp.get("next_cursor", "")
            if not cursor or len(items) < page_size:
                break

        except Exception as e:
            dbg.append(f"exception: {e!r}")
            break

    return ids, det, " | ".join(dbg)

# ---------- HTML 回退（仅当没有 api_key 时） ----------
def fetch_details(ids: List[int], https_proxy: str="") -> Dict[int, dict]:
    if not ids: return {}
    sess = _make_session(https_proxy)
    url = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
    out: Dict[int, dict] = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i+100]
        data = {"itemcount": len(chunk)}
        for idx, fid in enumerate(chunk):
            data[f"publishedfileids[{idx}]"] = str(fid)
        try:
            r = sess.post(url, data=data, timeout=(6,20))
            if not r.ok: continue
            arr = r.json().get("response",{}).get("publishedfiledetails",[]) or []
            for it in arr:
                fid = int(it.get("publishedfileid", 0))
                if fid: out[fid] = it
        except Exception:
            continue
    return out

def map_sort_html(sort_name: str) -> Tuple[str,int]:
    s = (sort_name or "").lower()
    if s == "top rated": return "vote", 0
    if s.startswith("most popular"):
        if "year" in s:  return "trend", 365
        if "month" in s: return "trend", 30
        if "week" in s:  return "trend", 7
        if "day" in s:   return "trend", 1
        return "trend", 7
    if s == "most recent": return "publicationdate", 0
    if s in ("most subscriptions","most subscribed"): return "totaluniquesubscriptions", 0
    return "trend", 7

def community_ids_html(sort_name: str, pages: int, per_page: int,
                       https_proxy: str = "", ages_csv: str = "", types_csv: str = "") -> List[int]:
    sess = _make_session(https_proxy)
    comm_sort, comm_days = map_sort_html(sort_name)
    out: List[int] = []; seen = set()
    base_url = "https://steamcommunity.com/workshop/browse/"
    headers = {"Referer": f"{base_url}?appid={APPID_WE}&browsesort={comm_sort}"}
    # requiredtags[] 支持多个值（服务端 AND）
    age_tag = _age_single_workshop_tag_from_csv(ages_csv)
    type_tag = _single_type_workshop_tag_from_csv(types_csv)
    required = []
    if age_tag:  required.append(age_tag)
    if type_tag: required.append(type_tag)

    for p in range(1, pages+1):
        params = {
            "appid": str(APPID_WE), "browsesort": comm_sort, "days": str(comm_days or 0),
            "actualsort": comm_sort, "l": "english", "numperpage": str(per_page), "p": str(p),
        }
        if required:
            params["requiredtags[]"] = required
        try:
            r = sess.get(base_url, params=params, headers=headers, timeout=(6,20))
            if not r.ok: continue
            html = r.text or ""
            for m in re.finditer(r'data-publishedfileid="(\d+)"', html):
                fid = int(m.group(1))
                if fid not in seen: seen.add(fid); out.append(fid)
            for m in re.finditer(r'/filedetails/\?id=(\d+)', html):
                fid = int(m.group(1))
                if fid not in seen: seen.add(fid); out.append(fid)
        except Exception:
            continue
    return out

# ---------- 过滤 ----------
_TYPE_ALIASES = {
    "video": {"video", "movie", "mp4", "webm"},
    "scene": {"scene", "scenery"},
    "web": {"web", "webpage", "html"},
    "application": {"application", "app"},
    "wallpaper": {"wallpaper"},
    "preset": {"preset"},
}

# 单一类型 → Workshop tag（供服务端 requiredtags 使用）
_TYPE_CANON_TO_TAG = {
    "video": "Video",
    "scene": "Scene",
    "web": "Web",
    "application": "Application",
    "wallpaper": "Wallpaper",
    "preset": "Preset",
}
def _single_type_workshop_tag_from_csv(types_csv: str) -> Optional[str]:
    """
    如果 [filters].types 只配置了一个值，则返回对应的 Workshop tag 字符串（如 "Scene"）。
    多个值返回 None（因为服务端 requiredtags 是 AND 逻辑，不适合多选 OR）。
    """
    vals = [t.strip().lower() for t in parse_csv(types_csv)]
    uniq = sorted(set(vals))
    if len(uniq) != 1:
        return None
    t = uniq[0]
    if t in _TYPE_CANON_TO_TAG:
        return _TYPE_CANON_TO_TAG[t]
    for canon, aliases in _TYPE_ALIASES.items():
        if t == canon or t in aliases:
            return _TYPE_CANON_TO_TAG.get(canon)
    return t.title() if t else None

def _type_matches(kv_type_val: str, tagset_lower: set, need_lower: set) -> bool:
    if not need_lower:
        return True
    if kv_type_val:
        t_l = kv_type_val.strip().lower()
        if t_l in need_lower:
            return True
        for canon, aliases in _TYPE_ALIASES.items():
            if t_l in aliases and (canon in need_lower or (need_lower & aliases)):
                return True
    if need_lower & tagset_lower:
        return True
    for t in tagset_lower:
        if not t.startswith("type"):
            continue
        for sep in (":", "-", "—", "–"):
            if sep in t:
                right = t.split(sep, 1)[1].strip().lower()
                if right in need_lower:
                    return True
                for canon, aliases in _TYPE_ALIASES.items():
                    if right in aliases and (canon in need_lower or (need_lower & aliases)):
                        return True
    return False

def filter_ids_with_details(base_ids: List[int], detail_map: Dict[int, dict], cfg: configparser.ConfigParser) -> List[int]:
    tags = parse_csv(cfg.get("filters","tags",fallback=""))
    show_only = parse_csv(cfg.get("filters","show_only",fallback=""))
    types_in  = parse_csv(cfg.get("filters","types",fallback=""))
    ages_csv = cfg.get("filters","age",fallback="")
    res = cfg.get("filters","resolution",fallback="")
    excluded = parse_csv(cfg.get("filters","exclude",fallback=""))

    need_types_lower = {t.strip().lower() for t in types_in if t.strip()}
    need_tags = [t for t in show_only if t.lower()!="approved"] + tags
    kv_exact = [{"key":"resolution","value":res.strip()}] if res.strip() else []

    out: List[int] = []
    for fid in base_ids:
        it = detail_map.get(fid, {})

        if excluded:
            low = {(t.get("tag") or "").strip().lower() for t in (it.get("tags") or [])}
            if any(x.lower() in low for x in excluded):
                continue

        if need_tags:
            tset = {(t.get("tag") or "").strip() for t in (it.get("tags") or [])}
            if not all(t in tset for t in need_tags):
                continue

        if kv_exact:
            kv = _kv_lookup(it)
            ok = True
            for cond in kv_exact:
                k = cond["key"].strip().lower()
                if (kv.get(k,"") or "").strip() != cond["value"]:
                    ok = False; break
            if not ok:
                continue

        # ★ 年龄过滤（tags 优先，OR）
        if not _age_match_any(it, ages_csv):
            continue

        if need_types_lower:
            kv = _kv_lookup(it)
            kv_type_val = kv.get("type", "")
            tagset_lower = {(t.get("tag") or "").strip().lower() for t in (it.get("tags") or [])}
            if not _type_matches(kv_type_val, tagset_lower, need_types_lower):
                continue

        out.append(fid)
    return out

# ---------- steamcmd：实时进度（隐藏黑窗） ----------
_PROGRESS_PAT = re.compile(r'(?P<pct>\d{1,3}(?:\.\d+)?)\s*%')
_SPEED_PAT = re.compile(r'(?P<spd>[0-9.]+\s*(?:B/s|KB/s|MB/s|GB/s))', re.I)

def _print_progress_line(line: str) -> None:
    pct = None; spd = None
    m = _PROGRESS_PAT.search(line)
    if m: pct = m.group('pct')
    m2 = _SPEED_PAT.search(line)
    if m2: spd = m2.group('spd')
    if pct or spd:
        extras = []
        if pct: extras.append(f"{pct}%")
        if spd: extras.append(spd)
        print(f"[progress] {' | '.join(extras)}")
    print(line.rstrip())

def steamcmd_download_batch(exe: Path, uid: str, pwd: Optional[str], guard: Optional[str],
                            ids: List[int]) -> Tuple[bool, str]:
    if not ids: return True, "no-op"
    args: List[str] = []
    if guard: args += ["+set_steam_guard_code", guard]
    if pwd:   args += ["+login", uid, pwd]
    else:     args += ["+login", uid]

    for wid in ids:
        print(f"[task] 下载条目：{wid}  https://steamcommunity.com/sharedfiles/filedetails/?id={wid}")
        args += ["+workshop_download_item", str(APPID_WE), str(wid), "validate"]
    args += ["+quit"]
    print("[steamcmd]", " ".join(args))

    proc = subprocess.Popen(
        [str(exe), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
        encoding="utf-8",
        errors="ignore",
        **_win_hidden_popen_kwargs()
    )
    all_out = io.StringIO()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            all_out.write(line)
            _print_progress_line(line)
    finally:
        proc.wait(timeout=3600)
    out_s = all_out.getvalue()

    ok_markers = (
        "Logged in",
        "Loading Steam API...OK",
        "Connecting anonymously to Steam Public",
        "Success. Downloaded item",
        "Success. App '431960'",
        "workshop_download_item <AppID>",
    )
    ok = (proc.returncode == 0) and any(m in out_s for m in ok_markers)
    if not ok:
        print(f"[error] steamcmd 退出码：{proc.returncode}")
        tail = "\n".join(out_s.splitlines()[-20:])
        print("[error] tail:\n" + tail)
    return ok, out_s

# ---------- 镜像 / 应用 / 清理 ----------
def mirror_dir(src: Path, dst: Path) -> bool:
    dst.mkdir(parents=True, exist_ok=True)
    try:
        rc = subprocess.run(
            ["robocopy", str(src), str(dst), "/MIR", "/NFL", "/NDL", "/NJH", "/NJS", "/NP"],
            capture_output=True, text=False,
            **_win_hidden_popen_kwargs()
        )
        if rc.returncode <= 7: return True
    except Exception:
        pass
    try:
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
            dst.mkdir(parents=True, exist_ok=True)
        for root, _, files in os.walk(src):
            rel = Path(root).relative_to(src)
            (dst/rel).mkdir(parents=True, exist_ok=True)
            for fn in files:
                sp = Path(root)/fn; dp = (dst/rel)/fn
                shutil.copy2(sp, dp)
        return True
    except Exception as e:
        print("[mirror] 复制失败：", e); return False

def find_entry(work_dir: Path) -> Optional[Path]:
    pj = work_dir / "project.json"
    if pj.exists(): return pj
    idx = work_dir / "index.html"
    if idx.exists(): return idx
    for p in work_dir.rglob("project.json"): return p
    for p in work_dir.rglob("index.html"):  return p
    vids = list(work_dir.rglob("*.mp4")) or list(work_dir.rglob("*.webm"))
    return vids[0] if vids else None

def apply_in_we(entry: Path, we_exe: Path, retries: int = 3, delay_s: float = 1.5) -> None:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            subprocess.run([str(we_exe), "-control", "openWallpaper", "-file", str(entry)], check=True)
            return
        except subprocess.CalledProcessError as e:
            last_err = e
            print(f"[apply/retry] 第 {attempt} 次失败，将在 {delay_s}s 后重试：{e}")
            time.sleep(delay_s)
        except Exception as e:
            last_err = e
            print(f"[apply/retry] 第 {attempt} 次异常，将在 {delay_s}s 后重试：{e}")
            time.sleep(delay_s)
    raise last_err if last_err else RuntimeError("apply_in_we: 未知错误")

def mirror_to_projects_backup(we_exe: Path, src_item_dir: Path, wid: int) -> Optional[Path]:
    we_dir = we_exe.parent
    backup_root = we_dir / "projects" / "backup"
    backup_root.mkdir(parents=True, exist_ok=True)
    dst = backup_root / str(wid)
    if mirror_dir(src_item_dir, dst): return dst
    return None

def delete_item_everywhere(wid: int, steamcmd_exe: Path, official_root: Path, we_exe: Path,
                           use_recycle_bin: bool=False) -> None:
    targets: List[Path] = []
    base_tmp = steamcmd_exe.parent / "steamapps" / "workshop" / "content" / str(APPID_WE) / str(wid)
    targets += [base_tmp, official_root / str(wid), we_exe.parent / "projects" / "backup" / str(wid)]
    if use_recycle_bin:
        trash = HERE / "Trash"; trash.mkdir(parents=True, exist_ok=True)
        for t in targets:
            if t.exists():
                try: shutil.move(str(t), str(trash / f"{t.name}-{int(time.time())}")); print(f"[cleanup] moved: {t}")
                except Exception as e: print(f"[cleanup] move failed: {t} -> {e}")
    else:
        for t in targets:
            if t.exists():
                try: shutil.rmtree(t, ignore_errors=True); print(f"[cleanup] deleted: {t}")
                except Exception as e: print(f"[cleanup] delete failed: {t} -> {e}")

# ---------- 日志 / 清理策略（不重写日志） ----------
_ID_IN_LINE = re.compile(r'id=(\d{6,})')

def _read_logged_ids(logp: Path) -> List[int]:
    ids: List[int] = []
    if not logp.exists(): return ids
    for line in logp.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = _ID_IN_LINE.search(line)
        if m:
            try: ids.append(int(m.group(1)))
            except Exception: pass
    uniq, seen = [], set()
    for i in ids:
        if i not in seen:
            uniq.append(i); seen.add(i)
    return uniq

def cleanup_all_others_if_needed(current_wid: int,
                                 cfg: configparser.ConfigParser,
                                 steamcmd_exe: Path,
                                 official_root: Path,
                                 we_exe: Path) -> None:
    one_per_run = _cfg_bool(cfg, "subscribe", "one_per_run", True)
    delete_prev = _cfg_bool(cfg, "cleanup", "delete_previous", False)
    keep_n = _cfg_int(cfg, "cleanup", "keep_last_n", 0)
    use_bin = _cfg_bool(cfg, "cleanup", "use_recycle_bin", False)
    protected = {int(x) for x in parse_csv(cfg.get("cleanup","protected_ids",fallback="")) if x.isdigit()}

    if not (one_per_run and delete_prev):
        return

    logp = HERE / cfg.get("logging","file",fallback="we_downloads.log")
    logged = _read_logged_ids(logp)

    keep: List[int] = [current_wid]
    if keep_n > 1:
        for wid in reversed(logged):
            if len(keep) >= keep_n: break
            if wid != current_wid and wid not in keep:
                keep.append(wid)
    keep_set = set(keep) | protected

    to_del = [wid for wid in logged if wid not in keep_set]
    try:
        for d in (official_root.iterdir() if official_root.exists() else []):
            if d.is_dir():
                try:
                    wid = int(d.name)
                    if wid not in keep_set and wid not in to_del:
                        to_del.append(wid)
                except Exception:
                    pass
    except Exception:
        pass

    if to_del:
        print(f"[cleanup] one_per_run 模式：删除历史 {len(to_del)} 项（保留 {len(keep_set)}）")
    for wid in to_del:
        if wid in protected:
            print(f"[cleanup] 跳过受保护：{wid}")
            continue
        delete_item_everywhere(wid, steamcmd_exe, official_root, we_exe, use_recycle_bin=use_bin)

# ---------- 状态 ----------
def load_state(path: Path) -> Dict:
    if path.exists():
        try: return json.loads(path.read_text(encoding="utf-8"))
        except Exception: pass
    return {"tracked_ids": [], "last_applied": None, "history": [], "failed_recent": [], "cursor": 0}

def save_state(path: Path, state: Dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------- 候选获取（支持过滤后最小候选数） ----------
def get_auto_candidates(cfg: configparser.ConfigParser) -> List[int]:
    key = (cfg.get("steam","api_key",fallback="") or "").strip()
    min_candidates = _cfg_int(cfg, "filters", "min_candidates", 0)
    https_proxy = cfg.get("network","https_proxy",fallback="").strip()
    sort_name = cfg.get("sort","method",fallback="Most Popular (Week)")
    pages = _cfg_int(cfg, "fallback", "pages", 3)
    page_size = _cfg_int(cfg, "filters", "numperpage", _cfg_int(cfg, "fallback", "page_size", 40))

    if key and min_candidates > 0:
        print(f"[auto/api] min_candidates={min_candidates}, page_size={page_size}")
        from requests.adapters import HTTPAdapter  # 确保 requests 存在
        sess = _make_session(https_proxy)

        qtype, days = map_sort_to_query(sort_name)
        req_tags = [t for t in parse_csv(cfg.get("filters","show_only",fallback="")) if t.lower()!="approved"]
        req_tags += parse_csv(cfg.get("filters","tags",fallback=""))
        exc_tags = parse_csv(cfg.get("filters","exclude",fallback=""))

        # ★ 单值 age/type → 服务端 requiredtags
        age_tag = _age_single_workshop_tag_from_csv(cfg.get("filters","age",fallback=""))
        if age_tag:
            req_tags = list(dict.fromkeys(req_tags + [age_tag]))
        type_tag = _single_type_workshop_tag_from_csv(cfg.get("filters","types",fallback=""))
        if type_tag:
            req_tags = list(dict.fromkeys(req_tags + [type_tag]))

        base_url = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
        ids: List[int] = []
        det: Dict[int, dict] = {}
        cursor = "*"
        p = 0
        max_pages = max(pages, _cfg_int(cfg, "fallback", "max_pages", 30))

        while True:
            p += 1
            payload = {
                "query_type": qtype,
                "appid": APPID_WE,
                "numperpage": page_size,
                "return_kv_tags": True,
                "return_tags": True,
                "return_children": False,
                "return_previews": False,
                "match_all_tags": True,
                "filetype": 0,
                "mature_content": True,
                "include_mature": True,
                "cache_max_age_seconds": 60,
            }
            if qtype == 3 and days:
                payload["days"] = days
                payload["include_recent_votes_only"] = False
            if req_tags:
                payload["requiredtags"] = req_tags
            if exc_tags:
                payload["excludedtags"] = exc_tags
            if cursor:
                payload["cursor"] = cursor
            else:
                payload["page"] = p

            params = {"key": key, "input_json": json.dumps(payload, separators=(",", ":"))}

            try:
                r = sess.get(base_url, params=params, timeout=(8, 25))
                print(f"[auto/api] p{p} -> HTTP {r.status_code}")
                if not r.ok:
                    t = (r.text or "")[:200].replace("\n"," ")
                    print(f"[auto/api] body: {t}")
                    break

                resp = r.json().get("response", {}) or {}
                items = resp.get("publishedfiledetails") or resp.get("files") or resp.get("items") or []
                if not items:
                    print(f"[auto/api] p{p}: empty items")
                    break

                for it in items:
                    fid = int(str(it.get("publishedfileid", "0")))
                    if not fid: continue
                    if fid not in det:
                        det[fid] = it
                        ids.append(fid)

                filtered = filter_ids_with_details(ids, det, cfg)
                print(f"[auto/api] after p{p}: raw={len(ids)} filtered={len(filtered)}")
                if len(filtered) >= min_candidates:
                    print("[auto/api] 达到 min_candidates，停止继续翻页。")
                    return filtered

                cursor = resp.get("next_cursor", "")
                if (not cursor) or (len(items) < page_size):
                    print("[auto/api] 没有更多页面。")
                    return filtered
                if p >= max_pages:
                    print(f"[auto/api] 达到 max_pages={max_pages}。")
                    return filtered

            except Exception as e:
                print(f"[auto/api] exception: {e!r}")
                break

        filtered = filter_ids_with_details(ids, det, cfg)
        print(f"[auto/api] fallback filtered={len(filtered)}")
        return filtered

    if key:
        ids_api, det_api, dbg = query_files_webapi(cfg)
        print(f"[auto/api] debug: {dbg}")
        if ids_api:
            filtered = filter_ids_with_details(ids_api, det_api, cfg)
            print(f"[auto/api] candidates after filter: {len(filtered)} (raw {len(ids_api)})")
            return filtered
        print("[auto/api] 0 条（或请求失败）。因为配置了 api_key，不回退 HTML。请检查 filters/sort。")
        return []

    # HTML 回退（无 api_key）
    ages_csv = cfg.get("filters","age",fallback="")
    types_csv = cfg.get("filters","types",fallback="")
    ids_html = community_ids_html(sort_name, pages, page_size,
                                  https_proxy=https_proxy, ages_csv=ages_csv, types_csv=types_csv)
    if not ids_html: return []
    det_more = fetch_details(ids_html, https_proxy=https_proxy)
    filtered = filter_ids_with_details(ids_html, det_more, cfg)
    print(f"[auto/html] candidates after filter: {len(filtered)} (from {len(ids_html)} raw)")
    return filtered

# ---------- 主执行（支持 skip 连续换下一个） ----------
def run_once(cfg: configparser.ConfigParser) -> str:
    steamcmd_path = expand(cfg.get("paths","steamcmd",fallback=""))
    if not steamcmd_path: raise RuntimeError("请在 [paths] steamcmd= 指定 steamcmd.exe")
    steamcmd_exe = Path(steamcmd_path)
    if not steamcmd_exe.exists(): raise RuntimeError(f"未找到 steamcmd：{steamcmd_exe}")

    we_exe = locate_we_exe(cfg)
    if not we_exe:
        print("[wait] 未检测到 Wallpaper Engine 可执行文件（可能磁盘未就绪/ISCSI 未挂载）。将于下个周期重试。")
        return "WAIT_WE"

    official_root = locate_workshop_root(cfg)
    if not official_root:
        print("[wait] 未发现已就绪的 Workshop 目录（可能 Steam 库所在磁盘未挂载或目录尚未创建）。将于下个周期重试。")
        return "WAIT_WS"
    print("[workshop] official root:", official_root)

    ids_conf = [int(x) for x in parse_csv(cfg.get("subscribe","ids",fallback="")) if x.isdigit()]
    if ids_conf:
        ids_all = list(dict.fromkeys(ids_conf))
        print(f"[subscribe] ids from config: {len(ids_all)}")
    else:
        ids_all = get_auto_candidates(cfg)

    if not ids_all:
        print("[pick] 无候选；请放宽 [filters] 或在 [subscribe] 填 ids。")
        return "NO_CANDIDATES"

    state_file = HERE / cfg.get("paths","state_file",fallback="we_auto_state.json")
    state = load_state(state_file)

    logp = HERE / cfg.get("logging","file",fallback="we_downloads.log")
    seen_ids = set(_read_logged_ids(logp))
    try:
        seen_ids.update(_safe_int(x, 0) for x in state.get("history", []) if _safe_int(x, 0) > 0)
    except Exception:
        pass
    fresh_ids = [i for i in ids_all if i not in seen_ids]
    if fresh_ids:
        print(f"[rotate] 优先未用过的候选：{len(fresh_ids)} / {len(ids_all)}")
        ids_all = fresh_ids
    else:
        print("[rotate] 候选全部都在历史里；暂时允许重复（已尽力避免）。")

    one_per_run = _cfg_bool(cfg, "subscribe", "one_per_run", True)
    rotate_if_all_done = _cfg_bool(cfg, "subscribe", "rotate_if_all_done", True)
    max_attempts = _cfg_int(cfg, "subscribe", "max_attempts_per_run", 5)

    n = len(ids_all)
    cur = _safe_int(state.get("cursor", 0), 0)
    if cur >= n:
        if rotate_if_all_done: cur = 0
        else:
            print("[pick] 所有候选已轮完；等待下次刷新。")
            save_state(state_file, state)
            return "DONE"

    if one_per_run:
        attempt_ids: List[int] = []
        max_try = min(max_attempts, n if rotate_if_all_done else (n - cur))
        for k in range(max_try):
            idx = cur + k
            if idx >= n:
                if rotate_if_all_done:
                    idx = (cur + k) % n
                else:
                    break
            attempt_ids.append(ids_all[idx])
        print(f"[pick] 本轮尝试顺序（最多 {len(attempt_ids)} 次）：{attempt_ids}")
    else:
        attempt_ids = ids_all
        print(f"[pick] 非 one_per_run 模式：本轮将处理 {len(attempt_ids)} 个条目")

    applied = False
    attempts_made = 0
    base_tmp_root = steamcmd_exe.parent / "steamapps" / "workshop" / "content" / str(APPID_WE)
    current_wid: Optional[int] = None

    for wid in attempt_ids:
        attempts_made += 1
        current_wid = wid

        username = cfg.get("auth","steam_username",fallback="").strip() or os.environ.get("STEAM_USERNAME","").strip()
        password = cfg.get("auth","steam_password",fallback=os.environ.get("STEAM_PASSWORD","")).strip() or None
        guard    = cfg.get("auth","steam_guard_code",fallback=os.environ.get("STEAM_GUARD_CODE","")).strip() or None
        if not username:
            raise RuntimeError("请在 [auth] steam_username= 配置你的 Steam 账号（密码可留空以复用 steamcmd 已保存会话）")
        print(f"[login] 账号：{username}（若未提供密码/验证码将尝试用已保存凭证）")
        ok, out = steamcmd_download_batch(steamcmd_exe, username, password, guard, [wid])
        if not ok:
            save_state(state_file, state)
            raise RuntimeError("steamcmd 登录或下载失败")

        src = base_tmp_root / str(wid)
        if not src.exists() or not any(src.rglob("*")):
            print(f"[skip] 未找到下载目录：{src}（可能是合集占位/受限条目），尝试下一个...")
            state.setdefault("failed_recent", []).append(wid)
            if one_per_run:
                continue
            else:
                continue

        dst = official_root / str(wid)
        print(f"[mirror] {src} -> {dst}")
        if not mirror_dir(src, dst):
            print(f"[warn] 镜像失败：{wid}；继续下一个。")
            state.setdefault("failed_recent", []).append(wid)
            if one_per_run:
                continue
            else:
                continue

        proj_dst = mirror_to_projects_backup(we_exe, dst, wid)
        if proj_dst: print(f"[integrate] mirrored to projects/backup: {proj_dst}")

        entry = find_entry(dst) or find_entry(src)
        if not entry:
            print(f"[warn] 未找到可应用入口文件（project.json/index.html/视频），跳过 {wid}")
            state.setdefault("failed_recent", []).append(wid)
            if one_per_run:
                continue
            else:
                continue

        print(f"[apply] {entry}")
        try:
            apply_in_we(entry, we_exe)
            applied = True
            state["last_applied"] = wid
            hist = state.get("history", []); hist.append(wid); state["history"] = hist[-500:]
            if _cfg_bool(cfg,"logging","enable",True):
                with (HERE / cfg.get("logging","file",fallback="we_downloads.log")).open("a", encoding="utf-8") as f:
                    f.write(f"[{now_str()}] https://steamcommunity.com/sharedfiles/filedetails/?id={wid}\n")
            cleanup_all_others_if_needed(wid, cfg, steamcmd_exe, official_root, we_exe)

            prev_tracked: List[int] = []
            for t in state.get("tracked_ids", []):
                ti = _safe_int(t, 0)
                if ti > 0:
                    prev_tracked.append(ti)
            tracked = list(dict.fromkeys([wid] + prev_tracked))
            state["tracked_ids"] = tracked[:30]

        except subprocess.CalledProcessError as e:
            print("[warn] 应用失败：", e)
            state.setdefault("failed_recent", []).append(wid)
            applied = False
        except Exception as e:
            print("[warn] 应用异常：", e)
            state.setdefault("failed_recent", []).append(wid)
            applied = False

        if one_per_run and applied:
            break

    if attempts_made > 0:
        if _cfg_bool(cfg, "subscribe", "rotate_if_all_done", True):
            state["cursor"] = (cur + attempts_made) % n
        else:
            state["cursor"] = min(n, cur + attempts_made)

    if one_per_run and (not applied) and (current_wid is not None):
        dst_try = official_root / str(current_wid)
        src_try = steamcmd_exe.parent / "steamapps" / "workshop" / "content" / str(APPID_WE) / str(current_wid)
        entry = find_entry(dst_try) or find_entry(src_try)
        if entry:
            print(f"[apply/fallback] {entry}")
            try:
                apply_in_we(entry, we_exe); applied = True
            except Exception as e:
                print("[warn] 兜底应用失败：", e)

    save_state(state_file, state)
    print("[done] 本轮完成。")
    return "DONE"

# =========================
# RUN_NOW 事件：唤醒并立刻执行一轮
# =========================
# 说明：
# - 托盘/外部进程调用 SetEvent(Global\\WEAutoTrayRunNow_xxx) 后，本进程会从等待中醒来，立即 run_once。
# - 事件为“手动复位”，收到后本进程会 ResetEvent，避免粘连。
if os.name == "nt":
    kernel32 = ctypes.windll.kernel32
else:
    kernel32 = None
WAIT_OBJECT_0 = 0x00000000

def _run_now_event_name() -> str:
    try:
        base = str(Path(sys.executable).resolve())
    except Exception:
        base = sys.argv[0]
    h = hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:8]
    return f"Global\\WEAutoTrayRunNow_{h}"

def _create_named_event_manual_reset(name: str, initial: bool=False):
    if os.name != "nt" or not kernel32:
        return None
    return kernel32.CreateEventW(None, True, bool(initial), name)

def _open_named_event(name: str):
    if os.name != "nt" or not kernel32:
        return None
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    SYNCHRONIZE = 0x00100000
    EVENT_MODIFY_STATE = 0x0002
    return kernel32.OpenEventW(SYNCHRONIZE | EVENT_MODIFY_STATE, False, name)

def _reset_event(h) -> None:
    if os.name == "nt" and kernel32 and h:
        try: kernel32.ResetEvent(h)
        except Exception: pass

def _wait_run_now_or_timeout(h, timeout_s: float) -> bool:
    """等待 RUN_NOW 事件或超时；返回 True 表示被事件触发提前唤醒。"""
    if os.name != "nt" or not kernel32 or not h:
        time.sleep(max(0.0, float(timeout_s))); return False
    ms = max(0, int(timeout_s * 1000))
    rc = kernel32.WaitForSingleObject(h, ms)
    if rc == WAIT_OBJECT_0:
        _reset_event(h)
        print("[wake] 收到 RUN_NOW 事件，提前执行一轮。")
        return True
    return False

# ---------- 入口 ----------
def main():
    # 可选：单次执行模式（不进入循环；便于脚本/计划任务调用）
    if "--once" in sys.argv:
        cfg = read_conf()
        try:
            run_once(cfg)
        except Exception as e:
            print("[error/once]", e)
            sys.exit(1)
        sys.exit(0)

    cfg = read_conf()
    mode = cfg.get("subscribe","mode",fallback="steamcmd").strip().lower()
    if mode != "steamcmd":
        print(f"[exit] [subscribe].mode={mode}；本脚本仅实现 steamcmd 模式。"); return

    # NEW: 创建/打开 RUN_NOW 事件
    run_now_evt = None
    if os.name == "nt":
        try:
            name = _run_now_event_name()
            run_now_evt = _open_named_event(name) or _create_named_event_manual_reset(name, initial=False)
        except Exception:
            run_now_evt = None

    run_on_start = _cfg_bool(cfg, "schedule", "run_on_startup", True)
    interval_s = parse_interval(cfg.get("schedule","interval",fallback=""))
    detect_s = parse_interval(cfg.get("schedule","detect_interval", fallback="5m"))
    if detect_s <= 0: detect_s = 300

    status = "INIT"
    if run_on_start:
        try:
            status = run_once(cfg)
        except Exception as e:
            print("[error/startup]", e)
            status = "ERROR"

    if interval_s <= 0:
        # 无主循环，仅在 WAIT_* 下检测；用“事件或超时”代替纯 sleep
        while isinstance(status, str) and status.startswith("WAIT_"):
            _wait_run_now_or_timeout(run_now_evt, detect_s)
            try:
                status = run_once(cfg)
            except KeyboardInterrupt:
                print("\n[exit] 用户中断"); break
            except Exception as e:
                print("[error/loop]", e); status = "ERROR"
        return

    while True:
        try:
            sleep_for = detect_s if (isinstance(status, str) and status.startswith("WAIT_")) else interval_s
            _wait_run_now_or_timeout(run_now_evt, sleep_for)
            status = run_once(cfg)
        except KeyboardInterrupt:
            print("\n[exit] 用户中断"); break
        except Exception as e:
            print("[error/loop]", e); status = "ERROR"

if __name__ == "__main__":
    main()