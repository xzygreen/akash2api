from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse
from fastapi.background import BackgroundTasks
import requests
import uuid
import json
import time
from typing import Optional
import asyncio
from curl_cffi import requests as cffi_requests
import re
import os

app = FastAPI()
security = HTTPBearer()

# OpenAI API Key 配置，可以通过环境变量覆盖
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", None)  # 设置为 None 表示不校验，或设置具体值,如"sk-proj-1234567890"

# 修改全局数据存储
global_data = {
    "cookie": None,
    "cookies": None,
    "last_update": 0
}

def get_cookie():
    try:
        # 使用 curl_cffi 发送请求
        response = cffi_requests.get(
            'https://chat.akash.network/',
            impersonate="chrome110",
            timeout=30
        )
        
        # 获取所有 cookies
        cookies = response.cookies.items()
        if cookies:
            cookie_str = '; '.join([f'{k}={v}' for k, v in cookies])
            global_data["cookie"] = cookie_str
            global_data["last_update"] = time.time()
            print(f"Got cookies: {cookie_str}")
            return cookie_str
                
    except Exception as e:
        print(f"Error fetching cookie: {e}")
    return None

async def check_and_update_cookie(background_tasks: BackgroundTasks):
    # 如果cookie超过30分钟，在后台更新
    if time.time() - global_data["last_update"] > 1800:
        background_tasks.add_task(get_cookie)

@app.on_event("startup")
async def startup_event():
    get_cookie()

async def get_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    
    # 如果设置了 OPENAI_API_KEY，则需要验证
    if OPENAI_API_KEY is not None:
        # 去掉 Bearer 前缀后再比较
        clean_token = token.replace("Bearer ", "") if token.startswith("Bearer ") else token
        if clean_token != OPENAI_API_KEY:
            raise HTTPException(
                status_code=401,
                detail="Invalid API key"
            )
    
    # 返回去掉 "Bearer " 前缀的token
    return token.replace("Bearer ", "") if token.startswith("Bearer ") else token

async def check_image_status(session: requests.Session, job_id: str, headers: dict) -> Optional[str]:
    """
    检查图片生成状态并获取生成的图片
    
    Args:
        session: 请求会话
        job_id: 任务ID
        headers: 请求头

    Returns:
        Optional[str]: base64格式的图片数据，如果生成失败则返回None
    """
    max_retries = 30  # 最多等待30秒
    for _ in range(max_retries):
        try:
            response = session.get(
                f'https://chat.akash.network/api/image-status?ids={job_id}',
                headers=headers
            )
            status_data = response.json()
            
            if status_data and isinstance(status_data, list) and len(status_data) > 0:
                job_info = status_data[0]
                
                # 如果result不为空，说明图片已生成
                if job_info.get("result"):
                    return job_info["result"]  # 直接返回base64数据
                
                # 如果状态是失败，则停止等待
                if job_info.get("status") == "failed":
                    print(f"Image generation failed for job {job_id}")
                    return None
                    
        except Exception as e:
            print(f"Error checking image status: {e}")
            
        await asyncio.sleep(1)  # 等待1秒后重试
    
    print(f"Timeout waiting for image generation job {job_id}")
    return None

@app.get("/")
async def health_check():
    """Health check endpoint"""
    return {"status": "ok"}

