from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.background import BackgroundTasks
from contextlib import asynccontextmanager
import requests
import uuid
import json
import time
from typing import Optional
import asyncio
import base64
import tempfile
import os
import re
import threading
import logging
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
import random

# ===================== 基础配置 =====================

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

global_data = {
    "cookie": None,
    "cookies": None,
    "last_update": 0,
    "cookie_expires": 0,
    "is_refreshing": False
}

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", None)
logger.info(f"OPENAI_API_KEY is set: {OPENAI_API_KEY is not None}")

# ===================== 工具函数：增量/去重 =====================

def _lcp_delta(prev: str, curr: str) -> str:
    """
    计算 curr 相比 prev 的新增部分：
    - 如果 curr 以 prev 为前缀，返回简单后缀；
    - 否则做一次最长公共前缀（LCP），兼容上游小幅回写。
    """
    if curr.startswith(prev):
        return curr[len(prev):]
    i = 0
    m = min(len(prev), len(curr))
    while i < m and prev[i] == curr[i]:
        i += 1
    return curr[i:]


def _suffix_prefix_overlap(a: str, b: str) -> int:
    """
    返回最大的 k，使得 a 的后缀 a[-k:] == b 的前缀 b[:k]。
    （保留以备需要；当前主要使用 _emit_from_cumulative）
    """
    max_k = min(len(a), len(b))
    for k in range(max_k, 0, -1):
        if a.endswith(b[:k]):
            return k
    return 0


def _novel_suffix(history: str, piece: str) -> str:
    """
    从 piece 中找出“在历史 history 中从未出现过”的最短后缀。
    若 piece 的所有后缀都已出现过，则返回空串（这帧无需发送）。
    O(n^2)，对常规对话长度足够；需要可换 KMP/后缀数组优化。
    """
    n = len(piece)
    for i in range(n):
        cand = piece[i:]
        if cand and cand not in history:
            return cand
    return ""


def _emit_from_cumulative(history: str, curr: str, last: str) -> str:
    """
    给定历史已发送文本 history、本帧累计文本 curr、上一帧累计文本 last，
    返回这帧应该增量发送的文本（保留 <think>）：
      1) 若 curr 中包含 history（取最后一次出现），仅发送其后的部分；
      2) 否则找 history 的“最长后缀”在 curr 中的匹配位置，发送其后的部分；
      3) 再不行，回退到 LCP(last, curr) 的差分；
      4) 若差分仍是历史已有内容，则取 curr 的“新颖后缀”（history 未出现过的最短后缀）。
    """
    if not curr:
        return ""
    if not history:
        # 首帧：没有历史，直接全发（包含 <think>）
        return curr

    # 1) 优先：history 在 curr 的最后一次出现（典型瀑布模式 curr = [旧段* + history + 新尾巴]）
    idx = curr.rfind(history)
    if idx != -1:
        return curr[idx + len(history):]

    # 2) 次优：history 的最长后缀在 curr 中的匹配
    max_k = min(len(history), len(curr))
    MIN_MATCH = 8  # 避免过短噪声匹配，可按需调整
    for k in range(max_k, MIN_MATCH - 1, -1):
        suf = history[-k:]
        pos = curr.find(suf)
        if pos != -1:
            return curr[pos + k:]

    # 3) 回退：LCP 与上一帧差分
    delta = _lcp_delta(last, curr)

    # 4) 若 delta 仍在历史中出现（说明这帧多半是旧段改写/插回），取新颖后缀
    if delta and delta in history:
        fresh = _novel_suffix(history, curr)
        return fresh

    return delta

# ===================== FastAPI 生命周期 =====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting FastAPI application, initializing cookie fetcher...")

    cookie_thread = threading.Thread(target=get_cookie_with_retry, daemon=True)
    cookie_thread.start()

    refresh_thread = threading.Thread(target=auto_refresh_cookie, daemon=True)
    refresh_thread.start()

    logger.info("Cookie fetcher and auto-refresh threads started")
    yield

    logger.info("Shutting down FastAPI application")
    global_data["cookie"] = None
    global_data["cookies"] = None
    global_data["last_update"] = 0
    global_data["is_refreshing"] = False


