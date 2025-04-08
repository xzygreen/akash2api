from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse, HTMLResponse
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
    "cookie_expires": 0  # 添加 cookie 过期时间
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时获取 cookie
    logger.info("Starting FastAPI application, initializing cookie fetcher...")
    
    # 创建并启动线程
    cookie_thread = threading.Thread(target=get_cookie_with_retry)
    cookie_thread.daemon = True  # 设置为守护线程
    cookie_thread.start()
    
    logger.info("Cookie fetcher thread started")
    yield
    
    # 关闭时清理资源
    logger.info("Shutting down FastAPI application")
    global_data["cookie"] = None
    global_data["cookies"] = None
    global_data["last_update"] = 0

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
logger.info(f"OPENAI_API_KEY value: {OPENAI_API_KEY}")

def get_cookie():
    try:
        logger.info("Starting cookie retrieval process...")
        
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
                        '--window-size=1920,1080',
                        '--disable-blink-features=AutomationControlled'  # 禁用自动化控制检测
                    ]
                )
                
                logger.info("Browser launched successfully")
                
                # 创建上下文，添加更多浏览器特征
                logger.info("Creating browser context...")
                context = browser.new_context(
                    viewport={'width': 1920, 'height': 1080},
                    user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    locale='en-US',
                    timezone_id='America/New_York',
                    permissions=['geolocation'],
                    extra_http_headers={
                        'Accept-Language': 'en-US,en;q=0.9',
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
                        'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                        'Sec-Ch-Ua-Mobile': '?0',
                        'Sec-Ch-Ua-Platform': '"macOS"',
                        'Sec-Fetch-Dest': 'document',
                        'Sec-Fetch-Mode': 'navigate',
                        'Sec-Fetch-Site': 'none',
                        'Sec-Fetch-User': '?1',
                        'Upgrade-Insecure-Requests': '1'
                    }
                )
                
                logger.info("Browser context created successfully")
                
                # 创建页面
                logger.info("Creating new page...")
                page = context.new_page()
                logger.info("Page created successfully")
                
                # 设置页面超时
                page.set_default_timeout(60000)
                
                # 访问目标网站
                logger.info("Navigating to target website...")
                page.goto("https://chat.akash.network/", timeout=50000)
                
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
                    except Exception as e:
                        logger.warning(f"Failed to simulate user interaction: {e}")
                    
                    # 再次等待一段时间
                    time.sleep(5)
                    
                except Exception as e:
                    logger.warning(f"Timeout waiting for load state: {e}")
                
                # 获取 cookies
                logger.info("Getting cookies...")
                cookies = context.cookies()
                
                if not cookies:
                    logger.error("No cookies found")
                    browser.close()
                    return None
                    
                # 检查是否有 cf_clearance cookie
                cf_cookie = next((cookie for cookie in cookies if cookie['name'] == 'cf_clearance'), None)
                if not cf_cookie:
                    logger.error("cf_clearance cookie not found")
                    browser.close()
                    return None
                    
                # 构建 cookie 字符串
                cookie_str = '; '.join([f"{cookie['name']}={cookie['value']}" for cookie in cookies])
                global_data["cookie"] = cookie_str
                global_data["cookies"] = cookies  # 保存完整的 cookies 列表
                global_data["last_update"] = time.time()
                
                # 查找 session_token cookie 的过期时间
                session_cookie = next((cookie for cookie in cookies if cookie['name'] == 'session_token'), None)
                if session_cookie and 'expires' in session_cookie and session_cookie['expires'] > 0:
                    global_data["cookie_expires"] = session_cookie['expires']
                    logger.info(f"Session token expires at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(session_cookie['expires']))}")
                else:
                    # 如果没有明确的过期时间，默认设置为1小时后过期
                    global_data["cookie_expires"] = time.time() + 3600
                    logger.info("No explicit expiration in session_token cookie, setting default 1 hour expiration")
                
                logger.info("Successfully retrieved cookies")
                browser.close()
                return cookie_str
                
            except Exception as e:
                logger.error(f"Error in browser operations: {e}")
                logger.error(f"Error type: {type(e)}")
                import traceback
                logger.error(f"Traceback: {traceback.format_exc()}")
                return None
                
    except Exception as e:
        logger.error(f"Error fetching cookie: {str(e)}")
        logger.error(f"Error type: {type(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return None

# 添加刷新 cookie 的函数
async def refresh_cookie():
    logger.info("Refreshing cookie due to 401 error")
    # 标记 cookie 为过期
    global_data["cookie_expires"] = 0
    # 获取新的 cookie
    return get_cookie()

async def check_and_update_cookie(background_tasks: BackgroundTasks):
    # 如果 cookie 不存在或已过期，则更新
    current_time = time.time()
    if not global_data["cookie"] or current_time >= global_data["cookie_expires"]:
        logger.info("Cookie expired or not available, refreshing...")
        background_tasks.add_task(get_cookie)
    else:
        logger.info("Using existing cookie")

async def get_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    logger.info(f"Received token: {token}")
    
    # 如果设置了 OPENAI_API_KEY，则需要验证
    if OPENAI_API_KEY is not None:
        # 去掉 Bearer 前缀后再比较
        clean_token = token.replace("Bearer ", "") if token.startswith("Bearer ") else token
        logger.info(f"Clean token: {clean_token}")
        if clean_token != OPENAI_API_KEY:
            logger.error(f"Token mismatch. Expected: {OPENAI_API_KEY}, Got: {clean_token}")
            raise HTTPException(
                status_code=401,
                detail="Invalid API key"
            )
        logger.info("API key validation passed")
    
    return True

async def validate_cookie(background_tasks: BackgroundTasks):
    # 检查并更新 cookie（如果需要）
    await check_and_update_cookie(background_tasks)
    
    # 等待 cookie 初始化完成
    max_wait = 30  # 最大等待时间（秒）
    start_time = time.time()
    while not global_data["cookie"] and time.time() - start_time < max_wait:
        await asyncio.sleep(1)
        logger.info("Waiting for cookie initialization...")
    
    # 检查是否有有效的 cookie
    if not global_data["cookie"]:
        logger.error("Cookie not available after waiting")
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable - Cookie not available"
        )
    
    logger.info("Cookie validation passed")
    return global_data["cookie"]

