#!/usr/bin/env python3
"""M3U8 抓取器（UI-first, CDP attach）

啟動流程：
- 以 UI 要求使用者輸入目標 `url`（或用 `--url` 指定）。
- 優先嘗試以 CDP 連到已啟動的 Chromium-based 瀏覽器（Chrome/Edge）；若找不到 CDP，程式會嘗試自動啟動 Chrome 並啟用 remote debugging。
"""
from __future__ import annotations

import argparse
import re
import time
from typing import List, Optional
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright, Page, Request

import subprocess
import shutil
import os
import sys
import concurrent.futures
import threading
from datetime import datetime, timezone

# 禁用 SSL 警告（因為許多視頻源使用自簽名證書）
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def safe_print(*args, **kwargs):
    """Print safely even when the console encoding (eg. cp950) cannot represent some characters.

    Falls back to replacement characters for unencodable bytes.
    """
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, 'encoding', None) or 'utf-8'
        safe_args = []
        for a in args:
            s = str(a)
            try:
                s2 = s.encode(enc, errors='replace').decode(enc)
            except Exception:
                s2 = s.encode('utf-8', errors='replace').decode('utf-8')
            safe_args.append(s2)
        print(*safe_args, **kwargs)


# 使用 Playwright 管理的 Chromium，不再嘗試啟動或附加系統 Chrome/Brave


def parse_args():
    p = argparse.ArgumentParser(description="抓取劇集頁面每集的 m3u8（優先 1080P），UI-first 流程")
    p.add_argument("--episode-selector", default=".jujiepisodios a, .module-play-list-content a, .play-list-box a, .playlist a, .episodes a, .episode-list a, .video_page_playlist a, .player_list a, .video-list a",
                   help="用於選取集數按鈕的 CSS selector（逗號分隔備援）")
    p.add_argument("--source-text", default="FLV",
                   help="偏好來源按鈕文字（例如 'FLV'），若需要切換來源可指定此關鍵字）")
    p.add_argument("--wait", type=float, default=2.0, help="每次點擊後等待秒數以讓請求發生")
    # CDP connect removed; script uses Playwright-managed Chromium by default
    p.add_argument("--url", default=None, help="直接指定要抓取的頁面 URL（若未指定將顯示 UI 要求）")
    p.add_argument("--out-dir", default=None, help="指定 MP4 輸出資料夾（若未指定可於 UI 選擇）")
    p.add_argument("--max-downloads", type=int, default=5, help="最多同時下載數量（預設 5）")
    # 移除原先的 --no-download / --no-clean 選項（功能改為固定行為）
    p.add_argument("--fast", action='store_true', help="啟用快速嗅探：先用 HTTP 直接解析頁面，並在 Playwright 使用 headless + 阻擋資源裝載")
    # 移除 --no-fast，預設採用 fast（headless + 阻擋資源）行為
    p.add_argument("--no-minimize", action='store_true', help="不要最小化瀏覽器（預設會最小化）")
    p.add_argument("--headless", action='store_true', help="以 headless（無視窗）模式啟動瀏覽器；等同於 --fast 的 headless 行為，但不啟用 UI")
    p.add_argument("--no-ui", action='store_true', help="不要顯示啟動 UI（需同時指定 --url）")
    # 移除 --show-ui，UI 顯示行為由參數與預設邏輯決定
    # 設定預設為 fast
    p.set_defaults(fast=True)
    return p.parse_args()


def ensure_playwright_browsers() -> bool:
    """Ensure Playwright browsers are available.

    Check for bundled browsers (in onedir exe) or per-user install.
    If found, set `PLAYWRIGHT_BROWSERS_PATH` and return True.
    """
    # prefer an explicit env or bundled browsers first
    env_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    if env_path and os.path.isdir(env_path):
        return True

    # check bundled (when distributed as onedir with `browsers/` next to exe)
    try:
        if getattr(sys, 'frozen', False):
            meipass = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        else:
            meipass = os.path.dirname(os.path.abspath(__file__))
        bundled = os.path.join(meipass, 'browsers')
        if os.path.isdir(bundled) and os.listdir(bundled):
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = bundled
            return True
    except Exception:
        pass

    # check per-user install location
    local_base = os.getenv('LOCALAPPDATA') or os.path.expanduser('~')
    target = os.path.join(local_base, 'm3u8_playwright_browsers')
    if os.path.isdir(target) and os.listdir(target):
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = target
        return True

    return False



def show_start_ui() -> tuple:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return (None, None, 1)

    root = tk.Tk()
    root.title('M3U8 抓取 — 啟動設定')
    root.geometry('560x200')
    
    # 設定視窗圖標
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(script_dir, 'lioil.ico')
        if os.path.exists(icon_path):
            root.iconbitmap(icon_path)
    except Exception:
        pass

    tk.Label(root, text='Target page URL:').pack(anchor='w', padx=8, pady=(8, 0))
    url_var = tk.StringVar()
    url_entry = tk.Entry(root, textvariable=url_var, width=92)
    url_entry.pack(padx=8)
    
    # 加入右鍵選單支援複製貼上
    def create_context_menu(widget):
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label="剪下 (Ctrl+X)", command=lambda: widget.event_generate('<<Cut>>'))
        menu.add_command(label="複製 (Ctrl+C)", command=lambda: widget.event_generate('<<Copy>>'))
        menu.add_command(label="貼上 (Ctrl+V)", command=lambda: widget.event_generate('<<Paste>>'))
        menu.add_separator()
        menu.add_command(label="全選 (Ctrl+A)", command=lambda: widget.select_range(0, tk.END))
        
        def show_menu(event):
            menu.tk_popup(event.x_root, event.y_root)
        widget.bind('<Button-3>', show_menu)
    
    create_context_menu(url_entry)

    tk.Label(root, text='Output folder (for MP4):').pack(anchor='w', padx=8, pady=(6, 0))
    out_var = tk.StringVar(value='F:/tmp')
    out_frm = tk.Frame(root)
    out_frm.pack(padx=8, fill='x')
    entry_out = tk.Entry(out_frm, textvariable=out_var)
    entry_out.pack(side='left', fill='x', expand=True)
    def browse_out():
        d = filedialog.askdirectory(title='Select output folder')
        if d:
            out_var.set(d)
    # Add a Browse button instead of binding to entry
    tk.Button(out_frm, text='Browse', command=browse_out).pack(side='left', padx=(4, 0))

    # 開始集數設定
    tk.Label(root, text='Start episode (default: 1):').pack(anchor='w', padx=8, pady=(6, 0))
    start_ep_var = tk.StringVar(value='1')
    start_ep_frm = tk.Frame(root)
    start_ep_frm.pack(padx=8, fill='x')
    start_ep_entry = tk.Entry(start_ep_frm, textvariable=start_ep_var, width=10)
    start_ep_entry.pack(side='left')
    tk.Label(start_ep_frm, text='(skip episodes before this number)').pack(side='left', padx=(8, 0))
    
    create_context_menu(start_ep_entry)

    # CDP endpoint 由程式自動使用 127.0.0.1:9222（不在 UI 中輸入），按 Start 後程式會自動嘗試啟動 Chrome

    result = {'ok': False}

    def on_start():
        result['ok'] = True
        root.destroy()

    def on_cancel():
        root.destroy()

    frm = tk.Frame(root)
    frm.pack(pady=8)
    tk.Button(frm, text='Start', command=on_start, width=12).pack(side='left', padx=8)
    tk.Button(frm, text='Cancel', command=on_cancel, width=12).pack(side='left')

    root.mainloop()
    # return entered URL, output folder, and start episode
    if result.get('ok'):
        val = url_var.get().strip()
        out = out_var.get().strip()
        start_ep_str = start_ep_var.get().strip()
        try:
            start_ep = max(1, int(start_ep_str))  # 最小值為 1
        except ValueError:
            start_ep = 1
        return (val if val else None, out if out else None, start_ep)
    return (None, None, 1)