app = FastAPI(lifespan=lifespan)
security = HTTPBearer()

# ===================== 指纹 & Cookie =====================

def get_random_browser_fingerprint():
    chrome_versions = ["120", "121", "122", "123", "124", "125"]
    edge_versions = ["120", "121", "122", "123", "124", "125"]
    selected_version = random.choice(chrome_versions)
    edge_version = random.choice(edge_versions)

    os_versions = [
        "Windows NT 10.0; Win64; x64",
        "Macintosh; Intel Mac OS X 10_15_7",
        "Macintosh; Intel Mac OS X 11_0_1",
        "Macintosh; Intel Mac OS X 12_0_1"
    ]
    selected_os = random.choice(os_versions)

    languages = [
        "en-US,en;q=0.9",
        "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "en-GB,en;q=0.9,en-US;q=0.8"
    ]
    selected_language = random.choice(languages)

    viewport_sizes = [
        (1920, 1080),
        (1366, 768),
        (1440, 900),
        (1536, 864),
        (1680, 1050)
    ]
    selected_viewport = random.choice(viewport_sizes)

    user_agent = (
        f"Mozilla/5.0 ({selected_os}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{selected_version}.0.0.0 Safari/537.36 Edg/{edge_version}.0.0.0"
    )

    headers = {
        "accept": "*/*",
        "accept-language": selected_language,
        "content-type": "application/json",
        "origin": "https://chat.akash.network",
        "referer": "https://chat.akash.network/",
        "sec-ch-ua": f'"Microsoft Edge";v="{edge_version}", "Not-A.Brand";v="8", "Chromium";v="{selected_version}"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": user_agent
    }

    return {"headers": headers, "viewport": selected_viewport, "user_agent": user_agent}


def get_cookie_with_retry(max_retries=3, retry_delay=5):
    retries = 0
    while retries < max_retries:
        logger.info(f"Cookie fetching attempt {retries + 1}/{max_retries}")
        cookie = get_cookie()
        if cookie:
            logger.info("Successfully retrieved cookie")
            return cookie
        retries += 1
        if retries < max_retries:
            logger.info(f"Retrying cookie fetch in {retry_delay} seconds...")
            time.sleep(retry_delay)
    logger.error(f"Failed to fetch cookie after {max_retries} attempts")
    return None


