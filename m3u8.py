#!/usr/bin/env python3
"""M3U8 視頻下載器 - 簡化版本"""

import argparse
import os
import sys
import time
import subprocess
import re
import signal
import atexit
import locale
from datetime import datetime, timezone
from typing import List
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import queue
import json

from playwright.sync_api import sync_playwright, Request

# 禁用 SSL 警告
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def safe_print(*args, **kwargs):
    """安全的 Unicode 輸出"""
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


def parse_episode_selection(selection_str: str, max_episodes: int) -> set:
    """解析集數選擇字符串，返回應下載的集數集合
    
    支持格式：
    - "." 或空字符串：從第1集到最後
    - "1": 僅下載第1集
    - "1,5,9,15": 僅下載這些集數
    - "1-10, 22-30": 下載 1-10 集和 22-30 集
    - 混合："1-5,8,10-12"
    """
    if not selection_str or selection_str.strip() == '.':
        # 默認：從第1集到最後
        return set(range(1, max_episodes + 1))
    
    selection_str = selection_str.strip()
    
    # 檢查是否是單個數字（只下載該集）
    if selection_str.isdigit():
        ep = int(selection_str)
        # 允許超出範圍的集數（可能是後續集數）
        if ep > 0:
            return {ep}
        else:
            return set(range(1, max_episodes + 1))
    
    selected = set()
    parts = selection_str.split(',')
    
    for part in parts:
        part = part.strip()
        if not part:
            continue
        
        if '-' in part:
            try:
                range_parts = part.split('-')
                if len(range_parts) == 2:
                    start = int(range_parts[0].strip())
                    end = int(range_parts[1].strip())
                    start = max(1, start)
                    end = min(max_episodes, end)
                    if start <= end:
                        selected.update(range(start, end + 1))
            except ValueError:
                pass
        else:
            try:
                ep = int(part)
                if ep > 0:
                    selected.add(ep)
            except ValueError:
                pass
    
    return selected if selected else set(range(1, max_episodes + 1))


def format_episode_ranges(episode_nums: list) -> str:
    """將集數列表轉換為範圍格式 (e.g. 1-5,8,10-12)"""
    if not episode_nums:
        return ""
    
    sorted_eps = sorted(episode_nums)
    ranges = []
    start = sorted_eps[0]
    end = sorted_eps[0]
    
    for ep in sorted_eps[1:]:
        if ep == end + 1:
            end = ep
        else:
            if start == end:
                ranges.append(str(start))
            else:
                ranges.append(f'{start}-{end}')
            start = ep
            end = ep
    
    # 添加最後一個範圍
    if start == end:
        ranges.append(str(start))
    else:
        ranges.append(f'{start}-{end}')
    
    return ','.join(ranges)


def parse_args():
    p = argparse.ArgumentParser(description='M3U8 視頻下載器')
    p.add_argument('--url', default=None, help='目標頁面 URL')
    p.add_argument('--out-dir', default=None, help='輸出資料夾')
    p.add_argument('--flv-idx', type=int, default=1, help='FLV 來源索引')
    p.add_argument('--start-ep', default='.', help='集數選擇（. 為全部，1 為僅第1集，支持：1, 1-10, 1,5,9, 1-5,8,10-12）')
    p.add_argument('--max-downloads', type=int, default=5, help='最多並發下載')
    p.add_argument('--wait', type=float, default=2.0, help='M3U8 嗅探等待秒數')
    p.add_argument('--no-ui', action='store_true', help='不顯示 UI')
    p.add_argument('--filter-resolution', action='store_true', help='過濾低分辨率視頻')
    p.add_argument('--no-filter-resolution', action='store_false', dest='filter_resolution', help='不過濾低分辨率視頻')
    p.set_defaults(filter_resolution=True)
    p.set_defaults(fast=True)
    return p.parse_args()


