import os
import re
import sys
import time
import json
import base64
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse
from datetime import datetime
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Set, Dict, List

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm
from seleniumwire import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import WebDriverException

# --- Nastrojki ---
EXTS = ('.gltf', '.glb', '.obj', '.stl', '.ply', '.fbx')
DEFAULT_WAIT = 6
MAX_RETRIES = 3
RETRY_DELAY = 2
MAX_FILE_SIZE = 500_000_000  # 500 MB
PARALLEL_DOWNLOADS = 3
TIMEOUT_REQUEST = 30
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36'


# --- Logging ---
def setup_logging(out_folder: str) -> logging.Logger:
    Path(out_folder).mkdir(parents=True, exist_ok=True)
    log_file = Path(out_folder) / f'scraper_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
    logger = logging.getLogger('3d_scraper')
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        logger.handlers.clear()
    fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    fh = logging.FileHandler(log_file, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# --- Statistika ---
@dataclass
class DownloadStats:
    total_urls_found: int = 0
    total_files_downloaded: int = 0
    total_bytes_downloaded: int = 0
    failed_urls: List[str] = field(default_factory=list)
    successful_files: List[str] = field(default_factory=list)


# --- Vspomogatelnyye funkcii ---
def default_download_root() -> Path:
    home = Path.home()
    for cand in (home / 'Downloads', home / 'Zagruzki'):
        try:
            cand.mkdir(parents=True, exist_ok=True)
            return cand
        except Exception:
            continue
    return Path.cwd() / 'downloads'

DEFAULT_DOWNLOAD_FOLDER = str(default_download_root())


def resolve_out_folder(path_like: Optional[str]) -> str:
    if not path_like:
        path_like = DEFAULT_DOWNLOAD_FOLDER
    p = os.path.expandvars(os.path.expanduser(path_like))
    if not os.path.isabs(p):
        p = os.path.abspath(p)
    Path(p).mkdir(parents=True, exist_ok=True)
    return p


def unique_path(out_path: str) -> str:
    base, ext = os.path.splitext(out_path)
    cand, i = out_path, 1
    while os.path.exists(cand):
        cand = f"{base} ({i}){ext}"
        i += 1
    return cand


def is_3d_url(url: str) -> bool:
    if not url:
        return False
    url = url.strip()
    if url.startswith('data:'):
        return True
    p = url.split('?')[0].split('#')[0].lower()
    return any(p.endswith(ext) for ext in EXTS)


def is_url_cached(url: str, cache_file: str) -> bool:
    if not os.path.exists(cache_file):
        return False
    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            return url in json.load(f).get('downloaded_urls', [])
    except Exception:
        return False


def save_to_cache(url: str, cache_file: str) -> None:
    cache_data = {'downloaded_urls': []}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
        except Exception:
            pass
    if url not in cache_data['downloaded_urls']:
        cache_data['downloaded_urls'].append(url)
    try:
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# --- Sohranenie artefaktov ---
def save_page_artifacts(driver, out_folder: str, page_url: str, logger: logging.Logger) -> Dict[str, str]:
    ts = time.strftime('%Y%m%d_%H%M%S')
    artifacts: Dict[str, str] = {}
    try:
        html_path = unique_path(os.path.join(out_folder, f'page_{ts}.html'))
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(driver.page_source or '')
        artifacts['page_html'] = html_path
    except Exception as e:
        logger.warning(f'Failed to save HTML: {e}')
    try:
        urls_path = unique_path(os.path.join(out_folder, f'network_urls_{ts}.txt'))
        with open(urls_path, 'w', encoding='utf-8') as f:
            for req in getattr(driver, 'requests', []):
                try:
                    f.write((req.url or '').strip() + '\n')
                except Exception:
                    continue
        artifacts['network_urls'] = urls_path
    except Exception as e:
        logger.warning(f'Failed to save network URLs: {e}')
    return artifacts


# --- Skachivanie ---
_CD_RE = re.compile(r'filename\*?=(?:UTF-8\'\'\')?"?([^";]+)"?', re.IGNORECASE)


def pick_filename(url: str, resp: requests.Response) -> str:
    name = os.path.basename(urlparse(url).path)
    cd = resp.headers.get('Content-Disposition') or resp.headers.get('content-disposition', '')
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


def save_data_url(data_url: str, out_folder: str, logger: logging.Logger) -> Optional[str]:
    try:
        header, b64 = data_url.split(',', 1)
    except ValueError:
        logger.error('Invalid data URL format')
        return None
    mime = header.split(':', 1)[1].split(';', 1)[0] if ':' in header else ''
    ext = next((e for e in EXTS if e.lstrip('.') in mime.lower()), '.bin')
    out_path = unique_path(os.path.join(out_folder, f'embedded_{int(time.time()*1000)}{ext}'))
    try:
        with open(out_path, 'wb') as f:
            f.write(base64.b64decode(b64))
        logger.info(f'Saved embedded -> {out_path}')
        return out_path
    except Exception as e:
        logger.error(f'Error saving data URL: {e}')
        return None


def download_url(url: str, out_folder: str, session: Optional[requests.Session] = None,
                 logger: Optional[logging.Logger] = None,
                 stats: Optional[DownloadStats] = None) -> Optional[str]:
    logger = logger or logging.getLogger('3d_scraper')
    Path(out_folder).mkdir(parents=True, exist_ok=True)
    if url.startswith('data:'):
        return save_data_url(url, out_folder, logger)

    sess = session or requests.Session()
    resp = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = sess.get(url, headers={'User-Agent': USER_AGENT}, stream=True, timeout=TIMEOUT_REQUEST)
            if resp.status_code == 200:
                break
            if resp.status_code == 429:
                wait = min(RETRY_DELAY * (2 ** (attempt - 1)), 30)
                logger.warning(f'Rate limited for {url}, waiting {wait}s')
                time.sleep(wait)
                continue
            logger.warning(f'Skip {url}, status {resp.status_code}')
            if stats:
                stats.failed_urls.append(f'{url} (status {resp.status_code})')
            return None
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            logger.warning(f'{type(e).__name__} for {url} (attempt {attempt}/{MAX_RETRIES})')
            time.sleep(RETRY_DELAY)
        except Exception as e:
            logger.error(f'Request error for {url}: {e}')
            if stats:
                stats.failed_urls.append(f'{url} (error: {str(e)[:50]})')
            return None
    else:
        logger.error(f'Failed to download {url} after {MAX_RETRIES} attempts')
        if stats:
            stats.failed_urls.append(f'{url} (max retries exceeded)')
        return None

    total = int(resp.headers.get('content-length', '0') or 0)
    if total > MAX_FILE_SIZE:
        logger.warning(f'File too large ({total/1e6:.1f}MB) for {url}, skipping')
        if stats:
            stats.failed_urls.append(f'{url} (too large: {total/1e6:.1f}MB)')
        return None

    filename = pick_filename(url, resp)
    out_path = unique_path(os.path.join(out_folder, filename))
    try:
        with open(out_path, 'wb') as f:
            if total:
                with tqdm(total=total, unit='B', unit_scale=True, desc=filename, leave=False) as bar:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            bar.update(len(chunk))
                            if stats:
                                stats.total_bytes_downloaded += len(chunk)
            else:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        if stats:
                            stats.total_bytes_downloaded += len(chunk)
        logger.info(f'Saved -> {out_path}')
        if stats:
            stats.successful_files.append(out_path)
        return out_path
    except Exception as e:
        logger.error(f'Error writing file {out_path}: {e}')
        if stats:
            stats.failed_urls.append(f'{url} (write error: {str(e)[:50]})')
        return None


# --- Poisk 3D-ssylok v HTML ---
def find_3d_urls_from_html(page_url: str, html: str, logger: logging.Logger) -> Set[str]:
    found: Set[str] = set()
    try:
        soup = BeautifulSoup(html, 'html.parser')
        for tag in soup.find_all(['a', 'link'], href=True):
            full = urljoin(page_url, tag.get('href') or '')
            if is_3d_url(full):
                found.add(full)
        for tag in soup.find_all(['img', 'source', 'script'], src=True):
            full = urljoin(page_url, tag.get('src') or '')
            if is_3d_url(full):
                found.add(full)
        for t in soup.find_all(True):
            for _, val in t.attrs.items():
                if isinstance(val, str) and is_3d_url(val):
                    found.add(urljoin(page_url, val))
        ext_pattern = '|'.join(e.lstrip('.') for e in EXTS)
        for m in re.finditer(rf'["\']([^"\']+\.(?:{ext_pattern})(?:\?[^"\']*)?)["\']', html, re.IGNORECASE):
            found.add(urljoin(page_url, m.group(1)))
        for m in re.finditer(r'(data:[^,]+;base64,[A-Za-z0-9+/=_-]+)', html):
            found.add(m.group(1))
    except Exception as e:
        logger.warning(f'Error parsing HTML: {e}')
    return found


# --- Parallel'naya zagruzka ---
def parallel_download(urls: List[str], out_folder: str, session: requests.Session,
                      stats: DownloadStats, logger: logging.Logger) -> List[str]:
    def _dl(url: str) -> Optional[str]:
        return download_url(url, out_folder, session, logger, stats)
    with ThreadPoolExecutor(max_workers=PARALLEL_DOWNLOADS) as ex:
        return [r for r in ex.map(_dl, urls) if r is not None]


# --- Osnovnaya logika ---
def parse_dynamic_page(page_url: str,
                       out_folder: str = DEFAULT_DOWNLOAD_FOLDER,
                       wait: int = DEFAULT_WAIT,
                       save_artifacts: bool = True,
                       marker_on_empty: bool = True,
                       use_cache: bool = True,
                       parallel: bool = True) -> List[str]:
    out_folder = resolve_out_folder(out_folder)
    logger = setup_logging(out_folder)
    logger.info(f'Starting 3D scraper for {page_url}')

    stats = DownloadStats()
    cache_file = os.path.join(out_folder, '.3d_cache.json')

    chrome_opts = Options()
    for arg in ('--headless=new', '--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled', f'--user-agent={USER_AGENT}'):
        chrome_opts.add_argument(arg)

    driver = None
    try:
        driver = webdriver.Chrome(options=chrome_opts)
    except WebDriverException as e:
        logger.error(f'Error starting Chrome driver: {e}')
        return []

    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})

    try:
        driver.scopes = ['.*']
        driver.get(page_url)
        logger.info(f'Waiting {wait}s for dynamic content...')
        time.sleep(wait)

        results: List[str] = []
        if save_artifacts:
            results.extend(save_page_artifacts(driver, out_folder, page_url, logger).values())

        found: Set[str] = set()
        if hasattr(driver, 'requests'):
            for req in driver.requests:
                try:
                    if is_3d_url(req.url):
                        found.add(req.url)
                except Exception:
                    continue
            logger.info(f'Found {len(found)} 3D URLs in network requests')
        else:
            logger.warning('seleniumwire requests not available')

        found.update(find_3d_urls_from_html(page_url, driver.page_source, logger))
        stats.total_urls_found = len(found)
        logger.info(f'Total candidate 3D URLs: {len(found)}')

        urls_to_download = sorted(u for u in found if not (use_cache and is_url_cached(u, cache_file)))
        logger.info(f'URLs to download: {len(urls_to_download)}')

        if parallel and len(urls_to_download) > 1:
            downloaded = parallel_download(urls_to_download, out_folder, session, stats, logger)
            results.extend(downloaded)
            stats.total_files_downloaded = len(downloaded)
        else:
            for url in urls_to_download:
                saved = download_url(url, out_folder, session=session, logger=logger, stats=stats)
                if saved:
                    results.append(saved)
                    stats.total_files_downloaded += 1
                    if use_cache:
                        save_to_cache(url, cache_file)

        if not any(Path(p).suffix.lower() in EXTS for p in results) and marker_on_empty:
            marker = unique_path(os.path.join(out_folder, 'NO_3D_FOUND.txt'))
            with open(marker, 'w', encoding='utf-8') as f:
                f.write(f'URL: {page_url}\n3D-extensions: {", ".join(EXTS)}\nNo 3D resources found.')
            results.append(marker)
            logger.warning('Created marker NO_3D_FOUND.txt')

        logger.info(f'Done. Files: {len(results)}, Bytes: {stats.total_bytes_downloaded}')
        return results

    except Exception as e:
        logger.error(f'Unexpected error: {e}', exc_info=True)
        return []
    finally:
        if driver:
            try:
                driver.quit()
            except Exception as e:
                logger.warning(f'Error closing driver: {e}')