@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    api_key: str = Depends(get_api_key)
):
    try:
        data = await request.json()
        print(f"Chat request data: {data}")
        
        chat_id = str(uuid.uuid4()).replace('-', '')[:16]
        
        akash_data = {
            "id": chat_id,
            "messages": data.get('messages', []),
            "model": data.get('model', "DeepSeek-R1"),
            "system": data.get('system_message', "You are a helpful assistant."),
            "temperature": data.get('temperature', 0.6),
            "topP": data.get('top_p', 0.95)
        }
        
        headers = {
            "Content-Type": "application/json",
            "Cookie": f"session_token={api_key}",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Origin": "https://chat.akash.network",
            "Referer": "https://chat.akash.network/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Connection": "keep-alive",
            "Priority": "u=1, i"
        }
        
        print(f"Sending request to Akash with headers: {headers}")
        print(f"Request data: {akash_data}")
        
        with requests.Session() as session:
            response = session.post(
                'https://chat.akash.network/api/chat',
                json=akash_data,
                headers=headers,
                stream=True
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
                                match = re.search(r"jobId='([^']+)' prompt='([^']+)' negative='([^']*)'", msg_data)
                                if match:
                                    job_id, prompt, negative = match.groups()
                                    print(f"Starting image generation process for job_id: {job_id}")
                                    
                                    # 立即发送思考开始的消息
                                    start_time = time.time()
                                    think_msg = "<think>\n"
                                    think_msg += "🎨 Generating image...\n\n"
                                    think_msg += f"Prompt: {prompt}\n"
                                    
                                    # 发送思考开始消息 (使用标准 OpenAI 格式)
                                    chunk = {
                                        "id": f"chatcmpl-{chat_id}",
                                        "object": "chat.completion.chunk",
                                        "created": int(time.time()),
                                        "model": data.get('model'),  # 使用请求中指定的模型
                                        "choices": [{
                                            "delta": {"content": think_msg},
                                            "index": 0,
                                            "finish_reason": None
                                        }]
                                    }
                                    yield f"data: {json.dumps(chunk)}\n\n"
                                    
                                    # 同步方式检查图片状态
                                    max_retries = 10
                                    retry_interval = 3
                                    result = None
                                    
                                    for attempt in range(max_retries):
                                        try:
                                            print(f"\nAttempt {attempt + 1}/{max_retries} for job {job_id}")
                                            status_response = cffi_requests.get(
                                                f'https://chat.akash.network/api/image-status?ids={job_id}',
                                                headers=headers,
                                                impersonate="chrome110"
                                            )
                                            print(f"Status response code: {status_response.status_code}")
                                            status_data = status_response.json()
                                            print(f"Status data: {json.dumps(status_data, indent=2)}")
                                            
                                            if status_data and isinstance(status_data, list) and len(status_data) > 0:
                                                job_info = status_data[0]
                                                print(f"Job status: {job_info.get('status')}")
                                                
                                                if job_info.get("result"):
                                                    result = job_info['result']
                                                    if result and not result.startswith("Failed"):
                                                        break
                                                elif job_info.get("status") == "failed":
                                                    result = None
                                                    break
                                        except Exception as e:
                                            print(f"Error checking status: {e}")
                                            
                                        if attempt < max_retries - 1:
                                            time.sleep(retry_interval)
                                    
                                    # 发送结束消息
                                    elapsed_time = time.time() - start_time
                                    end_msg = f"\n🤔 Thinking for {elapsed_time:.1f}s...\n"
                                    end_msg += "</think>\n\n"
                                    if result and not result.startswith("Failed"):
                                        end_msg += f"![Generated Image]({result})"
                                    else:
                                        end_msg += "*Image generation failed or timed out.*\n"
                                    
                                    # 发送结束消息 (使用标准 OpenAI 格式)
                                    chunk = {
                                        "id": f"chatcmpl-{chat_id}",
                                        "object": "chat.completion.chunk",
                                        "created": int(time.time()),
                                        "model": data.get('model'),  # 使用请求中指定的模型
                                        "choices": [{
                                            "delta": {"content": end_msg},
                                            "index": 0,
                                            "finish_reason": None
                                        }]
                                    }
                                    yield f"data: {json.dumps(chunk)}\n\n"
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
                                "model": data.get('model'),  # 使用请求中指定的模型
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
        return {"error": str(e)}

@app.get("/v1/models")
async def list_models(api_key: str = Depends(get_api_key)):
    try:
        headers = {
            "Content-Type": "application/json",
            "Cookie": f"session_token={api_key}",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Origin": "https://chat.akash.network",
            "Referer": "https://chat.akash.network/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Connection": "keep-alive"
        }
        
        response = requests.get(
            'https://chat.akash.network/api/models',
            headers=headers
        )
        
        akash_response = response.json()
        
        # 转换为标准 OpenAI 格式
        openai_models = {
            "object": "list",
            "data": [
                {
                    "id": model["id"],
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "akash",
                    "permission": [{
                        "id": "modelperm-" + model["id"],
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
                } for model in akash_response.get("models", [])
            ]
        }
        
        return openai_models
        
    except Exception as e:
        print(f"Error in list_models: {e}")
        return {"error": str(e)}

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=9000)