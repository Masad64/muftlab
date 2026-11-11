"""
Awaaz Jadoo  (آواز جادو)  —  Web Dashboard
Free voiceover tool wrapping Microsoft edge-tts neural voices.
Auto-splits long scripts into safe chunks => koi word limit nahi.
"""
import asyncio
import base64
import os
import re
import ssl
import tempfile
import threading
import webbrowser

import aiohttp
from aiohttp import web
import edge_tts

def _desktop_dir():
    # OneDrive wale PCs pe Desktop ~\Desktop pe nahi hota; sab cases handle karo
    cands = [
        os.path.join(os.path.expanduser("~"), "Desktop"),
        os.path.join(os.environ.get("USERPROFILE", ""), "OneDrive", "Desktop"),
        os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop"),
    ]
    for c in cands:
        if c and os.path.isdir(c):
            return c
    return os.path.expanduser("~")


CLOUD = bool(os.environ.get("PORT"))   # Render/hosting PORT env => cloud mode
if CLOUD:
    DESKTOP = os.path.join(tempfile.gettempdir(), "muftlab_out")
    try:
        os.makedirs(DESKTOP, exist_ok=True)
    except Exception:
        pass
else:
    DESKTOP = _desktop_dir()
HOSTNAME = "awaaz.studio"   # hosts file mein 127.0.0.1 pe mapped (local only)
MAX_CHARS = 1200

BASE = os.path.dirname(os.path.abspath(__file__))
CERT = os.path.join(BASE, "certs", "server.crt")
KEY = os.path.join(BASE, "certs", "server.key")

# Voice test ke liye chhote sample jumle (language ke hisaab se)
SAMPLES = {
    "ur": "السلام علیکم۔ یہ میری آواز کا ایک نمونہ ہے۔ آپ اِسے سن کر فیصلہ کر سکتے ہیں کہ یہ آواز آپ کی ویڈیو کے لیے مناسب ہے یا نہیں۔ شکریہ۔",
    "hi": "नमस्ते। यह मेरी आवाज़ का एक नमूना है। आप इसे सुनकर तय कर सकते हैं कि यह आवाज़ आपके वीडियो के लिए सही है या नहीं। धन्यवाद।",
    "ar": "مرحبًا. هذا مثال على صوتي. يمكنك الاستماع إليه لتقرر ما إذا كان هذا الصوت مناسبًا لمقطع الفيديو الخاص بك أم لا. شكرًا لك.",
    "en": "Hello there. This is a sample of my voice. You can listen to it and decide whether this voice is a good fit for your video or project. Thank you for listening.",
}


def sample_for(voice):
    lang = voice.split("-")[0].lower() if voice else "en"
    return SAMPLES.get(lang, SAMPLES["en"])


_PREVIEW_CACHE = {}   # voice -> mp3 bytes (taake dobara foran mile)
_VOICES = []


async def load_voices():
    global _VOICES
    if not _VOICES:
        _VOICES = await edge_tts.list_voices()
    return _VOICES


def split_text(text, max_chars=MAX_CHARS):
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    parts = re.split(r'(?<=[.!?۔؟])\s+', text)
    chunks, cur = [], ""
    for p in parts:
        if len(cur) + len(p) + 1 <= max_chars:
            cur = (cur + " " + p).strip()
        else:
            if cur:
                chunks.append(cur)
            if len(p) <= max_chars:
                cur = p
            else:
                cur = ""
                for w in p.split():
                    if len(cur) + len(w) + 1 <= max_chars:
                        cur = (cur + " " + w).strip()
                    else:
                        chunks.append(cur)
                        cur = w
    if cur:
        chunks.append(cur)
    return chunks


async def synth_chunk(ch_text, voice, rate, pitch, volume, want_subs, sem):
    async with sem:
        last_err = None
        for _ in range(3):
            try:
                com = edge_tts.Communicate(
                    ch_text, voice, rate=rate, pitch=pitch, volume=volume,
                    boundary="WordBoundary" if want_subs else "SentenceBoundary")
                audio = bytearray()
                bounds = []
                end = 0
                async for ch in com.stream():
                    if ch["type"] == "audio":
                        audio += ch["data"]
                    elif want_subs and ch["type"] == "WordBoundary":
                        bounds.append(dict(ch))
                        end = max(end, ch["offset"] + ch["duration"])
                return bytes(audio), bounds, end
            except Exception as e:
                last_err = e
                await asyncio.sleep(1)
        raise last_err


async def synthesize(text, voice, rate, pitch, volume, path, srt_path, want_subs):
    # Saare chunks EK SATH (parallel) banao -> bohot tez. Order gather se barqarar.
    chunks = split_text(text)
    sem = asyncio.Semaphore(4)   # 4 tukde ek saath (sabse tez + safe); 5 pe Microsoft rate-limit karta hai
    results = await asyncio.gather(
        *[synth_chunk(c, voice, rate, pitch, volume, want_subs, sem) for c in chunks])
    with open(path, "wb") as f:
        for audio, _bounds, _end in results:
            f.write(audio)
    if want_subs:
        try:
            sm = edge_tts.SubMaker()
            offset = 0
            for _audio, bounds, end in results:
                for b in bounds:
                    d = dict(b)
                    d["offset"] = d["offset"] + offset
                    sm.feed(d)
                offset += end
            with open(srt_path, "w", encoding="utf-8") as f:
                f.write(sm.get_srt())
        except Exception:
            pass


