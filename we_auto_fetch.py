# -*- coding: utf-8 -*-
r"""
we_auto_fetch.py — Steam Workshop -> Wallpaper Engine 自动拉取/筛选/应用

本版要点（2025-10-01）：
- [filters] 的 show_only / types / age / resolution / tags：**维度内 OR、维度间 AND**；
  exclude 则在合集中再减去（同时传给服务端 excludedtags[] 预过滤）。
- 服务端抓取 = “单 tag 分次抓取并集”，本地再做“维度 AND”过滤；达到 min_candidates 才早停。
- 分辨率兼容多写法（1280x720 / 1280 × 720 / 1280 x 720），tag 或 KV 都能匹配。
- RUN_NOW 命名事件（配合托盘“立即更换一次”）、--once 单次执行模式。
- 控制台会输出：各维度配置、抓取分页摘要、应用时该条目的 Type/Age/Resolution/Genres 等元信息。
- HTML 排序映射修正：mostrecent / lastupdated / totaluniquesubscribers / trend(+days)。
- **修复“卡在即将应用”**：应用壁纸时不再等待 wallpaper32/64.exe 退出；先确保 WE 在运行，再用短超时发送 -control 命令。

新增（2025-10-14）：
- [we_control] 在检测到 Wallpaper Engine 正在运行时（与由谁启动无关），可按配置延迟执行一次自定义指令。
  配置示例：
    [we_control]
    enable = true
    cmd    = -control closeWallpaper -monitor 1
    delay  = 3s

新增（2026-01-20）：
- [filters] 支持按“标题包含关键字”与“上传者 SteamID64”剔除候选：
    title_exclude_contains = xxx, yyy
    creator_exclude_ids    = 7656119..., https://steamcommunity.com/profiles/7656119...
"""

from __future__ import annotations
import configparser, json, os, re, shutil, subprocess, sys, time, io, ctypes, hashlib, locale, threading
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
            _print_filters_summary(cfg)
            return cfg
    tried = "\n  - " + "\n  - ".join(str(p) for p in candidates)
    raise RuntimeError("未找到配置文件；已尝试：" + tried)

# ---------- 小工具 ----------
def expand(p: str) -> str:
    return os.path.expandvars((p or "").strip())

def parse_csv(s: str) -> List[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]

_STEAMID64_RE = re.compile(r"(?<!\d)(\d{17})(?!\d)")

def parse_steamid64_list(s: str) -> List[str]:
    """
    支持：
      - 直接填 SteamID64（17 位数字）
      - 直接填个人主页 URL，例如 https://steamcommunity.com/profiles/7656...
    返回：去重保序的 SteamID64 字符串列表。
    """
    out: List[str] = []
    seen = set()
    for token in parse_csv(s):
        for m in _STEAMID64_RE.finditer(token):
            sid = m.group(1)
            if sid not in seen:
                seen.add(sid); out.append(sid)
    return out

def _title_block_substrings(cfg: configparser.ConfigParser) -> List[str]:
    # 统一用 casefold 做不区分大小写匹配
    return [x.casefold() for x in parse_csv(cfg.get("filters", "title_exclude_contains", fallback="")) if x]

def _creator_block_ids(cfg: configparser.ConfigParser) -> set[str]:
    return set(parse_steamid64_list(cfg.get("filters", "creator_exclude_ids", fallback="")))

def filter_ids_meta_only(base_ids: List[int], detail_map: Dict[int, dict],
                         cfg: configparser.ConfigParser) -> List[int]:
    """
    只做“元信息过滤”（标题包含关键字 / 上传者 SteamID64），不做 tags 的 OR-AND 维度过滤。
    用途：当 [subscribe].ids 手工指定时，仍希望能按标题/上传者剔除。
    """
    title_blk = _title_block_substrings(cfg)
    creator_blk = _creator_block_ids(cfg)
    if (not title_blk) and (not creator_blk):
        return list(base_ids)

    removed_title = 0
    removed_creator = 0
    ex_title: List[tuple[int, str, str]] = []
    ex_creator: List[tuple[int, str, str]] = []
    out: List[int] = []
    for fid in base_ids:
        it = detail_map.get(fid, {})

        if title_blk:
            title = (it.get("title") or "")
            t_low = str(title).casefold()
            if t_low and any(sub in t_low for sub in title_blk):
                removed_title += 1
                if len(ex_title) < 5:
                    creator = it.get("creator")
                    ex_title.append((fid, str(title).strip(), str(creator).strip() if creator is not None else "-"))
                continue

        if creator_blk:
            creator = it.get("creator")
            if creator is not None and (str(creator).strip() in creator_blk):
                removed_creator += 1
                if len(ex_creator) < 5:
                    title = (it.get("title") or "")
                    ex_creator.append((fid, str(title).strip(), str(creator).strip()))
                continue

        out.append(fid)

    if removed_title or removed_creator:
        print(f"[filters/meta] 已剔除：title 命中 {removed_title} 个 / creator 命中 {removed_creator} 个；剩余 {len(out)} 个候选。")
        for fid, title, creator in ex_title:
            t = title.replace("\n", " ").strip()
            if len(t) > 80: t = t[:77] + "..."
            print(f"[filters/meta]  - title 命中：{fid} | Creator: {creator} | Title: {t}")
        for fid, title, creator in ex_creator:
            t = title.replace("\n", " ").strip()
            if len(t) > 80: t = t[:77] + "..."
            print(f"[filters/meta]  - creator 命中：{fid} | Creator: {creator} | Title: {t}")
    return out

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

