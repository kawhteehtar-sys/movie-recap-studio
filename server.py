from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field
import edge_tts
import io

app = FastAPI(title="Movie Recap Studio API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    voice: str = "my-MM-ThihaNeural"
    rate: str = "+10%"

ALLOWED_VOICES = {"my-MM-ThihaNeural", "my-MM-NilarNeural"}

@app.get("/")
async def root():
    return {"service": "Movie Recap Studio API", "status": "ok"}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/api/tts")
async def text_to_speech(payload: TTSRequest):
    if payload.voice not in ALLOWED_VOICES:
        raise HTTPException(status_code=400, detail="Unsupported voice")
    if payload.rate not in {"+0%", "+5%", "+10%", "+15%", "+20%"}:
        raise HTTPException(status_code=400, detail="Unsupported speech rate")
    try:
        communicate = edge_tts.Communicate(
            payload.text, payload.voice, rate=payload.rate
        )
        audio = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.write(chunk["data"])
        if audio.tell() == 0:
            raise RuntimeError("No audio was returned by the TTS service")
        return Response(
            content=audio.getvalue(),
            media_type="audio/mpeg",
            headers={"Content-Disposition": "attachment; filename=voice.mp3"}
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TTS generation failed: {exc}")