def get_cookie():
    browser = None
    context = None
    page = None
    try:
        logger.info("Starting cookie retrieval process...")
        fingerprint = get_random_browser_fingerprint()
        logger.info(f"Using browser fingerprint: {fingerprint['user_agent']}")

        with sync_playwright() as p:
            try:
                logger.info("Launching browser...")
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        '--no-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-gpu',
                        '--disable-software-rasterizer',
                        '--disable-extensions',
                        '--disable-setuid-sandbox',
                        '--no-first-run',
                        '--no-zygote',
                        '--single-process',
                        f'--window-size={fingerprint["viewport"][0]},{fingerprint["viewport"][1]}',
                        '--disable-blink-features=AutomationControlled',
                        '--disable-features=IsolateOrigins,site-per-process'
                    ]
                )
                logger.info("Browser launched successfully")

                logger.info("Creating browser context...")
                context = browser.new_context(
                    viewport={'width': fingerprint["viewport"][0], 'height': fingerprint["viewport"][1]},
                    user_agent=fingerprint["user_agent"],
                    locale='en-US',
                    timezone_id='America/New_York',
                    permissions=['geolocation'],
                    extra_http_headers=fingerprint["headers"]
                )

                context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => false });
                    Object.defineProperty(navigator, 'plugins',  { get: () => [1, 2, 3, 4, 5] });
                """)

                page = context.new_page()
                page.set_default_timeout(60000)

                max_retries = 3
                retry_delay = 5
                for attempt in range(max_retries):
                    try:
                        page.goto("https://chat.akash.network/", timeout=50000)
                        break
                    except Exception as e:
                        if attempt == max_retries - 1:
                            raise
                        logger.warning(f"Navigation attempt {attempt + 1} failed: {e}")
                        time.sleep(retry_delay)

                try:
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                    time.sleep(5)
                    try:
                        page.mouse.move(100, 100)
                        page.mouse.click(100, 100)
                        page.mouse.wheel(0, 100)
                        time.sleep(0.5)
                        page.mouse.wheel(0, -50)
                    except Exception as e:
                        logger.warning(f"Failed to simulate user interaction: {e}")
                    time.sleep(5)
                except Exception as e:
                    logger.warning(f"Timeout waiting for load state: {e}")

                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

                cookies = context.cookies()
                if not cookies:
                    logger.error("No cookies found")
                    return None

                cf_cookie = next((c for c in cookies if c['name'] == 'cf_clearance'), None)
                if not cf_cookie:
                    logger.error("cf_clearance cookie not found")
                    return None

                session_cookie = next((c for c in cookies if c['name'] == 'session_token'), None)

                cookie_str = '; '.join([f"{c['name']}={c['value']}" for c in cookies])

                global_data["cookie"] = cookie_str
                global_data["cookies"] = cookies
                global_data["last_update"] = time.time()

                if session_cookie and 'expires' in session_cookie and session_cookie['expires'] > 0:
                    global_data["cookie_expires"] = session_cookie['expires']
                else:
                    global_data["cookie_expires"] = time.time() + 1800

                return cookie_str

            finally:
                try:
                    if page: page.close()
                except Exception: pass
                try:
                    if context: context.close()
                except Exception: pass
                try:
                    if browser: browser.close()
                except Exception: pass
                import gc; gc.collect()

    except Exception as e:
        logger.error(f"Error fetching cookie: {e}")
        import traceback; logger.error(traceback.format_exc())

    try:
        if page: page.close()
    except Exception: pass
    try:
        if context: context.close()
    except Exception: pass
    try:
        if browser: browser.close()
    except Exception: pass
    import gc; gc.collect()
    return None

# ===================== Cookie 刷新/校验 =====================

async def refresh_cookie():
    logger.info("Refreshing cookie due to 401 error")
    if global_data["is_refreshing"]:
        for _ in range(10):
            await asyncio.sleep(1)
            if not global_data["is_refreshing"]:
                break
    if global_data["is_refreshing"]:
        global_data["is_refreshing"] = False
    try:
        global_data["is_refreshing"] = True
        global_data["cookie_expires"] = 0
        executor = ThreadPoolExecutor(max_workers=1)
        loop = asyncio.get_event_loop()
        new_cookie = await loop.run_in_executor(executor, get_cookie_with_retry)
        return new_cookie
    finally:
        global_data["is_refreshing"] = False


async def background_refresh_cookie():
    if global_data["is_refreshing"]:
        return
    try:
        global_data["is_refreshing"] = True
        executor = ThreadPoolExecutor(max_workers=1)
        loop = asyncio.get_event_loop()
        new_cookie = await loop.run_in_executor(executor, get_cookie)
        if new_cookie:
            global_data["cookie"] = new_cookie
            global_data["last_update"] = time.time()
            session_cookie = next((c for c in global_data["cookies"] if c['name'] == 'session_token'), None)
            if session_cookie and 'expires' in session_cookie and session_cookie['expires'] > 0:
                global_data["cookie_expires"] = session_cookie['expires']
            else:
                global_data["cookie_expires"] = time.time() + 1800
    except Exception as e:
        logger.error(f"Error in background cookie refresh: {e}")
    finally:
        global_data["is_refreshing"] = False


async def check_and_update_cookie():
    try:
        now = time.time()
        if not global_data["cookie"] or now >= global_data["cookie_expires"]:
            logger.info("Cookie expired or not available, starting refresh")
            try:
                loop = asyncio.get_event_loop()
                with ThreadPoolExecutor() as executor:
                    new_cookie = await loop.run_in_executor(executor, get_cookie)
                if not new_cookie:
                    logger.error("Cookie refresh failed")
            except Exception as e:
                logger.error(f"Error during cookie refresh: {e}")
        else:
            logger.info("Using existing cookie")
    except Exception as e:
        logger.error(f"Error in check_and_update_cookie: {e}")


async def get_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    if OPENAI_API_KEY is not None:
        clean_token = token.replace("Bearer ", "") if token.startswith("Bearer ") else token
        if clean_token != OPENAI_API_KEY:
            raise HTTPException(status_code=401, detail="Invalid API key")
    return True


async def validate_cookie(background_tasks: BackgroundTasks):
    await check_and_update_cookie()
    max_wait = 30
    start_time = time.time()
    while not global_data["cookie"] and time.time() - start_time < max_wait:
        await asyncio.sleep(1)
    if not global_data["cookie"]:
        raise HTTPException(status_code=503, detail="Service temporarily unavailable - Cookie not available")
    return global_data["cookie"]

# ===================== 图像状态与上传 =====================

async def check_image_status(session: requests.Session, full_job_id: str, short_job_id: str, headers: dict) -> Optional[str]:
    max_retries = 30
    for attempt in range(max_retries):
        try:
            resp = session.get(f'https://chat.akash.network/api/image-status?ids={full_job_id}', headers=headers)
            if resp.status_code == 404:
                if hasattr(check_image_status, '_consecutive_404s'):
                    check_image_status._consecutive_404s += 1
                else:
                    check_image_status._consecutive_404s = 1
                if check_image_status._consecutive_404s >= 3:
                    return None
                await asyncio.sleep(1)
                continue
            else:
                check_image_status._consecutive_404s = 0

            status_data = resp.json()
            if status_data and isinstance(status_data, list):
                job = status_data[0]
                status = job.get('status')
                if status in ["completed", "succeeded"]:
                    result = job.get("result")
                    if not result or (isinstance(result, str) and result.startswith("Failed")):
                        return None

                    # dataURL base64
                    if isinstance(result, str):
                        m = re.match(r"^data:image/[a-zA-Z0-9.+-]+;base64,(.+)$", result)
                        if m:
                            try:
                                img_bytes = base64.b64decode(m.group(1), validate=True)
                                url = await upload_to_xinyew(img_bytes, full_job_id)
                                if url:
                                    return url
                            except Exception:
                                return None

                    # 纯 base64
                    if isinstance(result, str):
                        try:
                            img_bytes = base64.b64decode(result, validate=True)
                            if img_bytes and len(img_bytes) > 100:
                                url = await upload_to_xinyew(img_bytes, full_job_id)
                                if url:
                                    return url
                        except Exception:
                            pass

                    # http(s) 绝对地址
                    if isinstance(result, str) and result.startswith("http"):
                        try:
                            r2 = session.get(result, headers=headers)
                            if r2.status_code == 200:
                                url = await upload_to_xinyew(r2.content, full_job_id)
                                if url:
                                    return url
                            return result
                        except Exception:
                            return result

                    # 相对地址
                    if isinstance(result, str) and result.startswith("/"):
                        real = f"https://chat.akash.network{result}"
                        r3 = session.get(real, headers=headers)
                        if r3.status_code == 200:
                            url = await upload_to_xinyew(r3.content, full_job_id)
                            if url:
                                return url
                        return None

                    # 回退：构造默认 URL
                    return f"https://chat.akash.network/api/image/job_{short_job_id}_00001_.webp"

                elif status == "failed":
                    return None

                await asyncio.sleep(1)
                continue

        except Exception:
            return None

    return None


async def upload_to_xinyew(image_data: bytes, job_id: str) -> Optional[str]:
    try:
        with tempfile.NamedTemporaryFile(suffix='.webp', delete=False) as tmp:
            tmp.write(image_data)
            temp_path = tmp.name
        try:
            files = {'file': (f"{job_id}.webp", open(temp_path, 'rb'), 'image/webp')}
            headers = {
                'User-Agent': 'Mozilla/5.0',
                'Accept': 'application/json, text/javascript, */*; q=0.01',
                'Origin': 'https://api.xinyew.cn',
                'Referer': 'https://api.xinyew.cn/',
                'X-Requested-With': 'XMLHttpRequest'
            }
            r = requests.post('https://api.xinyew.cn/api/jdtc', files=files, headers=headers, timeout=30)
            if r.status_code == 200:
                j = r.json()
                if j.get('errno') == 0 and j.get('data', {}).get('url'):
                    return j['data']['url']
            return None
        finally:
            try:
                os.unlink(temp_path)
            except Exception:
                pass
    except Exception:
        return None

# ===================== 健康检查 =====================

@app.get("/", response_class=HTMLResponse)
async def health_check():
    cookie_status = "ok" if global_data["cookie"] else "error"
    status_color = "green" if cookie_status == "ok" else "red"
    status_text = "正常" if cookie_status == "ok" else "异常"

    current_time = datetime.now(timezone(timedelta(hours=8)))

    if global_data["cookie_expires"]:
        expires_time = datetime.fromtimestamp(global_data["cookie_expires"], timezone(timedelta(hours=8)))
        expires_str = expires_time.strftime("%Y-%m-%d %H:%M:%S")
        time_left = global_data["cookie_expires"] - time.time()
        hours_left = int(time_left // 3600)
        minutes_left = int((time_left % 3600) // 60)
        time_left_str = f"{hours_left}小时{minutes_left}分钟" if hours_left > 0 else f"{minutes_left}分钟"
    else:
        expires_str = "未知"
        time_left_str = "未知"

    if global_data["last_update"]:
        last_update_time = datetime.fromtimestamp(global_data["last_update"], timezone(timedelta(hours=8)))
        last_update_str = last_update_time.strftime("%Y-%m-%d %H:%M:%S")
        since = time.time() - global_data["last_update"]
        update_ago = f"{int(since)}秒前" if since < 60 else (f"{int(since // 60)}分钟前" if since < 3600 else f"{int(since // 3600)}小时前")
    else:
        last_update_str = "从未更新"
        update_ago = "未知"

    status = {
        "status": "ok",
        "cookie_status": {
            "status": cookie_status,
            "status_text": status_text,
            "status_color": status_color,
            "expires": expires_str,
            "time_left": time_left_str,
            "available": bool(global_data["cookie"]),
            "last_update": last_update_str,
            "update_ago": update_ago
        }
    }

    return HTMLResponse(content=f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Akash API 服务状态</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <script>setTimeout(function(){{location.reload();}},30000);</script>
        <style>
            body {{font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial; background:#f5f5f5; padding:20px}}
            .container {{max-width:800px; margin:0 auto; background:#fff; border-radius:12px; padding:30px; box-shadow:0 4px 12px rgba(0,0,0,.1)}}
            .status {{display:flex; align-items:center; margin-bottom:20px}}
            .status-dot {{width:16px; height:16px; border-radius:50%; margin-right:12px}}
            .green {{background:#4CAF50}} .red{{background:#f44336}}
            .value {{font-weight:600; background:#f0f0f0; padding:4px 8px; border-radius:4px}}
            .footer {{margin-top:20px; color:#999; text-align:center}}
        </style>
    </head>
    <body>
    <div class="container">
        <h2>Akash API</h2>
        <div class="status">
            <div class="status-dot {status["cookie_status"]["status_color"]}"></div>
            <div>服务状态：<b>{status["cookie_status"]["status_text"]}</b></div>
        </div>
        <p>过期时间：<span class="value">{status["cookie_status"]["expires"]}</span></p>
        <p>剩余时间：<span class="value">{status["cookie_status"]["time_left"]}</span></p>
        <p>最后更新：<span class="value">{status["cookie_status"]["last_update"]}</span>（{status["cookie_status"]["update_ago"]}）</p>
        <div class="footer">当前时间：{current_time.strftime("%Y-%m-%d %H:%M:%S")} (北京时间)</div>
    </div>
    </body>
    </html>
    """)