# ---------- Wallpaper Engine 运行检测/确保运行 ----------
def _is_proc_running(*names: str) -> bool:
    """不用 psutil，直接 tasklist 粗查。"""
    if os.name != "nt":
        return False
    try:
        out = subprocess.check_output(["tasklist"], **_win_hidden_popen_kwargs())
        enc = "mbcs" if os.name == "nt" else (locale.getpreferredencoding(False) or "utf-8")
        text = out.decode(enc, errors="ignore").lower()
        return any(n.lower() in text for n in names)
    except Exception:
        return False

def _ensure_we_running(we_bin: Path, wait_s: float = 15.0) -> None:
    """
    确保 Wallpaper Engine 主进程在运行；若没运行就静默拉起并轮询到就绪。
    """
    if _is_proc_running("wallpaper64.exe", "wallpaper32.exe", "wallpaper_engine.exe"):
        return
    subprocess.Popen([str(we_bin)], **_win_hidden_popen_kwargs())  # 不等待，静默启动
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if _is_proc_running("wallpaper64.exe", "wallpaper32.exe", "wallpaper_engine.exe"):
            return
        time.sleep(0.3)

# ===== 新增：WE 运行后执行一次自定义指令（不依赖启动来源/状态跃迁） =====
_WE_START_CMD_DONE = False

def _maybe_run_custom_we_cmd(cfg: configparser.ConfigParser, we_exe: Optional[Path] = None) -> None:
    """
    条件：
      - [we_control].enable = true
      - Wallpaper Engine 正在运行（不管谁启动的，程序也可能更晚启动）
      - 本进程尚未执行过（只执行一次）
    行为：
      - 等待 [we_control].delay 后，用 we_exe + [we_control].cmd 作为参数启动一次
    """
    global _WE_START_CMD_DONE
    if _WE_START_CMD_DONE:
        return
    if not _cfg_bool(cfg, "we_control", "enable", False):
        return
    raw_cmd = (cfg.get("we_control", "cmd", fallback="") or "").strip()
    if not raw_cmd:
        return
    delay_s = parse_interval(cfg.get("we_control", "delay", fallback="0s"))
    if we_exe is None:
        we_exe = locate_we_exe(cfg)
    if not we_exe or not we_exe.exists():
        return
    # 仅基于“是否正在运行”的静态判定，不做“从未运行->运行”的跃迁判断
    if not _is_proc_running("wallpaper64.exe", "wallpaper32.exe", "wallpaper_engine.exe"):
        return

    # 标记为已安排，避免重复；采用后台线程延迟执行
    _WE_START_CMD_DONE = True

    def _runner():
        try:
            if delay_s > 0:
                time.sleep(delay_s)
            try:
                import shlex
                args = shlex.split(raw_cmd, posix=False)
            except Exception:
                args = raw_cmd.split()
            print(f"[we_control] 执行：{we_exe.name} {raw_cmd}（延迟 {delay_s}s）")
            subprocess.Popen([str(we_exe), *args], **_win_hidden_popen_kwargs())
        except Exception as e:
            print("[we_control] 运行失败：", e)

    threading.Thread(target=_runner, daemon=True).start()

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
        "User-Agent": "we-auto-fetch/steamcmd-webapi-or-AND-1.2 (+requests)",
        "Accept": "application/json, text/html,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s

# ---------- Web API 映射 ----------
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
    if s in ("last updated","recently updated","updated"):  # WebAPI 没有直接映射，退回 Most Recent
        return 1, 0
    return 3, 7

# ---------- 类型/年龄/分辨率 ----------
_TYPE_ALIASES = {
    "video": {"video", "movie", "mp4", "webm"},
    "scene": {"scene", "scenery"},
    "web": {"web", "webpage", "html"},
    "application": {"application", "app"},
    "wallpaper": {"wallpaper"},
    "preset": {"preset"},
}
_TYPE_CANON_TO_TAG = {
    "video": "Video",
    "scene": "Scene",
    "web": "Web",
    "application": "Application",
    "wallpaper": "Wallpaper",
    "preset": "Preset",
}

