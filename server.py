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
import base64
import re
import httpx
import asyncio

app = FastAPI(title="Movie Recap Studio API", version="0.4.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

ALLOWED_VOICES = {"my-MM-ThihaNeural", "my-MM-NilarNeural"}
GEMINI_MODEL = os.getenv("GEMINI_AUDIO_MODEL", "gemini-2.5-flash")


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    voice: str = "my-MM-ThihaNeural"
    rate: str = "+0%"
    pitch: str = "+0Hz"
    volume: str = "+5%"


def run(cmd, timeout=1800):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout)[-5000:])
    return p


@app.get("/")
async def root():
    return {"service": "Movie Recap Studio API", "status": "ok", "version": "0.4.0"}


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.4.0"}


@app.post("/api/tts")
async def text_to_speech(payload: TTSRequest):
    if payload.voice not in ALLOWED_VOICES:
        raise HTTPException(status_code=400, detail="Unsupported voice")

    if payload.rate not in {"-10%", "-5%", "+0%", "+5%", "+10%"}:
        raise HTTPException(status_code=400, detail="Unsupported speech rate")

    if payload.pitch not in {"-5Hz", "+0Hz", "+5Hz"}:
        raise HTTPException(status_code=400, detail="Unsupported pitch")

    if payload.volume not in {"+0%", "+5%", "+10%"}:
        raise HTTPException(status_code=400, detail="Unsupported volume")

    try:
        communicate = edge_tts.Communicate(
            payload.text,
            payload.voice,
            rate=payload.rate,
            pitch=payload.pitch,
            volume=payload.volume
        )

        audio = io.BytesIO()

        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.write(chunk["data"])

        if audio.tell() == 0:
            raise RuntimeError("No audio was returned")

        return Response(
            audio.getvalue(),
            media_type="audio/mpeg",
            headers={"Content-Disposition": "attachment; filename=voice.mp3"},
        )

    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TTS failed: {exc}")


async def gemini_transcribe_translate(audio_path: str, api_key: str, language: str):
    """Ask Gemini to return timed speech segments and Myanmar translations."""
    with open(audio_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode("ascii")

    prompt = f"""
You are a careful film dialogue transcription and translation assistant.
Listen to the supplied audio. Return ONLY valid JSON, with this exact shape:
{{
  "segments": [
    {{
      "start": 1.25,
      "end": 3.80,
      "speaker": "speaker_1",
      "source_text": "the complete original spoken words",
      "myanmar_text": "complete natural Myanmar translation"
    }}
  ]
}}
Rules:
- Include dialogue and clearly spoken narration only. Do not create speech for footsteps,
  music, action, silence, or ambient noise.
- Transcribe every audible spoken word. Do not summarize, omit, or invent dialogue.
- Keep each turn as its own segment. Split when the speaker changes.
- start/end are seconds from the beginning of the supplied audio.
- Use non-overlapping chronological segments where possible. If two people truly speak
  at once, keep both segments and preserve their actual overlapping times.
- speaker labels must be stable (speaker_1, speaker_2, etc.) across the clip.
- Translate all meaning into natural {language}; do not shorten the meaning.
- If words are genuinely unintelligible, mark only that short part as [မကြားရ] in
  myanmar_text and do not guess.
- Return an empty segments array if there is no speech.
- No markdown fences, explanations, or extra keys.
"""

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )
    body = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "audio/mpeg", "data": audio_b64}},
            ]
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
        },
    }
    async with httpx.AsyncClient(timeout=300) as client:
        response = await client.post(url, json=body)
    if response.status_code != 200:
        raise RuntimeError(f"Gemini API {response.status_code}: {response.text[:2500]}")
    data = response.json()
    try:
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(raw)
        segments = parsed.get("segments", [])
    except Exception as exc:
        raise RuntimeError(f"Gemini JSON response မဖတ်နိုင်ပါ: {str(exc)}; {str(data)[:1200]}")
    if not isinstance(segments, list):
        raise RuntimeError("Gemini က segments list မပြန်ပေးပါ")
    return segments


