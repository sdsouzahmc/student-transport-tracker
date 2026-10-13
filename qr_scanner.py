"""Live QR scanner (Streamlit component) — uses the BACK camera by default, scans continuously and returns each badge it reads.
Built on the bundled html5-qrcode library (Apache-2.0, qr_lib.js). No external website is needed."""
import os
import shutil

import streamlit.components.v1 as components

_HERE = os.path.dirname(os.path.abspath(__file__))
_DIR = os.path.join(_HERE, "_qr_scanner_component")

_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#1f2937}
  .bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:4px 0 8px}
  button{padding:9px 14px;border-radius:10px;border:1px solid #cbd5e1;background:#fff;font-size:15px}
  button.primary{background:#1c5490;color:#fff;border-color:#1c5490}
  #status{font-size:13px;color:#475569}
  #reader{width:100%;max-width:520px;border-radius:12px;overflow:hidden;background:#0f172a}
  #reader video{width:100%!important;height:auto!important}
  #last{margin-top:8px;font-size:14px;font-weight:600;min-height:20px}
  #flash{position:fixed;inset:0;pointer-events:none;opacity:0;transition:opacity .35s;background:rgba(30,142,62,.45)}
</style>
<script src="qr_lib.js"></script></head>
<body>
<div class="bar">
  <button id="startBtn" class="primary">▶ Start camera</button>
  <button id="flipBtn">🔄 Switch camera</button>
  <span id="status">Back camera</span>
</div>
<div id="reader"></div>
<div id="last"></div>
<div id="flash"></div>
<script>
const send=(type,extra)=>window.parent.postMessage(Object.assign({isStreamlitMessage:true,apiVersion:1,type},extra||{}),"*");
const $=id=>document.getElementById(id);
let facing="environment", scanner=null, running=false, last="", lastT=0, started=false, ctx=null;
function setHeight(){send("streamlit:setFrameHeight",{height:document.body.scrollHeight+12});}
function beep(){try{ctx=ctx||new (window.AudioContext||window.webkitAudioContext)();const o=ctx.createOscillator(),g=ctx.createGain();
  o.frequency.value=1100;o.connect(g);g.connect(ctx.destination);g.gain.value=.15;o.start();o.stop(ctx.currentTime+.12);}catch(e){}}
function onScan(text){
  const now=Date.now();
  try{ last=sessionStorage.getItem("qr_last")||last; lastT=+(sessionStorage.getItem("qr_lastT")||lastT); }catch(e){}
  if(text===last && now-lastT<20000){                 // same badge still in view (survives screen refresh): ignore for 20 s
    $("last").textContent="Same badge again — ignored. Show the next badge."; return; }
  last=text; lastT=now;
  try{ sessionStorage.setItem("qr_last",text); sessionStorage.setItem("qr_lastT",String(now)); }catch(e){}
  $("flash").style.opacity=1; setTimeout(()=>$("flash").style.opacity=0,250);
  beep(); if(navigator.vibrate) navigator.vibrate(120);
  $("last").textContent="Read: "+text.slice(0,24)+(text.length>24?"…":"")+" — sent";
  send("streamlit:setComponentValue",{value:{code:text,nonce:now+"-"+Math.random().toString(36).slice(2,8)},dataType:"json"});
}
async function stop(){ if(scanner&&running){ try{await scanner.stop();}catch(e){} } running=false; }
async function start(){
  await stop();
  try{
    if(!scanner) scanner=new Html5Qrcode("reader",{formatsToSupport:[Html5QrcodeSupportedFormats.QR_CODE],verbose:false});
    $("status").textContent="Starting "+(facing==="environment"?"back":"front")+" camera…";
    await scanner.start({facingMode:facing},
      {fps:10,qrbox:(w,h)=>{const s=Math.floor(Math.min(w,h)*0.72);return {width:s,height:s};}},
      onScan, ()=>{});
    running=true;
    $("status").textContent=(facing==="environment"?"Back":"Front")+" camera on — hold the badge inside the square";
    $("startBtn").textContent="⏸ Stop camera";
  }catch(err){
    running=false;
    $("status").textContent="Camera not started: "+(err&&err.message?err.message:err)+" — tap Start camera and allow access.";
    $("startBtn").textContent="▶ Start camera";
  }
  setTimeout(setHeight,300); setTimeout(setHeight,1200);
}
$("startBtn").onclick=async()=>{ beep(); if(running){ await stop(); $("startBtn").textContent="▶ Start camera"; $("status").textContent="Camera stopped"; } else { await start(); } setHeight(); };
$("flipBtn").onclick=async()=>{ facing=(facing==="environment")?"user":"environment"; await start(); };
window.addEventListener("message",e=>{
  const d=e.data||{};
  if(d.type==="streamlit:render"){ if(!started){ started=true; start(); } setHeight(); }
});
new ResizeObserver(setHeight).observe(document.body);
send("streamlit:componentReady");
setHeight();
</script></body></html>
"""


def _ensure_files():
    os.makedirs(_DIR, exist_ok=True)
    idx = os.path.join(_DIR, "index.html")
    if not os.path.exists(idx) or open(idx, encoding="utf-8").read() != _HTML:
        with open(idx, "w", encoding="utf-8") as f:
            f.write(_HTML)
    lib_dst = os.path.join(_DIR, "qr_lib.js")
    lib_src = os.path.join(_HERE, "qr_lib.js")
    if not os.path.exists(lib_dst) or os.path.getsize(lib_dst) != os.path.getsize(lib_src):
        shutil.copyfile(lib_src, lib_dst)


_ensure_files()
_component = components.declare_component("qr_scanner", path=_DIR)


def qr_scanner(key):
    """Returns {'code': ..., 'nonce': ...} for the most recent badge read (or None). Compare the nonce to detect new scans."""
    return _component(key=key, default=None)
