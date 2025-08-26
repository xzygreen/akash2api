from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse
from fastapi.background import BackgroundTasks
from contextlib import asynccontextmanager
import requests
from curl_cffi import requests as cffi_requests
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
# 加载环境变量
load_dotenv(override=True)
# 配置日志
logging.basicConfig(
    level=logging.INFO,  # 改为 INFO 级别
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
# 修改全局数据存储
global_data = {
    "cookie": None,
    "cookies": None,
    "last_update": 0,
    "cookie_expires": 0,  # 添加 cookie 过期时间
    "is_refreshing": False  # 添加刷新状态标志
}
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时获取 cookie
    logger.info("Starting FastAPI application, initializing cookie fetcher...")
    
    # 创建并启动线程
    cookie_thread = threading.Thread(target=get_cookie_with_retry)
    cookie_thread.daemon = True  # 设置为守护线程
    cookie_thread.start()
    
    # 创建并启动自动刷新线程
    refresh_thread = threading.Thread(target=auto_refresh_cookie)
    refresh_thread.daemon = True
    refresh_thread.start()
    
    logger.info("Cookie fetcher and auto-refresh threads started")
    yield
    
    # 关闭时清理资源
    logger.info("Shutting down FastAPI application")
    global_data["cookie"] = None
    global_data["cookies"] = None
    global_data["last_update"] = 0
    global_data["is_refreshing"] = False
def get_cookie_with_retry(max_retries=3, retry_delay=5):
    """带重试机制的获取 cookie 函数"""
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
app = FastAPI(lifespan=lifespan)
security = HTTPBearer()
# OpenAI API Key 配置，可以通过环境变量覆盖
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", None)
logger.info(f"OPENAI_API_KEY is set: {OPENAI_API_KEY is not None}")

def get_random_browser_fingerprint():
    """生成随机的浏览器指纹"""
    # 随机选择浏览器版本
    chrome_versions = ["120", "121", "122", "123", "124", "125"]
    edge_versions = ["120", "121", "122", "123", "124", "125"]
    selected_version = random.choice(chrome_versions)
    edge_version = random.choice(edge_versions)
    
    # 随机选择操作系统
    os_versions = [
        "Windows NT 10.0; Win64; x64",
        "Macintosh; Intel Mac OS X 10_15_7",
        "Macintosh; Intel Mac OS X 11_0_1",
        "Macintosh; Intel Mac OS X 12_0_1"
    ]
    selected_os = random.choice(os_versions)
    
    # 随机选择语言偏好
    languages = [
        "en-US,en;q=0.9",
        "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "en-GB,en;q=0.9,en-US;q=0.8"
    ]
    selected_language = random.choice(languages)
    
    # 随机选择视口大小
    viewport_sizes = [
        (1920, 1080),
        (1366, 768),
        (1440, 900),
        (1536, 864),
        (1680, 1050)
    ]
    selected_viewport = random.choice(viewport_sizes)
    
    # 构建用户代理字符串
    user_agent = f"Mozilla/5.0 ({selected_os}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{selected_version}.0.0.0 Safari/537.36 Edg/{edge_version}.0.0.0"
    
    # 构建请求头
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
    
    return {
        "headers": headers,
        "viewport": selected_viewport,
        "user_agent": user_agent
    }
def get_cookie():
    """获取 cookie 的函数"""
    browser = None
    context = None
    page = None
    
    try:
        logger.info("Starting cookie retrieval process...")
        
        # 获取随机浏览器指纹
        fingerprint = get_random_browser_fingerprint()
        logger.info(f"Using browser fingerprint: {fingerprint['user_agent']}")
        
        with sync_playwright() as p:
            try:
                # 启动浏览器
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
                
                # 创建上下文，使用随机指纹
                logger.info("Creating browser context...")
                context = browser.new_context(
                    viewport={'width': fingerprint["viewport"][0], 'height': fingerprint["viewport"][1]},
                    user_agent=fingerprint["user_agent"],
                    locale='en-US',
                    timezone_id='America/New_York',
                    permissions=['geolocation'],
                    extra_http_headers=fingerprint["headers"]
                )
                
                # 添加脚本以覆盖 navigator.webdriver
                context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => false,
                    });
                    // 更多指纹伪装
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => [1, 2, 3, 4, 5],
                    });
                """)
                
                logger.info("Browser context created successfully")
                
                # 创建页面
                logger.info("Creating new page...")
                page = context.new_page()
                logger.info("Page created successfully")
                
                # 设置页面超时
                page.set_default_timeout(60000)
                
                # 访问目标网站，添加重试机制
                max_retries = 3
                retry_delay = 5
                
                for attempt in range(max_retries):
                    try:
                        logger.info(f"Navigating to target website (attempt {attempt + 1}/{max_retries})...")
                        page.goto("https://chat.akash.network/", timeout=50000)
                        break
                    except Exception as e:
                        if attempt == max_retries - 1:
                            raise
                        logger.warning(f"Navigation attempt {attempt + 1} failed: {e}")
                        time.sleep(retry_delay)
                
                # 等待页面加载
                logger.info("Waiting for page load...")
                try:
                    # 首先等待 DOM 加载完成
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                    logger.info("DOM content loaded")
                    
                    # 等待一段时间，让 Cloudflare 检查完成
                    logger.info("Waiting for Cloudflare check...")
                    time.sleep(5)
                    
                    # 尝试点击页面，模拟用户行为
                    try:
                        page.mouse.move(100, 100)
                        page.mouse.click(100, 100)
                        logger.info("Simulated user interaction")
                        
                        # 随机滚动页面
                        page.mouse.wheel(0, 100)
                        time.sleep(0.5)
                        page.mouse.wheel(0, -50)
                        logger.info("Simulated scrolling")
                    except Exception as e:
                        logger.warning(f"Failed to simulate user interaction: {e}")
                    
                    # 再次等待一段时间
                    time.sleep(5)
                    
                except Exception as e:
                    logger.warning(f"Timeout waiting for load state: {e}")
                
                # 等待更长时间确保页面完全加载
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                    logger.info("Network idle reached")
                except Exception as e:
                    logger.warning(f"Timeout waiting for network idle: {e}")
                
                # 获取 cookies
                logger.info("Getting cookies...")
                cookies = context.cookies()
                
                if not cookies:
                    logger.error("No cookies found")
                    return None
                
                # 记录所有 cookie 名称以进行调试
                cookie_names = [cookie['name'] for cookie in cookies]
                logger.info(f"Retrieved cookies: {cookie_names}")
                    
                # 检查是否有 cf_clearance cookie
                cf_cookie = next((cookie for cookie in cookies if cookie['name'] == 'cf_clearance'), None)
                if not cf_cookie:
                    logger.error("cf_clearance cookie not found")
                    return None
                
                # 检查是否有 session_token cookie
                session_cookie = next((cookie for cookie in cookies if cookie['name'] == 'session_token'), None)
                if not session_cookie:
                    logger.error("session_token cookie not found")
                    
                # 构建 cookie 字符串
                cookie_str = '; '.join([f"{cookie['name']}={cookie['value']}" for cookie in cookies])
                logger.info(f"Cookie string length: {len(cookie_str)}")
                
                global_data["cookie"] = cookie_str
                global_data["cookies"] = cookies  # 保存完整的 cookies 列表
                global_data["last_update"] = time.time()
                
                # 设置 cookie 过期时间
                if session_cookie and 'expires' in session_cookie and session_cookie['expires'] > 0:
                    global_data["cookie_expires"] = session_cookie['expires']
                    logger.info(f"Session token expires at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(session_cookie['expires']))}")
                else:
                    global_data["cookie_expires"] = time.time() + 1800  # 30 分钟
                    logger.info("No explicit expiration in session_token cookie, setting default 30 minute expiration")
                
                logger.info("Successfully retrieved cookies")
                return cookie_str
                
            except Exception as e:
                logger.error(f"Error in browser operations: {e}")
                import traceback
                logger.error(f"Traceback: {traceback.format_exc()}")
                return None
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
                import gc
                gc.collect()
    
    except Exception as e:
        logger.error(f"Error fetching cookie: {str(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
    
    # 最后再次确保资源被清理
    try:
        if page: page.close()
    except Exception: pass
    try:
        if context: context.close()
    except Exception: pass
    try:
        if browser: browser.close()
    except Exception: pass
    import gc
    gc.collect()
    return None

async def refresh_cookie():
    """刷新 cookie 的函数，用于401错误触发"""
    logger.info("Refreshing cookie due to 401 error")
    if global_data["is_refreshing"]:
        logger.info("Cookie refresh already in progress, waiting...")
        for _ in range(10):
            await asyncio.sleep(1)
            if not global_data["is_refreshing"]: break
    
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
    """后台刷新 cookie 的函数，不影响接口调用"""
    if global_data["is_refreshing"]:
        logger.info("Cookie refresh already in progress, skipping")
        return
    
    try:
        global_data["is_refreshing"] = True
        logger.info("Starting background cookie refresh")
        
        executor = ThreadPoolExecutor(max_workers=1)
        loop = asyncio.get_event_loop()
        new_cookie = await loop.run_in_executor(executor, get_cookie)
        
        if new_cookie:
            logger.info("Background cookie refresh successful")
        else:
            logger.error("Background cookie refresh failed")
    except Exception as e:
        logger.error(f"Error in background cookie refresh: {e}")
    finally:
        global_data["is_refreshing"] = False

async def check_and_update_cookie():
    """检查并更新 cookie"""
    current_time = time.time()
    if not global_data["cookie"] or current_time >= global_data["cookie_expires"]:
        logger.info("Cookie expired or not available, starting refresh")
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as executor:
            await loop.run_in_executor(executor, get_cookie)

async def get_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    if OPENAI_API_KEY is not None:
        clean_token = token.replace("Bearer ", "") if token.startswith("Bearer ") else token
        if clean_token != OPENAI_API_KEY:
            raise HTTPException(status_code=401, detail="Invalid API key")
    return True

async def validate_cookie():
    # 等待 cookie 初始化完成
    max_wait = 30
    start_time = time.time()
    while not global_data["cookie"] and time.time() - start_time < max_wait:
        await asyncio.sleep(1)
        logger.info("Waiting for cookie initialization...")
    
    if not global_data["cookie"]:
        raise HTTPException(status_code=503, detail="Service temporarily unavailable - Cookie not available")
    
    return global_data["cookie"]

async def check_image_status(session: requests.Session, full_job_id: str, short_job_id: str, headers: dict) -> Optional[str]:
    max_retries = 30
    for attempt in range(max_retries):
        try:
            response = session.get(f'https://chat.akash.network/api/image-status?ids={full_job_id}', headers=headers)
            if response.status_code == 404:
                await asyncio.sleep(1)
                continue
            
            status_data = response.json()
            if status_data and isinstance(status_data, list) and len(status_data) > 0:
                job_info = status_data[0]
                status = job_info.get('status')
                if status in ["completed", "succeeded"]:
                    result = job_info.get("result")
                    if result and not result.startswith("Failed"):
                        if result.startswith("/api/image/"):
                            image_url = f"https://chat.akash.network{result}"
                            image_response = session.get(image_url, headers=headers)
                            if image_response.status_code == 200:
                                return await upload_to_xinyew(image_response.content, full_job_id)
                        elif result and not result.startswith("http"):
                            if not result.startswith("/"):
                                return f"https://chat.akash.network/api/image/job_{short_job_id}_00001_.webp"
                    return None
                elif status == "failed":
                    return None
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error checking image status: {e}")
            return None
    return None

@app.get("/", response_class=HTMLResponse)
async def health_check():
    cookie_status = "ok" if global_data["cookie"] else "error"
    status_color = "green" if cookie_status == "ok" else "red"
    status_text = "正常" if cookie_status == "ok" else "异常"
    current_time = datetime.now(timezone(timedelta(hours=8)))
    
    expires_str, time_left_str = "未知", "未知"
    if global_data["cookie_expires"] > 0:
        expires_time = datetime.fromtimestamp(global_data["cookie_expires"], timezone(timedelta(hours=8)))
        expires_str = expires_time.strftime("%Y-%m-%d %H:%M:%S")
        time_left = global_data["cookie_expires"] - time.time()
        if time_left > 0:
            hours_left = int(time_left // 3600)
            minutes_left = int((time_left % 3600) // 60)
            time_left_str = f"{hours_left}小时{minutes_left}分钟" if hours_left > 0 else f"{minutes_left}分钟"
        else:
            time_left_str = "已过期"

    last_update_str, update_ago = "从未更新", "未知"
    if global_data["last_update"] > 0:
        last_update_time = datetime.fromtimestamp(global_data["last_update"], timezone(timedelta(hours=8)))
        last_update_str = last_update_time.strftime("%Y-%m-%d %H:%M:%S")
        time_since_update = time.time() - global_data["last_update"]
        if time_since_update < 60: update_ago = f"{int(time_since_update)}秒前"
        elif time_since_update < 3600: update_ago = f"{int(time_since_update // 60)}分钟前"
        else: update_ago = f"{int(time_since_update // 3600)}小时前"

    status = {
        "status": "ok",
        "cookie_status": { "status": cookie_status, "status_text": status_text, "status_color": status_color, "expires": expires_str, "time_left": time_left_str, "last_update": last_update_str, "update_ago": update_ago }
    }
    return HTMLResponse(content=f"""
    <!DOCTYPE html><html><head><title>Akash API 服务状态</title><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><style>body{{font-family:sans-serif;margin:20px;background-color:#f5f5f5;color:#333}}.container{{max-width:800px;margin:0 auto;background-color:white;padding:30px;border-radius:12px;box-shadow:0 4px 12px rgba(0,0,0,0.1)}}.status{{display:flex;align-items:center;margin-bottom:20px}}.status-dot{{width:16px;height:16px;border-radius:50%;margin-right:12px}}.status-dot.green{{background-color:#4CAF50}}.status-dot.red{{background-color:#f44336}}.status-text{{font-size:20px;font-weight:600}}.info-item{{display:flex;justify-content:space-between;margin:10px 0}}.footer{{text-align:center;margin-top:30px;color:#999}}</style></head>
    <body><div class="container"><h1>Akash API 服务状态</h1><div class="status"><div class="status-dot {status["cookie_status"]["status_color"]}"></div><div class="status-text">服务状态: {status["cookie_status"]["status_text"]}</div></div>
    <div class="info-section"><h3>Cookie 信息</h3><div class="info-item"><span>过期时间:</span><span>{status["cookie_status"]["expires"]} ({status["cookie_status"]["time_left"]})</span></div><div class="info-item"><span>最后更新:</span><span>{status["cookie_status"]["last_update"]} ({status["cookie_status"]["update_ago"]})</span></div></div>
    <div class="footer"><p>当前时间: {current_time.strftime("%Y-%m-%d %H:%M:%S")} (北京时间)</p></div></div></body></html>
    """)

@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    api_key: bool = Depends(get_api_key),
    cookie: str = Depends(validate_cookie)
):
    try:
        data = await request.json()
        fingerprint = get_random_browser_fingerprint()
        chat_id = str(uuid.uuid4()).replace('-', '')[:16]
        
        processed_messages = [
            {"role": msg.get("role"), "content": msg.get("content"), "parts": [{"type": "text", "text": msg.get("content")}]}
            for msg in data.get('messages', [])
        ]
        
        akash_data = {
            "id": chat_id,
            "messages": processed_messages,
            "model": data.get('model', "DeepSeek-R1"),
            "system": data.get('system_message') or data.get('system', "You are a helpful assistant."),
            "temperature": data.get('temperature', 0.85 if data.get('model') == 'AkashGen' else 0.6),
            "topP": data.get('top_p', 1.0 if data.get('model') == 'AkashGen' else 0.95),
            "context": []
        }
        
        with requests.Session() as session:
            session.headers.update(fingerprint["headers"])
            cookies_dict = {name: value for name, value in (item.strip().split('=', 1) for item in cookie.split(';') if '=' in item)}
            
            response = session.post('https://chat.akash.network/api/chat', json=akash_data, cookies=cookies_dict, stream=True)
            
            if response.status_code in [401, 403]:
                logger.info("Auth failed, refreshing cookie and retrying...")
                new_cookie = await refresh_cookie()
                if new_cookie:
                    new_cookies_dict = {name: value for name, value in (item.strip().split('=', 1) for item in new_cookie.split(';') if '=' in item)}
                    response = session.post('https://chat.akash.network/api/chat', json=akash_data, cookies=new_cookies_dict, stream=True)
            
            if response.status_code not in [200, 201]:
                raise HTTPException(status_code=response.status_code, detail=f"Akash API error: {response.text}")
            
            def generate():
                for line in response.iter_lines():
                    if not line:
                        continue
                    try:
                        line_str = line.decode('utf-8')
                        msg_type, msg_data = line_str.split(':', 1)
                        
                        if msg_type == '0':
                            try:
                                content = json.loads(msg_data)
                            except json.JSONDecodeError:
                                content = msg_data[1:-1] if msg_data.startswith('"') and msg_data.endswith('"') else msg_data
                            content = content.replace("\\n", "\n")

                            # 直接将接收到的内容作为增量发送，因为API本身就是片段流
                            # （除了第一个特殊的<think>块，它也作为一个完整的片段处理）
                            chunk = {
                                "id": f"chatcmpl-{chat_id}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": data.get('model'),
                                "choices": [{"delta": {"content": content}, "index": 0, "finish_reason": None}]
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"
                        
                        elif msg_type in ['e', 'd']:
                            chunk = {
                                "id": f"chatcmpl-{chat_id}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": data.get('model'),
                                "choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}]
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"
                            yield "data: [DONE]\n\n"
                            break
                    except Exception as e:
                        logger.error(f"Error processing line: {e} - line was: {line_str}")
                        continue
            
            return StreamingResponse(generate(), media_type='text/event-stream')
    
    except Exception as e:
        logger.error(f"Error in chat_completions: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return HTTPException(status_code=500, detail=str(e))

@app.get("/v1/models")
async def list_models(
    api_key: bool = Depends(get_api_key),
    cookie: str = Depends(validate_cookie)
):
    try:
        fingerprint = get_random_browser_fingerprint()
        headers = fingerprint["headers"]
        
        with requests.Session() as session:
            session.headers.update(headers)
            cookies_dict = {name: value for name, value in (item.strip().split('=', 1) for item in cookie.split(';') if '=' in item)}
            
            response = session.get('https://chat.akash.network/api/models', cookies=cookies_dict)

            if response.status_code in [401, 403]:
                new_cookie = await refresh_cookie()
                if new_cookie:
                    new_cookies_dict = {name: value for name, value in (item.strip().split('=', 1) for item in new_cookie.split(';') if '=' in item)}
                    response = session.get('https://chat.akash.network/api/models', cookies=new_cookies_dict)
        
            if response.status_code not in [200, 201]:
                raise HTTPException(status_code=response.status_code, detail="Failed to fetch models.")
        
            akash_response = response.json()
            models_list = akash_response if isinstance(akash_response, list) else akash_response.get("models", [])
            
            openai_models = {
                "object": "list",
                "data": [
                    {
                        "id": model["id"] if isinstance(model, dict) else model,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "akash"
                    } for model in models_list
                ]
            }
            return openai_models
            
    except Exception as e:
        logger.error(f"Error in list_models: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def process_image_generation(msg_data: str, session: requests.Session, headers: dict, chat_id: str) -> Optional[list]:
    match = re.search(r"jobId='([^']+)' prompt='([^']+)'", msg_data)
    if not match: return create_error_messages(chat_id, "无法解析图片任务。")
    
    job_id = match.group(1)
    if not job_id or job_id in ['undefined', 'null']:
        return create_error_messages(chat_id, "无法获取有效的任务ID。")
    
    short_job_id = job_id.replace('-', '')[:8]
    result = await check_image_status(session, job_id, short_job_id, headers)
    
    if result:
        image_msg = f"\n\n![Generated Image]({result})"
        return [{"id": f"chatcmpl-{chat_id}-image", "object": "chat.completion.chunk", "created": int(time.time()), "model": "AkashGen", "choices": [{"delta": {"content": image_msg}, "index": 0, "finish_reason": None}]}]
    else:
        return create_error_messages(chat_id, "图片生成或上传失败。")

def create_error_messages(chat_id: str, error_message: str) -> list:
    return [{"id": f"chatcmpl-{chat_id}-error", "object": "chat.completion.chunk", "created": int(time.time()), "model": "AkashGen", "choices": [{"delta": {"content": f"\n\n**❌ {error_message}**"}, "index": 0, "finish_reason": None}]}]

async def upload_to_xinyew(image_data: bytes, job_id: str) -> Optional[str]:
    try:
        with tempfile.NamedTemporaryFile(suffix='.webp', delete=False) as temp_file:
            temp_file.write(image_data)
            temp_file_path = temp_file.name
        
        try:
            with open(temp_file_path, 'rb') as f:
                files = {'file': (f"{job_id}.webp", f, 'image/webp')}
                response = requests.post('https://api.xinyew.cn/api/jdtc', files=files, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                if result.get('errno') == 0:
                    return result.get('data', {}).get('url')
        finally:
            os.unlink(temp_file_path)
    except Exception as e:
        logger.error(f"Error in upload_to_xinyew: {e}")
    return None

def auto_refresh_cookie():
    """自动刷新 cookie 的线程函数"""
    while True:
        try:
            if (not global_data["cookie"] or time.time() >= global_data["cookie_expires"]) and not global_data["is_refreshing"]:
                logger.info("Auto-refreshing cookie...")
                get_cookie()
            time.sleep(60)
        except Exception as e:
            logger.error(f"Error in auto-refresh thread: {e}")
            global_data["is_refreshing"] = False
            time.sleep(60)

if __name__ == '__main__':
    import uvicorn
    # 我还对代码进行了一些简化和重构，使其更健壮和易于维护。
    uvicorn.run(app, host='0.0.0.0', port=9000)