async def make_tts_bytes(text: str, voice: str):
    stream = edge_tts.Communicate(
        text,
        voice=voice,
        rate="+0%",
        pitch="+0Hz",
        volume="+5%"
    )
    buf = io.BytesIO()

    async for item in stream.stream():
        if item["type"] == "audio":
            buf.write(item["data"])

    if not buf.tell():
        raise RuntimeError("TTS က အသံအလွတ်ပြန်ပေးပါတယ်")

    return buf.getvalue()


def media_duration(path: str) -> float:
    p = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path
    ])
    return float(p.stdout.strip())


@app.post("/api/auto-dub")
async def auto_dub(
    video: UploadFile = File(...),
    api_key: str = Form(...),
    voice: str = Form("my-MM-ThihaNeural"),
    language: str = Form("မြန်မာ"),
):
    """
    Returns JSON: timed translated subtitles plus one continuous dubbed MP3.
    Frontend can set audioFile from audio_base64 and subtitles from segments.
    """
    if voice not in ALLOWED_VOICES:
        raise HTTPException(status_code=400, detail="သီဟ သို့မဟုတ် နီလာ အသံကို ရွေးပါ")
    if not api_key.strip():
        raise HTTPException(status_code=400, detail="Gemini API Key ထည့်ပါ")

    workdir = tempfile.mkdtemp(prefix="movie_auto_dub_")
    try:
        video_path = os.path.join(workdir, "input_video")
        source_audio = os.path.join(workdir, "source_audio.mp3")
        silent_audio = os.path.join(workdir, "silent.mp3")
        with open(video_path, "wb") as f:
            shutil.copyfileobj(video.file, f)
        if not os.path.getsize(video_path):
            raise ValueError("Video ဖိုင်အလွတ်ဖြစ်နေပါတယ်")

        # Extract source soundtrack as mono MP3 for Gemini audio understanding.
        run([
            "ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1",
            "-ar", "16000", "-b:a", "64k", source_audio
        ], timeout=900)
        duration = media_duration(video_path)
        segments = await gemini_transcribe_translate(source_audio, api_key.strip(), language)

        clean = []
        for item in segments:
            try:
                start = max(0.0, float(item.get("start", 0)))
                end = min(duration, float(item.get("end", 0)))
                source_text = str(item.get("source_text", "")).strip()
                myanmar_text = str(item.get("myanmar_text", "")).strip()
                speaker = str(item.get("speaker", "speaker_1")).strip()[:40]
                if not myanmar_text or end <= start or not source_text:
                    continue
                clean.append({
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "speaker": speaker,
                    "source_text": source_text,
                    "text": myanmar_text,
                    "size": 30,
                    "bottom": 7,
                })
            except (TypeError, ValueError):
                continue
        clean.sort(key=lambda x: (x["start"], x["end"]))
        if not clean:
            raise ValueError("စကားပြောအပိုင်း မတွေ့ပါ။ အသံပါတဲ့ Video နဲ့ ပြန်စမ်းပါ။")

        # Create one silent bed equal to video duration.
        run([
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            "anullsrc=channel_layout=stereo:sample_rate=24000",
            "-t", str(duration), "-q:a", "9", silent_audio
        ])

        # Render each translated utterance separately, fit it inside its detected
        # time window without deleting words, then mix at its timestamp.
        inputs = [silent_audio]
        filters = []
        labels = []
        warnings = []
        for idx, seg in enumerate(clean):
            speech_path = os.path.join(workdir, f"speech_{idx}.mp3")
            audio_bytes = await make_tts_bytes(seg["text"], voice)
            with open(speech_path, "wb") as f:
                f.write(audio_bytes)
            speech_duration = media_duration(speech_path)
            slot = seg["end"] - seg["start"]
            if speech_duration > slot + 0.08:
                # Never truncate content. Speed up only within a conservative limit.
                ratio = speech_duration / max(slot, 0.1)
                if ratio > 1.8:
                    warnings.append(
                        f"အပိုင်း {idx+1}: မြန်မာဘာသာပြန်အသံက မူရင်းစကားပြောချိန်ထက် "
                        "ရှည်နေပါတယ်။ စကားမဖြတ်ထားပေမဲ့ အချိန်ကျော်နိုင်ပါတယ်။"
                    )
                    fit_rate = 1.8
                else:
                    fit_rate = ratio
                fitted = os.path.join(workdir, f"fit_{idx}.mp3")
                run([
                    "ffmpeg", "-y", "-i", speech_path, "-filter:a",
                    f"atempo={fit_rate:.5f}", fitted
                ])
                speech_path = fitted
            input_index = len(inputs)
            inputs.append(speech_path)
            label = f"a{idx}"
            delay_ms = int(seg["start"] * 1000)
            filters.append(
                f"[{input_index}:a]aresample=24000,adelay={delay_ms}|{delay_ms},"
                f"apad[a{idx}]"
            )
            labels.append(f"[{label}]")

        # Mix the silent bed and all utterances. Overlapping original speech remains
        # a potential overlap; flag it rather than silently dropping either speaker.
        for i in range(1, len(clean)):
            if clean[i]["start"] < clean[i-1]["end"] - 0.05:
                warnings.append("မူရင်းအသံတွင် စကားပြောချိန် ထပ်နေသောအပိုင်းရှိသည်။")
                break

        input_args = []
        for path in inputs:
            input_args += ["-i", path]
        mix_inputs = "[0:a]" + "".join(labels)
        filter_complex = ";".join(filters + [
            f"{mix_inputs}amix=inputs={len(inputs)}:duration=first:dropout_transition=0,"
            f"atrim=duration={duration}[out]"
        ])
        dubbed_path = os.path.join(workdir, "dubbed.mp3")
        cmd = ["ffmpeg", "-y"] + input_args + [
            "-filter_complex", filter_complex, "-map", "[out]",
            "-codec:a", "libmp3lame", "-b:a", "192k", dubbed_path
        ]
        run(cmd, timeout=1800)
        with open(dubbed_path, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode("ascii")

        return {
            "ok": True,
            "duration": round(duration, 3),
            "voice": voice,
            "segments": clean,
            "audio_base64": audio_b64,
            "warnings": warnings,
            "note": (
                "AI က စကားပြောသူနှင့် အချိန်ကို ခန့်မှန်းထားခြင်းဖြစ်သည်။ "
                "မူရင်းအသံနှင့် အချိန်ကို နားထောင်စစ်ဆေးပါ။"
            ),
        }
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Gemini တုံ့ပြန်ချိန်ကျော်သွားပါတယ်")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
@app.post("/api/render-mp4")
async def render_mp4(
    video: UploadFile = File(...),
    audio: UploadFile = File(...),
    blur_regions: str = Form("[]"),
    subtitles: str = Form("[]"),
):

    workdir = tempfile.mkdtemp(prefix="movie_recap_")
    try:
        vp, ap, op = [
            os.path.join(workdir, n)
            for n in ("input_video", "recap_audio.mp3", "movie-recap.mp4")
        ]
        with open(vp, "wb") as f:
            shutil.copyfileobj(video.file, f)
        with open(ap, "wb") as f:
            shutil.copyfileobj(audio.file, f)
        if not os.path.getsize(vp) or not os.path.getsize(ap):
            raise ValueError("Video သို့မဟုတ် Audio ဖိုင်အလွတ်ဖြစ်နေပါတယ်")

        probe = run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration", "-of", "json", vp
        ])
        streams = json.loads(probe.stdout).get("streams", [])
        if not streams:
            raise ValueError("Video stream မတွေ့ပါ")
        width, height = int(streams[0]["width"]), int(streams[0]["height"])

        try:
            regions, subs = json.loads(blur_regions), json.loads(subtitles)
        except json.JSONDecodeError:
            raise ValueError("Blur/Subtitles JSON မမှန်ပါ")
        if not isinstance(regions, list) or len(regions) > 30:
            raise ValueError("Blur Box အများဆုံး 30 ခု")
        if not isinstance(subs, list) or len(subs) > 500:
            raise ValueError("စာတန်း အများဆုံး 500 ခု")

        filters = []
        current = "[0:v]"
        for i, r in enumerate(regions):
            x, y, w, h = [
                float(r.get(k, d))
                for k, d in (("x", 0), ("y", 0), ("width", .2), ("height", .1))
            ]
            strength = float(r.get("strength", 50))
            if not all(0 <= v <= 1 for v in (x, y, w, h)) or w <= 0 or h <= 0:
                raise ValueError("Blur coordinate မမှန်ပါ")
            px = max(0, min(width - 2, int(x * width)))
            py = max(0, min(height - 2, int(y * height)))
            pw = max(2, min(width - px, int(w * width)))
            ph = max(2, min(height - py, int(h * height)))
            px -= px % 2
            py -= py % 2
            pw = max(2, pw - pw % 2)
            ph = max(2, ph - ph % 2)
            radius = min(max(1, int(strength / 2)), max(1, (min(pw, ph) - 1) // 2))
            filters += [
                f"{current}split=2[b{i}][c{i}]",
                f"[c{i}]crop={pw}:{ph}:{px}:{py},boxblur={radius}:2[z{i}]",
                f"[b{i}][z{i}]overlay={px}:{py}[v{i}]",
            ]
            current = f"[v{i}]"

        font = "/usr/share/fonts/truetype/noto/NotoSansMyanmar-Regular.ttf"
        if not os.path.exists(font):
            font = "/usr/share/fonts/truetype/noto/NotoSansMyanmar-VF.ttf"
        for i, s in enumerate(subs):
            text = str(s.get("text", "")).strip()
            if not text:
                continue
            start = max(0, float(s.get("start", 0)))
            end = float(s.get("end", 0))
            size = max(12, min(80, int(s.get("size", 30))))
            bottom = max(0, min(90, float(s.get("bottom", 7))))
            if end <= start:
                raise ValueError("စာတန်းအချိန် မမှန်ပါ")
            safe = (
                text.replace("\\", "\\\\").replace(":", "\\:")
                .replace("'", "\\'").replace("%", "\\%")
                .replace(",", "\\,").replace("[", "\\[")
                .replace("]", "\\]")
            )
            yexpr = f"h-th-{int(height * bottom / 100)}"
            filters.append(
                f"{current}drawtext=fontfile='{font}':text='{safe}':"
                f"fontcolor=yellow:borderw=2:bordercolor=black:fontsize={size}:"
                f"x=(w-text_w)/2:y={yexpr}:enable='between(t,{start},{end})'[s{i}]"
            )
            current = f"[s{i}]"

        cmd = ["ffmpeg", "-y", "-i", vp, "-i", ap]
        if filters:
            cmd += ["-filter_complex", ";".join(filters), "-map", current]
        else:
            cmd += ["-map", "0:v:0"]
     
        cmd += [
            "-map", "1:a:0", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest",
            "-movflags", "+faststart", "-max_muxing_queue_size", "1024", op
        ]
        run(cmd)
        if not os.path.exists(op):
            raise RuntimeError("MP4 ဖိုင် မဖန်တီးနိုင်ပါ")
        return FileResponse(
            op, media_type="video/mp4", filename="movie-recap.mp4",
            background=BackgroundTask(shutil.rmtree, workdir, ignore_errors=True)
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=504, detail="Video rendering timed out")
    except Exception as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc))