PAGE = r"""<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Muft Lab — Free AI Voice Over</title>
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/png" sizes="256x256" href="/icon.png">
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<style>
  :root{
    --bg:#0c1711; --bg2:#0f2017; --card:#12291c; --card2:#193524; --fg:#e7f3ec;
    --sub:#9bbaa7; --accent:#2f9e57; --accent2:#5bd38b; --line:#214a35;
    --good:#4ade80; --warn:#fbbf24; --bad:#fb7185; --shadow:0 10px 40px rgba(0,0,0,.4);
  }
  html[data-theme="light"]{
    --bg:#f1f7f3; --bg2:#e6f0ea; --card:#ffffff; --card2:#edf5f0; --fg:#122a1c;
    --sub:#4f6b5c; --accent:#1f7a43; --accent2:#0e9f6e; --line:#d6e6dd;
    --good:#059669; --warn:#b45309; --bad:#e11d48; --shadow:0 10px 30px rgba(30,90,60,.12);
  }
  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{margin:0;background:var(--bg);color:var(--fg);
       font-family:'Segoe UI',system-ui,-apple-system,sans-serif;line-height:1.5;}
  a{color:inherit;text-decoration:none}
  .grad{background:linear-gradient(135deg,var(--accent),var(--accent2));
        -webkit-background-clip:text;background-clip:text;color:transparent;}

  /* ---------- NAV ---------- */
  .nav{position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:16px;
       padding:12px 22px;background:color-mix(in srgb,var(--bg) 82%,transparent);
       backdrop-filter:blur(10px);border-bottom:1px solid var(--line);}
  .brand{font-weight:800;font-size:20px;letter-spacing:.3px;}
  .eai{background:#fff url("data:image/png;base64,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") center/78% no-repeat;border-radius:50%;
       box-shadow:0 4px 16px rgba(0,0,0,.28);
       border:2px solid color-mix(in srgb,var(--accent) 55%,#ffffff);}
  .nav .links{margin-left:auto;display:flex;gap:18px;align-items:center;}
  .nav .links a{color:var(--sub);font-size:14px;font-weight:600;}
  .nav .links a:hover{color:var(--fg)}
  .tgl{background:var(--card2);border:1px solid var(--line);color:var(--fg);
       width:40px;height:40px;border-radius:12px;cursor:pointer;font-size:18px;}
  @media(max-width:720px){ .nav .links a.hidemob{display:none} }

  /* ---------- HERO ---------- */
  .hero{position:relative;overflow:hidden;text-align:center;
        padding:46px 20px 18px;background:
        radial-gradient(900px 400px at 50% -10%, color-mix(in srgb,var(--accent) 28%,transparent), transparent 60%),
        var(--bg);}
  .hero h1{font-size:clamp(40px,8vw,76px);margin:0 0 6px;font-weight:900;letter-spacing:.5px;}
  .hero .slogan{font-size:clamp(16px,2.4vw,22px);color:var(--sub);font-weight:600;margin-bottom:6px;}
  .hero .sub{color:var(--sub);max-width:560px;margin:10px auto 26px;font-size:15px;}
  .cta{display:inline-flex;gap:10px;align-items:center;
       background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;
       padding:15px 30px;border-radius:14px;font-weight:800;font-size:16px;border:0;cursor:pointer;
       box-shadow:var(--shadow);}
  .cta:hover{transform:translateY(-2px);transition:.15s}
  /* mic + equalizer */
  .mic{font-size:58px;display:inline-block;animation:bob 2.2s ease-in-out infinite;filter:drop-shadow(0 6px 18px rgba(47,158,87,.55));}
  @keyframes bob{0%,100%{transform:translateY(0)}50%{transform:translateY(-12px)}}
  .eq{display:flex;gap:6px;justify-content:center;align-items:flex-end;height:60px;margin:8px 0 4px;}
  .eq span{width:8px;border-radius:6px;background:linear-gradient(var(--accent),var(--accent2));animation:bar 1s ease-in-out infinite;}
  .eq span:nth-child(1){animation-delay:0s} .eq span:nth-child(2){animation-delay:.15s}
  .eq span:nth-child(3){animation-delay:.3s} .eq span:nth-child(4){animation-delay:.45s}
  .eq span:nth-child(5){animation-delay:.6s} .eq span:nth-child(6){animation-delay:.3s}
  .eq span:nth-child(7){animation-delay:.1s}
  @keyframes bar{0%,100%{height:14px}50%{height:56px}}
  /* floating bubbles */
  .bub{position:absolute;font-size:26px;opacity:.5;animation:floaty 9s linear infinite;}
  @keyframes floaty{0%{transform:translateY(40px);opacity:0}15%{opacity:.6}
    100%{transform:translateY(-280px);opacity:0}}

  /* ---------- SECTION HEADINGS ---------- */
  section{padding:58px 20px}
  .sec-h{text-align:center;font-size:32px;font-weight:800;margin:0 0 6px}
  .sec-s{text-align:center;color:var(--sub);margin:0 0 30px}

  /* ---------- DASHBOARD ---------- */
  .dash{background:var(--bg2)}
  .dashwrap{display:grid;grid-template-columns:250px 1fr 370px;gap:22px;
            max-width:1480px;margin:0 auto;}
  @media(max-width:980px){ .dashwrap{grid-template-columns:1fr} }
  .panel{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px;box-shadow:var(--shadow)}
  .ptitle{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--sub);margin:0 0 12px;font-weight:700}
  /* left modes */
  .mode{display:flex;align-items:center;gap:10px;padding:11px 12px;border-radius:11px;cursor:pointer;
        color:var(--sub);font-weight:600;font-size:14px;margin-bottom:4px;border:1px solid transparent}
  .mode:hover{background:var(--card2)}
  .mode.active{background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--fg);border-color:color-mix(in srgb,var(--accent) 40%,transparent)}
  .mode .ico{font-size:18px}
  .mode .soon{margin-left:auto;font-size:10px;background:var(--card2);color:var(--sub);padding:2px 7px;border-radius:20px}
  /* center */
  label{display:block;font-size:13px;color:var(--sub);margin:0 0 6px}
  textarea,input[type=text],select{width:100%;background:var(--card2);color:var(--fg);
     border:1px solid var(--line);border-radius:11px;padding:12px;font-size:15px;font-family:inherit;outline:none}
  textarea{min-height:300px;resize:vertical;line-height:1.7;font-size:16px}
  textarea:focus,input:focus,select:focus{border-color:var(--accent)}
  .selvoice{display:inline-flex;align-items:center;gap:8px;background:var(--card2);
     border:1px solid var(--line);border-radius:30px;padding:6px 14px;font-size:13px;margin-bottom:12px}
  .selvoice b{color:var(--accent)}
  .sliders{display:flex;gap:16px;flex-wrap:wrap;margin:14px 0 4px}
  .sl{flex:1;min-width:150px}
  .sl .v{color:var(--accent);font-weight:700}
  input[type=range]{width:100%;accent-color:var(--accent)}
  .gen{width:100%;margin-top:14px;background:linear-gradient(135deg,var(--accent),var(--accent2));
       color:#fff;border:0;border-radius:13px;padding:15px;font-size:16px;font-weight:800;cursor:pointer}
  .gen:disabled{opacity:.6;cursor:default}
  #status{font-size:14px;margin-top:10px;min-height:20px}
  #player audio{width:100%;margin-top:12px}
  .dl{display:inline-block;margin-top:8px;color:var(--good);font-weight:700;font-size:14px}
  .count{font-size:12px;color:var(--sub);margin-top:6px;text-align:right}
  /* right: voices */
  .vlist{max-height:440px;overflow:auto;margin-top:4px;padding-right:4px}
  .vcard{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:11px;cursor:pointer;
         border:1px solid transparent;margin-bottom:5px}
  .vcard:hover{background:var(--card2)}
  .vcard.sel{background:color-mix(in srgb,var(--accent) 16%,transparent);border-color:color-mix(in srgb,var(--accent) 45%,transparent)}
  .vinfo{flex:1;min-width:0}
  .vname{display:block;font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .vgender{font-size:11px;color:var(--sub)}
  .vplay{flex:none;width:34px;height:34px;border-radius:9px;background:var(--card2);
         border:1px solid var(--line);color:var(--fg);cursor:pointer;font-size:13px}
  .vplay:hover{border-color:var(--accent);color:var(--accent)}
  .outbox{margin-top:14px;padding-top:14px;border-top:1px dashed var(--line)}
  .chk{display:flex;align-items:center;gap:9px;font-size:14px;margin-top:10px;cursor:pointer}
  .chk input{width:18px;height:18px;accent-color:var(--accent)}

  /* ---------- HOW TO ---------- */
  .steps{max-width:660px;margin:0 auto;display:grid;grid-template-columns:repeat(2,1fr);gap:16px}
  @media(max-width:560px){ .steps{grid-template-columns:1fr} }
  .step{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px}
  .step .n{width:34px;height:34px;border-radius:11px;display:flex;align-items:center;justify-content:center;
           font-weight:800;color:#fff;background:linear-gradient(135deg,var(--accent),var(--accent2));margin-bottom:10px}
  .step h4{font-size:15px}
  .step h4{margin:0 0 6px;font-size:16px}
  .step p{margin:0;color:var(--sub);font-size:14px}

  /* ---------- COMMUNITY ---------- */
  .community{background:var(--bg2)}
  .cwrap{max-width:900px;margin:0 auto;display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}
  .ccard{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:24px;text-align:center;box-shadow:var(--shadow)}
  .ccard .ci{font-size:40px;margin-bottom:10px}
  .ccard h4{margin:0 0 6px}
  .ccard p{color:var(--sub);font-size:14px;margin:0 0 16px}
  .ccard a{display:inline-block;padding:11px 22px;border-radius:11px;font-weight:700;color:#fff;
           background:linear-gradient(135deg,var(--accent),var(--accent2))}
  footer{text-align:center;padding:26px;color:var(--sub);font-size:13px;border-top:1px solid var(--line)}

  /* =================== NEUMORPHIC (Soft UI) SKIN — Black & White =================== */
  :root{
    --bg:#25262a; --bg2:#222327; --card:#25262a; --card2:#25262a; --fg:#e6e8ee;
    --sub:#9499a3; --line:transparent; --accent:#cfd3db; --accent2:#cfd3db;
    --sd:#1b1c1f; --sl:#2f3136; --good:#cfd3db; --warn:#d8c07a; --bad:#e39aa0; --fill:#8a9bff;
  }
  html[data-theme="light"]{
    --bg:#e7e8ec; --bg2:#e3e4e8; --card:#e7e8ec; --card2:#e7e8ec; --fg:#3a3e46;
    --sub:#8b909b; --line:transparent; --accent:#4a4e57; --accent2:#4a4e57;
    --sd:#c3c5cc; --sl:#ffffff; --good:#4a4e57; --warn:#7a6a3a; --bad:#9a4a4a; --fill:#5b74e8;
  }
  body{background:var(--bg)}
  .nav{background:var(--bg);backdrop-filter:none;border-bottom:none;box-shadow:0 5px 20px var(--sd)}
  .brand{display:flex;align-items:center;gap:11px}
  .navbadge{width:38px;height:38px;border-radius:50%;background:var(--bg);display:inline-flex;align-items:center;justify-content:center;flex:none;
     box-shadow:3px 3px 7px var(--sd),-3px -3px 7px var(--sl)}
  .navbadge svg{width:21px;height:21px;fill:var(--fill);stroke:var(--fill)}
  .hero{background:var(--bg)}
  .dash,.community{background:var(--bg2)}
  .dash{padding-top:30px}
  .panel,.ccard,.step{background:var(--bg);border:none;border-radius:22px;
     box-shadow:8px 8px 18px var(--sd),-8px -8px 18px var(--sl)}
  textarea,input[type=text],select,#lang,#fname{background:var(--bg);border:none;color:var(--fg);
     border-radius:14px;box-shadow:inset 4px 4px 8px var(--sd),inset -4px -4px 8px var(--sl)}
  textarea:focus,input:focus,select:focus{box-shadow:inset 5px 5px 10px var(--sd),inset -5px -5px 10px var(--sl)}
  .gen,.cta{background:var(--bg);color:var(--fg);border-radius:16px;
     box-shadow:6px 6px 14px var(--sd),-6px -6px 14px var(--sl)}
  .gen:hover,.cta:hover{box-shadow:4px 4px 10px var(--sd),-4px -4px 10px var(--sl)}
  .gen:active{box-shadow:inset 4px 4px 9px var(--sd),inset -4px -4px 9px var(--sl)}
  .tgl,.vplay{background:var(--bg);border:none;color:var(--fg);
     box-shadow:4px 4px 9px var(--sd),-4px -4px 9px var(--sl)}
  .tgl:hover,.vplay:hover{box-shadow:2px 2px 5px var(--sd),-2px -2px 5px var(--sl);color:var(--fg)}
  .mode{border:none}
  .mode:hover{background:var(--bg);box-shadow:3px 3px 7px var(--sd),-3px -3px 7px var(--sl)}
  .mode.active{background:var(--bg);color:var(--fg);box-shadow:inset 3px 3px 7px var(--sd),inset -3px -3px 7px var(--sl)}
  .vcard{border:none}
  .vcard:hover{background:var(--bg);box-shadow:3px 3px 7px var(--sd),-3px -3px 7px var(--sl)}
  .vcard.sel{background:var(--bg);box-shadow:inset 3px 3px 7px var(--sd),inset -3px -3px 7px var(--sl)}
  .selvoice{background:var(--bg);border:none;box-shadow:inset 3px 3px 6px var(--sd),inset -3px -3px 6px var(--sl)}
  .ccard a{background:var(--bg);color:var(--fg);box-shadow:5px 5px 11px var(--sd),-5px -5px 11px var(--sl)}
  .ccard a:hover{box-shadow:3px 3px 8px var(--sd),-3px -3px 8px var(--sl)}
  .step .n{background:var(--bg);color:var(--fg);box-shadow:inset 3px 3px 6px var(--sd),inset -3px -3px 6px var(--sl)}
  .outbox{border-top:none}
  .grad{background:none;-webkit-background-clip:border-box;background-clip:border-box;-webkit-text-fill-color:currentColor;color:var(--fg)}
  .eq span{background:var(--sub)}
  .mic{filter:drop-shadow(0 6px 14px var(--sd))}
  .bub{filter:grayscale(1);opacity:.22}
  input[type=range]{-webkit-appearance:none;appearance:none;height:12px;border-radius:12px;background:var(--bg);
     box-shadow:inset 2px 2px 5px var(--sd),inset -2px -2px 5px var(--sl)}
  input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:20px;height:20px;border-radius:50%;
     background:var(--bg);box-shadow:3px 3px 6px var(--sd),-3px -3px 6px var(--sl);cursor:pointer}
  .eai{border:none;box-shadow:5px 5px 11px var(--sd),-5px -5px 11px var(--sl)}
  .mode .soon{background:var(--bg);box-shadow:inset 2px 2px 4px var(--sd),inset -2px -2px 4px var(--sl)}
  /* community = ek single box, course center, faded dividers */
  .cwrap{display:block}
  .cbox{display:flex;flex-wrap:wrap;background:var(--bg);border-radius:24px;max-width:660px;margin:0 auto;
     box-shadow:9px 9px 20px var(--sd),-9px -9px 20px var(--sl)}
  .ccell{position:relative;flex:1 1 0;min-width:150px;text-align:center;padding:26px 12px}
  .ccell:nth-child(1){order:1} .ccell:nth-child(2){order:3} .ccell:nth-child(3){order:2}
  .ccell .ci{font-size:40px;margin-bottom:10px}
  .ccell h4{margin:0 0 6px}
  .ccell p{color:var(--sub);font-size:14px;margin:0 0 16px}
  .ccell a{display:inline-block;padding:11px 22px;border-radius:12px;font-weight:700;color:var(--fg);
     background:var(--bg);box-shadow:5px 5px 11px var(--sd),-5px -5px 11px var(--sl)}
  .ccell a:hover{box-shadow:3px 3px 8px var(--sd),-3px -3px 8px var(--sl)}
  .ccell+.ccell::before{content:"";position:absolute;left:0;top:18%;height:64%;width:1px;
     background:linear-gradient(to bottom,transparent,var(--sub),transparent);opacity:.2}
  @media(max-width:640px){
    .cbox{flex-direction:column}
    .ccell+.ccell::before{top:0;left:18%;width:64%;height:1px;
       background:linear-gradient(to right,transparent,var(--sub),transparent)}
  }
  /* history rows */
  #histBox{max-height:210px;overflow:auto}
  .hrow{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:10px;margin-bottom:6px;font-size:12px;
     background:var(--bg);box-shadow:inset 2px 2px 5px var(--sd),inset -2px -2px 5px var(--sl)}
  .hname{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--fg)}
  .hlink{color:var(--fill);font-weight:700;text-decoration:none;padding:2px 4px}
  .hlink:hover{text-decoration:underline}
  .hdel{flex:none;width:22px;height:22px;border-radius:6px;border:none;cursor:pointer;background:var(--bg);color:var(--sub);
     box-shadow:2px 2px 4px var(--sd),-2px -2px 4px var(--sl)}
  .hdel:hover{color:var(--bad)}
</style>
</head>
<body>

<!-- NAV -->
<div class="nav">
  <div class="brand"><span class="navbadge"><svg viewBox="0 0 48 48" aria-hidden="true"><rect x="20" y="9" width="8" height="16" rx="4"/><path d="M14 22 a10 10 0 0 0 20 0" fill="none" stroke-width="2.6" stroke-linecap="round"/><line x1="24" y1="32" x2="24" y2="38" stroke-width="2.6" stroke-linecap="round"/><line x1="18" y1="38" x2="30" y2="38" stroke-width="2.6" stroke-linecap="round"/></svg></span><span class="grad">Muft Lab</span></div>
  <div class="links">
    <a href="#dashboard" data-i18n="nav_tool">Tool</a>
    <a href="#imagestudio">Image Jadoo</a>
    <a href="#how" class="hidemob" data-i18n="nav_how">How to use</a>
    <a href="#community" class="hidemob" data-i18n="nav_comm">Community</a>
    <select id="uiLang" title="Language" style="width:auto;background:var(--card2);color:var(--fg);border:1px solid var(--line);border-radius:10px;padding:8px 10px;font-size:13px;font-weight:600;cursor:pointer">
      <option value="en">English</option>
      <option value="ur">Roman Urdu</option>
    </select>
    <button class="tgl" id="themeBtn" title="Theme">&#9790;</button>
  </div>
</div>

<!-- HERO -->
<section class="hero" id="top">
  <span class="bub" style="left:12%;animation-delay:0s">&#127908;</span>
  <span class="bub" style="left:26%;animation-delay:2s">&#128266;</span>
  <span class="bub" style="left:70%;animation-delay:1s">&#127925;</span>
  <span class="bub" style="left:84%;animation-delay:3s">&#128483;</span>
  <span class="bub" style="left:50%;animation-delay:4s">&#127911;</span>
  <div class="mic">&#127908;</div>
  <div class="eq"><span></span><span></span><span></span><span></span><span></span><span></span><span></span></div>
  <h1><span class="grad">Muft Lab</span></h1>
  <div class="slogan" data-i18n="slogan">Jahan lafz boltey hain</div>
  <p class="sub" data-i18n="hero_sub">Free AI Voice Over Studio &mdash; 300+ real voices, 100+ languages. Script likho, voice chuno, download karo.</p>
</section>

<!-- DASHBOARD -->
<section class="dash" id="dashboard">
  <h2 class="sec-h" data-i18n-html="dash_h">Voice Over <span class="grad">Studio</span></h2>
  <p class="sec-s" data-i18n="dash_s">Apni script se professional voiceover banao &mdash; seconds mein.</p>
  <div class="dashwrap">

    <!-- LEFT: modes -->
    <div class="panel">
      <p class="ptitle" data-i18n="left_tools">Tools</p>
      <div class="mode active"><span class="ico">&#128483;</span> <span data-i18n="left_tts">Text to Speech</span></div>
      <div class="mode" id="vlibBtn" onclick="toggleVoiceLib()"><span class="ico">&#127908;</span> <span data-i18n="left_vlib">Voice Library</span></div>
      <div class="mode" style="cursor:default;opacity:.7"><span class="ico">&#127760;</span> <span data-i18n="left_translate">Translate</span> <span class="soon" data-i18n="soon">Soon</span></div>
      <div class="mode" style="cursor:default;opacity:.7"><span class="ico">&#129302;</span> <span data-i18n="left_clone">Voice Clone</span> <span class="soon" data-i18n="soon">Soon</span></div>
    </div>

    <!-- CENTER: script + generate -->
    <div class="panel">
      <p class="ptitle" data-i18n="center_title">Your Script</p>
      <div class="selvoice" onclick="toggleVoiceLib()" style="cursor:pointer" title="Click to change voice"><span data-i18n="voice_label">Voice:</span> <b id="selName">...</b> <span style="margin-left:4px">&#9662;</span></div>
      <textarea id="text" data-i18n-ph="script_ph" placeholder="Yahan apni script paste karo... (jitni lambi chahe)">Assalam o alaikum, yeh meri pehli AI voiceover hai.</textarea>
      <div class="count" id="count"></div>
      <div class="sliders">
        <div class="sl"><label><span data-i18n="lbl_speed">Speed</span>: <span class="v" id="sv">0</span>%</label><input type="range" id="speed" min="-50" max="50" value="0"></div>
        <div class="sl"><label><span data-i18n="lbl_pitch">Pitch</span>: <span class="v" id="pv">0</span>Hz</label><input type="range" id="pitch" min="-20" max="20" value="0"></div>
        <div class="sl"><label><span data-i18n="lbl_volume">Volume</span>: <span class="v" id="vv">0</span>%</label><input type="range" id="vol" min="-50" max="50" value="0"></div>
      </div>
      <button class="gen" id="gen" data-i18n-html="gen_btn">&#9889; Generate &amp; Save</button>
      <div id="status"></div>
      <div id="player"></div>
    </div>

    <!-- RIGHT: language + voices + output -->
    <div class="panel">
      <p class="ptitle" data-i18n="right_lang">Language</p>
      <select id="lang"></select>
      <div id="voicePanel" style="display:none">
        <p class="ptitle" style="margin-top:16px" data-i18n="right_chars">Characters</p>
        <div class="vlist" id="voiceList"></div>
      </div>
      <div class="outbox">
        <p class="ptitle" data-i18n="output">Output</p>
        <label data-i18n="fname_label">File name</label>
        <input type="text" id="fname" value="voiceover">
        <label class="chk"><input type="checkbox" id="subs"> <span data-i18n="subs_label">Subtitles (.srt) bhi banao</span></label>
        <p class="ptitle" style="margin-top:18px" data-i18n="history">History</p>
        <div id="histBox"></div>
      </div>
    </div>
  </div>
  <audio id="prev" style="display:none"></audio>
</section>

<!-- HOW TO -->
<section class="how" id="how">
  <h2 class="sec-h" data-i18n-html="how_h">How to <span class="grad">use</span></h2>
  <p class="sec-s" data-i18n="how_s">Sirf 4 asaan steps.</p>
  <div class="steps">
    <div class="step"><div class="n">1</div><h4 data-i18n="s1t">Language & Voice chuno</h4><p data-i18n="s1d">Right side se apni language aur character choose karo. &#9654; dabakar voice pehle sun lo.</p></div>
    <div class="step"><div class="n">2</div><h4 data-i18n="s2t">Script likho</h4><p data-i18n="s2d">Center box mein apni script paste karo &mdash; chhoti ho ya lambi, koi word limit nahi.</p></div>
    <div class="step"><div class="n">3</div><h4 data-i18n="s3t">Adjust karo</h4><p data-i18n="s3d">Chaho to Speed, Pitch, Volume set karo. Subtitles (.srt) chahiye to tick kar do.</p></div>
    <div class="step"><div class="n">4</div><h4 data-i18n="s4t">Generate & Download</h4><p data-i18n="s4d">Generate dabao &mdash; mp3 ban kar aa jayega, play karo ya download kar lo.</p></div>
  </div>
</section>

<!-- IMAGE STUDIO (TEST) -->
<section class="dash" id="imagestudio">
  <h2 class="sec-h">Image <span class="grad">Jadoo</span></h2>
  <p class="sec-s">Master prompt + SRT ek jagah daalo &mdash; neeche image prompts ban jayenge.</p>
  <div style="display:grid;grid-template-columns:300px 1fr;gap:18px;max-width:1120px;margin:0 auto;align-items:start" class="imgwrap">

    <!-- LEFT: settings + bulk button -->
    <div class="panel">
      <p class="ptitle">Settings</p>
      <label>Provider</label>
      <select id="imgProvider" onchange="fillModels();autoLoadModels()">
        <option value="gemini">Gemini</option>
        <option value="openrouter">OpenRouter</option>
      </select>
      <label style="margin-top:12px;display:block">Model</label>
      <select id="imgModel" onchange="onModelChange()"></select>
      <input type="text" id="imgModelCustom" placeholder="model id type karo (e.g. deepseek/deepseek-chat)" style="display:none;margin-top:8px">
      <a href="#" id="imgLoadModels" onclick="loadGModels(event)" style="display:none;font-size:12px;color:var(--acc,#8a9bff);margin-top:6px">&#8635; Meri key ke available models load karo</a>
      <label style="margin-top:12px;display:block">API Key</label>
      <input type="password" id="imgKey" placeholder="apni API key paste karo" onblur="autoLoadModels()" onchange="autoLoadModels()">
      <button type="button" id="imgTest" onclick="testKey()" style="margin-top:8px;font-size:13px;padding:7px 14px;background:var(--card2);color:var(--fg);border:1px solid var(--line);border-radius:8px;cursor:pointer">&#10003; Test API Key</button>
      <div id="imgKeyStatus" style="font-size:12px;margin-top:6px"></div>
      <p style="font-size:12px;color:var(--sub);margin-top:8px">API key sirf aap ke browser mein save hoti hai.</p>
      <div style="border-top:1px solid var(--line);margin:18px 0 14px"></div>
      <p class="ptitle">Bulk Image Generator</p>
      <p style="font-size:13px;color:var(--sub);line-height:1.6">Neeche bane prompts <b>Copy</b> karke is tool mein paste karo. Pehli baar <b>Google login</b> &mdash; ek hi baar.</p>
      <a class="gen" href="https://gemini.google.com/share/5d7cb64e5809" onclick="openBulkTool(event)" rel="noopener" style="text-decoration:none;display:block;text-align:center">&#128444;&#65039; Open Bulk Image Generator</a>
    </div>

    <!-- RIGHT: master prompt + SRT -> prompts -->
    <div class="panel">
      <p class="ptitle">Master Prompt</p>
      <textarea id="imgUniversal" style="min-height:90px" placeholder="Apna master prompt yahan paste karo — jiske according SRT se image prompts banenge (style, look, rules...)">cinematic, dramatic lighting, highly detailed, 16:9</textarea>
      <p class="ptitle" style="margin-top:14px">SRT / Script</p>
      <textarea id="imgSrt" style="min-height:140px" placeholder="apni SRT file ka text ya script yahan paste karo"></textarea>
      <button class="gen" id="imgGen" onclick="generatePrompts()">&#9889; Generate Image Prompts</button>
      <div id="imgStatus"></div>
      <p class="ptitle" style="margin-top:14px">Image Prompts <button type="button" onclick="copyPrompts()" style="float:right;font-size:12px;padding:4px 10px;background:var(--card2);color:var(--fg);border:1px solid var(--line);border-radius:8px;cursor:pointer">Copy All</button></p>
      <textarea id="imgOut" readonly style="min-height:170px" placeholder="yahan image prompts ban ke aayenge..."></textarea>
    </div>
  </div>
</section>

<!-- COMMUNITY -->
<section class="community" id="community">
  <h2 class="sec-h" data-i18n-html="comm_h">Get <span class="grad">150+ Hours</span> Training</h2>
  <p class="sec-s" data-i18n="comm_s">Learn YT Automation and AI</p>
  <div class="cwrap">
    <div class="cbox">
    <div class="ccell"><div class="ci"><svg viewBox="0 0 24 24" width="48" height="48" fill="#25D366"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.885-9.885 9.885M20.52 3.449C18.24 1.245 15.24 0 12.045 0 5.463 0 .104 5.358.101 11.892c0 2.096.549 4.142 1.595 5.945L0 24l6.335-1.652a11.882 11.882 0 005.71 1.447h.006c6.585 0 11.946-5.359 11.949-11.893a11.821 11.821 0 00-3.481-8.413"/></svg></div><h4 data-i18n="wa_t">WhatsApp</h4><p data-i18n="wa_s">Official Number</p><a href="https://wa.me/923046012916" target="_blank" rel="noopener" data-i18n="wa_btn">Contact Us</a></div>
    <div class="ccell"><div class="ci"><svg viewBox="0 0 24 24" width="48" height="48" fill="#1877F2"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z"/></svg></div><h4 data-i18n="fb_t">Facebook Group</h4><p data-i18n="fb_s">Creators community</p><a href="https://www.facebook.com/groups/546405077781054" target="_blank" rel="noopener" data-i18n="fb_btn">Join Group</a></div>
    <div class="ccell"><div class="ci"><span class="eai" style="width:54px;height:54px;display:inline-block"></span></div><h4 data-i18n="course_t">Course Outlines</h4><p data-i18n="course_s">Join us</p><a href="https://portal.emergeai.io/en/course-detail/course2-0/44396/" target="_blank" rel="noopener" data-i18n="course_btn">Visit</a></div>
    </div>
  </div>
</section>

<footer data-i18n-html="footer">&copy; Muft Lab &mdash; Free AI Voice Over. Powered by edge-tts.</footer>

<script>
const $ = id => document.getElementById(id);
let VOICES=[], MAP={}, selectedVoice=null;

/* ---- Image Studio (TEST) ---- */
const MODELS = {
  gemini: ["gemini-1.5-flash","gemini-2.0-flash-lite","gemini-2.0-flash","gemini-1.5-flash-8b"],
  openrouter: ["deepseek/deepseek-chat","google/gemini-2.0-flash-exp:free","google/gemini-flash-1.5-8b"]
};
function fillModels(saved){
  const prov=$('imgProvider').value, sel=$('imgModel');
  if(!sel) return;
  sel.innerHTML='';
  (MODELS[prov]||[]).forEach(m=>{const o=document.createElement('option');o.value=m;o.textContent=m;sel.appendChild(o);});
  const c=document.createElement('option');c.value='__custom__';c.textContent='Custom (type model id)';sel.appendChild(c);
  if(saved){ sel.value=saved; if(sel.value!==saved){ sel.value='__custom__'; $('imgModelCustom').value=saved; } }
  const ln=$('imgLoadModels'); if(ln) ln.style.display = (prov==='gemini')?'inline-block':'none';
  onModelChange();
}
function testKey(){
  const provider=$('imgProvider').value, apikey=$('imgKey').value.trim(), model=_curModel();
  const st=$('imgKeyStatus'), btn=$('imgTest');
  if(!apikey){ st.innerHTML='<span style="color:#f38ba8">API key daalo.</span>'; return; }
  if(!model){ st.innerHTML='<span style="color:#f38ba8">Model select karo.</span>'; return; }
  const old=btn.innerHTML; btn.disabled=true; btn.innerHTML='Test ho raha...';
  st.innerHTML='<span style="color:#f9e2af">Test ho raha hai...</span>';
  fetch('/testkey',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider,model,apikey})})
   .then(r=>r.json()).then(j=>{
     if(j.ok){ st.innerHTML='<span style="color:#a6e3a1">&#10003; Key chal rahi hai! Ab prompts bana sakte ho.</span>'; }
     else{ st.innerHTML='<span style="color:#f38ba8">&#10007; '+(j.error||'Key kaam nahi kar rahi')+'</span>'; }
   }).catch(err=>{ st.innerHTML='<span style="color:#f38ba8">Error: '+err+'</span>'; })
   .finally(()=>{ btn.disabled=false; btn.innerHTML=old; });
}
function loadGModels(e, silent){
  if(e) e.preventDefault();
  const key=$('imgKey').value.trim(), st=$('imgKeyStatus');
  if(!key){ if(!silent) st.innerHTML='<span style="color:#f38ba8">Pehle Gemini API key daalo.</span>'; return; }
  if(!silent) st.innerHTML='<span style="color:#f9e2af">Models load ho rahe hain...</span>';
  fetch('/gmodels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({apikey:key})})
   .then(r=>r.json()).then(j=>{
     if(j.ok && j.models && j.models.length){
       MODELS.gemini=j.models; fillModels(localStorage.getItem('ml_imgmodel'));
       try{localStorage.setItem('ml_gmodels',JSON.stringify(j.models));}catch(x){}
       if(!silent) st.innerHTML='<span style="color:#a6e3a1">'+j.models.length+' models load ho gaye.</span>';
     } else if(!silent){ st.innerHTML='<span style="color:#f38ba8">'+(j.error||'koi model nahi mila')+'</span>'; }
   }).catch(err=>{ if(!silent) st.innerHTML='<span style="color:#f38ba8">Error: '+err+'</span>'; });
}
function autoLoadModels(){
  if($('imgProvider').value==='gemini' && $('imgKey').value.trim()) loadGModels(null, true);
}
function onModelChange(){
  const cu=$('imgModelCustom');
  if(cu) cu.style.display = ($('imgModel').value==='__custom__')?'block':'none';
}
function _curModel(){
  const m=$('imgModel').value;
  return (m==='__custom__') ? $('imgModelCustom').value.trim() : m;
}
function _imgLoad(){
  try{
    const k=localStorage.getItem('ml_imgkey'); if(k&&$('imgKey')) $('imgKey').value=k;
    const p=localStorage.getItem('ml_imgprov'); if(p&&$('imgProvider')) $('imgProvider').value=p;
    const u=localStorage.getItem('ml_imguni'); if(u&&$('imgUniversal')) $('imgUniversal').value=u;
    const sr=localStorage.getItem('ml_imgsrt'); if(sr&&$('imgSrt')) $('imgSrt').value=sr;
    const o=localStorage.getItem('ml_imgout'); if(o&&$('imgOut')) $('imgOut').value=o;
    try{ const gm=localStorage.getItem('ml_gmodels'); if(gm){ const arr=JSON.parse(gm); if(arr&&arr.length) MODELS.gemini=arr; } }catch(x){}
    fillModels(localStorage.getItem('ml_imgmodel'));
    autoLoadModels();
  }catch(e){ try{fillModels();}catch(x){} }
}
async function generatePrompts(){
  const btn=$('imgGen'), st=$('imgStatus'), out=$('imgOut');
  const provider=$('imgProvider').value, apikey=$('imgKey').value.trim(), model=_curModel();
  const universal=$('imgUniversal').value.trim(), srt=$('imgSrt').value.trim();
  try{
    localStorage.setItem('ml_imgkey',apikey);
    localStorage.setItem('ml_imgprov',provider);
    localStorage.setItem('ml_imgmodel',model);
    localStorage.setItem('ml_imguni',universal);
  }catch(e){}
  if(!apikey){ st.innerHTML='<span style="color:#f38ba8">API key daalo.</span>'; return; }
  if(!model){ st.innerHTML='<span style="color:#f38ba8">Model select karo.</span>'; return; }
  if(!srt){ st.innerHTML='<span style="color:#f38ba8">SRT ya script daalo.</span>'; return; }
  btn.disabled=true; const old=btn.innerHTML; btn.innerHTML='Ban raha hai...';
  st.innerHTML='<span style="color:#f9e2af">Prompts ban rahe hain, thoda ruko...</span>';
  try{
    const r=await fetch('/genprompts',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({provider,model,apikey,universal,srt})});
    const j=await r.json();
    if(j.ok){ out.value=j.prompts; try{localStorage.setItem('ml_imgsrt',srt);localStorage.setItem('ml_imgout',j.prompts);}catch(x){} st.innerHTML='<span style="color:#a6e3a1">Ho gaya &mdash; '+j.count+' prompts. Ab Copy karke bulk tool mein paste karo.</span>'; }
    else{ st.innerHTML='<span style="color:#f38ba8">Error: '+(j.error||'fail')+'</span>'; }
  }catch(e){ st.innerHTML='<span style="color:#f38ba8">Error: '+e+'</span>'; }
  btn.disabled=false; btn.innerHTML=old;
}
const CANVAS_URL="https://gemini.google.com/share/5d7cb64e5809";
function openBulkTool(e){
  if(e) e.preventDefault();
  try{
    if(window.pywebview && window.pywebview.api && window.pywebview.api.open_canvas){
      window.pywebview.api.open_canvas();   // app ke apne window mein kholo
      return;
    }
  }catch(err){}
  window.location.href = CANVAS_URL;         // USI tab mein kholo (naya tab nahi)
}
function copyPrompts(){
  const out=$('imgOut'); if(!out.value){ return; }
  out.select();
  try{ navigator.clipboard.writeText(out.value); }catch(e){ document.execCommand('copy'); }
  const st=$('imgStatus'); if(st) st.innerHTML='<span style="color:#a6e3a1">Copy ho gaya!</span>';
}
document.addEventListener('DOMContentLoaded',_imgLoad);

/* ---- i18n (English / Roman Urdu) ---- */
const I18N = {
 en:{
  nav_tool:"Tool", nav_how:"How to use", nav_comm:"Community",
  slogan:"Where words speak",
  hero_sub:"Free AI Voice Over Studio — 300+ real voices, 100+ languages. Write your script, pick a voice, download.",
  dash_h:"Voice Over <span class='grad'>Studio</span>",
  dash_s:"Make professional voiceovers from your script — in seconds.",
  left_tools:"Tools", left_tts:"Text to Speech", left_vlib:"Voice Library",
  left_translate:"Translate", left_clone:"Voice Clone", soon:"Soon",
  center_title:"Your Script", voice_label:"Voice:",
  script_ph:"Paste your script here... (any length)",
  lbl_speed:"Speed", lbl_pitch:"Pitch", lbl_volume:"Volume",
  gen_btn:"⚡ Generate & Save", generating:"Generating...",
  right_lang:"Language", right_chars:"Characters", output:"Output",
  fname_label:"File name", subs_label:"Also make Subtitles (.srt)",
  how_h:"How to <span class='grad'>use</span>", how_s:"Just 4 easy steps.",
  s1t:"Pick Language & Voice", s1d:"Choose your language and character on the right. Press ▶ to hear a voice first.",
  s2t:"Write your script", s2d:"Paste your script in the center box — short or long, no word limit.",
  s3t:"Adjust", s3d:"Optionally set Speed, Pitch, Volume. Tick Subtitles (.srt) if you want captions.",
  s4t:"Generate & Download", s4d:"Press Generate — the mp3 is ready, play it or download it.",
  comm_h:"Get <span class='grad'>150+ Hours</span> Training", comm_s:"Learn YT Automation and AI",
  wa_t:"WhatsApp", wa_s:"Official Number", wa_btn:"Contact Us",
  fb_t:"Facebook Group", fb_s:"Creators community", fb_btn:"Join Group",
  course_t:"Course Outlines", course_s:"Join us", course_btn:"Visit",
  footer:"&copy; Muft Lab — Free AI Voice Over. Powered by edge-tts.",
  characters:"characters", words:"words", test_tip:"Click to test this voice",
  st_script:"Write a script first.", st_voice:"Choose a voice.",
  st_gen:"Generating audio...", st_done:"Done: ", st_done2:"  (saved to Desktop)",
  st_err:"Error (try again): ", st_testfail:"Voice test failed - try again",
  st_voicesfail:"Voices failed to load (check internet): ", dl:"⬇ Download mp3", dl_srt:"⬇ Download .srt", saved_srt:" + .srt", history:"History", no_history:"No voiceovers yet.", remove:"Remove", open_folder:"Open folder (Desktop)"
 },
 ur:{
  nav_tool:"Tool", nav_how:"Kaise istemal karein", nav_comm:"Community",
  slogan:"Jahan lafz boltey hain",
  hero_sub:"Free AI Voice Over Studio — 300+ real voices, 100+ languages. Script likho, voice chuno, download karo.",
  dash_h:"Voice Over <span class='grad'>Studio</span>",
  dash_s:"Apni script se professional voiceover banao — seconds mein.",
  left_tools:"Tools", left_tts:"Text to Speech", left_vlib:"Voice Library",
  left_translate:"Translate", left_clone:"Voice Clone", soon:"Jald",
  center_title:"Apni Script", voice_label:"Voice:",
  script_ph:"Yahan apni script paste karo... (jitni lambi chahe)",
  lbl_speed:"Speed", lbl_pitch:"Pitch", lbl_volume:"Volume",
  gen_btn:"⚡ Awaaz Banao", generating:"Ban rahi hai...",
  right_lang:"Language", right_chars:"Characters", output:"Output",
  fname_label:"File ka naam", subs_label:"Subtitles (.srt) bhi banao",
  how_h:"Kaise <span class='grad'>istemal</span> karein", how_s:"Sirf 4 asaan steps.",
  s1t:"Language & Voice chuno", s1d:"Right side se apni language aur character choose karo. ▶ dabakar voice pehle sun lo.",
  s2t:"Script likho", s2d:"Center box mein apni script paste karo — chhoti ho ya lambi, koi word limit nahi.",
  s3t:"Adjust karo", s3d:"Chaho to Speed, Pitch, Volume set karo. Subtitles (.srt) chahiye to tick kar do.",
  s4t:"Generate & Download", s4d:"Generate dabao — mp3 ban kar aa jayega, play karo ya download kar lo.",
  comm_h:"Hasil karo <span class='grad'>150+ Hours</span> Training", comm_s:"YT Automation aur AI seekho",
  wa_t:"WhatsApp", wa_s:"Official Number", wa_btn:"Rabta Karein",
  fb_t:"Facebook Group", fb_s:"Creators community", fb_btn:"Group Join Karein",
  course_t:"Course Outlines", course_s:"Join us", course_btn:"Visit",
  footer:"&copy; Muft Lab — Free AI Voice Over. Powered by edge-tts.",
  characters:"characters", words:"words", test_tip:"Voice test karne ke liye click karo",
  st_script:"Pehle script likho.", st_voice:"Voice choose karo.",
  st_gen:"Aawaz ban rahi hai...", st_done:"Ban gayi: ", st_done2:"  (Desktop pe save)",
  st_err:"Error (dobara try karo): ", st_testfail:"Voice test fail - dobara try karo",
  st_voicesfail:"Voices load fail (internet check karo): ", dl:"⬇ Download mp3", dl_srt:"⬇ Download .srt", saved_srt:" + .srt", history:"History", no_history:"Abhi koi voiceover nahi.", remove:"Remove", open_folder:"Folder kholo (Desktop)"
 }
};
let curLang="ur";
function t(k){ var d=I18N[curLang]||I18N.ur; return (d[k]!=null)?d[k]:(I18N.en[k]!=null?I18N.en[k]:k); }
function setUiLang(lang){
  if(!I18N[lang]) lang="ur"; curLang=lang;
  document.querySelectorAll("[data-i18n]").forEach(function(el){var k=el.getAttribute("data-i18n"); if(I18N[lang][k]!=null) el.textContent=I18N[lang][k];});
  document.querySelectorAll("[data-i18n-html]").forEach(function(el){var k=el.getAttribute("data-i18n-html"); if(I18N[lang][k]!=null) el.innerHTML=I18N[lang][k];});
  document.querySelectorAll("[data-i18n-ph]").forEach(function(el){var k=el.getAttribute("data-i18n-ph"); if(I18N[lang][k]!=null) el.placeholder=I18N[lang][k];});
  try{localStorage.setItem("ml_lang",lang);}catch(e){}
  if(typeof updCount==="function") updCount();
}

/* ---- theme ---- */
(function(){ try{ const t=localStorage.getItem('aj_theme'); if(t) document.documentElement.setAttribute('data-theme',t);}catch(e){}
  updThemeIcon(); })();
function updThemeIcon(){ const d=document.documentElement.getAttribute('data-theme')==='dark'; $('themeBtn').innerHTML= d? '&#9728;' : '&#9790;'; }
$('themeBtn').onclick=()=>{ const cur=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';
  document.documentElement.setAttribute('data-theme',cur); try{localStorage.setItem('aj_theme',cur);}catch(e){} updThemeIcon(); };

/* ---- voices ---- */
function groupLangs(){ const m={}; VOICES.forEach(v=>{(m[v.lang]=m[v.lang]||[]).push(v);}); return m; }
async function init(){
  const r=await fetch('/voices'); VOICES=await r.json(); MAP=groupLangs();
  const langs=Object.keys(MAP).sort(); const ls=$('lang');
  langs.forEach(l=>{const o=document.createElement('option');o.value=l;o.textContent=l;ls.appendChild(o);});
  ls.value=langs.find(l=>l.includes('Urdu'))||langs[0];
  fillVoices();
}
const warmed=new Set();
function warm(v){ if(!v||warmed.has(v))return; warmed.add(v); fetch('/preview?voice='+encodeURIComponent(v)).catch(()=>{}); }
function fillVoices(){
  const vs=MAP[$('lang').value]||[]; const list=$('voiceList'); list.innerHTML='';
  vs.forEach(v=>{
    const card=document.createElement('div'); card.className='vcard'; card.dataset.voice=v.name;
    card.innerHTML='<div class="vinfo"><span class="vname">'+v.name+'</span><span class="vgender">'+v.gender+'</span></div>'+
      '<button class="vplay" title="'+t('test_tip')+'">&#9654;</button>';
    card.querySelector('.vinfo').onclick=()=>selectVoice(v.name,card);
    card.querySelector('.vplay').onclick=(e)=>{e.stopPropagation(); preview(v.name, e.currentTarget);};
    list.appendChild(card);
  });
  stopPreview();
  if(vs.length) selectVoice(vs[0].name, list.firstChild);
}
function selectVoice(name,card){
  selectedVoice=name; $('selName').textContent=name;
  document.querySelectorAll('.vcard').forEach(c=>c.classList.remove('sel'));
  if(card) card.classList.add('sel');
  warm(name);
}
$('lang').onchange=fillVoices;

/* ---- preview play/pause ---- */
let playingVoice=null, playingBtn=null;
const prev=$('prev');
function setBtn(btn,state){ if(btn) btn.innerHTML = state==='pause' ? '&#10073;&#10073;' : '&#9654;'; }
function stopPreview(){ try{prev.pause();}catch(e){} if(playingBtn)setBtn(playingBtn,'play'); playingVoice=null; playingBtn=null; }
prev.addEventListener('ended',()=>{ if(playingBtn)setBtn(playingBtn,'play'); playingVoice=null; playingBtn=null; });
async function preview(voice,btn){
  if(playingVoice===voice){
    if(prev.paused){ try{await prev.play(); setBtn(btn,'pause');}catch(e){} }
    else { prev.pause(); setBtn(btn,'play'); }
    return;
  }
  if(playingBtn) setBtn(playingBtn,'play');
  btn.innerHTML='&#9203;';
  try{ prev.src='/preview?voice='+encodeURIComponent(voice)+'&t='+Date.now();
    await prev.play(); playingVoice=voice; playingBtn=btn; setBtn(btn,'pause'); }
  catch(e){ setBtn(btn,'play'); setStatus(t('st_testfail'),'var(--warn)'); }
}

/* ---- sliders + count ---- */
function paintSlider(el){
  const mn=+el.min, mx=+el.max, v=+el.value;
  const pct=(v-mn)/(mx-mn)*100, lo=Math.min(pct,50), hi=Math.max(pct,50);
  el.style.background='linear-gradient(90deg, var(--bg) 0 '+lo+'%, var(--fill) '+lo+'% '+hi+'%, var(--bg) '+hi+'% 100%)';
}
['speed','pitch','vol'].forEach(id=>{const m={speed:'sv',pitch:'pv',vol:'vv'}; const el=$(id);
  el.oninput=()=>{ $(m[id]).textContent=el.value; paintSlider(el); };
  paintSlider(el);
});
function updCount(){const tx=$('text').value;
  $('count').textContent=tx.length+' '+t('characters')+'  ·  '+tx.trim().split(/\s+/).filter(Boolean).length+' '+t('words');}
$('text').oninput=updCount; updCount();
function setSubs(v){ $('subs').checked=v; }
function toggleVoiceLib(){
  const p=$('voicePanel'); const show=(!p.style.display||p.style.display==='none');
  p.style.display=show?'block':'none';
  const b=$('vlibBtn'); if(b) b.classList.toggle('active',show);
  if(show) p.scrollIntoView({behavior:'smooth',block:'center'});
}
function sign(n){n=parseInt(n);return (n>=0?'+':'')+n;}

/* ---- history (localStorage) ---- */
function getHist(){ try{return JSON.parse(localStorage.getItem('ml_history')||'[]');}catch(e){return [];} }
function saveHist(h){ try{localStorage.setItem('ml_history',JSON.stringify(h));}catch(e){} }
function addHist(file,srt){ var h=getHist(); h=h.filter(function(e){return e.file!==file;}); h.unshift({file:file,srt:srt||null,ts:Date.now()}); h=h.slice(0,25); saveHist(h); renderHist(); }
function removeHist(i){ var h=getHist(); h.splice(i,1); saveHist(h); renderHist(); }
function renderHist(){
  var h=getHist(), box=$('histBox'); if(!box) return;
  if(!h.length){ box.innerHTML='<div class="small" style="color:var(--sub)">'+t('no_history')+'</div>'; return; }
  box.innerHTML=h.map(function(e,i){
    var u='/audio?f='+encodeURIComponent(e.file)+'&t='+Date.now();
    var srtL = e.srt ? '<a class="hlink" href="/audio?f='+encodeURIComponent(e.srt)+'&t='+Date.now()+'" download="'+e.srt+'">.srt</a>' : '';
    return '<div class="hrow"><span class="hname" title="'+e.file+'">'+e.file+'</span>'+
      '<a class="hlink" href="'+u+'" download="'+e.file+'">mp3</a>'+srtL+
      '<button class="hdel" onclick="removeHist('+i+')" title="'+t('remove')+'">✕</button></div>';
  }).join('');
}

/* ---- generate ---- */
$('gen').onclick=async()=>{
  const text=$('text').value.trim();
  if(!text){ setStatus(t('st_script'),'var(--warn)'); return; }
  if(!selectedVoice){ setStatus(t('st_voice'),'var(--warn)'); return; }
  $('gen').disabled=true; $('gen').textContent=t('generating'); $('player').innerHTML='';
  let secs=0; setStatus(t('st_gen')+' (0s)','var(--warn)');
  const timer=setInterval(()=>{secs++; setStatus(t('st_gen')+' ('+secs+'s)','var(--warn)');},1000);
  try{
    const res=await fetch('/generate',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text,voice:selectedVoice,
        rate:sign($('speed').value)+'%',pitch:sign($('pitch').value)+'Hz',volume:sign($('vol').value)+'%',
        filename:$('fname').value.trim()||'voiceover',subs:$('subs').checked})});
    const d=await res.json(); clearInterval(timer);
    if(d.ok){
      setStatus(t('st_done')+d.file+(d.subs?t('saved_srt'):'')+t('st_done2'),'var(--good)');
      const u='/audio?f='+encodeURIComponent(d.file)+'&t='+Date.now();
      var html='<audio controls autoplay src="'+u+'"></audio>'+
        '<div><a class="dl" href="'+u+'" download="'+d.file+'">'+t('dl')+'</a></div>';
      if(d.srt){ var us='/audio?f='+encodeURIComponent(d.srt)+'&t='+Date.now();
        html+='<div><a class="dl" href="'+us+'" download="'+d.srt+'">'+t('dl_srt')+'</a></div>'; }
      html+='<div><a class="dl" style="cursor:pointer" onclick="openFolder()">📁 '+t('open_folder')+'</a></div>';
      $('player').innerHTML=html;
      addHist(d.file, d.srt);
    } else setStatus(t('st_err')+(d.error||''),'var(--bad)');
  }catch(e){ clearInterval(timer); setStatus('Error: '+e,'var(--bad)'); }
  $('gen').disabled=false; $('gen').innerHTML=t('gen_btn');
};
function setStatus(t,c){const s=$('status');s.textContent=t;s.style.color=c||'var(--fg)';}
function openFolder(){ fetch('/openfolder').catch(function(){}); }
init().catch(e=>setStatus(t('st_voicesfail')+e,'var(--bad)'));
(function(){ var L="ur"; try{L=localStorage.getItem("ml_lang")||"ur";}catch(e){} if(!I18N[L])L="ur"; $('uiLang').value=L; setUiLang(L); })();
$('uiLang').onchange=function(){ setUiLang($('uiLang').value); renderHist(); };
renderHist();
</script>
</body>
</html>"""


FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs><radialGradient id="g" cx="38%" cy="33%" r="80%"><stop offset="0%" stop-color="#34363d"/><stop offset="100%" stop-color="#1c1d21"/></radialGradient></defs><circle cx="50" cy="50" r="48" fill="url(#g)"/><rect x="42" y="22" width="16" height="30" rx="8" fill="#8a9bff"/><path d="M33 44 a17 17 0 0 0 34 0" fill="none" stroke="#8a9bff" stroke-width="4.5" stroke-linecap="round"/><line x1="50" y1="60" x2="50" y2="70" stroke="#8a9bff" stroke-width="4.5" stroke-linecap="round"/><line x1="40" y1="70" x2="60" y2="70" stroke="#8a9bff" stroke-width="4.5" stroke-linecap="round"/><text x="50" y="90" text-anchor="middle" font-family="Arial Black, Arial, sans-serif" font-weight="900" font-size="15" fill="#e2e6ed">Muft Lab</text></svg>"""


async def favicon(req):
    return web.Response(text=FAVICON, content_type="image/svg+xml")


FAVICON_PNG_B64 = "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"


async def favicon_png(req):
    try:
        return web.Response(body=base64.b64decode(FAVICON_PNG_B64), content_type="image/png")
    except Exception:
        return web.Response(status=404)


FAVICON_ICO_B64 = "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"


async def favicon_ico(req):
    try:
        return web.Response(body=base64.b64decode(FAVICON_ICO_B64), content_type="image/x-icon")
    except Exception:
        return web.Response(status=404)


async def index(req):
    return web.Response(text=PAGE, content_type="text/html")


async def voices(req):
    vs = await load_voices()
    out = []
    for v in vs:
        fr = v.get("FriendlyName", "")
        lang = fr.split(" - ")[-1] if " - " in fr else v["Locale"]
        out.append({"name": v["ShortName"], "gender": v["Gender"], "lang": lang})
    return web.json_response(out)


async def generate(req):
    b = await req.json()
    text = (b.get("text") or "").strip()
    voice = b.get("voice")
    rate = b.get("rate", "+0%")
    pitch = b.get("pitch", "+0Hz")
    volume = b.get("volume", "+0%")
    fn = (b.get("filename") or "voiceover").strip() or "voiceover"
    fn = "".join(c for c in fn if c not in '\\/:*?"<>|')
    subs = bool(b.get("subs"))
    if not text or not voice:
        return web.json_response({"ok": False, "error": "text/voice missing"}, status=400)

    path = os.path.join(DESKTOP, fn + ".mp3")
    srt = os.path.join(DESKTOP, fn + ".srt")
    try:
        await synthesize(text, voice, rate, pitch, volume, path, srt, subs)
        return web.json_response({"ok": True, "file": fn + ".mp3",
                                  "srt": (fn + ".srt") if subs else None,
                                  "subs": subs, "path": path})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def audio(req):
    fn = os.path.basename(req.query.get("f", "voiceover.mp3"))
    path = os.path.join(DESKTOP, fn)
    if os.path.exists(path):
        return web.FileResponse(path)
    return web.Response(status=404)


async def openfolder(req):
    try:
        os.startfile(DESKTOP)
    except Exception:
        pass
    return web.json_response({"ok": True})


async def preview(req):
    """Chuni hui voice ka sample bhejo. Cache se foran, warna bana kar cache karo (3 retry)."""
    voice = req.query.get("voice")
    if not voice:
        return web.Response(status=400)
    hdr = {"Cache-Control": "no-store"}
    if voice in _PREVIEW_CACHE:
        return web.Response(body=_PREVIEW_CACHE[voice], content_type="audio/mpeg", headers=hdr)
    text = sample_for(voice)
    last_err = "empty"
    for _ in range(3):
        try:
            com = edge_tts.Communicate(text, voice)
            buf = bytearray()
            async for ch in com.stream():
                if ch["type"] == "audio":
                    buf += ch["data"]
            if buf:
                data = bytes(buf)
                _PREVIEW_CACHE[voice] = data
                return web.Response(body=data, content_type="audio/mpeg", headers=hdr)
        except Exception as e:
            last_err = str(e)
        await asyncio.sleep(1)
    return web.Response(status=500, text=str(last_err))


CANVAS_URL = "https://gemini.google.com/share/5d7cb64e5809"


def _srt_segments(raw):
    """SRT ya plain script ko scene-wise lines mein todo."""
    raw = (raw or "").replace("\r", "").strip()
    if not raw:
        return []
    segs = []
    if "-->" in raw:
        for block in re.split(r"\n\s*\n", raw):
            lines = [l for l in block.split("\n") if l.strip()]
            txt = [l for l in lines if "-->" not in l and not re.fullmatch(r"\d+", l.strip())]
            if txt:
                segs.append(" ".join(txt).strip())
    else:
        for l in raw.split("\n"):
            if l.strip():
                segs.append(l.strip())
    return segs


def _clean_prompts(text):
    """Output ko saaf karo: number, timestamp, label hatao; har prompt ke beech blank line."""
    text = (text or "").strip()
    blocks = re.split(r"\n\s*\n", text)
    if len(blocks) < 2:
        parts = re.split(r"\n(?=\s*\d+\s*[.)]\s)", text)
        if len(parts) > 1:
            blocks = parts
    out = []
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        b = re.sub(r"^\s*\d+\s*[.)]\s*", "", b)                      # "1. " / "1) "
        b = re.sub(r"^\s*\d{1,2}:\d{2}(:\d{2})?\s*[|\-–]\s*", "", b)  # "00:07 | "
        b = b.strip()
        if b:
            out.append(b)
    return "\n\n".join(out)


async def _call_openrouter(key, model, prompt):
    url = "https://openrouter.ai/api/v1/chat/completions"
    body = {"model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.8}
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(url, json=body, headers=headers) as r:
            j = await r.json()
            if r.status != 200:
                raise RuntimeError(j.get("error", {}).get("message", str(j)))
            return j["choices"][0]["message"]["content"]


async def _call_gemini(key, model, prompt):
    model = model or "gemini-2.0-flash"
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + model + ":generateContent?key=" + key)
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(url, json=body) as r:
            j = await r.json()
            if r.status != 200:
                raise RuntimeError(j.get("error", {}).get("message", str(j)))
            return j["candidates"][0]["content"]["parts"][0]["text"]


async def gmodels(req):
    """Gemini se aap ki key ke available writing models laao (sirf jo chalte hain)."""
    try:
        data = await req.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad request"}, status=400)
    key = (data.get("apikey") or "").strip()
    if not key:
        return web.json_response({"ok": False, "error": "API key daalo"}, status=400)
    url = "https://generativelanguage.googleapis.com/v1beta/models?key=" + key
    timeout = aiohttp.ClientTimeout(total=60)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url) as r:
                j = await r.json()
                if r.status != 200:
                    raise RuntimeError(j.get("error", {}).get("message", str(j)))
        out = []
        for m in j.get("models", []):
            methods = m.get("supportedGenerationMethods", [])
            name = m.get("name", "").replace("models/", "")
            if ("generateContent" in methods and name.startswith("gemini")
                    and "vision" not in name and "embedding" not in name):
                out.append(name)
        out = sorted(set(out))
        return web.json_response({"ok": True, "models": out})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def testkey(req):
    """API key + model ko ek chhote call se test karo."""
    try:
        data = await req.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad request"}, status=400)
    provider = (data.get("provider") or "gemini").lower()
    model = (data.get("model") or "").strip()
    key = (data.get("apikey") or "").strip()
    if not key:
        return web.json_response({"ok": False, "error": "API key daalo"})
    try:
        if provider == "openrouter":
            if not model:
                return web.json_response({"ok": False, "error": "model select karo"})
            await _call_openrouter(key, model, "Reply with just: OK")
        else:
            await _call_gemini(key, model, "Reply with just: OK")
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})


async def genprompts(req):
    try:
        data = await req.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad request"}, status=400)
    provider = (data.get("provider") or "gemini").lower()
    model = (data.get("model") or "").strip()
    key = (data.get("apikey") or "").strip()
    universal = (data.get("universal") or "").strip()
    srt = (data.get("srt") or "").strip()
    if not key:
        return web.json_response({"ok": False, "error": "API key daalo"}, status=400)
    segs = _srt_segments(srt)
    if not segs:
        return web.json_response({"ok": False, "error": "SRT ya script daalo"}, status=400)
    joined = "\n".join("%d. %s" % (i + 1, s) for i, s in enumerate(segs))
    style = universal or "cinematic, highly detailed, dramatic lighting"
    prompt = (
        "You are an expert image-prompt writer for faceless YouTube videos.\n"
        "For EACH scene below, write ONE vivid image-generation prompt.\n"
        "Apply this style to every prompt: " + style + "\n"
        "Keep the SAME number of prompts and the SAME order as the scenes.\n"
        "Write EACH prompt as one clean paragraph. Do NOT add numbers, timestamps, "
        "or labels of any kind - only the prompt text.\n"
        "Put a BLANK LINE between each prompt. Return ONLY the prompts, nothing else.\n\n"
        "Scenes:\n" + joined
    )
    try:
        if provider == "openrouter":
            if not model:
                return web.json_response({"ok": False, "error": "model select karo"}, status=400)
            out = await _call_openrouter(key, model, prompt)
        else:
            out = await _call_gemini(key, model, prompt)
        return web.json_response({"ok": True, "prompts": _clean_prompts(out), "count": len(segs)})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


def main():
    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/favicon.ico", favicon_ico),
        web.get("/icon.svg", favicon),
        web.get("/icon.png", favicon_png),
        web.get("/voices", voices),
        web.post("/generate", generate),
        web.get("/audio", audio),
        web.get("/openfolder", openfolder),
        web.get("/preview", preview),
        web.post("/genprompts", genprompts),
        web.post("/gmodels", gmodels),
        web.post("/testkey", testkey),
    ])
    cloud_port = os.environ.get("PORT")
    if cloud_port:
        web.run_app(app, host="0.0.0.0", port=int(cloud_port), print=None)
        return
    ssl_ctx = None
    if os.path.exists(CERT) and os.path.exists(KEY):
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(CERT, KEY)
        scheme, port = "https", 443
    else:
        scheme, port = "http", 80
    suffix = "" if port in (80, 443) else f":{port}"
    url = f"{scheme}://{HOSTNAME}{suffix}"
    threading.Timer(1.3, lambda: webbrowser.open(url)).start()
    print(f"\n  Awaaz Jadoo chal raha hai:  {url}")
    print("  Band karne ke liye yeh window close kar do.\n")
    web.run_app(app, host="127.0.0.1", port=port, ssl_context=ssl_ctx, print=None)


if __name__ == "__main__":
    main()
