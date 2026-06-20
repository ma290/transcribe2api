"""
Chunked Transcribe API
-----------------------
Ek hi /transcribe endpoint leta hai bade audio file ko, usko 5-5 minute ke
chunks mein todta hai, har chunk ko upstream transcribe API (Koyeb wali)
pe SEQUENTIALLY hit karta hai, aur saare responses ko timestamp-offset
karke ek hi merged JSON mein wapas bhejta hai — same shape jaisa upstream
API deta hai, taaki frontend (karaoke UI) bina kisi change ke chal jaaye.

Run:
    pip install fastapi uvicorn python-multipart pydub httpx --break-system-packages
    uvicorn main:app --host 0.0.0.0 --port 8000

Note: pydub ko system me ffmpeg chahiye (audio cut karne ke liye).
Ubuntu/Debian: apt-get install -y ffmpeg
Agar ffmpeg nahi mila, startup pe hi clear error aayega (neeck dekho).
"""

import os
import io
import uuid
import shutil
import logging
import tempfile
from typing import Optional

import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from pydub import AudioSegment
from pydub.utils import which

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("chunked-transcribe")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
UPSTREAM_URL = os.environ.get(
    "UPSTREAM_TRANSCRIBE_URL",
    "https://slimy-melisa-ashutosh0879-af2acd0b.koyeb.app/transcribe",
)
CHUNK_MINUTES = 5
CHUNK_MS = CHUNK_MINUTES * 60 * 1000  # 5 min in milliseconds
UPSTREAM_TIMEOUT_SECONDS = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "600"))  # 10 min per chunk, tune as needed

app = FastAPI(title="Chunked Transcribe API", version="1.0.0")