# --- CLI ---
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Ishchet 3D-resursy na stranice i skachivayut modeli.')
    parser.add_argument('url', nargs='?', help='URL stranicy, naprimer: https://example.com/')
    parser.add_argument('--out', default=DEFAULT_DOWNLOAD_FOLDER, help='Katalog vyvoda')
    parser.add_argument('--wait', type=int, default=DEFAULT_WAIT, help='Pauza ozhidaniya, sek')
    parser.add_argument('--no-artifacts', action='store_true', help='Ne sokhraniat HTML/manifest')
    parser.add_argument('--no-empty-marker', action='store_true', help='Ne sozdat NO_3D_FOUND.txt')
    parser.add_argument('--no-cache', action='store_true', help="Otklyuchit' kesching")
    parser.add_argument('--sequential', action='store_true', help="Posledovatel'naya zagruzka")
    args = parser.parse_args()

    url = args.url or input('Page URL: ').strip()
    if not url:
        print('URL ne ukazan -- rabota prekrashchena.')
        sys.exit(1)

    files = parse_dynamic_page(
        url,
        out_folder=args.out,
        wait=args.wait,
        save_artifacts=not args.no_artifacts,
        marker_on_empty=not args.no_empty_marker,
        use_cache=not args.no_cache,
        parallel=not args.sequential,
    )
    print(f'\nGotovo! Sokhraneno fajlov: {len(files)}')
    for p in files:
        print(' -', p)
