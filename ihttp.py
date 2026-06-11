from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.concurrency import run_in_threadpool
import httpx
import time
import urllib.parse
from playwright.sync_api import sync_playwright

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_URL = "https://audioconvert.ai/api"
DEFAULT_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.8",
    "origin": "https://audioconvert.ai",
    "referer": "https://audioconvert.ai/mp3-to-text",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
}


def _first_present(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _extract_transcript_text(payload):
    transcription = _first_present(
        payload.get("transcription"),
        payload.get("transcript"),
        payload.get("result", {}).get("transcription"),
        payload.get("result", {}).get("transcript"),
        payload.get("data", {}).get("transcription"),
        payload.get("data", {}).get("transcript"),
        payload.get("data", {}).get("result", {}).get("transcription"),
        payload.get("data", {}).get("result", {}).get("transcript"),
    )

    if isinstance(transcription, str) and transcription.strip():
        return transcription

    if isinstance(transcription, dict):
        words = transcription.get("words") or transcription.get("segments") or []
        if words:
            parts = []
            for item in words:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("word") or item.get("content")
                else:
                    text = item
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            if parts:
                return " ".join(parts)

    return None


def get_fresh_token_via_webkit():
    print("Step 1: Initializing guest session & fetching token...")
    with sync_playwright() as p:
        browser = p.webkit.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        
        auth_token = None
        
        def intercept_response(response):
            nonlocal auth_token
            if "api/" in response.url:
                req_headers = response.request.headers
                if "authorization" in req_headers and req_headers["authorization"].startswith("Bearer "):
                    auth_token = req_headers["authorization"]

        page.on("response", intercept_response)
        
        try:
            page.goto("https://audioconvert.ai/mp3-to-text", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3500)
        except Exception as e:
            print(f"WebKit token capture warning: {e}")
        finally:
            try:
                context.clear_cookies()
            except:
                pass
            context.close()
            browser.close()
            
        if auth_token:
            return auth_token
        else:
            # Fallback static token
            return "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOiIzNGM4MjFiYy1iNzliLTRmNGItODE4OS1kNjEzMjk3NTFiMGMifQ.Wxm6x3nHKtVKxuT4l-IftA3_kJj-9612KlUlbbtZFzA"

def run_hybrid_api_workflow(audio_bytes: bytes, filename: str):
    auth_token = get_fresh_token_via_webkit()
    encoded_filename = urllib.parse.quote(filename)
    
    request_headers = DEFAULT_HEADERS.copy()
    request_headers["authorization"] = auth_token
    
    # Infinite timeout setup to bypass client-side write timeouts
    custom_timeout = httpx.Timeout(None)
    
    with httpx.Client(headers=request_headers, timeout=custom_timeout) as client:
        
        # --- STEP 2: Presign URL ---
        print("Step 2: Requesting presigned upload URL...")
        presign_res = client.get(f"{BASE_URL}/resource/upload/presign?filename={encoded_filename}")
        if presign_res.status_code != 200:
            raise Exception(f"Failed to get presign URL: {presign_res.text}")
            
        presign_data = presign_res.json()
        print(f"Presign Data Logged: {presign_data}")
        
        # Exact response dict key structure match
        inner_data = presign_data.get("data", {})
        upload_url = inner_data.get("upload_url") or inner_data.get("uploadUrl") or presign_data.get("uploadUrl")
        
        if not upload_url:
            raise Exception(f"Upload URL missing from response schema: {presign_data}")

        # Final audio link bina query parameters ke nikala (Aliyun Storage URL)
        final_audio_url = upload_url.split('?')[0]

        # --- STEP 3: Cloud Storage Upload ---
        print("Step 3: Uploading audio bytes directly to cloud storage...")
        upload_headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(audio_bytes)),
            "Connection": "keep-alive"
        }
        
        # Use httpx.put to prevent sending Authorization header that breaks OSS signature
        # Also, do NOT send custom headers (like Content-Type), as they break the pre-signed URL signature!
        upload_res = httpx.put(upload_url, content=audio_bytes, timeout=None)
        if upload_res.status_code not in [200, 201]:
            raise Exception(f"Cloud storage PUT upload failed ({upload_res.status_code}): {upload_res.text}")
        print("Upload to Cloud Storage Successful!")

        # --- STEP 4: Quota verification ---
        print("Step 4: Confirming guest limits...")
        try:
            client.post(f"{BASE_URL}/transcribe/check-guest-quota", json={"duration_minutes": 6})
        except:
            pass

        # --- STEP 5: Trigger Transcription Task ---
        print("Step 5: Activating transcription pipeline...")
        transcribe_payload = {
            "audio_url": final_audio_url,
            "language_code": "",
            "file_name": filename,
            "scenario": "auto"
        }
        
        task_res = client.post(f"{BASE_URL}/transcribe/", json=transcribe_payload)
        if task_res.status_code not in [200, 201]:
            raise Exception(f"Failed to trigger task: {task_res.text}")
            
        task_data = task_res.json()
        task_id = task_data.get("id") or task_data.get("data", {}).get("id")
        if not task_id:
            raise Exception(f"Task ID allocation failed. Response: {task_data}")

        # --- STEP 6: Capture Response via Polling Loop ---
        print(f"Step 6: Polling status for Task ID: {task_id} ...")
        polling_url = f"{BASE_URL}/transcribe/{task_id}"
        
        for attempt in range(120):  # Wait up to 6 minutes
            poll_res = client.get(polling_url)
            if poll_res.status_code == 200:
                poll_data = poll_res.json()
                
                # Extract inner data object
                data_dict = poll_data.get("data") or {}
                status = poll_data.get("status") or data_dict.get("status")
                print(f"Attempt {attempt+1}: Status is '{status}' | Raw: {poll_data}")

                result_payload = data_dict if isinstance(data_dict, dict) else {}
                transcript_text = _extract_transcript_text(result_payload) or _extract_transcript_text(poll_data)
                transcript_field = _first_present(
                    result_payload.get("transcript"),
                    poll_data.get("transcript"),
                    result_payload.get("text"),
                    poll_data.get("text"),
                    transcript_text,
                )
                pdf_url = _first_present(
                    result_payload.get("pdf_url"),
                    poll_data.get("pdf_url"),
                    result_payload.get("result", {}).get("pdf_url"),
                    poll_data.get("result", {}).get("pdf_url"),
                )
                srt_url = _first_present(
                    result_payload.get("srt_url"),
                    poll_data.get("srt_url"),
                    result_payload.get("result", {}).get("srt_url"),
                    poll_data.get("result", {}).get("srt_url"),
                )

                if status in ["success", 3] or "transcript" in poll_data or "transcript" in result_payload or "transcription" in poll_data or "transcription" in result_payload or pdf_url or srt_url:
                    print("Success! Response captured fully.")
                    return {
                        "success": True,
                        "task_id": task_id,
                        "transcript": transcript_field,
                        "pdf_url": pdf_url,
                        "srt_url": srt_url,
                        "full_response": poll_data,
                    }
                elif status in ["failed", 4, "error"]:
                    return {"success": False, "error": "Transcription failed internally on website backend."}
            
            time.sleep(3)
            
        return {"success": False, "error": "Polling timeout exceeded."}

# Back to stable UploadFile format, stream handling threadpool handle karega
@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    try:
        audio_bytes = await file.read()
        if not audio_bytes:
            raise HTTPException(status_code=400, detail="Empty file payload.")
            
        result = await run_in_threadpool(run_hybrid_api_workflow, audio_bytes, file.filename)
        return result
    except Exception as e:
        print(f"Fatal error in endpoint: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
def health():
    return {"status": "ok", "engine": "fastapi-ihttp-fixed"}