def probe_m3u8_resolution(url: str, verbose: bool = False, timeout: int = 15) -> tuple:
    """用 ffprobe 檢測 m3u8 的實際解析度
    回傳: (width, height) 或 (0, 0) 若失敗
    """
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        ffmpeg_exe = os.path.join(script_dir, 'exe', 'ffmpeg.exe')
        if not os.path.exists(ffmpeg_exe):
            return (0, 0)
        
        # 使用 ffmpeg -i 來獲取資訊，增加 probesize 和 analyzeduration 以確保準確
        cmd = [
            ffmpeg_exe, '-hide_banner',
            '-probesize', '5000000',
            '-analyzeduration', '5000000',
            '-i', url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding='utf-8', errors='replace')
        
        # ffmpeg -i 會輸出資訊到 stderr
        output = result.stderr
        
        # 尋找 Video: 行中的解析度，例如 "Video: h264 ... 1920x1080"
        m = re.search(r'Video:.*?(\d{3,4})x(\d{3,4})', output)
        if m:
            w = int(m.group(1))
            h = int(m.group(2))
            if verbose:
                safe_print(f'    [ffprobe] 實際解析度: {w}x{h}')
            return (w, h)
        
        # 退而求其次，找任何解析度模式
        m2 = re.search(r'(\d{3,4})x(\d{3,4})', output)
        if m2:
            w = int(m2.group(1))
            h = int(m2.group(2))
            if verbose:
                safe_print(f'    [ffprobe] 偵測到: {w}x{h}')
            return (w, h)
        
        return (0, 0)
    except Exception as e:
        if verbose:
            safe_print(f'    [ffprobe] 錯誤: {e}')
        return (0, 0)


def quick_verify_resolution(m3u8_url: str, timeout: int = 15) -> tuple:
    """快速驗證：讀取 m3u8 文件的 RESOLUTION 標籤，檢查寬度 >= 1920
    
    返回: (is_1080p, width, height) 或 (False, 0, 0) 如果失敗
    """
    try:
        # 讀取 m3u8 查看是否有 RESOLUTION 標籤
        try:
            resp = requests.get(m3u8_url, timeout=2, verify=False)
            if resp.ok:
                m3u8_text = resp.text
                
                # 檢查是否有 RESOLUTION 標籤（master playlist）
                if '#EXT-X-STREAM-INF' in m3u8_text:
                    # 解析所有 RESOLUTION 標籤，找最高寬度
                    max_w, max_h = 0, 0
                    for line in m3u8_text.splitlines():
                        m = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
                        if m:
                            w, h = int(m.group(1)), int(m.group(2))
                            if w >= max_w:
                                max_w, max_h = w, h
                    
                    if max_w >= 1920:
                        return (True, max_w, max_h)
                    elif max_w > 0:
                        return (False, max_w, max_h)
        except Exception:
            pass
        
        return (False, 0, 0)
    
    except Exception:
        return (False, 0, 0)


def verify_final_mp4_resolution(mp4_path: str, min_width: int = 1920) -> tuple:
    """最終驗證：檢查完整 mp4 文件的實際分辨率
    
    返回: (is_1080p, width, height) 或 (False, 0, 0) 如果失敗
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    if not os.path.exists(mp4_path):
        return (False, 0, 0)
    
    try:
        # 優先使用 ffprobe（更快）
        ffprobe_path = os.path.join(script_dir, 'exe', 'ffprobe.exe')
        if os.path.exists(ffprobe_path):
            cmd = [ffprobe_path, '-v', 'error', '-select_streams', 'v:0',
                  '-show_entries', 'stream=width,height', '-of', 'csv=p=0', mp4_path]
            try:
                proc = subprocess.run(
                    cmd,
                    timeout=10,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding='utf-8',
                    errors='replace'
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    parts = proc.stdout.strip().split(',')
                    if len(parts) >= 2:
                        w = int(parts[0])
                        h = int(parts[1])
                        is_1080p = w >= min_width
                        return (is_1080p, w, h)
            except Exception:
                pass
        
        # 回退到 ffmpeg
        ffmpeg_path = os.path.join(script_dir, 'exe', 'ffmpeg.exe')
        if not os.path.exists(ffmpeg_path):
            return (False, 0, 0)
        
        cmd = [ffmpeg_path, '-i', mp4_path]
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                timeout=10,
                text=True,
                encoding='utf-8',
                errors='replace'
            )
            output = proc.stdout
            m = re.search(r'(\d{3,4})x(\d{3,4})', output)
            if m:
                w = int(m.group(1))
                h = int(m.group(2))
                is_1080p = w >= min_width
                return (is_1080p, w, h)
        except Exception:
            pass
        
        return (False, 0, 0)
    
    except Exception:
        return (False, 0, 0)


def download_and_probe_sample(m3u8_url: str, verbose: bool = False, timeout: int = 30) -> tuple:
    """下載 m3u8 的前幾個 TS 段，用 ffmpeg 檢測實際解析度
    回傳: (sample_path, width, height)
    - sample_path: 下載的樣本檔案路徑（None 表示失敗，由呼叫者負責清理）
    - width, height: 0, 0 若失敗
    """
    import tempfile
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        ffmpeg_exe = os.path.join(script_dir, 'exe', 'ffmpeg.exe')
        if not os.path.exists(ffmpeg_exe):
            return (None, 0, 0)
        
        # 先獲取 m3u8 內容找出 TS 段 URL
        try:
            resp = requests.get(m3u8_url, timeout=5.0, verify=False)
            if not resp.ok:
                return (None, 0, 0)
            m3u8_text = resp.text
        except Exception:
            return (None, 0, 0)
        
        # 解析 m3u8 找出 TS 檔案列表
        ts_urls = []
        lines = m3u8_text.splitlines()
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#'):
                ts_urls.append(urljoin(m3u8_url, line))
                if len(ts_urls) >= 3:  # 最多嘗試前 3 個
                    break
        
        if not ts_urls:
            return (None, 0, 0)
        
        # 依序嘗試下載和檢測每個 TS 檔案
        for ts_url in ts_urls:
            with tempfile.NamedTemporaryFile(suffix='.ts', delete=False) as tmp:
                tmp_path = tmp.name
            
            try:
                resp = requests.get(ts_url, timeout=10.0, verify=False)
                if not resp.ok:
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                    continue
                
                with open(tmp_path, 'wb') as f:
                    f.write(resp.content)
                
                # 用 ffmpeg 檢測此 TS 檔案的解析度
                cmd = [
                    ffmpeg_exe, '-hide_banner',
                    '-i', tmp_path
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding='utf-8', errors='replace')
                output = result.stderr
                
                # 尋找解析度 - 嘗試多種模式
                m = re.search(r'Video:.*?(\d{3,4})x(\d{3,4})', output)
                if m:
                    w = int(m.group(1))
                    h = int(m.group(2))
                    return (tmp_path, w, h)
                
                m2 = re.search(r'(\d{3,4})x(\d{3,4})', output)
                if m2:
                    w = int(m2.group(1))
                    h = int(m2.group(2))
                    return (tmp_path, w, h)
                
                # 此 TS 無法檢測，嘗試下一個
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
                
            except Exception:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
        
        return (None, 0, 0)
    except Exception:
        return (None, 0, 0)


def find_1080_variant(url: str, verbose: bool = False) -> tuple:
    """檢測 m3u8 的實際解析度
    
    策略：
    1. 如果是 master playlist，優先使用 RESOLUTION 標籤
    2. 沒有 RESOLUTION 標籤時，嘗試下載樣本檢測
    3. 非 master playlist 也嘗試下載樣本檢測
    
    回傳: (variant_url, resolution_str, is_1080p, sample_files)
    """
    sample_files = []
    
    try:
        resp = requests.get(url, timeout=3.0, verify=False)
        if not resp.ok:
            return (None, None, False, sample_files)
        txt = resp.text
    except Exception as e:
        if verbose:
            safe_print(f'    [驗證] 無法取得 m3u8: {e}')
        return (None, None, False, sample_files)

    variant_url = url
    
    # 策略1：嘗試從 master playlist 的 RESOLUTION 標籤提取解析度
    if '#EXT-X-STREAM-INF' in txt:
        lines = txt.splitlines()
        best_variant = None
        best_res = (0, 0)
        variants_no_res = []  # 沒有 RESOLUTION 標籤的變體
        
        for i, line in enumerate(lines):
            if line.strip().startswith('#EXT-X-STREAM-INF'):
                res_match = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
                uri = lines[i+1].strip() if i+1 < len(lines) else ''
                
                if uri:
                    variant_url_abs = urljoin(url, uri)
                    if res_match:
                        w = int(res_match.group(1))
                        h = int(res_match.group(2))
                        # 選擇最高解析度
                        if w * h > best_res[0] * best_res[1]:
                            best_variant = variant_url_abs
                            best_res = (w, h)
                    else:
                        # 沒有 RESOLUTION，記下來稍後嘗試
                        variants_no_res.append(variant_url_abs)
        
        # 如果有 RESOLUTION 標籤，使用最高的
        if best_variant and best_res[0] > 0:
            w, h = best_res
            is_1080p = w >= 1920
            if verbose:
                safe_print(f'    [驗證] Master 讀取 {w}x{h}')
            return (best_variant, f'{w}x{h}', is_1080p, sample_files)
        
        # 沒有 RESOLUTION 標籤，嘗試下載第一個變體的樣本
        if variants_no_res:
            if verbose:
                safe_print(f'    [驗證] Master 無 RESOLUTION，嘗試樣本檢測')
            variant_url = variants_no_res[0]
        else:
            if verbose:
                safe_print(f'    [驗證] Master 無有效變體，無法驗證')
            return (None, None, False, sample_files)
    else:
        # 不是 master playlist，直接使用此 URL 作為變體
        if verbose:
            safe_print(f'    [驗證] 非 master，嘗試樣本檢測')
    
    # 策略2：下載樣本檢測（當 master 無 RESOLUTION 或不是 master）
    sample_path, w, h = download_and_probe_sample(variant_url, verbose=verbose, timeout=30)
    
    if sample_path:
        sample_files.append(sample_path)
    
    # 只有驗證成功才返回結果
    if w > 0 and h > 0:
        is_1080p = w >= 1920
        if verbose:
            status = '✓ 1080P' if is_1080p else f'✗ {w}x{h}'
            safe_print(f'    [驗證] 樣本檢測: {status}')
        return (variant_url, f'{w}x{h}', is_1080p, sample_files)
    
    # 驗證失敗
    if verbose:
        safe_print(f'    [驗證] 無法確認解析度')
    return (None, None, False, sample_files)


def collect_m3u8_for_episode(page: Page, click_el, wait_seconds: float) -> List[str]:
    collected: List[str] = []

    def on_request(req: Request):
        url = req.url
        if '.m3u8' in url:
            if url not in collected:
                collected.append(url)

    page.on('request', on_request)

    try:
        click_el.click()
    except Exception:
        page.evaluate("el => el.click()", click_el)

    # wait up to wait_seconds but return early if we captured *.m3u8
    end = time.time() + wait_seconds
    while time.time() < end:
        if collected:
            break
        time.sleep(0.12)

    try:
        page.remove_listener('request', on_request)
    except Exception:
        pass
    return collected


def run_downloader_task(url: str, out_dir: str, save_name: str, tmp_root: str) -> dict:
    """Run N_m3u8DL-RE to download segments (skip merge). Returns dict with status and tmp_dir."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    downloader = os.path.join(script_dir, 'exe', 'N_m3u8DL-RE.exe')
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    tmp_dir = os.path.join(tmp_root, f"nm3_tmp_{save_name}_{timestamp}")
    os.makedirs(tmp_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    # 建立下載器的基本命令參數
    base_cmd = [downloader, url, '--save-dir', out_dir, '--save-name', save_name,
                '--skip-merge', '--tmp-dir', tmp_dir, '--no-log']

    # 嘗試以常見的靜默參數啟動下載器，避免產生日誌
    # 若某組參數執行失敗會嘗試下一組，直到成功或所有組合皆失敗
    candidate_flag_sets = [
        [],
        ['--quiet'],
        ['--log-level', 'error'],
    ]

    last_proc = None
    success = False
    for extra in candidate_flag_sets:
        cmd = base_cmd + extra
        try:
            proc = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                                stdin=subprocess.DEVNULL, encoding='utf-8', errors='replace')
            last_proc = proc
            if proc.returncode == 0:
                success = True
                break
            else:
                success = False
        except FileNotFoundError as e:
            return {'success': False, 'tmp_dir': tmp_dir, 'error': str(e)}
        except Exception as e:
            # try next candidate
            success = False
            last_proc = None
            continue

    if last_proc is None:
        return {'success': False, 'tmp_dir': tmp_dir, 'error': 'failed to execute downloader'}

    return {'success': success, 'tmp_dir': tmp_dir, 'returncode': last_proc.returncode}