_AGE_CANON_TO_TAG = {
    "G": "Everyone",
    "PG13": "Questionable",
    "R": "Mature",
}

def _normalize_resolution_variants(s: str) -> List[str]:
    s = (s or "").strip()
    if not s:
        return []
    s_norm = s.replace("×", "x").replace("X", "x").replace("*", "x")
    m = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", s_norm)
    if not m:
        return [s]
    w, h = m.group(1), m.group(2)
    return [f"{w} x {h}", f"{w}x{h}", f"{w} × {h}"]

def _norm_tag(s: str) -> str:
    return (s or "").lower().replace("×","x").replace("*","x").replace(" ", "").strip()

# ---------- 维度构建（维度内 OR、维度间 AND） ----------
def _build_dimensions(cfg: configparser.ConfigParser):
    # genres（show_only + tags）
    genres = parse_csv(cfg.get("filters","show_only",fallback="")) + parse_csv(cfg.get("filters","tags",fallback=""))
    genres_norm = {_norm_tag(x) for x in genres if x}

    # types
    types_in = [t.strip().lower() for t in parse_csv(cfg.get("filters","types",fallback=""))]
    type_tags = []
    for t in types_in:
        if t in _TYPE_CANON_TO_TAG:
            type_tags.append(_TYPE_CANON_TO_TAG[t])
        else:
            hit = False
            for canon, aliases in _TYPE_ALIASES.items():
                if t == canon or t in aliases:
                    type_tags.append(_TYPE_CANON_TO_TAG.get(canon, t.title()))
                    hit = True
                    break
            if not hit:
                type_tags.append(t.title())
    types_norm = {_norm_tag(x) for x in type_tags}

    # age
    ages_in = [x.strip().upper() for x in parse_csv(cfg.get("filters","age",fallback=""))]
    age_tags = [_AGE_CANON_TO_TAG[a] for a in ages_in if a in _AGE_CANON_TO_TAG]
    ages_norm = {_norm_tag(x) for x in age_tags}

    # resolution
    res_in = parse_csv(cfg.get("filters","resolution",fallback=""))
    res_sets = []
    for r in res_in:
        vars = _normalize_resolution_variants(r)
        if vars:
            res_sets.append({_norm_tag(x) for x in vars})

    # exclude
    exclude_norm = {_norm_tag(x) for x in parse_csv(cfg.get("filters","exclude",fallback=""))}

    return {
        "genres_norm": genres_norm,
        "types_norm": types_norm,
        "ages_norm": ages_norm,
        "res_sets": res_sets,
        "exclude_norm": exclude_norm,
    }

def _print_filters_summary(cfg: configparser.ConfigParser):
    dims = _build_dimensions(cfg)
    def fmt(s): return ", ".join(sorted(s)) if s else "(未设)"
    def fmt_res(rs): return ", ".join(sorted(next(iter(r)) for r in rs)) if rs else "(未设)"
    print("[filters] 维度（OR-AND 模式）：")
    print("  - Genres(show_only+tags):", fmt(dims["genres_norm"]))
    print("  - Types:", fmt(dims["types_norm"]))
    print("  - Ages:", fmt(dims["ages_norm"]))
    print("  - Resolutions:", fmt_res(dims["res_sets"]))
    print("  - Exclude:", fmt(dims["exclude_norm"]))
    # 元信息过滤（不参与 tag 维度的 OR-AND，但属于候选剔除条件）
    title_blk = _title_block_substrings(cfg)
    creator_blk = _creator_block_ids(cfg)
    print("  - Title exclude contains:", ", ".join(title_blk) if title_blk else "(未设)")
    print("  - Creator exclude ids:", ", ".join(sorted(creator_blk)) if creator_blk else "(未设)")

# ---------- 构造“查询用”的原始 include tag 列表（用于 requiredtags） ----------
def _include_plain_tags_raw_for_queries(cfg: configparser.ConfigParser) -> List[str]:
    inc: List[str] = []
    # 原始 show_only + tags（保持用户的写法，含空格大小写）
    inc += parse_csv(cfg.get("filters","show_only",fallback=""))
    inc += parse_csv(cfg.get("filters","tags",fallback=""))
    # types 映射为标准可见 tag（首字母大写）
    types_in = [t.strip().lower() for t in parse_csv(cfg.get("filters","types",fallback=""))]
    for t in types_in:
        if t in _TYPE_CANON_TO_TAG:
            inc.append(_TYPE_CANON_TO_TAG[t])
        else:
            hit = False
            for canon, aliases in _TYPE_ALIASES.items():
                if t == canon or t in aliases:
                    inc.append(_TYPE_CANON_TO_TAG.get(canon, t.title())); hit = True; break
            if not hit:
                inc.append(t.title())
    # age
    ages_in = [x.strip().upper() for x in parse_csv(cfg.get("filters","age",fallback=""))]
    inc += [_AGE_CANON_TO_TAG[a] for a in ages_in if a in _AGE_CANON_TO_TAG]
    # 去重保序
    seen, uniq = set(), []
    for x in inc:
        if x and x not in seen:
            uniq.append(x); seen.add(x)
    return uniq