# ===================== Chat Completions =====================

@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    background_tasks: BackgroundTasks,
    api_key: bool = Depends(get_api_key),
    cookie: str = Depends(validate_cookie)
):
    try:
        data = await request.json()

        fingerprint = get_random_browser_fingerprint()
        logger.info(f"Using browser fingerprint: {fingerprint['user_agent']}")

        chat_id = str(uuid.uuid4()).replace('-', '')[:16]
        system_message = data.get('system_message') or data.get('system', "You are a helpful assistant.")

        processed_messages = []
        for msg in data.get('messages', []):
            processed_messages.append({
                "role": msg.get("role"),
                "content": msg.get("content"),
                "parts": [{"type": "text", "text": msg.get("content")}]
            })

        akash_data = {
            "id": chat_id,
            "messages": processed_messages,
            "model": data.get('model', "DeepSeek-R1"),
            "system": system_message,
            "temperature": data.get('temperature', 0.85 if data.get('model') == 'AkashGen' else 0.6),
            "topP": data.get('top_p', 1.0 if data.get('model') == 'AkashGen' else 0.95),
            "context": []
        }

        cookie_start = cookie[:20]
        cookie_end = cookie[-20:] if len(cookie) > 40 else ""
        logger.info(f"Using cookie: {cookie_start}...{cookie_end}")

        session = requests.Session()
        try:
            session.headers.update(fingerprint["headers"])
            cookies_dict = {}
            for item in cookie.split(';'):
                if '=' in item:
                    name, value = item.strip().split('=', 1)
                    cookies_dict[name] = value

            response = session.post(
                'https://chat.akash.network/api/chat',
                json=akash_data,
                cookies=cookies_dict,
                stream=True
            )

            if response.status_code in [401, 403]:
                new_cookie = await refresh_cookie()
                if new_cookie:
                    new_cookies = {}
                    for item in new_cookie.split(';'):
                        if '=' in item:
                            name, value = item.strip().split('=', 1)
                            new_cookies[name] = value
                    response = session.post(
                        'https://chat.akash.network/api/chat',
                        json=akash_data,
                        cookies=new_cookies,
                        stream=True
                    )

            if response.status_code not in [200, 201]:
                try:
                    response.close()
                except Exception:
                    pass
                session.close()
                raise HTTPException(status_code=response.status_code, detail=f"Akash API error: {response.text}")

            # ----------- 关键：按累计文本对齐的增量算法（保留 <think>） -----------
            def generate():
                last_text = ""      # 上一帧的累计文本（包含 <think>）
                sent_total = ""     # 已实际发送给前端的总文本
                sent_role = False
                image_job_done = False

                try:
                    for line in response.iter_lines(decode_unicode=True):
                        if not line:
                            continue
                        try:
                            line_str = line if isinstance(line, str) else line.decode('utf-8', 'ignore')
                            if ':' not in line_str:
                                continue
                            msg_type, msg_data = line_str.split(':', 1)

                            if msg_type == '0':
                                if msg_data.startswith('"') and msg_data.endswith('"'):
                                    msg_data = msg_data[1:-1].replace('\\"', '"')
                                msg_data = msg_data.replace("\\n", "\n")

                                # 图生只触发一次
                                if data.get('model') == 'AkashGen' and "<image_generation>" in msg_data and not image_job_done:
                                    async def process_and_send():
                                        messages = await process_image_generation(msg_data, session, fingerprint["headers"], chat_id)
                                        if messages: return messages
                                        return None
                                    loop = asyncio.new_event_loop()
                                    asyncio.set_event_loop(loop)
                                    try:
                                        result_messages = loop.run_until_complete(process_and_send())
                                    finally:
                                        loop.close()
                                    image_job_done = True
                                    if result_messages:
                                        for m in result_messages:
                                            yield f"data: {json.dumps(m, ensure_ascii=False)}\n\n"
                                    continue

                                # ==== compute emit begin (使用累计对齐法) ====
                                emit = _emit_from_cumulative(sent_total, msg_data, last_text)
                                last_text = msg_data
                                if not emit:
                                    continue
                                # ==== compute emit end ====

                                if not sent_role:
                                    role_chunk = {
                                        "id": f"chatcmpl-{chat_id}",
                                        "object": "chat.completion.chunk",
                                        "created": int(time.time()),
                                        "model": data.get('model'),
                                        "choices": [{
                                            "delta": {"role": "assistant"},
                                            "index": 0,
                                            "finish_reason": None
                                        }]
                                    }
                                    yield f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\n"
                                    sent_role = True

                                chunk = {
                                    "id": f"chatcmpl-{chat_id}",
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": data.get('model'),
                                    "choices": [{
                                        "delta": {"content": emit},
                                        "index": 0,
                                        "finish_reason": None
                                    }]
                                }
                                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                                sent_total += emit

                            elif msg_type in ['e', 'd']:
                                end_chunk = {
                                    "id": f"chatcmpl-{chat_id}",
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": data.get('model'),
                                    "choices": [{
                                        "delta": {},
                                        "index": 0,
                                        "finish_reason": "stop"
                                    }]
                                }
                                yield f"data: {json.dumps(end_chunk, ensure_ascii=False)}\n\n"
                                yield "data: [DONE]\n\n"
                                break

                        except Exception as e:
                            print(f"Error processing line: {e}")
                            continue
                finally:
                    try:
                        response.close()
                    except Exception:
                        pass
                    try:
                        session.close()
                    except Exception:
                        pass

            return StreamingResponse(
                generate(),
                media_type='text/event-stream',
                headers={
                    'Cache-Control': 'no-cache',
                    'Connection': 'keep-alive',
                    'Content-Type': 'text/event-stream'
                }
            )

        except Exception:
            try:
                session.close()
            except Exception:
                pass
            raise

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return {"error": str(e)}