def show_start_ui() -> tuple:
    """顯示 UI 讓用戶輸入參數"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return (None, 1, None, 1)

    root = tk.Tk()
    root.title('M3U8 下載器 - 設定')
    root.geometry('400x180')    
    
    # 設定視窗圖標
    try:
        # 優先檢查 _MEIPASS（打包 EXE 環境）
        icon_path = None
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            icon_path = os.path.join(sys._MEIPASS, 'lioil.ico')
        
        # 如果沒找到，檢查開發環境路徑
        if not icon_path or not os.path.exists(icon_path):
            script_dir = os.path.dirname(os.path.abspath(__file__))
            icon_path = os.path.join(script_dir, 'lioil.ico')
        
        if os.path.exists(icon_path):
            root.iconbitmap(icon_path)
    except Exception:
        pass
    
    # URL 標籤和輸入
    tk.Label(root, text='Target page URL:').pack(anchor='w', padx=8, pady=(8, 0))
    url_var = tk.StringVar()
    url_entry = tk.Entry(root, textvariable=url_var, width=92)
    url_entry.pack(padx=8)
    
    # 添加右鍵菜單
    def create_context_menu(widget):
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label="貼上", command=lambda: paste_text(widget))

        def paste_text(w):
            try:
                text = root.clipboard_get()
                w.insert('insert', text)
            except Exception:
                try:
                    w.event_generate('<<Paste>>')
                except Exception:
                    pass
        
        def show_menu(event):
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                try:
                    menu.grab_release()
                except Exception:
                    pass

        # Windows / 觸控板 / 部分滑鼠驅動的右鍵事件兼容
        widget.bind('<Button-3>', show_menu)
        widget.bind('<ButtonRelease-3>', show_menu)
        widget.bind('<Control-Button-1>', show_menu)
        widget.bind('<Control-v>', lambda e: paste_text(widget))
    
    create_context_menu(url_entry)

    # FLV 和開始集數放在同一行
    flv_start_frm = tk.Frame(root)
    flv_start_frm.pack(anchor='w', padx=8, pady=(8, 0), fill='x')
    
    tk.Label(flv_start_frm, text='FLV source:').pack(side='left')
    flv_var = tk.StringVar(value='1')
    flv_entry = tk.Entry(flv_start_frm, textvariable=flv_var, width=5)
    flv_entry.pack(side='left', padx=(0, 30))
    create_context_menu(flv_entry)
    
    tk.Label(flv_start_frm, text='Episodes:').pack(side='left')
    start_ep_var = tk.StringVar(value='.')
    start_ep_entry = tk.Entry(flv_start_frm, textvariable=start_ep_var, width=30)
    start_ep_entry.pack(side='left', padx=(0, 0))
    create_context_menu(start_ep_entry)
    
    tk.Label(flv_start_frm, text='(. 全部, 1 僅第1集, 1-10, 1,5,9, 1-5,8,10-12)').pack(side='left', padx=(4, 0))

    # 輸出文件夾
    tk.Label(root, text='Output folder (for MP4):').pack(anchor='w', padx=8, pady=(8, 0))
    out_var = tk.StringVar(value='F:/tmp')
    out_frm = tk.Frame(root)
    out_frm.pack(padx=8, fill='x')
    entry_out = tk.Entry(out_frm, textvariable=out_var)
    entry_out.pack(side='left', fill='x', expand=True)
    
    def browse_out():
        d = filedialog.askdirectory(title='Select output folder')
        if d:
            out_var.set(d)
    
    tk.Button(out_frm, text='Browse', command=browse_out).pack(side='left', padx=(4, 0))
    create_context_menu(entry_out)

    # 按鈕
    result = {'ok': False, 'closed': False}

    def on_start():
        result['ok'] = True
        root.destroy()

    def on_cancel():
        result['closed'] = True
        root.destroy()

    root.protocol('WM_DELETE_WINDOW', on_cancel)

    btn_frm = tk.Frame(root)
    btn_frm.pack(pady=12)
    
    # 添加分辨率過濾 checkbox
    filter_var = tk.BooleanVar(value=True)  # 預設打勾
    tk.Checkbutton(btn_frm, text='過濾低分辨率 (<1920寬)', variable=filter_var).pack(side='left', padx=(0, 8))
    
    tk.Button(btn_frm, text='Start', command=on_start, width=12).pack(side='left', padx=8)
    tk.Button(btn_frm, text='Cancel', command=on_cancel, width=12).pack(side='left')

    # Windows 下有些終端/滑鼠環境會誤觸發 SIGINT，導致 GUI 被 KeyboardInterrupt 中斷
    old_sigint_handler = None
    try:
        if os.name == 'nt':
            old_sigint_handler = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
    except Exception:
        old_sigint_handler = None

    try:
        root.mainloop()
    finally:
        try:
            if os.name == 'nt' and old_sigint_handler is not None:
                signal.signal(signal.SIGINT, old_sigint_handler)
        except Exception:
            pass

    if result.get('ok'):
        val = url_var.get().strip()
        flv_idx_str = flv_var.get().strip()
        out = out_var.get().strip()
        start_ep_str = start_ep_var.get().strip()
        filter_resolution = filter_var.get()
        try:
            flv_idx = max(1, int(flv_idx_str))
        except ValueError:
            flv_idx = 1
        # start_ep_str 直接保留為字符串，在後面解析
        return (val if val else None, flv_idx, out if out else None, start_ep_str if start_ep_str else '1', filter_resolution)
    return (None, 1, None, '1', True)


def sniff_m3u8(page, episode_el, wait_seconds: float = 1.5, max_retries: int = 2, exclude_urls: set = None) -> List[str]:
    """快速嗅探 M3U8 URL - 支持重試與去重"""
    exclude_urls = exclude_urls or set()
    for attempt in range(max_retries):
        collected = []
        handler_registered = [False]

        def on_request(req):
            try:
                url = req.url
                if '.m3u8' in url and url not in collected:
                    collected.append(url)
            except Exception:
                pass

        # 在點擊前就註冊監聽器（避免遺漏）
        page.on('request', on_request)
        handler_registered[0] = True

        try:
            # 立即點擊集數按鈕
            try:
                try:
                    page.evaluate('(el) => el.scrollIntoView({block: "center"})', episode_el)
                except Exception:
                    pass
                episode_el.click()
            except Exception:
                try:
                    page.evaluate('(el) => el.click()', episode_el)
                except:
                    pass

            # 快速等待 M3U8 URL（通常會立即返回）
            start = time.time()
            current_wait = wait_seconds + (attempt * 0.8)
            while time.time() - start < current_wait:
                if collected:
                    # 及時返回，不浪費時間
                    break
                time.sleep(0.01)  # 更頻繁地檢查
            
        except Exception:
            pass
        finally:
            # 移除監聽器
            if handler_registered[0]:
                try:
                    page.remove_listener('request', on_request)
                except:
                    pass
        
        # 優先返回未出現過的新 URL
        if collected:
            new_urls = [u for u in collected if u not in exclude_urls]
            if new_urls:
                return new_urls
        
        # 如果沒找到且還有重試機會，等待後重試
        if attempt < max_retries - 1:
            time.sleep(0.2)
    
    return []


def run_downloader(url: str, out_dir: str, save_name: str, tmp_root: str) -> str:
    """執行下載器，返回 tmp_dir 路徑或 None"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    downloader = os.path.join(script_dir, 'exe', 'N_m3u8DL-RE.exe')

    if not os.path.exists(downloader):
        safe_print(f'  ❌ 下載器不存在')
        return None

    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    tmp_dir = os.path.join(tmp_root, f"nm3_tmp_{save_name}_{timestamp}")
    os.makedirs(tmp_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    cmd = [downloader, url, '--save-dir', out_dir, '--save-name', save_name,
           '--skip-merge', '--tmp-dir', tmp_dir, '--no-log']

    try:
        proc = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, encoding='utf-8', errors='replace', timeout=600)
        if proc.returncode == 0:
            return tmp_dir
        else:
            safe_print(f'  ❌ 下載器返回錯誤代碼 {proc.returncode}')
            if proc.stdout:
                safe_print(f'  輸出: {proc.stdout.strip()}')
            return None
    except Exception as e:
        safe_print(f'  ❌ 執行失敗: {e}')
        return None