# ---------- WebAPI：按单 tag 抓取并集 ----------
def _make_session_for_cfg(cfg):
    return _make_session(cfg.get("network","https_proxy",fallback="").strip())

def _query_webapi_single_tag(sess, key: str, qtype: int, days: int, npp: int,
                             req_tag: Optional[str], exc_tags: List[str], cursor: str) -> Tuple[Dict, str]:
    base_url = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
    payload = {
        "query_type": qtype, "appid": APPID_WE, "numperpage": npp,
        "return_kv_tags": True, "return_tags": True,
        "return_children": False, "return_previews": False,
        "match_all_tags": True, "filetype": 0,
        "mature_content": True, "include_mature": True,
        "cache_max_age_seconds": 60,
    }
    if qtype == 3 and days:
        payload["days"] = days
        payload["include_recent_votes_only"] = False
    if req_tag:
        payload["requiredtags"] = [req_tag]
    if exc_tags:
        payload["excludedtags"] = exc_tags
    if cursor:
        payload["cursor"] = cursor

    params = {"key": key, "input_json": json.dumps(payload, separators=(",", ":"))}
    r = sess.get(base_url, params=params, timeout=(8, 25))
    if not r.ok:
        return {}, ""
    try:
        resp = r.json().get("response", {}) or {}
        return resp, resp.get("next_cursor", "")
    except Exception:
        return {}, ""

def query_files_webapi_union_AND(cfg: configparser.ConfigParser) -> Tuple[List[int], Dict[int,dict], str]:
    key = (cfg.get("steam","api_key",fallback="") or "").strip()
    if not key:
        return [], {}, "no_key"

    sess = _make_session_for_cfg(cfg)
    sort_name = cfg.get("sort","method",fallback="Most Popular (Week)")
    qtype, days = map_sort_to_query(sort_name)
    page_size = _cfg_int(cfg, "filters", "numperpage", _cfg_int(cfg, "fallback", "page_size", 40))
    pages = _cfg_int(cfg, "fallback", "pages", 3)
    max_pages = max(pages, _cfg_int(cfg, "fallback", "max_pages", pages))
    min_cands = _cfg_int(cfg, "filters", "min_candidates", 0)

    dims = _build_dimensions(cfg)
    include_plain = _include_plain_tags_raw_for_queries(cfg)
    # resolution：使用 'W x H'
    res_req_tags = []
    for r in parse_csv(cfg.get("filters","resolution",fallback="")):
        vars = _normalize_resolution_variants(r)
        if vars:
            res_req_tags.append(vars[0])
    exc_tags = parse_csv(cfg.get("filters","exclude",fallback=""))

    tags_to_query: List[Optional[str]] = [*include_plain, *res_req_tags] if (include_plain or res_req_tags) else [None]

    ids: List[int] = []
    det: Dict[int, dict] = {}
    seen_ids = set()
    dbg_logs: List[str] = []

    for tag in tags_to_query:
        cursor = "*"
        for p in range(1, max_pages+1):
            resp, cursor = _query_webapi_single_tag(sess, key, qtype, days, page_size, tag, exc_tags, cursor)
            items = resp.get("publishedfiledetails") or resp.get("files") or resp.get("items") or []
            dbg_logs.append(f"[api] tag={tag or '<none>'} p{p} items={len(items)}")
            if not items:
                break
            for it in items:
                fid = int(str(it.get("publishedfileid", "0")))
                if not fid or fid in seen_ids: continue
                seen_ids.add(fid); ids.append(fid)
                if fid not in det: det[fid] = it

            # 仅当“维度 AND”过滤后数量达到 min_candidates 才早停
            if min_cands > 0:
                cur = filter_ids_with_details_AND(ids, det, cfg)
                if len(cur) >= min_cands:
                    dbg_logs.append(f"[api] early-stop: reached min_candidates={min_cands} with AND filter")
                    return cur, {i: det[i] for i in cur}, " | ".join(dbg_logs)

            if (not cursor) or (len(items) < page_size):
                break

    filtered = filter_ids_with_details_AND(ids, det, cfg)
    return filtered, {i: det[i] for i in filtered}, " | ".join(dbg_logs)