@app.on_event("startup")
async def check_ffmpeg():
    """Startup par hi check karle ki ffmpeg available hai ya nahi — taaki
    request fail hone se pehle hi clear pata chal jaaye."""
    ffmpeg_path = which("ffmpeg")
    if ffmpeg_path is None:
        log.error(
            "ffmpeg NAHI mila system PATH mein! pydub iske bina audio cut "
            "nahi kar payega. Install karo: 'apt-get install -y ffmpeg' "
            "(Linux) ya deployment image mein ffmpeg add karo (Koyeb par "
            "Dockerfile use karo agar buildpack mein ffmpeg nahi hai)."
        )
    else:
        log.info(f"ffmpeg mil gaya: {ffmpeg_path}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def split_audio_into_chunks(audio: AudioSegment, chunk_ms: int) -> list[AudioSegment]:
    """AudioSegment ko chunk_ms duration ke chunks mein todta hai."""
    total_ms = len(audio)
    chunks = []
    start = 0
    while start < total_ms:
        end = min(start + chunk_ms, total_ms)
        chunks.append(audio[start:end])
        start = end
    return chunks


def shift_word_times(words: list, offset_seconds: float) -> list:
    """Har word ke start/end mein offset add karta hai (in-place copy)."""
    shifted = []
    for w in words:
        w2 = dict(w)
        if w2.get("start") is not None:
            w2["start"] = round(w2["start"] + offset_seconds, 3)
        if w2.get("end") is not None:
            w2["end"] = round(w2["end"] + offset_seconds, 3)
        shifted.append(w2)
    return shifted


def shift_segment_times(segments: list, offset_seconds: float) -> list:
    """Segments ke andar wale words ka time bhi shift karta hai."""
    shifted = []
    for seg in segments:
        seg2 = dict(seg)
        if "words" in seg2 and seg2["words"]:
            seg2["words"] = shift_word_times(seg2["words"], offset_seconds)
        shifted.append(seg2)
    return shifted


async def transcribe_single_chunk(
    client: httpx.AsyncClient,
    chunk_bytes: bytes,
    filename: str,
    content_type: str,
) -> dict:
    """Ek chunk ko upstream API pe bhejta hai aur JSON response wapas karta hai."""
    files = {"file": (filename, chunk_bytes, content_type)}
    resp = await client.post(UPSTREAM_URL, files=files, timeout=UPSTREAM_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def guess_export_format(content_type: Optional[str], filename: str) -> tuple[str, str]:
    """Returns (pydub_export_format, mime_type) based on input file.
    Hum chunks ko hamesha m4a (aac) mein export karenge taaki upstream
    API ko consistent format mile — agar tum chaaho toh ye change kar
    sakte ho (e.g. wav, mp3)."""
    return "ipod", "audio/x-m4a"  # pydub "ipod" codec = m4a/aac container


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------

@app.post("/transcribe")
async def transcribe_chunked(file: UploadFile = File(...)):
    """
    Bada audio file accept karta hai, 5-5 min ke chunks mein todta hai,
    har chunk ko upstream API pe SEQUENTIALLY bhejta hai, aur results ko
    merge karke same-shape response return karta hai jaisa upstream API
    deta hai (success, task_id, transcript text, full_response.data...).
    """
    if which("ffmpeg") is None:
        raise HTTPException(
            status_code=500,
            detail="ffmpeg server par install nahi hai. pydub ko audio "
                   "cut karne ke liye ffmpeg chahiye. Deployment image "
                   "mein ffmpeg add karo.",
        )

    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Empty file mila.")

    # Temp file mein likho taaki pydub/ffmpeg use kar sake (format auto-detect)
    suffix = os.path.splitext(file.filename or "")[1] or ".m4a"
    tmp_dir = tempfile.mkdtemp(prefix="chunked_transcribe_")
    input_path = os.path.join(tmp_dir, f"input{suffix}")

    try:
        with open(input_path, "wb") as f:
            f.write(raw_bytes)

        log.info(f"Loading audio: {file.filename} ({len(raw_bytes)} bytes)")
        try:
            audio = AudioSegment.from_file(input_path)
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Audio file decode nahi ho payi (ffmpeg/pydub error): {e}",
            )

        total_duration_sec = len(audio) / 1000.0
        log.info(f"Total duration: {total_duration_sec:.1f}s")

        chunks = split_audio_into_chunks(audio, CHUNK_MS)
        log.info(f"{len(chunks)} chunk(s) banaye (each ~{CHUNK_MINUTES} min)")

        export_format, content_type = guess_export_format(file.content_type, file.filename or "")

        merged_words: list = []
        merged_segments: list = []
        merged_text_parts: list = []
        language_code: Optional[str] = None
        any_success = False
        chunk_results_meta = []  # debugging/visibility ke liye

        async with httpx.AsyncClient() as client:
            for idx, chunk in enumerate(chunks):
                offset_seconds = idx * CHUNK_MINUTES * 60
                chunk_filename = f"chunk_{idx + 1}{os.path.splitext(file.filename or '.m4a')[1] or '.m4a'}"

                log.info(
                    f"[{idx + 1}/{len(chunks)}] Exporting chunk "
                    f"(offset={offset_seconds}s, len={len(chunk) / 1000:.1f}s)..."
                )

                buf = io.BytesIO()
                chunk.export(buf, format=export_format)
                chunk_bytes = buf.getvalue()

                log.info(f"[{idx + 1}/{len(chunks)}] Sending to upstream API...")
                try:
                    result = await transcribe_single_chunk(
                        client, chunk_bytes, chunk_filename, content_type
                    )
                except httpx.HTTPStatusError as e:
                    log.error(f"[{idx + 1}/{len(chunks)}] Upstream HTTP error: {e}")
                    chunk_results_meta.append({"chunk": idx + 1, "status": "failed", "error": str(e)})
                    continue
                except httpx.RequestError as e:
                    log.error(f"[{idx + 1}/{len(chunks)}] Upstream request error: {e}")
                    chunk_results_meta.append({"chunk": idx + 1, "status": "failed", "error": str(e)})
                    continue

                if not result.get("success"):
                    log.warning(f"[{idx + 1}/{len(chunks)}] Upstream success=false, skipping merge for this chunk.")
                    chunk_results_meta.append({"chunk": idx + 1, "status": "upstream_failed", "raw": result})
                    continue

                any_success = True
                transcription = (
                    result.get("full_response", {})
                    .get("data", {})
                    .get("transcription", {})
                )

                if language_code is None:
                    language_code = transcription.get("language_code")

                chunk_text = transcription.get("text", "")
                if chunk_text:
                    merged_text_parts.append(chunk_text.strip())

                chunk_words = transcription.get("words", []) or []
                merged_words.extend(shift_word_times(chunk_words, offset_seconds))

                chunk_segments = transcription.get("segments", []) or []
                merged_segments.extend(shift_segment_times(chunk_segments, offset_seconds))

                chunk_results_meta.append({"chunk": idx + 1, "status": "ok", "task_id": result.get("task_id")})

                log.info(f"[{idx + 1}/{len(chunks)}] Done. words={len(chunk_words)} segments={len(chunk_segments)}")

        if not any_success:
            raise HTTPException(
                status_code=502,
                detail="Koi bhi chunk upstream API se successfully transcribe nahi ho paya.",
            )

        merged_task_id = f"merged_{uuid.uuid4().hex[:20]}"

        final_response = {
            "success": True,
            "task_id": merged_task_id,
            "transcript": None,
            "pdf_url": None,
            "srt_url": None,
            "full_response": {
                "code": 100000,
                "message": "success",
                "data": {
                    "id": merged_task_id,
                    "status": "success",
                    "model_task_id": merged_task_id,
                    "scenario": "auto",
                    "audio_url": None,
                    "file_duration": round(total_duration_sec),
                    "file_size": len(raw_bytes),
                    "file_format": suffix.lstrip("."),
                    "file_name": file.filename,
                    "language": None,
                    "progress": 1,
                    "remain_time": None,
                    "source_type": "file",
                    "source_url": None,
                    "transcription": {
                        "language_code": language_code,
                        "text": " ".join(merged_text_parts),
                        "words": merged_words,
                        "segments": merged_segments,
                    },
                },
            },
            "_chunking_meta": {
                "chunk_minutes": CHUNK_MINUTES,
                "total_chunks": len(chunks),
                "chunk_results": chunk_results_meta,
                "total_duration_seconds": round(total_duration_sec, 1),
            },
        }

        return JSONResponse(content=final_response)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "ffmpeg_available": which("ffmpeg") is not None,
        "chunk_minutes": CHUNK_MINUTES,
        "upstream_url": UPSTREAM_URL,
    }