async def check_image_status(session: requests.Session, job_id: str, headers: dict) -> Optional[str]:
    """检查图片生成状态并获取生成的图片"""
    max_retries = 30
    for attempt in range(max_retries):
        try:
            print(f"\nAttempt {attempt + 1}/{max_retries} for job {job_id}")
            response = session.get(
                f'https://chat.akash.network/api/image-status?ids={job_id}',
                headers=headers
            )
            print(f"Status response code: {response.status_code}")
            status_data = response.json()
            
            if status_data and isinstance(status_data, list) and len(status_data) > 0:
                job_info = status_data[0]
                status = job_info.get('status')
                print(f"Job status: {status}")
                
                # 只有当状态为 completed 时才处理结果
                if status == "completed":
                    result = job_info.get("result")
                    if result and not result.startswith("Failed"):
                        print("Got valid result, attempting upload...")
                        image_url = await upload_to_xinyew(result, job_id)
                        if image_url:
                            print(f"Successfully uploaded image: {image_url}")
                            return image_url
                        print("Image upload failed")
                        return None
                    print("Invalid result received")
                    return None
                elif status == "failed":
                    print(f"Job {job_id} failed")
                    return None
                
                # 如果状态是其他（如 pending），继续等待
                await asyncio.sleep(1)
                continue
                    
        except Exception as e:
            print(f"Error checking status: {e}")
            return None
    
    print(f"Timeout waiting for job {job_id}")
    return None