# ---------- HTML 回退（并集抓取 + 维度 AND 过滤） ----------
def map_sort_html(sort_name: str) -> Tuple[str,int]:
    s = (sort_name or "").lower()
    # Top Rated
    if s in ("top rated", "most up votes", "most upvoted", "top-rated"):
        return "vote", 0
    # Most Popular (Day/Week/Month/Year)
    if s.startswith("most popular"):
        if "year" in s:  return "trend", 365
        if "month" in s: return "trend", 30
        if "week" in s:  return "trend", 7
        if "day" in s or "today" in s: return "trend", 1
        return "trend", 7  # 默认周榜
    # Most Recent
    if s in ("most recent", "newest", "recent"):
        return "mostrecent", 0
    # Last updated
    if s in ("last updated", "recently updated", "updated"):
        return "lastupdated", 0
    # Most Subscriptions / Most Subscribed
    if s in ("most subscriptions", "most subscribed", "subscriptions", "subscribed"):
        return "totaluniquesubscribers", 0
    # fallback
    return "trend", 7

def _html_fetch_ids_once(sess, base_url, comm_sort, comm_days, per_page, page, req_tag, exc_tags) -> List[int]:
    headers = {"Referer": f"{base_url}?appid={APPID_WE}&browsesort={comm_sort}"}
    params = {
        "appid": str(APPID_WE),
        "browsesort": comm_sort,
        "days": str(comm_days or 0),
        "section": "readytouseitems",
        "l": "english",
        "numperpage": str(per_page),
        "p": str(page),
        "actualsort": comm_sort,
    }
    if req_tag:
        params["requiredtags[]"] = [req_tag]
    if exc_tags:
        params["excludedtags[]"] = exc_tags
    try:
        r = sess.get(base_url, params=params, headers=headers, timeout=(6,20))
        if not r.ok: return []
        html = r.text or ""
        out, seen = [], set()
        for m in re.finditer(r'data-publishedfileid="(\d+)"', html):
            fid = int(m.group(1))
            if fid not in seen: seen.add(fid); out.append(fid)
        for m in re.finditer(r'/filedetails/\?id=(\d+)', html):
            fid = int(m.group(1))
            if fid not in seen: seen.add(fid); out.append(fid)
        return out
    except Exception:
        return []

def community_ids_html_union(cfg: configparser.ConfigParser) -> List[int]:
    sort_name = cfg.get("sort","method",fallback="Most Popular (Week)")
    comm_sort, comm_days = map_sort_html(sort_name)
    pages = _cfg_int(cfg, "fallback", "pages", 3)
    max_pages = max(pages, _cfg_int(cfg, "fallback","max_pages", pages))
    per_page = _cfg_int(cfg, "filters", "numperpage", _cfg_int(cfg, "fallback", "page_size", 40))
    min_cands = _cfg_int(cfg, "filters", "min_candidates", 0)

    sess = _make_session_for_cfg(cfg)
    base_url = "https://steamcommunity.com/workshop/browse/"

    include_plain = _include_plain_tags_raw_for_queries(cfg)
    # resolution tags for server
    res_req_tags = []
    for r in parse_csv(cfg.get("filters","resolution",fallback="")):
        vars = _normalize_resolution_variants(r)
        if vars:
            res_req_tags.append(vars[0])
    exc_tags = parse_csv(cfg.get("filters","exclude",fallback=""))

    tags_to_query: List[Optional[str]] = [*include_plain, *res_req_tags] if (include_plain or res_req_tags) else [None]

    ids, seen = [], set()
    for tag in tags_to_query:
        for p in range(1, max_pages+1):
            part = _html_fetch_ids_once(sess, base_url, comm_sort, comm_days, per_page, p, tag, exc_tags)
            print(f"[html] tag={tag or '<none>'} p{p} items={len(part)}")
            for fid in part:
                if fid not in seen:
                    seen.add(fid); ids.append(fid)
            if min_cands > 0 and len(ids) >= min_cands:  # 粗略早停，最终还会本地 AND 过滤
                return ids
            if len(part) < per_page:
                break
    return ids

# ---------- 详情获取 ----------
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

# ---------- 本地过滤（维度 AND） ----------
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

def _extract_age_tag(item: dict) -> Optional[str]:
    for t in (item.get("tags") or []):
        tag = (t.get("tag") or "").strip().lower()
        if tag in ("everyone","questionable","mature"):
            return tag.title()
    kv = _kv_lookup(item)
    v = (kv.get("age rating") or kv.get("agerating") or kv.get("age_rating") or "").strip().lower()
    if v in ("everyone","questionable","mature"):
        return v.title()
    return None

def _extract_type_tags(item: dict) -> List[str]:
    out = []
    lows = {(t.get("tag") or "").strip().lower() for t in (item.get("tags") or [])}
    for _, v in _TYPE_CANON_TO_TAG.items():
        if v.lower() in lows:
            out.append(v)
    return out