def merge_ts_to_mp4(tmp_dir: str, out_mp4: str, ffmpeg_path: str = None, clean: bool = True) -> bool:
    """合併 TS 為 MP4"""
    if not ffmpeg_path:
        ffmpeg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'exe', 'ffmpeg.exe')

    if not os.path.exists(ffmpeg_path):
        return False

    try:
        # 尋找 raw.m3u8 或 index.m3u8
        raw_m3u8 = None
        for cand in ('raw.m3u8', 'index.m3u8'):
            p = os.path.join(tmp_dir, cand)
            if os.path.exists(p):
                raw_m3u8 = p
                break

        if raw_m3u8:
            cmd = [ffmpeg_path, '-allowed_extensions', 'ALL', '-i', raw_m3u8, '-c', 'copy',
                   '-bsf:a', 'aac_adtstoasc', '-y', out_mp4]
            proc = subprocess.run(cmd, cwd=tmp_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, encoding='utf-8', errors='replace', timeout=300)
            ok = proc.returncode == 0
        else:
            # 掃描 .ts 檔建立 concat.txt
            seg_dir = os.path.join(tmp_dir, '0____')
            if os.path.isdir(seg_dir):
                ts_files = sorted([f for f in os.listdir(seg_dir) if f.endswith('.ts')])
                if not ts_files:
                    return False
                concat_path = os.path.join(tmp_dir, 'concat.txt')
                with open(concat_path, 'w', encoding='ascii') as f:
                    for t in ts_files:
                        f.write(f"file '0____/{t}'\n")
                cmd = [ffmpeg_path, '-f', 'concat', '-safe', '0', '-i', 'concat.txt', '-c', 'copy',
                       '-bsf:a', 'aac_adtstoasc', '-y', out_mp4]
            else:
                segs = sorted([os.path.join(dp, f) for dp, dn, filenames in os.walk(tmp_dir)
                             for f in filenames if f.endswith('.ts')])
                if not segs:
                    return False
                concat_path = os.path.join(tmp_dir, 'concat.txt')
                with open(concat_path, 'w', encoding='utf-8') as f:
                    for s in segs:
                        f.write(f"file '{s.replace(chr(92), '/')}'\n")
                cmd = [ffmpeg_path, '-f', 'concat', '-safe', '0', '-i', concat_path, '-c', 'copy',
                       '-bsf:a', 'aac_adtstoasc', '-y', out_mp4]

            proc = subprocess.run(cmd, cwd=tmp_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, encoding='utf-8', errors='replace', timeout=300)
            ok = proc.returncode == 0

        if ok and clean:
            try:
                import shutil
                shutil.rmtree(tmp_dir)
            except:
                pass
        return ok
    except Exception:
        return False


