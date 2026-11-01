from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
import edge_tts, io, os, json, shutil, tempfile, subprocess

app = FastAPI(title="Movie Recap Studio API", version="0.3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False,
                   allow_methods=["GET","POST","OPTIONS"], allow_headers=["*"])

class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    voice: str = "my-MM-ThihaNeural"
    rate: str = "+10%"

ALLOWED_VOICES = {"my-MM-ThihaNeural", "my-MM-NilarNeural"}

@app.get("/")
async def root():
    return {"service":"Movie Recap Studio API","status":"ok","version":"0.3.0"}

@app.get("/health")
async def health():
    return {"status":"ok"}

@app.post("/api/tts")
async def text_to_speech(payload: TTSRequest):
    if payload.voice not in ALLOWED_VOICES:
        raise HTTPException(status_code=400, detail="Unsupported voice")
    if payload.rate not in {"+0%","+5%","+10%","+15%","+20%"}:
        raise HTTPException(status_code=400, detail="Unsupported speech rate")
    try:
        communicate = edge_tts.Communicate(payload.text, payload.voice, rate=payload.rate)
        audio = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.write(chunk["data"])
        if audio.tell() == 0:
            raise RuntimeError("No audio was returned")
        return Response(audio.getvalue(), media_type="audio/mpeg",
                        headers={"Content-Disposition":"attachment; filename=voice.mp3"})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TTS failed: {exc}")

def run(cmd, timeout=1800):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(p.stderr[-3500:])
    return p

@app.post("/api/render-mp4")
async def render_mp4(video: UploadFile = File(...), audio: UploadFile = File(...),
                    blur_regions: str = Form("[]"), subtitles: str = Form("[]"),
                    audio_offset: float = Form(0), audio_tempo: float = Form(1)):
    workdir = tempfile.mkdtemp(prefix="movie_recap_")
    try:
        vp, ap, op = [os.path.join(workdir,n) for n in ("input_video","recap_audio.mp3","movie-recap.mp4")]
        with open(vp,"wb") as f: shutil.copyfileobj(video.file,f)
        with open(ap,"wb") as f: shutil.copyfileobj(audio.file,f)
        if not os.path.getsize(vp) or not os.path.getsize(ap):
            raise ValueError("Video သို့မဟုတ် Audio ဖိုင်အလွတ်ဖြစ်နေပါတယ်")
        probe=run(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width,height,duration","-of","json",vp])
        streams=json.loads(probe.stdout).get("streams",[])
        if not streams: raise ValueError("Video stream မတွေ့ပါ")
        width,height=int(streams[0]["width"]),int(streams[0]["height"])
        try: regions=json.loads(blur_regions); subs=json.loads(subtitles)
        except json.JSONDecodeError: raise ValueError("Blur/Subtitles JSON မမှန်ပါ")
        if not isinstance(regions,list) or len(regions)>30: raise ValueError("Blur Box အများဆုံး 30 ခု")
        if not isinstance(subs,list) or len(subs)>300: raise ValueError("စာတန်း အများဆုံး 300 ခု")
        filters=[]; current="[0:v]"
        for i,r in enumerate(regions):
            x,y,w,h=[float(r.get(k,d)) for k,d in (("x",0),("y",0),("width",.2),("height",.1))]
            strength=float(r.get("strength",50))
            if not all(0<=v<=1 for v in (x,y,w,h)) or w<=0 or h<=0: raise ValueError("Blur coordinate မမှန်ပါ")
            px=max(0,min(width-2,int(x*width))); py=max(0,min(height-2,int(y*height)))
            pw=max(2,min(width-px,int(w*width))); ph=max(2,min(height-py,int(h*height)))
            px-=px%2; py-=py%2; pw=max(2,pw-pw%2); ph=max(2,ph-ph%2)
            # Blur radius ကို Crop အရွယ်အစားအလိုက် အလိုအလျောက် ကန့်သတ်ခြင်း
max_radius = max(1, (min(pw, ph) - 1) // 2)
requested_radius = max(1, min(50, int(strength / 2)))
radius = min(requested_radius, max_radius)
            filters += [f"{current}split=2[b{i}][c{i}]",
                        f"[c{i}]crop={pw}:{ph}:{px}:{py},boxblur={radius}:2[z{i}]",
                        f"[b{i}][z{i}]overlay={px}:{py}[v{i}]"]
            current=f"[v{i}]"
        # Burn-in subtitles using an installed Myanmar-capable font.
        font="/usr/share/fonts/truetype/noto/NotoSansMyanmar-Regular.ttf"
        if not os.path.exists(font):
            font="/usr/share/fonts/truetype/noto/NotoSansMyanmar-VF.ttf"
        for i,s in enumerate(subs):
            text=str(s.get("text","")).strip()
            if not text: continue
            start=float(s.get("start",0)); end=float(s.get("end",0))
            size=max(12,min(80,int(s.get("size",30))))
            bottom=max(0,min(90,float(s.get("bottom",7))))
            if start<0 or end<=start: raise ValueError("စာတန်းအချိန် မမှန်ပါ")
            # FFmpeg drawtext escapes special characters.
            safe=text.replace("\\","\\\\").replace(":","\\:").replace("'","\\'").replace("%","\\%").replace(",","\\,").replace("[","\\[").replace("]","\\]")
            yexpr=f"h-th-{int(height*bottom/100)}"
            filters.append(f"{current}drawtext=fontfile='{font}':text='{safe}':fontcolor=yellow:borderw=2:bordercolor=black:fontsize={size}:x=(w-text_w)/2:y={yexpr}:enable='between(t,{start},{end})'[s{i}]")
            current=f"[s{i}]"
        cmd=["ffmpeg","-y","-i",vp,"-i",ap]
        if filters: cmd += ["-filter_complex",";".join(filters),"-map",current]
        else: cmd += ["-map","0:v:0"]
        offset=max(0,float(audio_offset)); tempo=float(audio_tempo)
        if not .5<=tempo<=2: raise ValueError("Audio tempo သည် 0.5x မှ 2x အတွင်းဖြစ်ရပါမည်")
        # Delay narration when offset is positive; optional tempo correction.
        audio_filter=[]
        if abs(tempo-1)>.001: audio_filter.append(f"atempo={tempo}")
        if offset>0: audio_filter.append(f"adelay={int(offset*1000)}:all=1")
        if audio_filter: cmd += ["-filter:a",",".join(audio_filter)]
        cmd += ["-map","1:a:0","-c:v","libx264","-preset","veryfast","-crf","23",
                "-pix_fmt","yuv420p","-c:a","aac","-b:a","192k","-shortest",
                "-movflags","+faststart","-max_muxing_queue_size","1024",op]
        run(cmd)
        if not os.path.exists(op): raise RuntimeError("MP4 ဖိုင် မဖန်တီးနိုင်ပါ")
        return FileResponse(op,media_type="video/mp4",filename="movie-recap.mp4",
                            background=BackgroundTask(shutil.rmtree,workdir,ignore_errors=True))
    except subprocess.TimeoutExpired:
        shutil.rmtree(workdir,ignore_errors=True)
        raise HTTPException(status_code=504,detail="Video rendering timed out")
    except Exception as exc:
        shutil.rmtree(workdir,ignore_errors=True)
        raise HTTPException(status_code=500,detail=str(exc))