def _extract_resolution_strings(item: dict) -> List[str]:
    res = []
    kv = _kv_lookup(item)
    kv_val = (kv.get("resolution","") or "").strip()
    if kv_val:
        res += _normalize_resolution_variants(kv_val)
    for t in (item.get("tags") or []):
        s = (t.get("tag") or "").strip()
        if re.match(r"^\d+\s*(?:x|×)\s*\d+$", s.replace("X","x")):
            res += _normalize_resolution_variants(s)
    # 去重
    normed = set()
    out = []
    for x in res:
        n = _norm_tag(x)
        if n not in normed:
            normed.add(n); out.append(x)
    return out

def _extract_genres(item: dict) -> List[str]:
    builtin = {"everyone","questionable","mature",
               "video","scene","web","application","wallpaper","preset"}
    out = []
    for t in (item.get("tags") or []):
        s = (t.get("tag") or "").strip()
        if _norm_tag(s) not in builtin and not re.match(r"^\d+\s*(?:x|×)\s*\d+$", s.replace("X","x")):
            out.append(s)
    seen, uniq = set(), []
    for x in out:
        n = _norm_tag(x)
        if n not in seen:
            seen.add(n); uniq.append(x)
    return uniq[:8]

def filter_ids_with_details_AND(base_ids: List[int], detail_map: Dict[int, dict],
                                cfg: configparser.ConfigParser) -> List[int]:
    d = _build_dimensions(cfg)
    need_genre = bool(d["genres_norm"])
    need_type  = bool(d["types_norm"])
    need_age   = bool(d["ages_norm"])
    need_res   = bool(d["res_sets"])
    exclude    = d["exclude_norm"]

    title_blk = _title_block_substrings(cfg)
    creator_blk = _creator_block_ids(cfg)

    out: List[int] = []
    for fid in base_ids:
        it = detail_map.get(fid, {})

        # ---------- 元信息过滤：title / creator ----------
        if title_blk:
            title = (it.get("title") or "")
            t_low = str(title).casefold()
            if t_low and any(sub in t_low for sub in title_blk):
                continue
        if creator_blk:
            creator = it.get("creator")
            if creator is not None:
                if str(creator).strip() in creator_blk:
                    continue

        tags_norm = {_norm_tag((t.get("tag") or "").strip()) for t in (it.get("tags") or [])}

        # exclude
        if exclude and (exclude & tags_norm):
            continue

        ok = True
        # type
        if need_type:
            if not (d["types_norm"] & tags_norm):
                ok = False
        # age
        if ok and need_age:
            if not (d["ages_norm"] & tags_norm):
                ok = False
        # genre
        if ok and need_genre:
            if not (d["genres_norm"] & tags_norm):
                ok = False
        # resolution（tag 或 KV）
        if ok and need_res:
            res_ok = False
            if any(s & tags_norm for s in d["res_sets"]):
                res_ok = True
            else:
                kv = _kv_lookup(it)
                kv_val = (kv.get("resolution","") or "").strip()
                if kv_val:
                    kv_norms = {_norm_tag(x) for x in _normalize_resolution_variants(kv_val)}
                    if any(s & kv_norms for s in d["res_sets"]):
                        res_ok = True
            if not res_ok:
                ok = False

        if ok:
            out.append(fid)

    return out

# ---------- steamcmd 与镜像/应用 ----------
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