def check_video_resolution(mp4_path: str, ffprobe_path: str = None, max_retries: int = 3) -> dict:
    """檢查視頻分辨率和信息，支持重試"""
    
    # 等待文件完全寫入（最多等3秒）
    for i in range(10):
        if os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 1000:  # 至少 1KB
            break
        time.sleep(0.3)
    
    if not os.path.exists(mp4_path):
        return {'resolution': 'Unknown', 'width': 0, 'height': 0}
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 優先嘗試 ffprobe（更準確）
    if not ffprobe_path:
        ffprobe_path = os.path.join(script_dir, 'exe', 'ffprobe.exe')
    
    if os.path.exists(ffprobe_path):
        try:
            proc = subprocess.run(
                [ffprobe_path, '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'stream=width,height', '-of', 'csv=p=0', mp4_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, encoding='utf-8', errors='replace', timeout=10
            )
            
            if proc.returncode == 0 and proc.stdout.strip():
                parts = proc.stdout.strip().split(',')
                if len(parts) >= 2:
                    try:
                        width = int(parts[0])
                        height = int(parts[1])
                        if width > 0 and height > 0:
                            return {
                                'resolution': f'{width}x{height}',
                                'width': width,
                                'height': height
                            }
                    except ValueError:
                        pass
        except Exception:
            pass
    
    # 後備方案：用 ffmpeg -i
    ffmpeg_path = os.path.join(script_dir, 'exe', 'ffmpeg.exe')
    if not os.path.exists(ffmpeg_path):
        return {'resolution': 'Unknown', 'width': 0, 'height': 0}
    
    for attempt in range(max_retries):
        try:
            proc = subprocess.run(
                [ffmpeg_path, '-i', mp4_path],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, encoding='utf-8', errors='replace', timeout=10
            )
            output = proc.stdout
            
            # 尋找 Video: 行
            for line in output.split('\n'):
                if 'Video:' in line:
                    match = re.search(r'(\d{3,4})x(\d{3,4})', line)
                    if match:
                        width = int(match.group(1))
                        height = int(match.group(2))
                        if width > 100 and height > 100:  # 基本合理檢查
                            return {
                                'resolution': f'{width}x{height}',
                                'width': width,
                                'height': height
                            }
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(0.5)  # 重試前等待
            continue
    
    return {'resolution': 'Unknown', 'width': 0, 'height': 0}


def main():
    # 設置繁體中文 locale
    try:
        locale.setlocale(locale.LC_ALL, 'zh_TW.UTF-8')
    except locale.Error:
        try:
            # Windows 備用
            locale.setlocale(locale.LC_ALL, 'Chinese (Traditional)_Taiwan.950')
        except locale.Error:
            pass  # 使用默認 locale
    
    args = parse_args()

    # 取得參數
    if not args.no_ui and not args.url:
        url, flv_idx, out_dir, start_ep, filter_resolution = show_start_ui()
        if not url:
            # GUI 關閉或異常時，回退到終端輸入，避免直接退出
            try:
                safe_print('GUI 未取得 URL，改用終端輸入模式。')
                manual_url = input('請輸入目標 URL（留空退出）: ').strip()
            except Exception:
                manual_url = ''

            if not manual_url:
                safe_print('未提供 URL，程式退出。')
                return

            args.url = manual_url
            args.flv_idx = flv_idx
            if out_dir:
                args.out_dir = out_dir
            args.start_ep = start_ep
            args.filter_resolution = filter_resolution
        else:
            args.url = url
            args.flv_idx = flv_idx
            if out_dir:
                args.out_dir = out_dir
            args.start_ep = start_ep
            args.filter_resolution = filter_resolution
    else:
        args.start_ep = args.start_ep or 1
        # 確保 filter_resolution 有預設值
        if not hasattr(args, 'filter_resolution'):
            args.filter_resolution = True

    out_dir = args.out_dir or os.path.abspath('.')
    os.makedirs(out_dir, exist_ok=True)
    tmp_root = os.path.join(out_dir, 'nm3_tmp')

    safe_print('=' * 60)
    safe_print(f'URL: {args.url}')
    safe_print(f'FLV 來源: {args.flv_idx}')
    safe_print(f'開始集: {args.start_ep}')
    safe_print('=' * 60)

    try:
        # 設置 Playwright 瀏覽器路徑（支持 PyInstaller 打包）
        # PyInstaller 將資源放在 _internal 目錄下
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            # 打包的 EXE 環境
            base_path = sys._MEIPASS
        else:
            # 開發環境
            base_path = os.path.dirname(os.path.abspath(__file__))
        
        browsers_path = os.path.join(base_path, 'browsers')
        if os.path.exists(browsers_path):
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = browsers_path
        
        safe_print('\n[1/3] 初始化 Playwright...')
        safe_print('  ⏳ 正在啟動瀏覽器驅動...')
        playwright_instance = sync_playwright().start()

        safe_print('[2/3] 啟動瀏覽器和加載頁面...')
        safe_print('  ⏳ 正在啟動 Chromium...')
        browser = playwright_instance.chromium.launch(headless=True)
        page = browser.new_page()

        # 阻擋資源
        page.route('**/*', lambda route, request: route.abort() if request.resource_type in
                   ('image', 'stylesheet', 'font', 'media') else route.continue_())

        safe_print('  ⏳ 正在載入頁面...')
        page.goto(args.url, wait_until='domcontentloaded')
        safe_print('  ✓ 頁面載入完成')
        time.sleep(1)

        safe_print('[3/3] 分析 FLV 來源...')
        safe_print('  ⏳ 正在尋找容器...')
        
        # 獲取所有 FLV 容器
        containers = page.query_selector_all('.jujiepisodios')
        safe_print(f'  ✓ 發現 {len(containers)} 個容器')
        
        if len(containers) == 0:
            safe_print('  ⚠️  提示：未找到容器，頁面選擇器可能已改變')
            safe_print('  💡 請確認：')
            safe_print('     • URL 是否正確')
            safe_print('     • 頁面是否完全載入')
            safe_print('     • 瀏覽器視窗是否顯示')
        
        # 記錄每個容器的集數數量
        container_episodes = {}
        for i, cont in enumerate(containers):
            count = page.evaluate('(e) => e.querySelectorAll(":scope > a").length', cont)
            container_episodes[i] = count
        
        safe_print(f'  容器分佈: {sorted(set(container_episodes.values()))}')
        
        # 找到對應 FLV 索引的 FLV 按鈕並點擊
        flv_idx = args.flv_idx
        safe_print('  ⏳ 正在尋找 FLV 按鈕...')
        flv_buttons = page.locator('//a[contains(text(), "FLV")]').all()
        safe_print(f'  ✓ 找到 {len(flv_buttons)} 個 FLV 按鈕')
        
        flv_container_idx = None
        try:
            # FLV 按鈕的索引應該對應容器的索引
            if flv_idx - 1 < len(flv_buttons):
                flv_button = flv_buttons[flv_idx - 1]
                flv_text = flv_button.inner_text().strip()
                safe_print(f'  FLV {flv_idx}: {flv_text}')
                
                # 點擊選擇這個 FLV
                flv_button.click()
                time.sleep(2)
                
                # FLV 按鈕索引對應容器索引
                flv_container_idx = flv_idx - 1
                if flv_container_idx in container_episodes:
                    ep_count = container_episodes[flv_container_idx]
                    safe_print(f'  ✓ FLV {flv_idx} 對應容器 [{flv_container_idx}]，有 {ep_count} 個集數\n')
        except Exception as e:
            safe_print(f'  ⚠ 無法處理 FLV {flv_idx}: {e}')
        
        # 使用對應的容器
        if flv_container_idx is not None and flv_container_idx < len(containers):
            container = containers[flv_container_idx]
        else:
            container = page.query_selector('.jujiepisodios')
            
        if not container:
            safe_print('❌ 找不到集數容器')
            browser.close()
            playwright_instance.stop()
            return

        episode_elements = container.query_selector_all('a')
        safe_print(f'✓ 獲取 {len(episode_elements)} 個集數按鈕\n')

        # 取得標題
        try:
            raw_title = page.title() or ''
            show_name = raw_title.split(' - ')[0].strip() if raw_title else 'Unknown'
            show_name = re.sub(r'[\\/:*?"<>|]', '_', show_name).strip()
        except:
            show_name = 'Unknown'

        # 提取集數資訊（文本、季號、集號）
        safe_print('分析集數資訊...')
        episode_info = []  # 列表存 (index, ep_text, season, episode, suffix)
        ep_text_count = {}  # 用於統計重複集數
        
        for idx, el in enumerate(episode_elements):
            try:
                ep_text = page.evaluate('(e) => e.innerText', el).strip()
                ep_text_count[ep_text] = ep_text_count.get(ep_text, 0) + 1
                episode_info.append((idx, ep_text, None, None, None))
            except:
                pass
        
        safe_print(f'共 {len(episode_info)} 個集數，其中重複: {[k for k,v in ep_text_count.items() if v > 1]}')
        
        # 處理集數編碼
        special_ep_counter = 0  # 用於 S000 的計數
        repeat_counters = {}    # 用於統計重複集數
        
        for i, (idx, ep_text, _, _, _) in enumerate(episode_info):
            # 判斷是否是數字集數
            if ep_text.isdigit():
                ep_num = int(ep_text)
                season = 1
                
                # 檢查重複
                if ep_text_count[ep_text] > 1:
                    repeat_counters[ep_text] = repeat_counters.get(ep_text, 0) + 1
                    suffix = f'-{repeat_counters[ep_text]}' if repeat_counters[ep_text] > 1 else ''
                else:
                    suffix = ''
                
                episode_info[i] = (idx, ep_text, season, ep_num, suffix)
            else:
                # 特殊集數（總篇上、總篇下等）
                season = 0
                special_ep_counter += 1
                episode_info[i] = (idx, ep_text, season, special_ep_counter, '')

        # 流水線處理：邊掃描邊下載邊合併邊檢查
        safe_print(f'\n========== 流水線處理 (邊掃描邊下載邊合併) ==========\n')
        seen_m3u8_urls = set()
        
        # 流水線隊列和狀態跟蹤
        task_queue = queue.Queue()
        results_lock = threading.Lock()
        
        # 為每個集數跟蹤詳細狀態
        episodes_status = {}  # {episode_num: {'status': '...', 'resolution': '', 'error': ''}}
        max_episode_num = len(episode_info)  # 獲取最大集數
        
        def update_status(ep_num: int, status_text: str):
            """更新並打印集數狀態"""
            with results_lock:
                if ep_num not in episodes_status:
                    episodes_status[ep_num] = {'status': '', 'resolution': '', 'error': ''}
                
                episodes_status[ep_num]['status'] = status_text
                safe_print(f'[E{ep_num:03d}/{max_episode_num}] {status_text}', flush=True)
        
        # 消費者線程：處理下載→合併→檢查
        def worker(worker_id: int):
            while True:
                try:
                    task = task_queue.get(timeout=2)
                except queue.Empty:
                    continue
                
                if task is None:  # 哨兵值，表示結束
                    break
                
                episode_num, m3u8_url, save_name = task
                
                try:
                    # 下載
                    update_status(episode_num, '掃描完成...下載中')
                    tmp_dir = run_downloader(m3u8_url, out_dir, save_name, tmp_root)
                    
                    if not tmp_dir:
                        update_status(episode_num, '掃描完成...✗ 下載失敗')
                        with results_lock:
                            episodes_status[episode_num]['error'] = '下載失敗'
                        continue
                    
                    update_status(episode_num, '掃描完成...下載完成...合併中')
                    
                    # 合併
                    out_mp4 = os.path.join(out_dir, f'{save_name}.mp4')
                    if not merge_ts_to_mp4(tmp_dir, out_mp4):
                        update_status(episode_num, '掃描完成...下載完成...✗ 合併失敗')
                        with results_lock:
                            episodes_status[episode_num]['error'] = '合併失敗'
                        continue
                    
                    update_status(episode_num, '掃描完成...下載完成...合併完成...檢查中')
                    
                    # 檢查分辨率
                    res_info = check_video_resolution(out_mp4)
                    resolution = res_info.get('resolution', 'Unknown')
                    width = res_info.get('width', 0)
                    height = res_info.get('height', 0)
                    
                    update_status(episode_num, f'掃描完成...下載完成...合併完成...✓ {resolution}')
                    with results_lock:
                        episodes_status[episode_num]['resolution'] = resolution
                        episodes_status[episode_num]['width'] = width
                        episodes_status[episode_num]['height'] = height
                        episodes_status[episode_num]['save_name'] = save_name
                        
                except Exception as e:
                    error_msg = str(e)[:30]
                    update_status(episode_num, f'掃描完成...下載完成...✗ {error_msg}')
                    with results_lock:
                        episodes_status[episode_num]['error'] = error_msg
                finally:
                    task_queue.task_done()
        
        # 啟動消費者線程
        num_workers = args.max_downloads
        workers = []
        for i in range(num_workers):
            t = threading.Thread(target=worker, args=(i,), daemon=False)
            t.start()
            workers.append(t)
        
        # 解析集數選擇
        selected_episodes = parse_episode_selection(args.start_ep, len(episode_info))
        
        # 檢查是否輸入的集數超出範圍
        max_episode = len(episode_info)
        invalid_episodes = [ep for ep in selected_episodes if ep > max_episode]
        
        if invalid_episodes:
            safe_print('\n' + '=' * 70)
            safe_print(f'❌ 錯誤：輸入的集數超出範圍')
            safe_print(f'   找到的集數: 1-{max_episode}')
            safe_print(f'   無效的集數: {sorted(invalid_episodes)}')
            safe_print('=' * 70)
            
            # 清理：終止消費者線程
            for _ in range(num_workers):
                task_queue.put(None)
            for t in workers:
                t.join(timeout=1)
            
            browser.close()
            playwright_instance.stop()
            
            raise ValueError(f'集數 {min(invalid_episodes)} 超出最大集數 {max_episode}')
        
        # 生產者（主線程）：邊掃描邊提交任務
        total_episodes = len(selected_episodes)
        scanned_count = 0
        
        safe_print(f'\n========== 流水線處理 (掃描 {total_episodes} 集) ==========\n')
        
        for ep_idx, (el_idx, ep_text, season, episode, suffix) in enumerate(episode_info):
            # 檢查是否在選擇的集數範圍內
            if season == 1 and episode not in selected_episodes:
                continue
            if season == 0 and episode not in selected_episodes:
                continue
            
            # 生成集數顯示名稱和保存名稱
            if season == 0:
                save_name = f'{show_name}.S000.E{episode:03d}'
            else:
                save_name = f'{show_name}.S{season:03d}.E{episode:03d}{suffix}'
            
            scanned_count += 1
            safe_print(f'⏳ 掃描進度: [{scanned_count}/{total_episodes}] E{episode:03d}...', end='', flush=True)
            update_status(episode, '掃描中...')
            
            try:
                # 重新查詢集數元素
                try:
                    if flv_container_idx is not None and flv_container_idx < len(containers):
                        container = containers[flv_container_idx]
                    else:
                        container = page.query_selector('.jujiepisodios')
                    
                    if not container:
                        update_status(episode, '✗ 容器消失')
                        break
                    
                    current_episodes = container.query_selector_all('a')
                    if el_idx >= len(current_episodes):
                        update_status(episode, '✗ 索引越界')
                        continue

                    el = current_episodes[el_idx]

                except Exception as e:
                    update_status(episode, '✗ 掃描異常')
                    continue

                # 快速掃描 M3U8
                try:
                    m3u8_list = sniff_m3u8(page, el, wait_seconds=1.0, max_retries=3, exclude_urls=seen_m3u8_urls)
                    if m3u8_list:
                        url_m3u8 = m3u8_list[-1]
                        if url_m3u8 in seen_m3u8_urls:
                            # 再嘗試一次，避免抓到上一集的 URL
                            retry_list = sniff_m3u8(page, el, wait_seconds=2.0, max_retries=2, exclude_urls=seen_m3u8_urls)
                            if retry_list:
                                url_m3u8 = retry_list[-1]

                        if url_m3u8 in seen_m3u8_urls:
                            print()  # 新行
                            update_status(episode, '✗ URL 重複（已跳過）')
                            with results_lock:
                                episodes_status[episode]['error'] = 'URL 重複，疑似嗅探失敗'
                            continue

                        seen_m3u8_urls.add(url_m3u8)
                        print()  # 新行，分隔掃描進度和狀態輸出
                        update_status(episode, '掃描完成...排隊中')
                        # 立即提交到隊列，讓消費者開始處理
                        task_queue.put((episode, url_m3u8, save_name))
                    else:
                        print()  # 新行
                        update_status(episode, '✗ 掃描失敗')
                except Exception as e:
                    update_status(episode, '✗ 掃描異常')
                    
            except Exception as e:
                update_status(episode, '✗ 異常')
        
        # 等待所有任務完成
        task_queue.join()
        
        # 發送哨兵值終止消費者線程
        for _ in range(num_workers):
            task_queue.put(None)
        
        # 等待所有消費者線程結束
        for t in workers:
            t.join(timeout=5)

        browser.close()
        playwright_instance.stop()

        # 等待所有狀態更新完成
        time.sleep(1)
        
        # 結果統計和報告（只顯示解析度）
        safe_print('\n' + '=' * 70)
        success_count = sum(1 for status in episodes_status.values() if status.get('resolution'))
        total_count = len(episodes_status)
        safe_print(f'完成: {success_count}/{total_count} 集')
        safe_print('=' * 70)
        
        # 生成報告內容（同時寫入文件）
        report_lines = []
        report_lines.append('=' * 70)
        report_lines.append(f'完成: {success_count}/{total_count} 集')
        report_lines.append('=' * 70)
        
        # 詳細報告（按集數排序）
        if episodes_status:
            safe_print('\n【解析度報告】')
            report_lines.append('')
            report_lines.append('【解析度報告】')
            safe_print('-' * 70)
            report_lines.append('-' * 70)
            safe_print(f'{'集數':<15} {'解析度':<20}')
            report_lines.append(f'{'集數':<15} {'解析度':<20}')
            safe_print('-' * 70)
            report_lines.append('-' * 70)
            
            # 收集需要重新下載的集數（低分辨率 + 下載/掃描/合成失敗）
            need_redownload_eps = []
            
            for ep_num in sorted(episodes_status.keys()):
                status_info = episodes_status[ep_num]
                resolution = status_info.get('resolution', '-')
                if not resolution:
                    error = status_info.get('error', '未知錯誤')
                    resolution = f'✗ {error}'
                    # 失敗的集數也加入需要重新下載的列表
                    need_redownload_eps.append(ep_num)
                else:
                    # 檢查寬度是否 < 1920（只有在啟用過濾時才加入重新下載列表）
                    if getattr(args, 'filter_resolution', True):
                        try:
                            width = status_info.get('width', 0)
                            if width > 0 and width < 1920:
                                need_redownload_eps.append(ep_num)
                        except:
                            pass
                
                line = f'E{ep_num:03d}           {resolution}'
                safe_print(line)
                report_lines.append(line)
            
            safe_print('-' * 70)
            report_lines.append('-' * 70)
            
            # 添加需要重新下載的集數列表
            if need_redownload_eps:
                if getattr(args, 'filter_resolution', True):
                    safe_print('\n【需要重新下載的集數】（失敗 + 寬度 < 1920）')
                    report_lines.append('')
                    report_lines.append('【需要重新下載的集數】（失敗 + 寬度 < 1920）')
                else:
                    safe_print('\n【需要重新下載的集數】（失敗的集數）')
                    report_lines.append('')
                    report_lines.append('【需要重新下載的集數】（失敗的集數）')
                redownload_formatted = format_episode_ranges(need_redownload_eps)
                safe_print(redownload_formatted)
                report_lines.append(redownload_formatted)
        
        # 寫入文件
        safe_print('\n正在產生報告檔案...')
        try:
            report_path = os.path.join(out_dir, '重新下載.txt')
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(report_lines))
            safe_print(f'✓ 報告已保存: {report_path}')
        except Exception as e:
            safe_print(f'⚠️  無法保存報告: {e}')
        
        # 刪除寬度 < 1920 的視頻文件（如果啟用過濾）
        if getattr(args, 'filter_resolution', True):
            safe_print('\n清理低分辨率視頻...')
            deleted_count = 0
            try:
                for ep_num in sorted(episodes_status.keys()):
                    status_info = episodes_status[ep_num]
                    width = status_info.get('width', 0)
                    resolution = status_info.get('resolution', '')
                    save_name = status_info.get('save_name', '')
                    
                    # 調試信息
                    safe_print(f'  [檢查] E{ep_num:03d}: width={width}, save_name={save_name}', flush=True)
                    
                    # 只刪除成功下載但寬度 < 1920 的視頻
                    if width > 0 and width < 1920 and save_name:
                        mp4_file = os.path.join(out_dir, f'{save_name}.mp4')
                        if os.path.exists(mp4_file):
                            try:
                                os.remove(mp4_file)
                                safe_print(f'  ✓ 已刪除: {save_name}.mp4')
                                deleted_count += 1
                            except Exception as e:
                                safe_print(f'  ⚠️  無法刪除 {save_name}.mp4: {e}')
                        else:
                            safe_print(f'  ⚠️  找不到: {save_name}.mp4')
                
                safe_print(f'\n✓ 已刪除 {deleted_count} 個低分辨率視頻')
            except Exception as e:
                safe_print(f'⚠️  清理視頻時出錯: {e}')
        else:
            safe_print('\n跳過低分辨率視頻清理（過濾已禁用）')
        
        # 清理臨時文件夾
        safe_print('\n清理臨時文件...')
        try:
            if os.path.exists(tmp_root):
                import shutil
                shutil.rmtree(tmp_root)
                safe_print('✓ 臨時文件夾已刪除')
        except Exception as e:
            safe_print(f'⚠️  無法刪除臨時文件夾: {e}')
        
        # 最後顯示需要重新下載的資訊
        if 'need_redownload_eps' in locals() and need_redownload_eps:
            safe_print('\n' + '=' * 70)
            safe_print('【需要重新下載的集數】（失敗 + 寬度 < 1920）')
            safe_print(format_episode_ranges(need_redownload_eps))
            safe_print('=' * 70)

    except Exception as e:
        safe_print(f'❌ 錯誤: {e}')
        import traceback
        traceback.print_exc()
        
        # 即使出錯也嘗試清理
        try:
            if 'tmp_root' in locals() and os.path.exists(tmp_root):
                import shutil
                shutil.rmtree(tmp_root)
        except:
            pass
        
        # 清理 N_m3u8DL-RE 生成的 logs 資料夾
        try:
            if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
                logs_dir = os.path.join(sys._MEIPASS, 'exe', 'logs')
                if os.path.exists(logs_dir):
                    import shutil
                    shutil.rmtree(logs_dir)
        except Exception as e:
            safe_print(f'清理 logs 資料夾失敗: {e}')