def merge_ts_to_mp4_and_cleanup(tmp_dir: str, out_mp4: str, ffmpeg_path: Optional[str] = None, clean: bool = True) -> dict:
    """Merge segments in tmp_dir into out_mp4 using ffmpeg. If clean True, remove tmp_dir on success."""
    if not ffmpeg_path:
        ffmpeg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'exe', 'ffmpeg.exe')

    # 嘗試尋找下載器輸出的 raw.m3u8 (或 index.m3u8)，若找不到則使用 concat.txt 合併 .ts
    raw_m3u8 = None
    for cand in ('raw.m3u8', 'index.m3u8'):
        p = os.path.join(tmp_dir, cand)
        if os.path.exists(p):
            raw_m3u8 = p
            break

    try:
        cwd = tmp_dir
        if raw_m3u8 and os.path.exists(raw_m3u8):
            # 直接讓 ffmpeg 讀取 playlist
            cmd = [ffmpeg_path, '-allowed_extensions', 'ALL', '-i', raw_m3u8, '-c', 'copy', '-bsf:a', 'aac_adtstoasc', '-y', out_mp4]
            proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                                stdin=subprocess.DEVNULL, encoding='utf-8', errors='replace')
            ok = proc.returncode == 0
        else:
            # 若沒有可直接讀取的 playlist，嘗試從 .ts 檔建立 concat.txt
            seg_dir = os.path.join(tmp_dir, '0____')
            if not os.path.isdir(seg_dir):
                # 嘗試在 tmp_dir 底下尋找任何 .ts 檔
                segs = sorted([os.path.join(dp, f) for dp, dn, filenames in os.walk(tmp_dir) for f in filenames if f.endswith('.ts')])
                if not segs:
                    return {'success': False, 'error': 'no ts segments found'}
                # write concat with absolute paths
                concat_path = os.path.join(tmp_dir, 'concat.txt')
                with open(concat_path, 'w', encoding='utf-8') as fh:
                    for s in segs:
                        fh.write(f"file '{s.replace('\\', '/')}'\n")
                cmd = [ffmpeg_path, '-f', 'concat', '-safe', '0', '-i', concat_path, '-c', 'copy', '-bsf:a', 'aac_adtstoasc', '-y', out_mp4]
                proc = subprocess.run(cmd, cwd=tmp_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                                    stdin=subprocess.DEVNULL, encoding='utf-8', errors='replace')
                ok = proc.returncode == 0
            else:
                # 若 segments 放在 0____ 子資料夾，使用相對路徑建 concat
                ts_files = sorted([f for f in os.listdir(seg_dir) if f.endswith('.ts')])
                if not ts_files:
                    return {'success': False, 'error': 'no ts segments in 0____'}
                concat_path = os.path.join(tmp_dir, 'concat.txt')
                with open(concat_path, 'w', encoding='ascii') as fh:
                    for f in ts_files:
                        fh.write(f"file '0____/{f}'\n")
                cmd = [ffmpeg_path, '-f', 'concat', '-safe', '0', '-i', 'concat.txt', '-c', 'copy', '-bsf:a', 'aac_adtstoasc', '-y', out_mp4]
                proc = subprocess.run(cmd, cwd=tmp_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                                    stdin=subprocess.DEVNULL, encoding='utf-8', errors='replace')
                ok = proc.returncode == 0

        if ok:
            if clean:
                try:
                    shutil.rmtree(tmp_dir)
                except Exception:
                    pass
            return {'success': True, 'out': out_mp4}
        else:
            return {'success': False, 'error': getattr(proc, 'stdout', b'').decode('utf-8', errors='ignore')}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def orchestrate_downloads(results: List[tuple], args) -> None:
    """Orchestrate downloads with concurrency limit and submit merges when each download finishes."""
    if not results:
        print('沒有可下載的集數。')
        return

    out_dir = args.out_dir or os.path.abspath('.')
    os.makedirs(out_dir, exist_ok=True)
    tmp_root = os.path.join(out_dir, 'nm3_tmp')
    os.makedirs(tmp_root, exist_ok=True)

    max_dl = max(1, int(getattr(args, 'max_downloads', 5)))
    print(f'開始下載：總共 {len(results)} 集，最多同時 {max_dl} 個下載。')

    download_futures = {}
    merge_futures = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_dl) as dl_pool, \
            concurrent.futures.ThreadPoolExecutor(max_workers=max(1, max_dl)) as merge_pool:

        # submit download tasks (results format: ep, url, resolution, fname)
        for item in results:
            if len(item) == 4:
                ep, url, res, fname = item
            else:
                # 相容舊格式
                ep, url, fname = item[0], item[1], item[-1]
            save_name = os.path.splitext(fname)[0]
            fut = dl_pool.submit(run_downloader_task, url, out_dir, save_name, tmp_root)
            download_futures[fut] = (ep, url, fname)

        # as downloads complete, schedule merges
        for fut in concurrent.futures.as_completed(download_futures):
            ep, url, fname = download_futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                print(f'EP{ep:02d} 下載例外: {e}')
                continue

            if not res.get('success'):
                print(f'EP{ep:02d} 下載失敗: {res.get("error") or res.get("returncode")}')
                continue

            tmp_dir = res.get('tmp_dir')
            out_mp4 = os.path.join(out_dir, fname)
            clean_flag = not getattr(args, 'no_clean', False)
            # submit merge task
            mfut = merge_pool.submit(merge_ts_to_mp4_and_cleanup, tmp_dir, out_mp4, None, clean_flag)
            merge_futures[mfut] = (ep, fname)
            print(f'EP{ep:02d} 下載完成，已排入合併：{fname}')

        # wait for merges to finish
        for mfut in concurrent.futures.as_completed(merge_futures):
            ep, fname = merge_futures[mfut]
            try:
                mres = mfut.result()
            except Exception as e:
                print(f'EP{ep:02d} 合併例外: {e}')
                continue
            if mres.get('success'):
                print(f'EP{ep:02d} 合併成功：{fname}')
            else:
                print(f'EP{ep:02d} 合併失敗：{mres.get("error")}')