def apply_in_we(entry: Path, we_exe: Path, retries: int = 2, delay_s: float = 1.0,
                monitor: Optional[int] = None, send_timeout_s: float = 2.0) -> None:
    """
    改为“非阻塞发送命令”模式，避免卡住：
    1) 确保 Wallpaper Engine 主进程在运行；
    2) 通过 -control openWallpaper 发送切换命令；
    3) 最多等待 send_timeout_s 秒（仅等待命令发送程序返回），超时亦视为已发送成功。
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            _ensure_we_running(we_exe, wait_s=15.0)

            cmd = [str(we_exe), "-control", "openWallpaper", "-file", str(entry)]
            if monitor is not None:
                cmd += ["-monitor", str(monitor)]

            print(f"[apply] 调用：{Path(we_exe).name} -control openWallpaper -file \"{entry}\""
                  + (f" -monitor {monitor}" if monitor is not None else ""))

            p = subprocess.Popen(cmd, **_win_hidden_popen_kwargs())
            try:
                rc = p.wait(timeout=send_timeout_s)
                if rc == 0:
                    print("[apply] 已将壁纸指令发送给 Wallpaper Engine。")
                    return
                else:
                    # 返回非零也可能不致命，重试一次
                    print(f"[apply] 命令进程返回码 {rc}，将重试（尝试 {attempt}/{retries}）。")
                    last_err = subprocess.CalledProcessError(rc, cmd)
            except subprocess.TimeoutExpired:
                # 主进程常驻，发送器没退出并不代表失败，这里直接认为已发送
                print("[apply] 已发送指令（发送器未在超时时间内退出，继续）。")
                return

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

# ---------- 日志 / 状态 ----------
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

def load_state(path: Path) -> Dict:
    if path.exists():
        try: return json.loads(path.read_text(encoding="utf-8"))
        except Exception: pass
    return {"tracked_ids": [], "last_applied": None, "history": [], "failed_recent": [], "cursor": 0}

def save_state(path: Path, state: Dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------- 候选获取 ----------
def get_auto_candidates(cfg: configparser.ConfigParser) -> Tuple[List[int], Dict[int,dict]]:
    key = (cfg.get("steam","api_key",fallback="") or "").strip()
    if key:
        ids_api, det_api, dbg = query_files_webapi_union_AND(cfg)
        print(f"[auto/api] debug: {dbg}")
        return ids_api, det_api
    else:
        ids_html = community_ids_html_union(cfg)
        if not ids_html:
            return [], {}
        det_more = fetch_details(ids_html, https_proxy=cfg.get("network","https_proxy",fallback="").strip())
        filtered = filter_ids_with_details_AND(ids_html, det_more, cfg)
        print(f"[auto/html] candidates after AND filter: {len(filtered)} (from {len(ids_html)} raw)")
        return filtered, {i: det_more[i] for i in filtered if i in det_more}

# ---------- 元信息打印 ----------
def _print_item_meta(fid: int, it: dict):
    title = (it.get("title") or "-")
    creator = it.get("creator")
    creator_s = str(creator).strip() if creator is not None else "-"
    tps = _extract_type_tags(it)
    age = _extract_age_tag(it) or "-"
    res = _extract_resolution_strings(it)
    genres = _extract_genres(it)
    tps_s = ", ".join(tps) if tps else "-"
    res_s = ", ".join(res[:3]) if res else "-"
    genres_s = ", ".join(genres) if genres else "-"
    # title 太长时截断，便于控制台阅读
    title_s = str(title).replace("\n", " ").strip()
    if len(title_s) > 80:
        title_s = title_s[:77] + "..."
    print(f"[meta] {fid} | Creator: {creator_s} | Title: {title_s} | Type: {tps_s} | Age: {age} | Resolution: {res_s} | Genres: {genres_s}")

# ---------- 主执行 ----------
def run_once(cfg: configparser.ConfigParser) -> str:
    steamcmd_path = expand(cfg.get("paths","steamcmd",fallback=""))
    if not steamcmd_path: raise RuntimeError("请在 [paths] steamcmd= 指定 steamcmd.exe")
    steamcmd_exe = Path(steamcmd_path)
    if not steamcmd_exe.exists(): raise RuntimeError(f"未找到 steamcmd：{steamcmd_exe}")

    we_exe = locate_we_exe(cfg)
    if not we_exe:
        print("[wait] 未检测到 Wallpaper Engine 可执行文件。")
        return "WAIT_WE"

    # 每轮也尝试触发一次（若还未触发且 WE 已在运行）
    _maybe_run_custom_we_cmd(cfg, we_exe)

    official_root = locate_workshop_root(cfg)
    if not official_root:
        print("[wait] 未发现已就绪的 Workshop 目录。")
        return "WAIT_WS"
    print("[workshop] official root:", official_root)

    ids_conf = [int(x) for x in parse_csv(cfg.get("subscribe","ids",fallback="")) if x.isdigit()]
    det_all: Dict[int,dict] = {}

    if ids_conf:
        ids_all = list(dict.fromkeys(ids_conf))
        print(f"[subscribe] ids from config: {len(ids_all)}")
        det_all.update(fetch_details(ids_all, https_proxy=cfg.get("network","https_proxy",fallback="").strip()))
    else:
        ids_all, det_all = get_auto_candidates(cfg)

    # 元信息过滤：对“手工 ids”也生效（不影响 tags 维度过滤逻辑）
    if ids_all:
        ids_all = filter_ids_meta_only(ids_all, det_all, cfg)

    if not ids_all:
        print("[pick] 无候选；请放宽 [filters] 或在 [subscribe] 填 ids（或检查 title/creator 排除规则是否过严）。")
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

    # 选本轮尝试名单
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

    # 为了打印元信息，补全尝试条目的详情
    miss_ids = [i for i in attempt_ids if i not in det_all]
    if miss_ids:
        det_all.update(fetch_details(miss_ids, https_proxy=cfg.get("network","https_proxy",fallback="").strip()))
    print("[pick] 待尝试元信息：")
    for wid in attempt_ids:
        it = det_all.get(wid, {})
        _print_item_meta(wid, it)

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
            raise RuntimeError("请在右键菜单登录账号")
        print(f"[login] 账号：{username}（若未提供密码/验证码将尝试用已保存凭证）")
        ok, _ = steamcmd_download_batch(steamcmd_exe, username, password, guard, [wid])
        if not ok:
            save_state(state_file, state)
            raise RuntimeError("steamcmd 登录或下载失败")

        src = base_tmp_root / str(wid)
        if not src.exists() or not any(src.rglob("*")):
            print(f"[skip] 未找到下载目录：{src}，尝试下一个...")
            state.setdefault("failed_recent", []).append(wid)
            continue

        dst = locate_workshop_root(cfg) / str(wid)
        print(f"[mirror] {src} -> {dst}")
        if not mirror_dir(src, dst):
            print(f"[warn] 镜像失败：{wid}；继续下一个。")
            state.setdefault("failed_recent", []).append(wid)
            continue

        proj_dst = mirror_to_projects_backup(we_exe, dst, wid)
        if proj_dst: print(f"[integrate] mirrored to projects/backup: {proj_dst}")

        entry = find_entry(dst) or find_entry(src)
        if not entry:
            print(f"[warn] 未找到可应用入口（project.json/index.html/视频），跳过 {wid}")
            state.setdefault("failed_recent", []).append(wid)
            continue

        # 应用前再次打印该条元信息，便于确认
        print("[apply] 即将应用：")
        _print_item_meta(wid, det_all.get(wid, {}))
        try:
            apply_in_we(entry, we_exe)
            applied = True
            state["last_applied"] = wid
            hist = state.get("history", []); hist.append(wid); state["history"] = hist[-500:]
            if _cfg_bool(cfg,"logging","enable",True):
                with (HERE / cfg.get("logging","file",fallback="we_downloads.log")).open("a", encoding="utf-8") as f:
                    f.write(f"[{now_str()}] https://steamcommunity.com/sharedfiles/filedetails/?id={wid}\n")

            # 清理旧项（包 try，避免异常把 applied 变 False）
            try:
                cleanup_all_others_if_needed(wid, cfg, steamcmd_exe, locate_workshop_root(cfg), we_exe)
            except Exception as e:
                print("[cleanup] 忽略清理异常：", e)

            # 跟踪
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
        dst_try = locate_workshop_root(cfg) / str(current_wid)
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

# ---------- 清理 ----------
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

# =========================
# RUN_NOW 事件：唤醒并立刻执行一轮
# =========================
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
    if "--once" in sys.argv:
        cfg = read_conf()
        try:
            # 单次模式也尝试触发一次（若 WE 已在运行）
            _maybe_run_custom_we_cmd(cfg, locate_we_exe(cfg))
            run_once(cfg)
        except Exception as e:
            print("[error/once]", e)
            sys.exit(1)
        sys.exit(0)

    cfg = read_conf()
    mode = cfg.get("subscribe","mode",fallback="steamcmd").strip().lower()
    if mode != "steamcmd":
        print(f"[exit] [subscribe].mode={mode}；本脚本仅实现 steamcmd 模式。"); return

    run_now_evt = None
    if os.name == "nt":
        try:
            name = _run_now_event_name()
            run_now_evt = _open_named_event(name) or _create_named_event_manual_reset(name, initial=False)
        except Exception:
            run_now_evt = None

    # 启动即尝试触发（若 WE 已在运行）
    _maybe_run_custom_we_cmd(cfg, locate_we_exe(cfg))

    run_on_start = _cfg_bool(cfg, "schedule", "run_on_startup", True)
    interval_s = parse_interval(cfg.get("schedule","interval",fallback=""))
    detect_s = parse_interval(cfg.get("schedule","detect_interval", fallback="5m"))
    if detect_s <= 0: detect_s = 300

    status = "INIT"
    if run_on_start:
        try:
            status = run_once(read_conf())
        except Exception as e:
            print("[error/startup]", e)
            status = "ERROR"

    if interval_s <= 0:
        while isinstance(status, str) and status.startswith("WAIT_"):
            cfg = read_conf()
            # 循环中也持续尝试触发一次（若 WE 已在运行且尚未触发）
            _maybe_run_custom_we_cmd(cfg, locate_we_exe(cfg))
            _wait_run_now_or_timeout(run_now_evt, detect_s)
            try:
                status = run_once(read_conf())
            except KeyboardInterrupt:
                print("\n[exit] 用户中断"); break
            except Exception as e:
                print("[error/loop]", e); status = "ERROR"
        return

    while True:
        try:
            cfg = read_conf()
            # 定时循环中也持续尝试触发一次（若 WE 已在运行且尚未触发）
            _maybe_run_custom_we_cmd(cfg, locate_we_exe(cfg))
            sleep_for = detect_s if (isinstance(status, str) and status.startswith("WAIT_")) else interval_s
            _wait_run_now_or_timeout(run_now_evt, sleep_for)
            status = run_once(read_conf())
        except KeyboardInterrupt:
            print("\n[exit] 用户中断"); break
        except Exception as e:
            print("[error/loop]", e); status = "ERROR"

if __name__ == "__main__":
    main()