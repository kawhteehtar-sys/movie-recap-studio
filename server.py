from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
import edge_tts
import io
import os
import json
import shutil
import tempfile
import subprocess

app = FastAPI(title="Movie Recap Studio API", version="0.2.0")

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
            raise RuntimeError("No audio was returned")

        return Response(
            content=audio.getvalue(),
            media_type="audio/mpeg",
            headers={"Content-Disposition": "attachment; filename=voice.mp3"}
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TTS failed: {exc}")


@app.post("/api/render-mp4")
async def render_mp4(
    video: UploadFile = File(...),
    audio: UploadFile = File(...),
    blur_regions: str = Form("[]")
):
    workdir = tempfile.mkdtemp(prefix="movie_recap_")

    try:
        video_path = os.path.join(workdir, "input_video")
        audio_path = os.path.join(workdir, "recap_audio.mp3")
        output_path = os.path.join(workdir, "movie-recap.mp4")

        with open(video_path, "wb") as f:
            shutil.copyfileobj(video.file, f)

        with open(audio_path, "wb") as f:
            shutil.copyfileobj(audio.file, f)

        if os.path.getsize(video_path) == 0:
            raise ValueError("Video file is empty")
        if os.path.getsize(audio_path) == 0:
            raise ValueError("Audio file is empty")

        # Read original video dimensions
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "json", video_path
            ],
            capture_output=True,
            text=True,
            check=True
        )

        streams = json.loads(probe.stdout).get("streams", [])
        if not streams:
            raise ValueError("No video stream found")

        width = int(streams[0]["width"])
        height = int(streams[0]["height"])

        try:
            regions = json.loads(blur_regions)
        except json.JSONDecodeError:
            raise ValueError("Invalid blur_regions JSON")

        if not isinstance(regions, list) or len(regions) > 30:
            raise ValueError("Blur regions must be a list of up to 30 areas")

        filters = []
        current = "[0:v]"

        for i, region in enumerate(regions):
            x = float(region.get("x", 0))
            y = float(region.get("y", 0))
            w = float(region.get("width", 0.2))
            h = float(region.get("height", 0.1))
            strength = float(region.get("strength", 50))

            if not all(map(lambda n: 0 <= n <= 1, [x, y, w, h])):
                raise ValueError("Blur coordinates must be between 0 and 1")
            if w <= 0 or h <= 0:
                raise ValueError("Blur width and height must be positive")

            px = max(0, min(width - 2, int(x * width)))
            py = max(0, min(height - 2, int(y * height)))

            pw = max(2, int(w * width))
            ph = max(2, int(h * height))

            pw = min(pw, width - px)
            ph = min(ph, height - py)

            # FFmpeg crop dimensions and positions should be even
            px -= px % 2
            py -= py % 2
            pw -= pw % 2
            ph -= ph % 2

            radius = max(1, min(50, int(strength / 2)))

            next_label = f"v{i + 1}"
            base_label = f"base{i}"
            crop_label = f"crop{i}"
            blur_label = f"blur{i}"

            filters.append(
                f"{current}split=2[{base_label}][{crop_label}]"
            )
            filters.append(
                f"[{crop_label}]crop={pw}:{ph}:{px}:{py},"
                f"boxblur={radius}:2[{blur_label}]"
            )
            filters.append(
                f"[{base_label}][{blur_label}]"
                f"overlay={px}:{py}[{next_label}]"
            )

            current = f"[{next_label}]"

        command = ["ffmpeg", "-y", "-i", video_path, "-i", audio_path]

        if filters:
            command.extend([
                "-filter_complex", ";".join(filters),
                "-map", current
            ])
        else:
            command.extend(["-map", "0:v:0"])

        # Replace original audio with Burmese recap audio
        command.extend([
            "-map", "1:a:0",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            "-max_muxing_queue_size", "1024",
            output_path
        ])

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=1800
        )

        if result.returncode != 0:
            raise RuntimeError(result.stderr[-3000:])

        if not os.path.exists(output_path):
            raise RuntimeError("MP4 output was not created")

        return FileResponse(
            output_path,
            media_type="video/mp4",
            filename="movie-recap.mp4",
            background=BackgroundTask(
                shutil.rmtree, workdir, ignore_errors=True
            )
        )

    except subprocess.TimeoutExpired:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=504, detail="Video rendering timed out")
    except Exception as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc))
    