class StreamingDownloader:
    """即時下載管理器：驗證失敗時自動搜索下一個優先級的來源"""
    
    def __init__(self, args, max_workers: int = 5, search_func=None, search_params=None):
        self.args = args
        self.out_dir = args.out_dir or os.path.abspath('.')
        os.makedirs(self.out_dir, exist_ok=True)
        self.tmp_root = os.path.join(self.out_dir, 'nm3_tmp')
        os.makedirs(self.tmp_root, exist_ok=True)
        
        self.max_workers = max(1, max_workers)
        self.dl_pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers)
        self.merge_pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers)
        
        self.download_futures = {}
        self.merge_futures = {}
        self.results_log = []
        self.retry_queue = {}  # {ep: skip_src_indices}
        
        # 搜索函數及參數，用於驗證失敗時重新搜索下一個源
        self.search_func = search_func
        self.search_params = search_params or {}  # {ep: {'page': page, 'el': el, ...}}
        
        safe_print(f'即時下載模式啟動（驗證失敗自動找下一個來源），最多同時 {self.max_workers} 個下載')
    
    def submit_download(self, ep: int, url: str, source_name: str, src_idx: int, fallback_sources: list, filename: str):
        """
        提交下載任務
        
        參數:
        - ep: 集數
        - url: m3u8 URL
        - source_name: 來源名稱
        - src_idx: 源索引
        - fallback_sources: 備選來源（目前未使用，保留以兼容舊代碼）
        - filename: 輸出文件名
        """
        if not url:
            safe_print(f'EP{ep:02d} 沒有可用的 m3u8 URL，跳過')
            self.results_log.append((ep, None, None, filename, 'skipped'))
            return
        
        # 初始化該集的重試隊列（記錄失敗的源索引）
        self.retry_queue[ep] = {'skip_indices': [src_idx] if src_idx is not None else [], 'url': url, 'source_name': source_name, 'filename': filename}
        
        safe_print(f'EP{ep:02d} [1/2] 開始下載: {source_name}')
        
        # 提交下載
        save_name = os.path.splitext(filename)[0]
        fut = self.dl_pool.submit(run_downloader_task, url, self.out_dir, save_name, self.tmp_root)
        self.download_futures[fut] = (ep, url, source_name, filename)
        
        # 檢查並處理已完成的任務
        self._process_completed()
    
    def _try_next_source(self, ep: int, filename: str):
        """嘗試下一個來源（按優先級順序跳過失敗的源）"""
        if ep not in self.retry_queue:
            safe_print(f'EP{ep:02d} 無重試隊列，已放棄')
            self.results_log.append((ep, None, None, filename, 'no_retry_queue'))
            return
        
        if not self.search_func:
            safe_print(f'EP{ep:02d} 無搜索函數，已放棄')
            self.results_log.append((ep, None, None, filename, 'no_search_func'))
            self.retry_queue.pop(ep, None)
            return
        
        # 獲取該集已失敗的源索引列表
        skip_indices = self.retry_queue[ep].get('skip_indices', [])
        
        # 從搜索參數中獲取該集所需的參數
        if ep not in self.search_params:
            safe_print(f'EP{ep:02d} 搜索參數不存在，已放棄')
            self.retry_queue.pop(ep, None)
            return
        
        params = self.search_params[ep]
        
        try:
            # 調用搜索函數找下一個源（傳入要跳過的索引）
            result = self.search_func(
                params['page'], params['el'], params['idx'], params['all_sources'],
                params['ep_selectors'], params['wait_seconds'], params['original_url'],
                skip_src_indices=skip_indices
            )
            
            if result and result[0]:
                url, source_name, src_idx = result
                safe_print(f'EP{ep:02d} 嘗試備選來源: {source_name}')
                
                # 更新重試隊列，記錄新的源索引
                skip_indices.append(src_idx)  # 把新源加入skip列表，以備下次失敗
                self.retry_queue[ep] = {'skip_indices': skip_indices, 'url': url, 'source_name': source_name, 'filename': filename}
                
                # 提交下載
                save_name = os.path.splitext(filename)[0]
                fut = self.dl_pool.submit(run_downloader_task, url, self.out_dir, save_name, self.tmp_root)
                self.download_futures[fut] = (ep, url, source_name, filename)
            else:
                safe_print(f'EP{ep:02d} 沒有其他可用的來源，已放棄')
                self.results_log.append((ep, None, None, filename, 'all_sources_exhausted'))
                self.retry_queue.pop(ep, None)
        except Exception as e:
            safe_print(f'EP{ep:02d} 搜索下一個來源時出錯: {e}')
            self.retry_queue.pop(ep, None)
    
    def _merge_and_verify(self, tmp_dir: str, out_mp4: str, ep: int, fname: str, 
                          source_name: str, clean_flag: bool) -> tuple:
        """合併並驗證最終 mp4 的分辨率
        
        返回: (is_1080p, w, h, source_name) 或 (False, 0, 0, source_name)
        """
        try:
            # 合併
            mres = merge_ts_to_mp4_and_cleanup(tmp_dir, out_mp4, None, clean_flag)
            if not mres.get('success'):
                return (False, 0, 0, source_name)
            
            # 最終驗證
            is_1080p, w, h = verify_final_mp4_resolution(out_mp4, min_width=1920)
            
            # 如果驗證失敗，刪除檔案
            if not is_1080p:
                try:
                    if os.path.exists(out_mp4):
                        os.remove(out_mp4)
                except Exception:
                    pass
            
            return (is_1080p, w, h, source_name)
        
        except Exception as e:
            try:
                if os.path.exists(out_mp4):
                    os.remove(out_mp4)
            except Exception:
                pass
            return (False, 0, 0, source_name)
    
    def _process_completed(self):
        """處理已完成的任務"""
        # 檢查已完成的下載
        done_downloads = [f for f in self.download_futures.keys() if f.done()]
        for fut in done_downloads:
            if fut not in self.download_futures:
                continue
            ep, url, source_name, fname = self.download_futures.pop(fut)
            try:
                res = fut.result()
            except Exception as e:
                safe_print(f'EP{ep:02d} 下載例外: {e}，嘗試下一個來源')
                self._try_next_source(ep, fname)
                continue
            
            if not res.get('success'):
                safe_print(f'EP{ep:02d} 下載失敗，嘗試下一個來源')
                self._try_next_source(ep, fname)
                continue
            
            tmp_dir = res.get('tmp_dir')
            out_mp4 = os.path.join(self.out_dir, fname)
            clean_flag = not getattr(self.args, 'no_clean', False)
            
            safe_print(f'EP{ep:02d} [2/2] 合併並進行最終驗證')
            # 提交合併和最終驗證
            mfut = self.merge_pool.submit(
                self._merge_and_verify, tmp_dir, out_mp4, ep, fname, source_name, clean_flag
            )
            self.merge_futures[mfut] = (ep, fname)
        
        # 檢查已完成的合併和驗證
        done_merges = [f for f in self.merge_futures.keys() if f.done()]
        for mfut in done_merges:
            if mfut not in self.merge_futures:
                continue
            ep, fname = self.merge_futures.pop(mfut)
            try:
                result = mfut.result()
                is_1080p, w, h, source_name = result
                if is_1080p:
                    safe_print(f'EP{ep:02d} ✓ 完成 ({w}x{h}) | 來源: {source_name}')
                    self.results_log.append((ep, None, f'{w}x{h}', fname, 'success'))
                    self.retry_queue.pop(ep, None)  # 成功，移除重試隊列
                else:
                    safe_print(f'EP{ep:02d} ✗ 驗證失敗 ({w}x{h})，嘗試下一個來源')
                    self._try_next_source(ep, fname)
            except Exception as e:
                safe_print(f'EP{ep:02d} 合併/驗證例外: {e}')
                self._try_next_source(ep, fname)
    
    def wait_all(self):
        """等待所有下載和驗證任務完成"""
        safe_print('\n等待所有下載和驗證任務完成...')
        
        # 不斷檢查直到所有任務完成
        max_wait_cycles = 3600  # 30分鐘（每個週期0.5秒）
        wait_cycles = 0
        last_status = None
        
        while wait_cycles < max_wait_cycles:
            # 先處理已完成的任務
            self._process_completed()
            
            # 明確檢查未完成的任務
            pending_download = sum(1 for f in list(self.download_futures.keys()) if not f.done())
            pending_merge = sum(1 for f in list(self.merge_futures.keys()) if not f.done())
            pending_retry = sum(len(v) for v in self.retry_queue.values() if v)
            
            has_pending = pending_download + pending_merge + pending_retry > 0
            
            if not has_pending:
                break
                
            # 每10秒打印一次狀態
            if wait_cycles % 20 == 0:
                current_status = (pending_download, pending_merge)
                if current_status != last_status:
                    safe_print(f'  等待中... 下載: {current_status[0]}, 驗證: {current_status[1]}')
                    last_status = current_status
            
            time.sleep(0.5)
            wait_cycles += 1
        
        if wait_cycles >= max_wait_cycles:
            safe_print('\n警告：等待超時，可能還有未完成的任務')
        
        safe_print('正在關閉線程池...')
        
        # 立即關閉所有線程池（不等待）
        try:
            self.dl_pool.shutdown(wait=False)
        except Exception:
            pass
        try:
            self.merge_pool.shutdown(wait=False)
        except Exception:
            pass
        
        safe_print('全部都下載完也驗證完了！')
    
    def print_summary(self):
        """列印下載摘要"""
        safe_print('\n========== 下載摘要 ==========')
        success_count = sum(1 for r in self.results_log if r[4] == 'success')
        total_count = len(self.results_log)
        safe_print(f'成功: {success_count}/{total_count}')
        
        for ep, url, res, fname, status in sorted(self.results_log, key=lambda x: x[0]):
            status_icon = '✓' if status == 'success' else '✗'
            safe_print(f'  EP{ep:02d} {status_icon} {res} | {fname} | {status}')


