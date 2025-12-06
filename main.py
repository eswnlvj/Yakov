import os
import re
import time
import json
import base64
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

# --- Настройки ---
EXTS = ('.gltf', '.glb', '.obj', '.stl', '.ply', '.fbx')  # поддерживаемые расширения 3D
DEFAULT_WAIT = 6
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36'


def default_download_root() -> Path:
    """Подбор дефолтного каталога загрузки для разных ОС."""
    home = Path.home()
    for cand in (home / 'Downloads', home / 'Загрузки'):
        try:
            cand.mkdir(parents=True, exist_ok=True)
            return cand
        except Exception:
            continue
    return Path.cwd() / 'downloads'


DEFAULT_DOWNLOAD_FOLDER = str(default_download_root())


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------

def resolve_out_folder(path_like: str | None) -> str:
    r"""Нормализует путь вывода (переменные окружения, ~, абсолютность)."""
    if not path_like:
        path_like = DEFAULT_DOWNLOAD_FOLDER
    p = os.path.expandvars(os.path.expanduser(path_like))
    if not os.path.isabs(p):
        p = os.path.abspath(p)
    Path(p).mkdir(parents=True, exist_ok=True)
    return p


def ensure_folder(path: str):
    Path(path).mkdir(parents=True, exist_ok=True)


def unique_path(out_path: str) -> str:
    """Возвращает уникальный путь (file (1).ext, file (2).ext, ...)."""
    base, ext = os.path.splitext(out_path)
    cand = out_path
    i = 1
    while os.path.exists(cand):
        cand = f"{base} ({i}){ext}"
        i += 1
    return cand


def is_3d_url(u: str) -> bool:
    if not u:
        return False
    u = u.strip()
    if u.startswith('data:'):
        return True
    p = u.split('?')[0].split('#')[0].lower()
    return any(p.endswith(ext) for ext in EXTS)
