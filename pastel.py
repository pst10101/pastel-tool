#!/usr/bin/env python3
"""파스텔 도구 - 멤버 트윗 피드 + 스페이스 녹음 (Termux용, 파이썬 표준 라이브러리만 사용)

사용법
  python pastel.py                 피드 서버 시작 (브라우저로 http://127.0.0.1:8787 접속)
  python pastel.py add 핸들 [핸들…]  멤버 추가 (@ 없이)
  python pastel.py rm 핸들          멤버 삭제
  python pastel.py members         멤버 목록
  python pastel.py check 핸들       API가 잘 되는지 최근 트윗 3개 출력
  python pastel.py rec 링크 [-n 이름] 스페이스 녹음 (m3u8 링크 또는 x.com/i/spaces/… 주소)

설정/상태 파일은 ~/.pastel/ 에 저장돼요. (config.json 을 직접 고쳐도 돼요)
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.environ.get("PASTEL_HOME") or os.path.join(os.path.expanduser("~"), ".pastel")
CONF_PATH = os.path.join(HOME, "config.json")
FEED_PATH = os.path.join(HOME, "feed.json")
UA = "pastel-tool/1.0 (personal use)"

DEFAULT_CONF = {
    "members": [],               # 트위터 핸들 목록 (@ 없이)
    "interval": 60,              # 전체 멤버를 한 바퀴 도는 데 걸리는 시간(초)
    "count": 10,                 # 한 번에 가져올 트윗 수
    "include_replies": False,    # 답글 포함
    "include_reposts": False,    # 리포스트 포함 (켜면 매번 전체를 받아서 데이터를 더 써요)
    "translate_lang": "",        # 예: "ko" 로 두면 번역문도 같이 표시
    "notify": True,              # termux-notification 알림 (Termux:API 필요)
    "auto_record_spaces": False, # 새 트윗에서 스페이스 링크를 발견하면 자동 녹음 (yt-dlp 필요)
    "port": 8787,
    "save_dir": "",              # 비우면 자동 (공유 저장소 있으면 Pastel 폴더)
    "api_base": "https://api.fxtwitter.com",
}

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
SPACE_RE = re.compile(r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/i/spaces/[A-Za-z0-9]+", re.I)
SAFE_URL_RE = re.compile(r"^https://[A-Za-z0-9_./\-]+$")


# --------------------------------------------------------------------------- 설정

def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_conf():
    os.makedirs(HOME, exist_ok=True)
    conf = dict(DEFAULT_CONF)
    if os.path.exists(CONF_PATH):
        try:
            with open(CONF_PATH, encoding="utf-8") as f:
                conf.update(json.load(f))
        except Exception as e:
            print("config.json 을 읽지 못했어요:", e)
    else:
        save_json(CONF_PATH, conf)
    return conf


def norm_handle(h):
    h = (h or "").strip()
    m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]+)", h)
    if m:
        h = m.group(1)
    h = h.lstrip("@")
    return h if HANDLE_RE.match(h) else None


def default_save_dir():
    shared = os.path.join(os.path.expanduser("~"), "storage", "shared")
    if os.path.isdir(shared):
        return os.path.join(shared, "Pastel")
    return os.path.join(os.path.expanduser("~"), "pastel_recordings")


# --------------------------------------------------------------------------- fxtwitter

def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status == 204:
                return 204, None
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = None
        return e.code, body


def norm_status(s):
    """fxtwitter 응답의 status 하나를 화면용으로 간단히 정리"""
    a = s.get("author") or {}
    media = s.get("media") or {}
    handle = a.get("screen_name") or ""
    sid = str(s.get("id") or "")
    if HANDLE_RE.match(handle) and sid.isdigit():
        url = "https://x.com/%s/status/%s" % (handle, sid)
    else:
        url = s.get("url") or ""
    photos = [p.get("url") for p in (media.get("photos") or []) if p.get("url")]
    videos = [{"url": v.get("url"), "thumb": v.get("thumbnail_url")}
              for v in (media.get("videos") or []) if v.get("url")]
    q = None
    quote = s.get("quote")
    if isinstance(quote, dict) and quote.get("type") != "tombstone":
        qa = quote.get("author") or {}
        q = {"name": qa.get("name") or "", "handle": qa.get("screen_name") or "",
             "text": quote.get("text") or "", "url": quote.get("url") or ""}
    text = s.get("text") or ""
    hay = " ".join([text, (q or {}).get("text", ""), ((s.get("card") or {}).get("url") or "")])
    m = SPACE_RE.search(hay)
    rb = s.get("reposted_by") or {}
    rp = s.get("replying_to") or {}
    return {
        "id": sid,
        "url": url,
        "text": text,
        "translation": (s.get("translation") or {}).get("text") or "",
        "ts": int(s.get("created_timestamp") or 0),
        "name": a.get("name") or handle,
        "handle": handle,
        "avatar": a.get("avatar_url") or "",
        "photos": photos,
        "videos": videos,
        "quote": q,
        "reposted_by": rb.get("name") or rb.get("screen_name") or "",
        "replying_to": rp.get("screen_name") or "",
        "likes": s.get("likes") or 0,
        "reposts": s.get("reposts") or 0,
        "replies": s.get("replies") or 0,
        "space": m.group(0) if m else "",
    }


def notify(title, content, url=None):
    exe = shutil.which("termux-notification")
    if not exe:
        return
    cmd = [exe, "--title", title, "--content", content[:300]]
    if url and SAFE_URL_RE.match(url):
        cmd += ["--action", "termux-open-url " + url]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


class Feed:
    MAX_ITEMS = 300

    def __init__(self, conf):
        self.conf = conf
        self.lock = threading.Lock()
        self.items = {}      # id -> item
        self.last_ts = {}    # handle -> 마지막으로 본 시각
        self.errors = {}
        self.updated = 0
        self.hurry = False
        self._load()

    def _load(self):
        try:
            with open(FEED_PATH, encoding="utf-8") as f:
                d = json.load(f)
            self.items = {i["id"]: i for i in d.get("items", [])}
            self.last_ts = d.get("last_ts", {})
        except Exception:
            pass

    def _save(self):
        try:
            save_json(FEED_PATH, {"items": list(self.items.values()), "last_ts": self.last_ts})
        except Exception:
            pass

    def snapshot(self):
        with self.lock:
            items = sorted(self.items.values(), key=lambda i: i["ts"], reverse=True)
            return {
                "members": list(self.conf["members"]),
                "items": items[:200],
                "errors": dict(self.errors),
                "updated": self.updated,
                "save_dir": self.conf.get("save_dir") or default_save_dir(),
            }

    def poll_member(self, handle):
        c = self.conf
        params = {"count": int(c["count"])}
        last = self.last_ts.get(handle)
        # 리포스트를 빼는 경우에만 since 를 써서 변화 없을 때 204(빈 응답)로 데이터를 아껴요
        if last and not c["include_reposts"]:
            params["since"] = last
        if c["include_replies"]:
            params["with_replies"] = "1"
        if c["translate_lang"]:
            params["lang"] = c["translate_lang"]
        url = "%s/2/profile/%s/statuses?%s" % (c["api_base"].rstrip("/"), handle, urllib.parse.urlencode(params))
        code, data = http_json(url)
        if code == 204:
            self.updated = int(time.time())
            return []
        if code == 404:
            raise RuntimeError("계정을 못 찾았거나 트윗이 없어요")
        if code == 429:
            raise RuntimeError("요청이 너무 많아요(429) - interval 을 늘려보세요")
        if code != 200 or not isinstance(data, dict):
            raise RuntimeError("응답 오류 HTTP %s" % code)
        first_run = last is None
        fresh = []
        max_ts = last or 0
        with self.lock:
            for s in data.get("results") or []:
                if not isinstance(s, dict) or s.get("type") == "thread":
                    continue
                if s.get("type") not in (None, "status"):
                    continue
                it = norm_status(s)
                if not it["id"]:
                    continue
                if it["reposted_by"] and not c["include_reposts"]:
                    continue
                max_ts = max(max_ts, it["ts"])
                if it["id"] in self.items:
                    continue
                self.items[it["id"]] = it
                if not first_run and it["ts"] > (last or 0):
                    fresh.append(it)
            if max_ts:
                self.last_ts[handle] = max_ts
            if len(self.items) > self.MAX_ITEMS:
                keep = sorted(self.items.values(), key=lambda i: i["ts"], reverse=True)[:self.MAX_ITEMS]
                self.items = {i["id"]: i for i in keep}
            self.updated = int(time.time())
            self._save()
        return fresh

    def nap(self, sec):
        end = time.time() + sec
        while time.time() < end and not self.hurry:
            time.sleep(0.25)

    def run(self, recorder):
        tried_spaces = set()
        while True:
            members = list(self.conf["members"])
            if not members:
                self.nap(3)
                continue
            gap = 0.4 if self.hurry else max(2.0, float(self.conf["interval"]) / len(members))
            for h in members:
                try:
                    fresh = self.poll_member(h)
                    self.errors.pop(h, None)
                except Exception as e:
                    self.errors[h] = str(e)[:120]
                    fresh = []
                for it in reversed(fresh):
                    if self.conf["notify"]:
                        body = it["text"] or "(미디어)"
                        notify("%s (@%s)" % (it["name"], it["handle"]), body, it["url"])
                    if it["space"] and self.conf["auto_record_spaces"] and it["space"] not in tried_spaces:
                        tried_spaces.add(it["space"])
                        try:
                            recorder.start(it["space"], it["handle"])
                        except Exception as e:
                            notify("스페이스 자동 녹음 실패", str(e)[:150], it["space"])
                self.nap(gap)
            self.hurry = False


# --------------------------------------------------------------------------- 녹음

def resolve_stream(url):
    """m3u8 링크면 그대로, 스페이스 주소면 yt-dlp 로 m3u8 을 찾아요"""
    if ".m3u8" in url:
        return url
    if SPACE_RE.search(url):
        exe = shutil.which("yt-dlp")
        if not exe:
            raise RuntimeError("스페이스 주소는 yt-dlp 가 필요해요 (pip install -U yt-dlp). "
                               "또는 m3u8 링크를 직접 넣어주세요")
        try:
            r = subprocess.run([exe, "-g", url], capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            raise RuntimeError("yt-dlp 응답이 없어요. m3u8 링크를 직접 넣어주세요")
        lines = [l.strip() for l in r.stdout.splitlines() if l.strip().startswith("http")]
        if r.returncode != 0 or not lines:
            tail = (r.stderr.strip().splitlines() or ["알 수 없는 오류"])[-1][:160]
            raise RuntimeError("yt-dlp 실패: %s / m3u8 링크를 직접 넣어주세요" % tail)
        return lines[0]
    raise ValueError("m3u8 링크나 스페이스 주소(x.com/i/spaces/…)를 넣어주세요")


class Job:
    def __init__(self, jid, name, path):
        self.id = jid
        self.name = name
        self.path = path
        self.started = time.time()
        self.ended = None
        self.status = "recording"   # recording / done / stopped / error
        self.err = ""
        self.proc = None
        self.stopping = False

    def to_dict(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            size = 0
        end = self.ended or time.time()
        return {"id": self.id, "name": self.name, "file": os.path.basename(self.path),
                "size": size, "secs": int(end - self.started), "status": self.status,
                "stopping": self.stopping and self.status == "recording", "err": self.err}


class Recorder:
    def __init__(self, conf):
        self.conf = conf
        self.jobs = {}
        self.lock = threading.Lock()
        self.next_id = 1

    def save_dir(self):
        return self.conf.get("save_dir") or default_save_dir()

    def start(self, url, name=""):
        url = (url or "").strip()
        if not re.match(r"^https?://", url):
            raise ValueError("http(s) 링크만 지원해요")
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg 가 없어요. Termux 에서: pkg install ffmpeg")
        src = resolve_stream(url)
        safe = re.sub(r"[^\w\-]+", "_", name or "").strip("_")[:40] or "space"
        d = self.save_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "%s_%s.m4a" % (datetime.now().strftime("%Y%m%d_%H%M%S"), safe))
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-protocol_whitelist", "http,https,tcp,tls,crypto",
               "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
               # 재생목록에 남아 있는 가장 앞 조각부터 받아요 (기본값은 라이브 끝 3조각만 받음)
               "-live_start_index", "0",
               "-i", src, "-map", "0:a:0", "-vn", "-c:a", "copy",
               "-bsf:a", "aac_adtstoasc",
               # 2초마다 파일에 기록: 녹음 중 용량이 늘고, 앱이 꺼져도 그때까지는 재생돼요
               "-movflags", "+empty_moov+default_base_moof", "-frag_duration", "2000000",
               "-flush_packets", "1",
               "-f", "mp4", path]
        with self.lock:
            job = Job(self.next_id, safe, path)
            self.next_id += 1
            self.jobs[job.id] = job
        try:
            job.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE)
        except Exception as e:
            job.status, job.err, job.ended = "error", str(e), time.time()
            raise
        threading.Thread(target=self._watch, args=(job,), daemon=True).start()
        return job

    def _watch(self, job):
        tail = []
        try:
            for raw in job.proc.stderr:
                tail.append(raw.decode("utf-8", "replace").strip())
                tail = tail[-3:]
        except Exception:
            pass
        rc = job.proc.wait()
        job.ended = time.time()
        try:
            size = os.path.getsize(job.path)
        except OSError:
            size = 0
        if job.stopping:
            job.status = "stopped" if size > 0 else "error"
        elif rc == 0:
            job.status = "done"
        else:
            job.status = "error"
            job.err = " / ".join(t for t in tail if t)[:200] or ("ffmpeg 종료 코드 %s" % rc)
            if size > 0:
                job.err += " (여기까지는 저장됐어요)"

    def stop(self, jid):
        job = self.jobs.get(jid)
        if not job or job.status != "recording" or not job.proc:
            return job
        job.stopping = True
        try:
            # SIGINT 를 받으면 ffmpeg 가 하던 일을 멈추고 파일을 정상적으로 마무리해요
            job.proc.send_signal(signal.SIGINT)
        except Exception:
            try:
                job.proc.stdin.write(b"q")
                job.proc.stdin.flush()
            except Exception:
                job.proc.terminate()

        def force():
            if job.proc.poll() is None:
                job.proc.kill()
        threading.Timer(10, force).start()
        return job

    def stop_all(self):
        for j in list(self.jobs.values()):
            self.stop(j.id)
        end = time.time() + 12
        while time.time() < end and any(j.status == "recording" for j in self.jobs.values()):
            time.sleep(0.3)

    def listing(self):
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j.id, reverse=True)
        return [j.to_dict() for j in jobs]


# --------------------------------------------------------------------------- 웹 화면

PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>파스텔 도구</title>
<style>
:root{--bg:#1d1e26;--card:#292b36;--line:#3a3c4a;--fg:#ecedf5;--mute:#9b9eb3;--acc:#b9a4ff;--ok:#6ee7a8;--bad:#ff8f8f}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Noto Sans KR",sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 12px 0}
.top{display:flex;align-items:center;gap:10px}
.top b{font-size:17px}.mute{color:var(--mute);font-size:12px}.grow{flex:1}
button{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:6px 12px;font:inherit;cursor:pointer}
button.pri{background:var(--acc);color:#1d1e26;border-color:var(--acc);font-weight:600}
nav{display:flex;gap:4px;margin-top:8px}
nav button{border:0;border-bottom:3px solid transparent;border-radius:0;background:none;padding:8px 14px}
nav button.on{border-bottom-color:var(--acc);color:var(--acc)}
main{padding:12px;max-width:640px;margin:0 auto}
#chips{display:flex;gap:6px;overflow-x:auto;padding-bottom:8px}
#chips button{white-space:nowrap;border-radius:999px;padding:4px 12px;font-size:13px}
#chips button.on{background:var(--acc);color:#1d1e26;border-color:var(--acc)}
.err{color:var(--bad);font-size:12px;margin:2px 0}
.card{background:var(--card);border-left:4px solid var(--acc);border-radius:8px;padding:10px 12px;margin:0 0 10px}
.head{display:flex;align-items:center;gap:8px}
.av{width:34px;height:34px;border-radius:50%;background:var(--line);object-fit:cover}
.nm{font-weight:600}.hd{color:var(--mute);font-size:12px}
.rt{color:var(--mute);font-size:12px;margin-bottom:4px}
.txt{white-space:pre-wrap;word-break:break-word;margin:8px 0}
.tr{white-space:pre-wrap;color:var(--mute);border-top:1px dashed var(--line);padding-top:6px;margin-top:6px}
.txt a,.tr a,.q a{color:var(--acc)}
.imgs{display:grid;grid-template-columns:repeat(2,1fr);gap:4px;margin-top:6px}
.imgs.one{grid-template-columns:1fr}
.imgs img,.imgs video{width:100%;border-radius:6px;display:block;background:#000;max-height:420px;object-fit:cover}
.q{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-top:8px;font-size:14px}
.q .txt{margin:4px 0 0}
.foot{display:flex;gap:14px;color:var(--mute);font-size:12px;margin-top:8px;align-items:center}
.foot a{color:var(--mute)}
.space{margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
input{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:9px 10px;font:inherit;margin-bottom:8px}
.job{background:var(--card);border-radius:8px;padding:10px 12px;margin-bottom:8px}
.job .row{display:flex;justify-content:space-between;gap:8px;align-items:center}
.job button{margin-top:8px}
.badge{font-size:12px;border-radius:999px;padding:1px 9px;border:1px solid var(--line)}
.badge.recording{color:var(--bad);border-color:var(--bad)}.badge.done,.badge.stopped{color:var(--ok);border-color:var(--ok)}
.badge.error{color:var(--bad)}
.empty{color:var(--mute);text-align:center;padding:40px 0}
</style></head><body>
<header>
  <div class="top"><b>🌸 파스텔 도구</b><span id="stat" class="mute grow"></span><button id="refresh">새로고침</button></div>
  <nav><button data-tab="feed" class="on">트윗</button><button data-tab="rec">녹음</button></nav>
</header>
<main>
  <section id="feed"><div id="chips"></div><div id="errs"></div><div id="list"></div></section>
  <section id="rec" hidden>
    <input id="rurl" placeholder="m3u8 링크 또는 x.com/i/spaces/… 주소" autocomplete="off" autocapitalize="off">
    <input id="rname" placeholder="이름 (선택, 파일명에 들어가요)" autocomplete="off">
    <button class="pri" id="rstart">녹음 시작</button>
    <p class="mute" id="rdir"></p>
    <div id="jobs"></div>
  </section>
</main>
<script>
const $=s=>document.querySelector(s);
const HDR={'X-Pastel':'1','Content-Type':'application/json'};
let data={items:[],members:[],errors:{}}, filter='', sig='';

async function api(path, body){
  const r=await fetch(path, body?{method:'POST',headers:HDR,body:JSON.stringify(body)}:{cache:'no-store'});
  const j=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(j.error||('HTTP '+r.status));
  return j;
}
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function safeUrl(u){try{const x=new URL(u);return (x.protocol==='https:'||x.protocol==='http:')?x.href:''}catch(e){return ''}}
function linkify(parent,text){
  const re=/https?:\/\/[^\s]+/g;let last=0,m;
  while((m=re.exec(text))){
    if(m.index>last)parent.append(text.slice(last,m.index));
    const u=safeUrl(m[0]);
    if(u){const a=el('a',null,m[0]);a.href=u;a.target='_blank';a.rel='noopener noreferrer';parent.append(a)}else parent.append(m[0]);
    last=m.index+m[0].length;
  }
  if(last<text.length)parent.append(text.slice(last));
}
function ago(ts){const s=Math.max(0,Date.now()/1000-ts);
  if(s<60)return '방금';if(s<3600)return Math.floor(s/60)+'분 전';
  if(s<86400)return Math.floor(s/3600)+'시간 전';return Math.floor(s/86400)+'일 전'}
function fmtSize(b){if(b<1024)return b+'B';if(b<1048576)return (b/1024).toFixed(0)+'KB';return (b/1048576).toFixed(1)+'MB'}
function fmtDur(s){const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;
  return (h?h+':':'')+String(m).padStart(h?2:1,'0')+':'+String(x).padStart(2,'0')}

function card(it){
  const c=el('article','card');
  if(it.reposted_by)c.append(el('div','rt','🔁 '+it.reposted_by+' 님이 리포스트'));
  const head=el('div','head');
  const av=el('img','av');const a=safeUrl(it.avatar);if(a)av.src=a;av.loading='lazy';av.referrerPolicy='no-referrer';
  const who=el('div');who.append(el('div','nm',it.name||it.handle),el('div','hd','@'+it.handle+' · '+ago(it.ts)));
  head.append(av,who);c.append(head);
  if(it.replying_to)c.append(el('div','rt','↪ @'+it.replying_to+' 에게 답글'));
  if(it.text){const t=el('div','txt');linkify(t,it.text);c.append(t)}
  if(it.translation){const t=el('div','tr');linkify(t,it.translation);c.append(t)}
  const n=it.photos.length+it.videos.length;
  if(n){
    const g=el('div','imgs'+(n===1?' one':''));
    it.photos.forEach(p=>{const u=safeUrl(p);if(!u)return;const i=el('img');i.src=u;i.loading='lazy';i.referrerPolicy='no-referrer';g.append(i)});
    it.videos.forEach(v=>{const u=safeUrl(v.url);if(!u)return;const d=el('video');d.src=u;d.controls=true;d.preload='none';d.playsInline=true;const p=safeUrl(v.thumb||'');if(p)d.poster=p;g.append(d)});
    c.append(g);
  }
  if(it.quote){
    const q=el('div','q');q.append(el('div','hd',(it.quote.name||'')+' @'+it.quote.handle));
    const t=el('div','txt');linkify(t,it.quote.text||'');q.append(t);c.append(q);
  }
  if(it.space){
    const sp=el('div','space');sp.append(el('span',null,'🎙 스페이스 링크 발견'));
    const b=el('button','pri','녹음 시작');
    b.onclick=async()=>{b.disabled=true;try{await api('/api/rec',{url:it.space,name:it.handle});b.textContent='녹음 중…';show('rec')}catch(e){alert(e.message);b.disabled=false}};
    sp.append(b);c.append(sp);
  }
  const f=el('div','foot');f.append(el('span',null,'💬 '+it.replies),el('span',null,'🔁 '+it.reposts),el('span',null,'♥ '+it.likes));
  const u=safeUrl(it.url);if(u){const l=el('a',null,'원본 열기');l.href=u;l.target='_blank';l.rel='noopener noreferrer';f.append(l)}
  c.append(f);return c;
}
function renderFeed(){
  const chips=$('#chips');chips.replaceChildren();
  const all=el('button',filter===''?'on':'','전체');all.onclick=()=>{filter='';sig='';renderFeed()};chips.append(all);
  data.members.forEach(m=>{const b=el('button',filter===m?'on':'','@'+m);b.onclick=()=>{filter=m;renderFeed()};chips.append(b)});
  const errs=$('#errs');errs.replaceChildren();
  Object.entries(data.errors||{}).forEach(([h,e])=>errs.append(el('div','err','⚠ @'+h+': '+e)));
  const list=$('#list');list.replaceChildren();
  const items=data.items.filter(i=>!filter||i.handle.toLowerCase()===filter.toLowerCase());
  if(!items.length){list.append(el('div','empty',data.members.length?'아직 가져온 트윗이 없어요. 잠시만요…':'멤버가 없어요. 터미널에서 python pastel.py add 핸들 로 추가해 주세요.'));return}
  items.forEach(i=>list.append(card(i)));
}
async function loadFeed(force){
  try{
    data=await api('/api/feed');
    const s=data.items.length+':'+(data.items[0]?data.items[0].id:'')+':'+data.members.join(',')+':'+JSON.stringify(data.errors);
    $('#stat').textContent=data.updated?('업데이트 '+ago(data.updated)):'';
    if(force||s!==sig){sig=s;renderFeed()}
  }catch(e){$('#stat').textContent='서버 연결 끊김'}
}
async function loadJobs(){
  try{
    const j=await api('/api/recs');const box=$('#jobs');box.replaceChildren();
    $('#rdir').textContent='저장 위치: '+(data.save_dir||'');
    if(!j.jobs.length){box.append(el('div','empty','녹음 기록이 없어요'));return}
    const label={recording:'녹음 중',done:'완료',stopped:'중지·저장됨',error:'오류'};
    j.jobs.forEach(x=>{
      const d=el('div','job');const r=el('div','row');
      r.append(el('b',null,x.name),el('span','badge '+x.status,x.stopping?'저장 중…':(label[x.status]||x.status)));d.append(r);
      d.append(el('div','mute',x.file+' · '+fmtSize(x.size)+' · '+fmtDur(x.secs)));
      if(x.err)d.append(el('div','err',x.err));
      if(x.status==='recording'&&!x.stopping){const b=el('button',null,'중지하고 저장');b.onclick=async()=>{b.disabled=true;await api('/api/stop',{id:x.id}).catch(e=>alert(e.message));loadJobs()};d.append(b)}
      box.append(d);
    });
  }catch(e){}
}
function show(tab){
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.tab===tab));
  $('#feed').hidden=tab!=='feed';$('#rec').hidden=tab!=='rec';
  if(tab==='rec')loadJobs();
}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>show(b.dataset.tab));
$('#refresh').onclick=async()=>{try{await api('/api/refresh',{})}catch(e){}setTimeout(()=>loadFeed(true),1500)};
$('#rstart').onclick=async()=>{
  const b=$('#rstart');b.disabled=true;
  try{await api('/api/rec',{url:$('#rurl').value,name:$('#rname').value});$('#rurl').value='';loadJobs()}
  catch(e){alert(e.message)}b.disabled=false;
};
loadFeed(true);setInterval(loadFeed,15000);setInterval(()=>{if(!$('#rec').hidden)loadJobs()},3000);
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    feed = None
    rec = None
    allowed_hosts = set()

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _guard(self, post=False):
        # 이 PC(폰) 안에서만 쓰는 서버: 다른 사이트가 몰래 호출하지 못하게 막아요
        if self.headers.get("Host", "") not in self.allowed_hosts:
            self._json(403, {"error": "forbidden"})
            return False
        if post and self.headers.get("X-Pastel") != "1":
            self._json(403, {"error": "forbidden"})
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/":
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path == "/api/feed":
                self._json(200, self.feed.snapshot())
            elif path == "/api/recs":
                self._json(200, {"jobs": self.rec.listing()})
            else:
                self._json(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if not self._guard(post=True):
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 20000)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("잘못된 요청")
            if path == "/api/rec":
                job = self.rec.start(str(body.get("url", "")), str(body.get("name", "")))
                self._json(200, job.to_dict())
            elif path == "/api/stop":
                job = self.rec.stop(int(body.get("id", 0)))
                self._json(200, job.to_dict() if job else {})
            elif path == "/api/refresh":
                self.feed.hurry = True
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._json(400, {"error": str(e)})


# --------------------------------------------------------------------------- 명령

def install_signals():
    """Ctrl+C / 세션 종료(SIGTERM, SIGHUP) 때도 녹음을 정상 저장하고 끝나게 해요"""
    def _bye(signum, frame):
        raise KeyboardInterrupt
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        s = getattr(signal, name, None)
        if s is not None:
            try:
                signal.signal(s, _bye)
            except (ValueError, OSError):
                pass


def cmd_serve(conf):
    install_signals()
    feed = Feed(conf)
    rec = Recorder(conf)
    Handler.feed, Handler.rec = feed, rec
    port = int(conf["port"])
    Handler.allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}
    threading.Thread(target=feed.run, args=(rec,), daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("파스텔 도구 시작!  브라우저에서 http://127.0.0.1:%d 를 열어주세요" % port)
    print("멤버: %s" % (", ".join("@" + m for m in conf["members"]) or "(없음) → python pastel.py add 핸들"))
    print("녹음 저장 위치: %s" % (conf.get("save_dir") or default_save_dir()))
    print("종료: Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n녹음 중인 게 있으면 저장하고 끝낼게요…")
        rec.stop_all()
    finally:
        srv.server_close()


def cmd_check(conf, handle):
    h = norm_handle(handle)
    if not h:
        sys.exit("핸들 형식이 이상해요")
    url = "%s/2/profile/%s/statuses?count=3" % (conf["api_base"].rstrip("/"), h)
    code, data = http_json(url)
    if code != 200 or not data:
        sys.exit("실패: HTTP %s" % code)
    for s in (data.get("results") or [])[:3]:
        it = norm_status(s)
        when = datetime.fromtimestamp(it["ts"]).strftime("%m-%d %H:%M")
        print("[%s] %s" % (when, (it["text"] or "(미디어)").replace("\n", " ")[:80]))
        print("   ", it["url"])
    print("API 정상 작동!")


def cmd_rec(conf, url, name):
    install_signals()
    rec = Recorder(conf)
    job = rec.start(url, name)
    print("녹음 시작 → %s" % job.path)
    print("끝내려면 Ctrl+C (그때까지 녹음된 건 저장돼요)")
    try:
        while job.status == "recording":
            d = job.to_dict()
            print("\r%s  %s" % (fmt_dur(d["secs"]), fmt_size(d["size"])), end="", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n저장 중…")
        rec.stop_all()
    d = job.to_dict()
    print("\n%s  %s  %s" % (job.status, fmt_size(d["size"]), job.err))


def fmt_size(b):
    return "%dB" % b if b < 1024 else ("%dKB" % (b // 1024) if b < 1048576 else "%.1fMB" % (b / 1048576))


def fmt_dur(s):
    return "%d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


def main():
    ap = argparse.ArgumentParser(description="파스텔 도구 - 멤버 트윗 피드 + 스페이스 녹음")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="피드 서버 시작 (기본)")
    p = sub.add_parser("add", help="멤버 추가")
    p.add_argument("handles", nargs="+")
    p = sub.add_parser("rm", help="멤버 삭제")
    p.add_argument("handles", nargs="+")
    sub.add_parser("members", help="멤버 목록")
    p = sub.add_parser("check", help="API 동작 확인")
    p.add_argument("handle")
    p = sub.add_parser("rec", help="스페이스 녹음")
    p.add_argument("url")
    p.add_argument("-n", "--name", default="")
    args = ap.parse_args()

    conf = load_conf()
    if args.cmd in (None, "serve"):
        cmd_serve(conf)
    elif args.cmd in ("add", "rm"):
        cur = list(conf["members"])
        for raw in args.handles:
            h = norm_handle(raw)
            if not h:
                print("건너뜀(형식 오류):", raw)
                continue
            if args.cmd == "add" and h.lower() not in [m.lower() for m in cur]:
                cur.append(h)
            if args.cmd == "rm":
                cur = [m for m in cur if m.lower() != h.lower()]
        conf["members"] = cur
        save_json(CONF_PATH, conf)
        print("멤버:", ", ".join("@" + m for m in cur) or "(없음)")
    elif args.cmd == "members":
        print("\n".join("@" + m for m in conf["members"]) or "(없음)")
    elif args.cmd == "check":
        cmd_check(conf, args.handle)
    elif args.cmd == "rec":
        try:
            cmd_rec(conf, args.url, args.name)
        except (ValueError, RuntimeError) as e:
            sys.exit(str(e))


if __name__ == "__main__":
    main()