@app.get("/", response_class=HTMLResponse)
async def health_check():
    """Health check endpoint"""
    # 检查 cookie 状态
    cookie_status = "ok" if global_data["cookie"] is not None else "error"
    cookie_status_color = "#4CAF50" if cookie_status == "ok" else "#f44336"
    
    status = {
        "status": cookie_status,
        "version": "1.0.0",
        "cookie_status": {
            "available": global_data["cookie"] is not None,
            "last_update": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(global_data["last_update"])) if global_data["last_update"] > 0 else None,
            "expires": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(global_data["cookie_expires"])) if global_data["cookie_expires"] > 0 else None
        }
    }
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Akash API Status</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
                line-height: 1.6;
                margin: 0;
                padding: 20px;
                background-color: #f5f5f5;
            }}
            .container {{
                max-width: 800px;
                margin: 0 auto;
                background-color: white;
                padding: 20px;
                border-radius: 8px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }}
            h1 {{
                color: #333;
                margin-top: 0;
            }}
            .status {{
                display: inline-block;
                padding: 4px 8px;
                border-radius: 4px;
                font-weight: bold;
                background-color: {cookie_status_color};
                color: white;
            }}
            .info {{
                margin-top: 20px;
            }}
            .info-item {{
                margin-bottom: 10px;
            }}
            .label {{
                font-weight: bold;
                color: #666;
            }}
            .value {{
                color: #333;
            }}
            .cookie-status {{
                margin-top: 20px;
                padding: 15px;
                background-color: #f8f9fa;
                border-radius: 4px;
            }}
            .cookie-status .available {{
                color: {cookie_status_color};
            }}
            .error-message {{
                color: #f44336;
                margin-top: 10px;
                padding: 10px;
                background-color: #ffebee;
                border-radius: 4px;
                display: {"block" if cookie_status == "error" else "none"};
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Akash API Status <span class="status">{status["status"]}</span></h1>
            
            <div class="info">
                <div class="info-item">
                    <span class="label">Version:</span>
                    <span class="value">{status["version"]}</span>
                </div>
            </div>
            
            <div class="cookie-status">
                <h2>Cookie Status</h2>
                <div class="info-item">
                    <span class="label">Available:</span>
                    <span class="value available">{str(status["cookie_status"]["available"])}</span>
                </div>
                <div class="info-item">
                    <span class="label">Last Update:</span>
                    <span class="value">{status["cookie_status"]["last_update"] or "Never"}</span>
                </div>
                <div class="info-item">
                    <span class="label">Expires:</span>
                    <span class="value">{status["cookie_status"]["expires"] or "Unknown"}</span>
                </div>
                
                <div class="error-message">
                    Cookie retrieval failed. The service may not be fully functional.
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    
    return html

@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    background_tasks: BackgroundTasks,
    api_key: bool = Depends(get_api_key),
    cookie: str = Depends(validate_cookie)
):
    try:
        data = await request.json()
        
        chat_id = str(uuid.uuid4()).replace('-', '')[:16]
        
        akash_data = {
            "id": chat_id,
            "messages": data.get('messages', []),
            "model": data.get('model', "DeepSeek-R1"),
            "system": data.get('system_message', "You are a helpful assistant."),
            "temperature": data.get('temperature', 0.6),
            "topP": data.get('top_p', 0.95)
        }
        
        # 构建请求头
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Origin": "https://chat.akash.network",
            "Referer": "https://chat.akash.network/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Connection": "keep-alive"
        }
        
        # 设置 Cookie
        headers["Cookie"] = cookie
        
        with requests.Session() as session:
            response = session.post(
                'https://chat.akash.network/api/chat',
                json=akash_data,
                headers=headers,
                stream=True
            )
            
            # 检查响应状态码，如果是 401，尝试刷新 cookie 并重试
            if response.status_code == 401:
                logger.info("Cookie expired, refreshing...")
                new_cookie = await refresh_cookie()
                if new_cookie:
                    headers["Cookie"] = new_cookie
                    response = session.post(
                        'https://chat.akash.network/api/chat',
                        json=akash_data,
                        headers=headers,
                        stream=True
                    )
            
            if response.status_code != 200:
                logger.error(f"Akash API error: {response.text}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Akash API error: {response.text}"
            )
            
            def generate():
                content_buffer = ""
                for line in response.iter_lines():
                    if not line:
                        continue
                        
                    try:
                        line_str = line.decode('utf-8')
                        msg_type, msg_data = line_str.split(':', 1)
                        
                        if msg_type == '0':
                            if msg_data.startswith('"') and msg_data.endswith('"'):
                                msg_data = msg_data.replace('\\"', '"')
                                msg_data = msg_data[1:-1]
                            msg_data = msg_data.replace("\\n", "\n")
                            
                            # 在处理消息时先判断模型类型
                            if data.get('model') == 'AkashGen' and "<image_generation>" in msg_data:
                                # 图片生成模型的特殊处理
                                async def process_and_send():
                                    messages = await process_image_generation(msg_data, session, headers, chat_id)
                                    if messages:
                                        return messages
                                    return None

                                # 创建新的事件循环
                                loop = asyncio.new_event_loop()
                                asyncio.set_event_loop(loop)
                                try:
                                    result_messages = loop.run_until_complete(process_and_send())
                                finally:
                                    loop.close()
                                
                                if result_messages:
                                    for message in result_messages:
                                        yield f"data: {json.dumps(message)}\n\n"
                                    continue
                            
                            content_buffer += msg_data
                            
                            chunk = {
                                "id": f"chatcmpl-{chat_id}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": data.get('model'),
                                "choices": [{
                                    "delta": {"content": msg_data},
                                    "index": 0,
                                    "finish_reason": None
                                }]
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"
                        
                        elif msg_type in ['e', 'd']:
                            chunk = {
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
                            yield f"data: {json.dumps(chunk)}\n\n"
                            yield "data: [DONE]\n\n"
                            break
                            
                    except Exception as e:
                        print(f"Error processing line: {e}")
                        continue

            return StreamingResponse(
                generate(),
                media_type='text/event-stream',
                headers={
                    'Cache-Control': 'no-cache',
                    'Connection': 'keep-alive',
                    'Content-Type': 'text/event-stream'
                }
            )
    
    except Exception as e:
        print(f"Error in chat_completions: {e}")
        import traceback
        print(traceback.format_exc())
        return {"error": str(e)}

@app.get("/v1/models")
async def list_models(
    background_tasks: BackgroundTasks,
    cookie: str = Depends(validate_cookie)
):
    try:
        headers = {
            "accept": "application/json",
            "accept-language": "en-US,en;q=0.9",
            "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "referer": "https://chat.akash.network/"
        }
        
        # 设置 Cookie
        headers["Cookie"] = cookie
        
        print(f"Using cookie: {headers.get('Cookie', 'None')}")
        print("Sending request to get models...")
        
        response = requests.get(
            'https://chat.akash.network/api/models',
            headers=headers
        )
        
        print(f"Models response status: {response.status_code}")
        print(f"Models response headers: {response.headers}")
        
        if response.status_code == 401:
            print("Authentication failed. Please check your API key.")
            return {"error": "Authentication failed. Please check your API key."}
        
        akash_response = response.json()
        
        # 添加错误处理和调试信息
        print(f"Akash API response: {akash_response}")
        
        # 检查响应格式并适配
        models_list = []
        if isinstance(akash_response, list):
            # 如果直接是列表
            models_list = akash_response
        elif isinstance(akash_response, dict):
            # 如果是字典格式
            models_list = akash_response.get("models", [])
        else:
            print(f"Unexpected response format: {type(akash_response)}")
            models_list = []
        
        # 转换为标准 OpenAI 格式
        openai_models = {
            "object": "list",
            "data": [
                {
                    "id": model["id"] if isinstance(model, dict) else model,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "akash",
                    "permission": [{
                        "id": f"modelperm-{model['id'] if isinstance(model, dict) else model}",
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
                } for model in models_list
            ]
        }
        
        return openai_models
        
    except Exception as e:
        print(f"Error in list_models: {e}")
        import traceback
        print(traceback.format_exc())
        return {"error": str(e)}

async def process_image_generation(msg_data: str, session: requests.Session, headers: dict, chat_id: str) -> Optional[list]:
    """处理图片生成的逻辑，返回多个消息块"""
    match = re.search(r"jobId='([^']+)' prompt='([^']+)' negative='([^']*)'", msg_data)
    if match:
        job_id, prompt, negative = match.groups()
        print(f"Starting image generation process for job_id: {job_id}")
        
        # 记录开始时间
        start_time = time.time()
        
        # 发送思考开始的消息
        think_msg = "<think>\n"
        think_msg += "🎨 Generating image...\n\n"
        think_msg += f"Prompt: {prompt}\n"
        
        # 检查图片状态和上传
        result = await check_image_status(session, job_id, headers)
        
        # 计算实际花费的时间
        elapsed_time = time.time() - start_time
        
        # 完成思考部分
        think_msg += f"\n🤔 Thinking for {elapsed_time:.1f}s...\n"
        think_msg += "</think>"
        
        # 返回两个独立的消息块
        messages = []
        
        # 第一个消息块：思考过程
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
        
        # 第二个消息块：图片结果
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
    return None

async def upload_to_xinyew(image_base64: str, job_id: str) -> Optional[str]:
    """上传图片到新野图床并返回URL"""
    try:
        print(f"\n=== Starting image upload for job {job_id} ===")
        print(f"Base64 data length: {len(image_base64)}")
        
        # 解码base64图片数据
        try:
            image_data = base64.b64decode(image_base64.split(',')[1] if ',' in image_base64 else image_base64)
            print(f"Decoded image data length: {len(image_data)} bytes")
        except Exception as e:
            print(f"Error decoding base64: {e}")
            print(f"First 100 chars of base64: {image_base64[:100]}...")
            return None
        
        # 创建临时文件
        with tempfile.NamedTemporaryFile(suffix='.jpeg', delete=False) as temp_file:
            temp_file.write(image_data)
            temp_file_path = temp_file.name
        
        try:
            filename = f"{job_id}.jpeg"
            print(f"Using filename: {filename}")
            
            # 准备文件上传
            files = {
                'file': (filename, open(temp_file_path, 'rb'), 'image/jpeg')
            }
            
            print("Sending request to xinyew.cn...")
            response = requests.post(
                'https://api.xinyew.cn/api/jdtc',
                files=files,
                timeout=30
            )
            
            print(f"Upload response status: {response.status_code}")
            if response.status_code == 200:
                result = response.json()
                print(f"Upload response: {result}")
                
                if result.get('errno') == 0:
                    url = result.get('data', {}).get('url')
                    if url:
                        print(f"Successfully got image URL: {url}")
                        return url
                    print("No URL in response data")
                else:
                    print(f"Upload failed: {result.get('message')}")
            else:
                print(f"Upload failed with status {response.status_code}")
                print(f"Response content: {response.text}")
            return None
                
        finally:
            # 清理临时文件
            try:
                os.unlink(temp_file_path)
            except Exception as e:
                print(f"Error removing temp file: {e}")
            
    except Exception as e:
        print(f"Error in upload_to_xinyew: {e}")
        import traceback
        print(traceback.format_exc())
        return None

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=9000)