# ===================== Models 列表 =====================

@app.get("/v1/models")
async def list_models(
    background_tasks: BackgroundTasks,
    cookie: str = Depends(validate_cookie)
):
    try:
        fingerprint = get_random_browser_fingerprint()
        headers = fingerprint["headers"]
        session = requests.Session()
        try:
            session.headers.update(headers)
            cookies_dict = {}
            for item in cookie.split(';'):
                if '=' in item:
                    name, value = item.strip().split('=', 1)
                    cookies_dict[name] = value

            resp = session.get('https://chat.akash.network/api/models', cookies=cookies_dict)
            if resp.status_code in [401, 403]:
                new_cookie = await refresh_cookie()
                if new_cookie:
                    new_cookies = {}
                    for item in new_cookie.split(';'):
                        if '=' in item:
                            name, value = item.strip().split('=', 1)
                            new_cookies[name] = value
                    resp = session.get('https://chat.akash.network/api/models', cookies=new_cookies)

            if resp.status_code not in [200, 201]:
                return {"error": f"Authentication failed. Status: {resp.status_code}"}

            try:
                data = resp.json()
            except ValueError:
                return {"error": "Invalid response format"}

            models_list = data if isinstance(data, list) else data.get("models", [])

            openai_models = {
                "object": "list",
                "data": [
                    {
                        "id": m["id"] if isinstance(m, dict) else m,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "akash",
                        "permission": [{
                            "id": f"modelperm-{m['id'] if isinstance(m, dict) else m}",
                            "object": "model_permission",
                            "created": int(time.time()),
                            "allow_create_engine": False,
                            "allow_sampling": True,
                            "allow_logprobs": True,
                            "allow_search_indices": False,
                            "allow_view": True,
                            "allow_fine_tuning": False,
                            "organization": "*",
                            "group": None,
                            "is_blocking": False
                        }]
                    } for m in models_list
                ]
            }
            return openai_models
        finally:
            try:
                session.close()
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Error in list_models: {e}")
        return {"error": str(e)}

