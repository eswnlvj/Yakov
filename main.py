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


    # ---------- СОХРАНЕНИЕ АРТЕФАКТОВ (ГАРАНТИРУЕТ ФАЙЛЫ) ----------

def save_page_artifacts(driver, out_folder: str, page_url: str) -> dict:
    ts = time.strftime('%Y%m%d_%H%M%S')
    artifacts: dict[str, str] = {}

    # Итоговый HTML
    html_name = f'page_{ts}.html'
    html_path = unique_path(os.path.join(out_folder, html_name))
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(driver.page_source or '')
    artifacts['page_html'] = html_path

    # Список всех сетевых URL (из selenium-wire)
    urls_name = f'network_urls_{ts}.txt'
    urls_path = unique_path(os.path.join(out_folder, urls_name))
    with open(urls_path, 'w', encoding='utf-8') as f:
        for req in getattr(driver, 'requests', []):
            try:
                f.write((req.url or '').strip() + '\n')
            except Exception:
                continue
    artifacts['network_urls'] = urls_path

    # Манифест для удобства отладки
    manifest_name = f'manifest_{ts}.json'
    manifest_path = unique_path(os.path.join(out_folder, manifest_name))
    payload = {'timestamp': ts, 'page_url': page_url, 'artifacts': artifacts}
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    artifacts['manifest'] = manifest_path

    return artifacts
    
    
    # ---------- СКАЧИВАНИЕ ----------

_CD_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.IGNORECASE)

def pick_filename_from_headers(url: str, resp: requests.Response) -> str:
    parsed = urlparse(url)
    name = os.path.basename(parsed.path)
    cd = resp.headers.get('Content-Disposition') or resp.headers.get('content-disposition')
    if cd:
        m = _CD_RE.search(cd)
        if m:
            name = os.path.basename(m.group(1))
    if not name:
        name = f'downloaded_{int(time.time())}'
        ctype = (resp.headers.get('Content-Type') or '').lower()
        for ext in EXTS:
            if ext.lstrip('.') in ctype:
                name += ext
                break
    return name or f'downloaded_{int(time.time())}.bin'


def save_data_url(data_url: str, out_folder: str) -> str | None:
    """Сохраняет встроенный data:...;base64,... ресурс."""
    try:
        header, b64 = data_url.split(',', 1)
    except ValueError:
        print('Invalid data URL')
        return None
    mime = ''
    if ':' in header:
        mime = header.split(':', 1)[1].split(';', 1)[0]
    ext = '.bin'
    if 'gltf' in mime: ext = '.gltf'
    elif 'glb' in mime: ext = '.glb'
    elif 'stl' in mime: ext = '.stl'
    elif 'obj' in mime: ext = '.obj'
    elif 'ply' in mime: ext = '.ply'
    elif 'fbx' in mime: ext = '.fbx'

    fname = f'embedded_{int(time.time()*1000)}{ext}'
    out_path = unique_path(os.path.join(out_folder, fname))
    try:
        with open(out_path, 'wb') as f:
            f.write(base64.b64decode(b64))
        print('Saved embedded ->', out_path)
        return out_path
    except Exception as e:
        print('Error saving data URL:', e)
        return None


def download_url(url: str, out_folder: str, session: requests.Session | None = None) -> str | None:
    ensure_folder(out_folder)
    if url.startswith('data:'):
        return save_data_url(url, out_folder)

    sess = session or requests.Session()
    headers = {'User-Agent': USER_AGENT}
    try:
        resp = sess.get(url, headers=headers, stream=True, timeout=30)
    except Exception as e:
        print('Request error for', url, e)
        return None
    if resp.status_code != 200:
        print('Skip', url, 'status', resp.status_code)
        return None

    filename = pick_filename_from_headers(url, resp)
    out_path = unique_path(os.path.join(out_folder, filename))
    total = int(resp.headers.get('content-length', '0') or 0)

    try:
        with open(out_path, 'wb') as f:
            if total:
                with tqdm(total=total, unit='B', unit_scale=True, desc=filename) as bar:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            bar.update(len(chunk))
            else:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        print('Saved ->', out_path)
        return out_path
    except Exception as e:
        print('Error writing file', out_path, e)
        return None