def main():
    args = parse_args()

    # Default to fast + headless unless user disables with --no-fast
    if not getattr(args, 'no_fast', False):
        args.fast = True
        args.headless = True

    # handle UI suppression: --no-ui requires explicit --url
    if getattr(args, 'no_ui', False):
        if not args.url:
            print('使用 --no-ui 時必須同時指定 --url，程式退出。')
            return
        start_episode = 1
    else:
        if not args.url:
            ui_url, ui_out, start_episode = show_start_ui()
            if not ui_url:
                print('未提供 URL，程式退出。')
                return
            args.url = ui_url
            if ui_out:
                args.out_dir = ui_out
        else:
            start_episode = 1

    ep_selectors = [s.strip() for s in args.episode_selector.split(',') if s.strip()]

    def get_all_source_buttons(page, source_text: str) -> list:
        """獲取所有符合來源關鍵字的按鈕元素和文字"""
        candidates = []
        try:
            elems = page.query_selector_all('button, a, span, div')
            for el in elems:
                try:
                    txt = page.evaluate('(el) => (el.innerText || el.textContent || "").trim()', el)
                except Exception:
                    continue
                if not txt:
                    continue
                if source_text.lower() in txt.lower():
                    # 過濾太長的文字（可能是容器元素）
                    if len(txt) < 30:
                        candidates.append((el, txt))
        except Exception:
            pass
        return candidates

    def sniff_m3u8_from_episode(page, episode_el, wait_seconds: float, debug_ep: str = '') -> List[str]:
        """點擊集數按鈕並收集 m3u8 URL"""
        collected: List[str] = []
        
        def on_request(req: Request):
            try:
                url = req.url
                if '.m3u8' in url and url not in collected:
                    collected.append(url)
            except Exception:
                pass  # 忽略任何網路請求異常
        
        # 在點擊前設置監聽器
        page.on('request', on_request)
        
        try:
            # 使用 dispatchEvent 點擊（對不可見元素也有效）
            page.evaluate('''(el) => {
                el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
            }''', episode_el)
            
            # 短暫等待，然後嘗試點擊播放按鈕
            time.sleep(0.5)
            try:
                page.evaluate('''() => {
                    const playBtn = document.querySelector('.play, .play-btn, .player-play, .play-button, button[title*="播放"], button[title*="Play"], .video-play, .start-play');
                    if (playBtn) {
                        playBtn.click();
                    } else {
                        // 嘗試觸發 video 播放
                        const videos = document.querySelectorAll('video');
                        videos.forEach(v => v.play());
                    }
                }''')
            except Exception:
                pass
            
            # 等待一段時間收集請求
            end = time.time() + wait_seconds
            while time.time() < end and not collected:
                time.sleep(0.05)
        except Exception as e:
            if debug_ep:
                safe_print(f'      [DEBUG] sniff_m3u8 error: {e}')
        
        try:
            page.remove_listener('request', on_request)
        except Exception:
            pass  # 忽略監聽器移除異常
        
        return collected

    def check_m3u8_resolution(m3u8_urls: List[str]) -> tuple:
        """
        檢查 m3u8 列表，返回第一個驗證成功的 URL
        回傳: (best_url, resolution, is_1080p, sample_files) 或 (None, None, False, [])
        """
        all_sample_files = []
        
        for i, u in enumerate(m3u8_urls):
            # 進行樣本驗證
            variant_url, res_str, is_1080p, sample_files = find_1080_variant(u, verbose=False)
            all_sample_files.extend(sample_files)
            
            # 只要驗證成功就返回
            if variant_url and res_str:
                return (variant_url, res_str, is_1080p, all_sample_files)
        
        # 所有 m3u8 都驗證失敗
        return (None, None, False, all_sample_files)

    def get_best_m3u8_for_episode(page, episode_el, ep_idx: int, all_sources: list, ep_selectors: list, wait_seconds: float, original_url: str, skip_src_indices: list = None) -> tuple:
        """
        針對單一集數，依照優先順序逐一嘗試來源並驗證
        
        優先順序：海外推薦 > 海外 > 推薦 > 其他
        
        策略：
        1. 依序嘗試優先級最高的來源（跳過已失敗的）
        2. 找到 m3u8 後立即進行快速驗證
        3. 驗證成功就立即返回並開始下載
        4. 驗證失敗就返回下一個源的搜索位置
        
        參數：
        - skip_src_indices: 要跳過的源索引列表（已失敗的源）
        
        回傳: (url, source_name, failed_src_idx) 或 (None, None, None)
        - failed_src_idx: 這次嘗試失敗的源索引（用於下次重試時跳過）
        """
        if skip_src_indices is None:
            skip_src_indices = []
        
        # 提取來源文字列表
        source_texts = [(txt, '海外' in txt, '推薦' in txt) for _, txt in all_sources]
        
        # 分類來源索引（4個優先度）
        overseas_rec_indices = [i for i, (txt, is_overseas, is_rec) in enumerate(source_texts) if is_overseas and is_rec]
        overseas_indices = [i for i, (txt, is_overseas, is_rec) in enumerate(source_texts) if is_overseas and not is_rec]
        rec_indices = [i for i, (txt, is_overseas, is_rec) in enumerate(source_texts) if not is_overseas and is_rec]
        other_indices = [i for i in range(len(source_texts)) if i not in (overseas_rec_indices + overseas_indices + rec_indices)]
        
        # 按優先順序排列來源組
        source_groups = [
            ('海外推薦', overseas_rec_indices),
            ('海外', overseas_indices),
            ('推薦', rec_indices),
            ('其他', other_indices)
        ]
        
        def get_episodes_from_source(src_idx: int):
            """直接從指定索引的 sourcelist 獲取集數元素"""
            try:
                sourcelists = page.query_selector_all('.sourcelist')
                if src_idx >= len(sourcelists):
                    return []
                
                # 先嘗試從 .jujiepisodios 獲取
                container = sourcelists[src_idx].query_selector('.jujiepisodios')
                if container:
                    episodes = container.query_selector_all('a')
                    if episodes:
                        return episodes
                
                # 如果沒有 .jujiepisodios，直接從 sourcelist 獲取
                episodes = sourcelists[src_idx].query_selector_all('a')
                return episodes
            except Exception:
                return []
        
        def try_source_with_verification(src_idx: int, source_txt: str) -> tuple:
            """
            嘗試單一來源，抓取 m3u8 並進行快速驗證
            回傳: (url, source_txt) 如果驗證成功，否則 (None, source_txt)
            """
            try:
                # 確保在原始頁面
                if page.url != original_url:
                    page.goto(original_url, wait_until='domcontentloaded')
                    time.sleep(0.5)
                
                # 先切換到對應的 FLV 來源
                page.evaluate(f'''() => {{
                    const buttons = document.querySelectorAll('a.ctuijian');
                    if (buttons[{src_idx}]) {{
                        buttons.forEach(b => b.classList.remove('selected'));
                        buttons[{src_idx}].classList.add('selected');
                        buttons[{src_idx}].dispatchEvent(new MouseEvent('click', {{bubbles: true, cancelable: true, view: window}}));
                    }}
                }}''')
                time.sleep(0.3)
                
                # 獲取集數元素
                episode_elements = get_episodes_from_source(src_idx)
                
                if not episode_elements:
                    # 沒有找到任何集數 - 添加調試
                    if ep_idx < 5:  # 只在前幾集打印，避免日誌過多
                        safe_print(f'        [DEBUG] {source_txt}: 無法獲取集數元素')
                    return (None, source_txt, src_idx)
                
                if ep_idx >= len(episode_elements):
                    # 這個源沒有這個集數 - 集數不足
                    if ep_idx < 5:
                        safe_print(f'        [DEBUG] {source_txt}: 集數不足 (共{len(episode_elements)}集, 要求索引={ep_idx})')
                    return (None, source_txt, src_idx)
                
                current_ep_el = episode_elements[ep_idx]
                
                # 嗅探 m3u8（可能有多個）
                m3u8_list = sniff_m3u8_from_episode(page, current_ep_el, wait_seconds)
                
                if not m3u8_list:
                    if ep_idx < 5:
                        safe_print(f'        [DEBUG] {source_txt}: 無法嗅探到 m3u8')
                    return (None, source_txt, src_idx)
                
                # 針對此來源的所有 m3u8，逆序嘗試（優先最後一個）
                for m3u8_url in reversed(m3u8_list):
                    # 進行快速驗證（只讀 m3u8 標籤，不下載視頻）
                    is_1080p, w, h = quick_verify_resolution(m3u8_url, timeout=1)
                    
                    if is_1080p:
                        # 驗證成功，返回此 URL 和源索引
                        safe_print(f'      ✓ 驗證通過 ({w}x{h}): {source_txt}')
                        return (m3u8_url, source_txt, src_idx)
                
                # 此來源的所有 m3u8 都驗證失敗
                if ep_idx < 5:
                    safe_print(f'        [DEBUG] {source_txt}: m3u8 驗證失敗')
                return (None, source_txt, src_idx)
            
            except Exception as e:
                if ep_idx < 5:
                    safe_print(f'        [DEBUG] {source_txt}: 異常 {e}')
                return (None, source_txt, src_idx)
        
        total_count = sum(len(indices) for _, indices in source_groups)
        checked_count = 0
        
        try:
            # 依照優先順序遍歷所有來源組，跳過已失敗的
            for group_name, group_indices in source_groups:
                for src_idx in group_indices:
                    # 跳過已失敗的源
                    if src_idx in skip_src_indices:
                        continue
                    
                    checked_count += 1
                    source_txt = source_texts[src_idx][0]
                    safe_print(f'      [{checked_count}/{total_count}] {source_txt}', end='\r')
                    
                    url, src_name, src_idx = try_source_with_verification(src_idx, source_txt)
                    if url:
                        # 驗證成功，清除進度行
                        safe_print(f'                                                  ')
                        # 立即返回，開始下載（不繼續驗證其他來源）
                        return (url, src_name, src_idx)
            
            # 清除進度行
            safe_print(f'                                                  ')
            
            # 所有來源都驗證失敗或已跳過
            # 添加調試信息：未找到原因
            safe_print(f'  => 未找到可用的 m3u8 {source_texts[0][0] if source_texts else ""}')
            return (None, None, None)
        
        except Exception as e:
            safe_print(f'  => 異常: {e}')
            return (None, None, None)

    def get_all_sources_for_episode_search(page, source_text: str) -> list:
        """獲取所有可能的來源按鈕，用於每集搜尋"""
        candidates = get_all_source_buttons(page, source_text)
        if not candidates:
            # 如果沒有找到，嘗試更廣泛的搜尋
            try:
                elems = page.query_selector_all('button, a, span, div')
                for el in elems:
                    try:
                        txt = page.evaluate('(el) => (el.innerText || el.textContent || "").trim()', el)
                    except Exception:
                        continue
                    if not txt or len(txt) > 30:
                        continue
                    if any(kw in txt for kw in ['推薦', 'FLV', '海外', 'flv']):
                        candidates.append((el, txt))
            except Exception:
                pass
        return candidates

    is_frozen = getattr(sys, 'frozen', False)
    use_headless = bool(getattr(args, 'fast', False) or getattr(args, 'headless', False))

    browsers_ok = ensure_playwright_browsers()
    if not browsers_ok:
        print('[錯誤] 未找到 Playwright browsers。請確保有 browsers/ 目錄。')
        return

    chrome_process = None
    chrome_out = None

    try:
        print('初始化 Playwright...')
        playwright_instance = sync_playwright().start()
        print('Playwright 初始化成功')
    except Exception as e:
        print(f'[錯誤] Playwright 初始化失敗：{e}')
        if chrome_process:
            chrome_process.terminate()
        return

    try:
        p = playwright_instance
        browser = None

        # For frozen exe: use bundled Chromium
        if is_frozen:
            try:
                print('啟動捆綁的 Chromium...')
                browser = p.chromium.launch(headless=use_headless, args=['--no-sandbox', '--disable-setuid-sandbox'])
                print('捆綁的 Chromium 啟動成功')
            except Exception as e:
                print(f'[錯誤] 捆綁 Chromium 啟動失敗：{e}')
                return
        else:
            # Not frozen: use Playwright-managed Chromium
            try:
                if use_headless:
                    try:
                        browser = p.chromium.launch(headless=True, args=['--headless=new'])
                    except Exception:
                        browser = p.chromium.launch(headless=True)
                else:
                    launch_args = []
                    if not getattr(args, 'no_minimize', False):
                        launch_args.append('--start-minimized')
                    browser = p.chromium.launch(headless=False, args=launch_args)
            except Exception as e:
                print('啟動 Playwright Chromium 失敗：', e)
                print('請確認 Playwright browsers 已安裝（執行 `playwright install`），或檢查環境。')
                return

        context = None
        page = None
        if browser.contexts:
            context = browser.contexts[0]
            if context.pages:
                page = context.pages[0]
        if not context:
            context = browser.new_context()
            page = context.new_page()

        # if fast mode, block images/styles/fonts to speed up loading
        if getattr(args, 'fast', False) or getattr(args, 'headless', False):
            try:
                page.route('**/*', lambda route, request: route.abort() if request.resource_type in ('image', 'stylesheet', 'font', 'media') else route.continue_())
            except Exception:
                pass

        page.goto(args.url, wait_until='domcontentloaded')

        def sanitize_filename(name: str) -> str:
            return re.sub(r'[\\/:*?"<>|]', '_', name).strip()

        raw_title = ''
        try:
            raw_title = page.title() or ''
        except Exception:
            raw_title = ''

        show_name = raw_title.split(' - ')[0].strip() if raw_title else ''
        show_name = sanitize_filename(show_name) if show_name else 'Unknown'

        season = 1
        
        # 獲取所有可用的來源按鈕（用於每集獨立搜尋）
        all_sources = get_all_sources_for_episode_search(page, args.source_text)
        safe_print(f'\n找到 {len(all_sources)} 個可用來源: {[txt for _, txt in all_sources]}')
        
        # 嘗試所有來源，找出集數最多的那個
        episode_elements = []
        best_source_name = ''
        best_episode_count = 0
        
        if all_sources:
            safe_print('\n正在檢查所有來源的集數數量...')
            for src_idx, (src_el, src_txt) in enumerate(all_sources):
                try:
                    src_el.click()
                    time.sleep(0.8)  # 等待該來源的集數加載
                    
                    # 嘗試滾動該來源的容器以加載所有集數
                    source_ep_els = []
                    for container_sel in ['.jujiepisodios', '.module-play-list-content', '.play-list-box', '.playlist']:
                        try:
                            container = page.query_selector(container_sel)
                            if container:
                                # 激進滾動該容器
                                for _ in range(5):
                                    page.evaluate('''(el) => {
                                        el.scrollTop = el.scrollHeight;
                                    }''', container)
                                    time.sleep(0.1)
                                
                                els = container.query_selector_all('a')
                                if els and len(els) >= 2:
                                    source_ep_els = els
                                    break
                        except Exception:
                            continue
                    
                    if source_ep_els:
                        ep_count = len(source_ep_els)
                        print(f'  {src_txt}: {ep_count} 集')
                        if ep_count > best_episode_count:
                            best_episode_count = ep_count
                            episode_elements = source_ep_els
                            best_source_name = src_txt
                except Exception as e:
                    pass
            
            if episode_elements:
                safe_print(f'✓ 選擇集數最多的來源: {best_source_name} ({best_episode_count} 集)')
        
        # 如果遍歷所有來源仍然找不到，試試第一個容器
        if not episode_elements:
            safe_print('未能從來源標籤找到集數，嘗試直接查詢...')
            first_container_selectors = [
                '.jujiepisodios',  # movieffm.net 的集數容器
                '.module-play-list-content',
                '.play-list-box',
                '.playlist',
            ]
            
            for container_sel in first_container_selectors:
                try:
                    container = page.query_selector(container_sel)  # 只取第一個
                    if container:
                        els = container.query_selector_all('a')
                        if els and len(els) >= 2:  # 移除上限限制，允許更多
                            episode_elements = els
                            safe_print(f'使用容器 {container_sel} 找到 {len(els)} 個集數')
                            break
                except Exception:
                    continue
        
        # 如果容器方式找不到，試試直接 selector
        if not episode_elements:
            for sel in ep_selectors:
                try:
                    els = page.query_selector_all(sel)
                    if els and len(els) >= 2:  # 移除上限限制
                        episode_elements = els
                        safe_print(f'使用 selector {sel} 找到 {len(els)} 個集數')
                        break
                except Exception:
                    continue

        # 如果標準 selector 找不到，嘗試更廣泛的搜尋
        if not episode_elements:
            safe_print('標準 selector 未找到集數，嘗試擴展搜尋...')
            
            # 嘗試其他容器 selector
            extended_containers = [
                '.video-list',
                '.player_list',
                'ul.stui-content__playlist',
                '.myui-content__list',
                '.fed-list-info',
                '.vodlist-play',
            ]
            
            for container_sel in extended_containers:
                try:
                    container = page.query_selector(container_sel)
                    if container:
                        els = container.query_selector_all('a')
                        if els and len(els) >= 2:  # 移除上限限制
                            episode_elements = els
                            safe_print(f'  使用擴展容器 {container_sel} 找到 {len(els)} 個集數')
                            break
                except Exception:
                    continue
        
        if not episode_elements:
            print('找不到任何集數按鈕，請確認 selector 或手動檢查頁面。')
            browser.close()
            return

        print(f'找到 {len(episode_elements)} 個集數按鈕（滾動前）')

        # 嘗試滾動容器以加載所有集數（某些網站需要滾動才能顯示所有集數）
        try:
            # 先試著找容器並多次滾動到底部
            container = None
            for selector in ['.jujiepisodios', '.module-play-list-content', '.play-list-box', '.playlist', '.video-list', '.player_list']:
                el = page.query_selector(selector)
                if el:
                    container = el
                    break
            
            if container:
                print('  開始滾動加載集數...')
                prev_count = len(episode_elements)
                last_check_count = prev_count
                no_change_rounds = 0
                
                # 多次激進滾動以確保加載所有內容
                for scroll_round in range(20):
                    # 滾動到底部
                    page.evaluate('''(el) => {
                        el.scrollTop = el.scrollHeight;
                    }''', container)
                    time.sleep(0.5)  # 加長等待時間讓動態內容加載
                    
                    # 重新獲取集數元素檢查是否增加
                    new_els = container.query_selector_all('a')
                    if new_els and len(new_els) > len(episode_elements):
                        episode_elements = new_els
                        no_change_rounds = 0
                        print(f'    第 {scroll_round + 1} 輪: 發現 {len(episode_elements)} 個集數 (↑{len(episode_elements) - last_check_count})')
                        last_check_count = len(episode_elements)
                    else:
                        no_change_rounds += 1
                        if no_change_rounds >= 3:
                            # 連續 3 輪沒有增加，表示已加載完全
                            print(f'    第 {scroll_round + 1} 輪: 無新增集數，已加載完全')
                            break
            
            # 最後確認一次集數數量——用 JavaScript 直接查詢所有 <a>
            if container:
                # 先用 JS 查詢並計數
                js_count = page.evaluate('''(el) => {
                    return el.querySelectorAll('a').length;
                }''', container)
                
                final_els = container.query_selector_all('a')
                print(f'  最後確認: JS 計數 {js_count} 個, Playwright 獲取 {len(final_els)} 個 <a> 標籤')
                
                if js_count > len(final_els):
                    print(f'    ⚠️  JS 數量不同！嘗試重新獲取...')
                    # 重試一次
                    time.sleep(0.5)
                    final_els = container.query_selector_all('a')
                
                if final_els and len(final_els) >= 2 and len(final_els) <= 500:
                    if len(final_els) > len(episode_elements):
                        print(f'    ← 比之前多 {len(final_els) - len(episode_elements)} 個！')
                    episode_elements = final_els
                
                if len(episode_elements) > best_episode_count:
                    print(f'  ✓ 最終共獲得 {len(episode_elements)} 個集數按鈕')
                else:
                    print(f'  滾動後集數未增加，仍為 {len(episode_elements)} 個')
            
        except Exception as e:
            print(f'  滾動過程中出現異常: {e}，使用現有集數列表')

        print(f'最終找到 {len(episode_elements)} 個可能的集數按鈕')
        
        # 調試：逐個驗證集數
        try:
            all_ep_nums = set()
            for i, el in enumerate(episode_elements):
                txt = page.evaluate('(el) => (el.innerText || el.textContent || "").trim()', el)
                if txt:
                    match = re.search(r'\d+', txt)
                    if match:
                        ep_num = match.group()
                        all_ep_nums.add(int(ep_num))
            
            if all_ep_nums:
                sorted_nums = sorted(list(all_ep_nums))
                print(f'  找到的集數編號 ({len(all_ep_nums)} 個): {min(sorted_nums):03d} ~ {max(sorted_nums):03d}')
                # 檢查缺失的編號
                expected = set(range(min(sorted_nums), max(sorted_nums) + 1))
                missing = expected - all_ep_nums
                if missing:
                    print(f'  缺失集數: {sorted(list(missing))}')
        except Exception as e:
            pass

        total = min(len(episode_elements), 500)
        
        # 啟用即時下載模式
        downloader = StreamingDownloader(args, max_workers=getattr(args, 'max_downloads', 5),
                                        search_func=get_best_m3u8_for_episode, search_params={})
        
        # 使用每集獨立搜尋邏輯
        safe_print('\n===== 啟用每集獨立 1080P 搜尋模式 =====')
        if all_sources:
            safe_print(f'可用來源數量: {len(all_sources)}')
            safe_print('優先順序: 海外推薦 1080P → 推薦FLV 1080P → 其他 1080P → 最高解析度')
        else:
            safe_print('未找到來源按鈕，將直接從頁面抓取 m3u8')
        
        # 記錄初始找到的集數數量，用於後續重新獲取時的驗證
        expected_episode_count = len(episode_elements)
        safe_print(f'預期集數: {expected_episode_count}')
        
        # 建立一個函數來重新獲取正確的集數元素
        def get_episode_elements_safely():
            """安全地重新獲取集數元素，使用第一個容器"""
            # 優先嘗試容器方式（只取第一個容器）
            container_selectors = [
                '.jujiepisodios',
                '.module-play-list-content',
                '.play-list-box',
                '.playlist',
                '.video-list',
                '.player_list',
            ]
            
            for container_sel in container_selectors:
                try:
                    container = page.query_selector(container_sel)  # 只取第一個
                    if container:
                        els = container.query_selector_all('a')
                        if els and 2 <= len(els) <= 200:
                            return els
                except Exception:
                    continue
            
            return []
        
        for idx in range(total):
            # 檢查是否跳過此集
            if idx + 1 < start_episode:
                safe_print(f'\n[EP{idx+1:02d}] 跳過（開始集數為 {start_episode}）')
                continue
            
            try:
                # 重新獲取集數元素（因為切換來源後 DOM 可能變化）
                episode_elements = get_episode_elements_safely()
                
                if not episode_elements or idx >= len(episode_elements):
                    safe_print(f'  警告: 無法獲取第 {idx+1} 集的元素，跳過')
                    continue
                
                el = episode_elements[idx]
                try:
                    ep_label = page.evaluate('(el) => el.innerText || el.textContent', el).strip()
                except Exception:
                    ep_label = f'Episode {idx+1}'
                
                safe_print(f'\n[EP{idx+1:02d}] 處理集數: {ep_label}')
                
                suggested_filename = f"{show_name}.S{season:03d}.E{idx+1:03d}.mp4"
                
                if all_sources:
                    # 保存搜索參數，便於驗證失敗時重新搜索
                    downloader.search_params[idx + 1] = {
                        'page': page, 'el': el, 'idx': idx,
                        'all_sources': all_sources,
                        'ep_selectors': ep_selectors,
                        'wait_seconds': args.wait,
                        'original_url': args.url
                    }
                    
                    # 使用新邏輯：邊搜索邊驗證，第一個驗證通過就立即開始下載
                    result = get_best_m3u8_for_episode(
                        page, el, idx, all_sources, ep_selectors, args.wait, args.url, skip_src_indices=[]
                    )
                    if result and result[0]:
                        url, source_name, src_idx = result
                        safe_print(f'  => 找到來源: {source_name}')
                        # 立即開始下載，傳入源索引便於失敗時跳過
                        downloader.submit_download(idx + 1, url, source_name, src_idx, [], suggested_filename)
                    else:
                        safe_print(f'  => 未找到可用的 m3u8')
                else:
                    # 沒有來源按鈕，直接抓取
                    m3u8_list = sniff_m3u8_from_episode(page, el, args.wait)
                    if m3u8_list:
                        url = m3u8_list[-1]
                        source_name = '當前頁面'
                        safe_print(f'  => 找到來源: {source_name}')
                        downloader.submit_download(idx + 1, url, source_name, [], suggested_filename)
                    else:
                        safe_print(f'  => 未找到可用的 m3u8')
                
            except Exception as e:
                import traceback
                safe_print(f'  處理 EP{idx+1} 時發生錯誤: {e}')
                traceback.print_exc()

        # 等待所有下載完成
        downloader.wait_all()
        downloader.print_summary()
    finally:
        # Clean up Playwright - 設置超時防止卡住
        safe_print('\n正在清理資源...')
        
        # 抑制 asyncio 的 CancelledError，這些是 Playwright 關閉時的無害異常
        import warnings
        import asyncio
        
        def ignore_cancelled_errors(func):
            def wrapper(*args, **kwargs):
                try:
                    return func(*args, **kwargs)
                except asyncio.CancelledError:
                    pass  # 忽略已取消的異步任務
                except Exception as e:
                    # 其他異常可能需要報告
                    if 'CancelledError' not in str(type(e)):
                        raise
            return wrapper
        
        try:
            # 直接在主線程中停止 Playwright，避免線程間 greenlet 問題
            playwright_instance.stop()
            safe_print('資源清理完成')
        except asyncio.CancelledError:
            safe_print('資源清理完成')  # 異步任務取消是正常的
        except Exception as e:
            safe_print(f'資源清理異常（可能無害）: {e}')
        
        # 清理臨時資料夾
        try:
            out_dir = args.out_dir or os.path.abspath('.')
            tmp_root = os.path.join(out_dir, 'nm3_tmp')
            if os.path.exists(tmp_root):
                shutil.rmtree(tmp_root)
                safe_print('已刪除臨時資料夾 nm3_tmp')
        except Exception as e:
            safe_print(f'刪除臨時資料夾失敗: {e}')


if __name__ == '__main__':
    import asyncio
    
    try:
        main()
    except asyncio.CancelledError:
        # 忽略 Playwright 清理過程中的異步取消異常
        pass
    except Exception:
        import traceback
        traceback.print_exc()
        print("\n[錯誤] 程式發生異常停止。")
    else:
        print("\n[完成] 所有任務已執行完畢。")
    finally:
        # 等待用戶按鍵再關閉
        try:
            input("\n按 Enter 鍵結束...")
        except Exception:
            pass