# ===================== 图像消息拼装 =====================

async def process_image_generation(msg_data: str, session: requests.Session, headers: dict, chat_id: str) -> Optional[list]:
    if "jobId='undefined'" in msg_data or "jobId=''" in msg_data:
        return create_error_messages(chat_id, "Akash官网服务异常，无法生成图片,请稍后再试。")

    match = re.search(r"jobId='([^']+)' prompt='([^']+)' negative='([^']*)'", msg_data)
    if not match:
        return create_error_messages(chat_id, "无法解析图片生成任务。请稍后再试。")

    job_id, prompt, negative = match.groups()
    if not job_id or job_id in ('undefined', 'null'):
        return create_error_messages(chat_id, "Akash服务异常，无法获取有效的任务ID。请稍后再试。")

    full_job_id = job_id
    short_job_id = job_id.replace('-', '')[:8] if '-' in job_id else job_id[:8]

    start_time = time.time()
    think_msg = "<think>\n"
    think_msg += "🎨 Generating image...\n\n"
    think_msg += f"Prompt: {prompt}\n"

    try:
        result = await check_image_status(session, full_job_id, short_job_id, headers)
        elapsed = time.time() - start_time
        think_msg += f"\n🤔 Thinking for {elapsed:.1f}s...\n"
        think_msg += "</think>"

        messages = []
        messages.append({
            "id": f"chatcmpl-{chat_id}-think",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "AkashGen",
            "choices": [{
                "delta": {"content": think_msg},
                "index": 0,
                "finish_reason": None
            }]
        })

        if result:
            image_msg = f"\n\n![Generated Image]({result})"
            messages.append({
                "id": f"chatcmpl-{chat_id}-image",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "AkashGen",
                "choices": [{
                    "delta": {"content": image_msg},
                    "index": 0,
                    "finish_reason": None
                }]
            })
        else:
            fail_msg = "\n\n*Image generation or upload failed.*"
            messages.append({
                "id": f"chatcmpl-{chat_id}-fail",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "AkashGen",
                "choices": [{
                    "delta": {"content": fail_msg},
                    "index": 0,
                    "finish_reason": None
                }]
            })
        return messages
    except Exception:
        return create_error_messages(chat_id, "图片生成过程中发生错误。请稍后再试。")


def create_error_messages(chat_id: str, error_message: str) -> list:
    return [{
        "id": f"chatcmpl-{chat_id}-error",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "AkashGen",
        "choices": [{
            "delta": {"content": f"\n\n**❌ {error_message}**"},
            "index": 0,
            "finish_reason": None
        }]
    }]

# ===================== 自动刷新线程 =====================

def auto_refresh_cookie():
    while True:
        try:
            now = time.time()
            if (not global_data["cookie"] or now >= global_data["cookie_expires"]) and not global_data["is_refreshing"]:
                try:
                    global_data["is_refreshing"] = True
                    new_cookie = get_cookie()
                    if new_cookie:
                        logger.info("Cookie refresh successful")
                    else:
                        logger.error("Cookie refresh failed, will retry later")
                except Exception as e:
                    logger.error(f"Error during cookie refresh: {e}")
                finally:
                    global_data["is_refreshing"] = False
                    import gc; gc.collect()
            time.sleep(60)
        except Exception:
            global_data["is_refreshing"] = False
            import gc; gc.collect()
            time.sleep(60)

# ===================== 入口 =====================

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=9000)