if __name__ == '__main__':
    cleaned_up = [False]

    def cleanup_runtime(verbose: bool = False):
        """清理臨時資源（可重入）"""
        if cleaned_up[0]:
            return
        cleaned_up[0] = True

        # 清理臨時文件
        try:
            import tempfile
            temp_dir = os.path.join(tempfile.gettempdir(), 'nm3_tmp')
            if os.path.exists(temp_dir):
                import shutil
                shutil.rmtree(temp_dir)
        except Exception:
            pass

        # 清理 N_m3u8DL-RE 生成的 logs 資料夾
        try:
            if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
                logs_dir = os.path.join(sys._MEIPASS, 'exe', 'logs')
                if os.path.exists(logs_dir):
                    import shutil
                    shutil.rmtree(logs_dir)
                    if verbose:
                        safe_print(f'已清理 logs 資料夾: {logs_dir}')
        except Exception as e:
            if verbose:
                safe_print(f'清理 logs 資料夾失敗: {e}')

    # 強制結束處理器
    def force_exit(signum=None, frame=None):
        """強制退出程式"""
        if signum is not None:
            safe_print(f'\n[終止] 程式被強制關閉 (signal={signum})')
        else:
            safe_print('\n[終止] 程式被強制關閉')
        cleanup_runtime(verbose=True)
        sys.exit(0)
    
    # 註冊信號處理
    # Windows 終端有時會誤觸發 SIGINT，導致 GUI 被強制關閉；
    # 因此在 Windows 不註冊 signal handler，改由 KeyboardInterrupt 正常處理。
    if os.name != 'nt':
        try:
            signal.signal(signal.SIGINT, force_exit)  # Ctrl+C
            signal.signal(signal.SIGTERM, force_exit)  # 終止信號
        except Exception:
            pass
    
    # 程式正常結束時只做清理，不觸發強制退出訊息/例外
    atexit.register(cleanup_runtime)
    
    try:
        main()
    except KeyboardInterrupt:
        safe_print('\n[中止] 用戶停止')
    finally:
        cleanup_runtime(verbose=False)
        
        try:
            # 開發環境不要阻塞終端；僅打包 EXE 時保留暫停
            if getattr(sys, 'frozen', False):
                input('\n按 Enter 結束...')
        except:
            pass



