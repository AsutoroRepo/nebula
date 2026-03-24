"""
Nebula Killsay v3.0.0 — CS2 Kill Message Automation
=====================================================
pip install flask pywin32 pystray pillow pywebview keyboard requests

Features:
  - Per-weapon messages (knife, awp, pistol, grenade, zeus, default)
  - Kill streak detection with escalating messages
  - Death messages (detected via player health drop to 0)
  - Round win / loss messages
  - Per-map message overrides
  - Message variables: {weapon}, {streak}, {kills}, {map}, {hs}
  - Milestone kill messages (every N kills)
  - Message pool / randomiser (comma-separated, round-robin or random)
  - Message import / export (.json packs)
  - Round reset detection via GSI round phase
  - Headshot detection via player.state.headshots diff
  - Kill sound (Web Audio API, in-browser beep)
  - Global hotkey toggle (F9) — enable/disable without alt-tab
  - Auto-update checker
  - Onboarding wizard — auto-writes GSI config, detects CS2 path
  - Session stats: KPM, HS%, best streak
  - UI Themes: Nebula, Crimson, Arctic, Midnight
  - Connection timeout -> auto-disconnect indicator (60 s)
  - Export log to .txt
  - Kill history feed (last 8 kills in sidebar)
  - System tray minimise

PyInstaller:
pyinstaller --onefile --windowed --name "Nebula-Killsay" main.py
"""

import os, sys, json, time, math, random, threading, datetime, logging, glob
from flask import Flask, request, jsonify, Response

VERSION = "3.0.0"
UPDATE_URL = "https://raw.githubusercontent.com/AsutoroRepo/nebula-killsay/main/version.txt"

# -- Win32 --------------------------------------------------------------------
try:
    import win32api, win32con
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False

# -- Tray ---------------------------------------------------------------------
try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

# -- Keyboard hotkey ----------------------------------------------------------
try:
    import keyboard
    KEYBOARD_AVAILABLE = True
except ImportError:
    KEYBOARD_AVAILABLE = False

# -- Requests (update check) --------------------------------------------------
try:
    import requests as req_lib
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

import webview

# -- Constants ----------------------------------------------------------------
F13_VK        = 0x7C
SETTINGS_FILE = "killsay_settings.json"
GSI_TIMEOUT   = 60.0

# Common CS2 install paths to probe
CS2_SEARCH_PATHS = [
    os.path.join(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"),
                 "Steam", "steamapps", "common", "Counter-Strike Global Offensive", "game", "csgo", "cfg"),
    os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"),
                 "Steam", "steamapps", "common", "Counter-Strike Global Offensive", "game", "csgo", "cfg"),
]

def detect_cs2_cfg_dir() -> str:
    for p in CS2_SEARCH_PATHS:
        p = os.path.normpath(p)
        if os.path.isdir(p):
            return p
    for drive in ["C", "D", "E", "F"]:
        pattern = f"{drive}:\\*\\steamapps\\common\\Counter-Strike Global Offensive\\game\\csgo\\cfg"
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return ""

DEFAULT_CFG_DIR  = detect_cs2_cfg_dir()
DEFAULT_CFG_PATH = os.path.join(DEFAULT_CFG_DIR, "killsay.cfg") if DEFAULT_CFG_DIR else ""
DEFAULT_GSI_PATH = os.path.join(DEFAULT_CFG_DIR, "gamestate_integration_nebula.cfg") if DEFAULT_CFG_DIR else ""

GSI_CONFIG_CONTENT = '''"Nebula Killsay GSI"
{
  "uri"          "http://127.0.0.1:3000/gsi"
  "timeout"      "5.0"
  "heartbeat"    "10.0"
  "buffer"       "0.1"
  "throttle"     "0.1"
  "invert"       "0"
  "auth"
  {
    "token"      "nebula_killsay_token"
  }
  "data"
  {
    "provider"            "1"
    "map"                 "1"
    "round"               "1"
    "player_id"           "1"
    "player_state"        "1"
    "player_match_stats"  "1"
    "player_weapons"      "1"
  }
}
'''

# -- Weapon category classifier -----------------------------------------------
WEAPON_CATEGORIES = {
    "knife":   {"weapon_knife","weapon_knife_t","weapon_knife_css","weapon_bayonet",
                "weapon_knife_flip","weapon_knife_gut","weapon_knife_karambit",
                "weapon_knife_m9_bayonet","weapon_knife_tactical","weapon_knife_falchion",
                "weapon_knife_survival_bowie","weapon_knife_butterfly","weapon_knife_push",
                "weapon_knife_cord","weapon_knife_canis","weapon_knife_ursus",
                "weapon_knife_gypsy_jackknife","weapon_knife_outdoor","weapon_knife_stiletto",
                "weapon_knife_widowmaker","weapon_knife_skeleton","weapon_knife_ghost"},
    "awp":     {"weapon_awp"},
    "pistol":  {"weapon_glock","weapon_usp_silencer","weapon_p2000","weapon_p250",
                "weapon_deagle","weapon_elite","weapon_fiveseven","weapon_tec9",
                "weapon_cz75a","weapon_revolver","weapon_hkp2000"},
    "grenade": {"weapon_hegrenade","weapon_molotov","weapon_incgrenade",
                "weapon_flashbang","weapon_decoy"},
    "zeus":    {"weapon_taser"},
}

def classify_weapon(weapon_name: str) -> str:
    w = (weapon_name or "").lower().strip()
    for cat, names in WEAPON_CATEGORIES.items():
        if w in names:
            return cat
    return "default"

WEAPON_DISPLAY = {"knife":"Knife","awp":"AWP","pistol":"Pistol",
                  "grenade":"Grenade","zeus":"Zeus","default":"Rifle"}

# -- Message variable substitution --------------------------------------------
def apply_variables(msg: str, weapon_cat: str, streak: int, total: int,
                    current_map: str, is_headshot: bool) -> str:
    return (msg
        .replace("{weapon}", WEAPON_DISPLAY.get(weapon_cat, "Rifle"))
        .replace("{streak}", str(streak))
        .replace("{kills}", str(total))
        .replace("{map}", current_map or "")
        .replace("{hs}", "HS" if is_headshot else "")
        .strip())


# =============================================================================
#  HTML / CSS / JS
# =============================================================================
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Nebula Killsay</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#07080D;--s1:#0C0D16;--s2:#0F1020;
  --b1:rgba(255,255,255,0.055);--b2:rgba(255,255,255,0.095);
  --white:#F0F0F8;--dim:rgba(240,240,248,0.42);--dimmer:rgba(240,240,248,0.20);
  --accent:#A78BFA;--accent2:#7C3AED;
  --green:#34D399;--red:#F87171;--amber:#FBBF24;--sky:#7DD3FC;
  --r:10px;--rsm:6px;
}
body.theme-crimson{--bg:#0D0709;--s1:#160B0C;--s2:#1A0D0E;--accent:#F87171;--accent2:#B91C1C;}
body.theme-arctic{--bg:#070D12;--s1:#0C1520;--s2:#0F1A26;--accent:#7DD3FC;--accent2:#0369A1;}
body.theme-midnight{--bg:#080810;--s1:#0D0D1A;--s2:#111120;--accent:#C084FC;--accent2:#7E22CE;}

html,body{width:100%;height:100%;overflow:hidden;background:var(--bg);color:var(--white);
  font-family:'Inter','Segoe UI',system-ui,sans-serif;font-size:13px;
  -webkit-font-smoothing:antialiased;user-select:none;}
body::after{content:'';position:fixed;inset:0;z-index:1;pointer-events:none;
  background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.04'/%3E%3C/svg%3E");opacity:.55;}
#stars{position:fixed;inset:0;z-index:0;pointer-events:none}

#shell{position:relative;z-index:2;width:100vw;height:100vh;display:grid;
  grid-template-rows:40px 1fr;grid-template-columns:300px 1fr;
  grid-template-areas:"bar bar" "side main";}

/* titlebar */
#bar{grid-area:bar;display:flex;align-items:center;gap:10px;padding:0 14px;
  background:rgba(10,11,18,0.90);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);
  border-bottom:1px solid var(--b1);-webkit-app-region:drag;}
#bar *{-webkit-app-region:no-drag}
.blogo{display:flex;align-items:center;gap:7px;font-size:11px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;}
.bsep{flex:1}
.bsub{font-size:9.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--dimmer);}
#update-badge{display:none;font-size:9px;font-weight:600;padding:2px 8px;border-radius:99px;
  background:rgba(251,191,36,.15);border:1px solid rgba(251,191,36,.3);color:var(--amber);
  cursor:pointer;letter-spacing:.05em;}
#hotkey-badge{font-size:9px;padding:2px 7px;border-radius:4px;
  background:var(--b1);border:1px solid var(--b2);color:var(--dimmer);
  font-family:'JetBrains Mono',monospace;}
.wbtn{width:26px;height:26px;border-radius:50%;border:none;cursor:pointer;background:transparent;
  color:var(--dimmer);display:flex;align-items:center;justify-content:center;transition:background .15s,color .15s;}
.wbtn:hover{background:var(--b2);color:var(--white)}
#bcl:hover{background:rgba(248,113,113,.15);color:var(--red)}

/* sidebar */
#side{grid-area:side;background:rgba(10,11,18,0.74);backdrop-filter:blur(28px);
  -webkit-backdrop-filter:blur(28px);border-right:1px solid var(--b1);
  padding:20px 16px 16px;display:flex;flex-direction:column;overflow-y:auto;scrollbar-width:none;}
#side::-webkit-scrollbar{display:none}
.sl{font-size:9px;font-weight:600;letter-spacing:.15em;text-transform:uppercase;color:var(--dimmer);margin-bottom:8px;}
.sdiv{height:1px;background:var(--b1);margin:14px 0;}

#spill{display:inline-flex;align-items:center;gap:6px;padding:5px 11px 5px 8px;
  border-radius:99px;background:var(--s2);border:1px solid var(--b2);
  font-size:10px;font-weight:500;letter-spacing:.05em;transition:border-color .4s,background .4s;}
#spill.on{border-color:rgba(52,211,153,.22);background:rgba(52,211,153,.05);}
#sdot{width:6px;height:6px;border-radius:50%;background:var(--dimmer);transition:background .4s,box-shadow .4s;}
#spill.on #sdot{background:var(--green);box-shadow:0 0 7px rgba(52,211,153,.7)}
#stxt{color:var(--dim);transition:color .4s;font-size:10px;}
#spill.on #stxt{color:var(--green)}

#kbox{padding:14px 16px;background:var(--s2);border:1px solid var(--b1);border-radius:var(--r);
  position:relative;overflow:hidden;margin-bottom:8px;}
#kbox::after{content:'';position:absolute;top:-50px;right:-50px;width:130px;height:130px;
  border-radius:50%;background:radial-gradient(circle,rgba(124,58,237,.15) 0%,transparent 70%);pointer-events:none;}
#knum{font-size:40px;font-weight:300;letter-spacing:-.03em;line-height:1;color:var(--white);
  font-variant-numeric:tabular-nums;transition:color .15s;}
#knum.flash{color:var(--green)}
#knum.hs-flash{color:var(--amber)}
#ksub{font-size:9px;letter-spacing:.09em;text-transform:uppercase;color:var(--dimmer);margin-top:3px;}
#streak-badge{display:inline-flex;align-items:center;gap:4px;margin-top:6px;padding:3px 8px;
  border-radius:99px;font-size:9.5px;font-weight:600;letter-spacing:.05em;
  background:rgba(167,139,250,.12);border:1px solid rgba(167,139,250,.2);color:var(--accent);
  transition:all .3s;opacity:0;}
#streak-badge.show{opacity:1}
#streak-badge.fire{background:rgba(251,191,36,.1);border-color:rgba(251,191,36,.25);color:var(--amber);}
#streak-badge.rampage{background:rgba(248,113,113,.1);border-color:rgba(248,113,113,.25);color:var(--red);}

#stats-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:8px;}
.stat-box{background:var(--s2);border:1px solid var(--b1);border-radius:var(--rsm);padding:8px 10px;text-align:center;}
.stat-val{font-size:17px;font-weight:300;color:var(--white);font-variant-numeric:tabular-nums;}
.stat-lbl{font-size:8.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--dimmer);margin-top:2px;}

#kfeed{display:flex;flex-direction:column;gap:4px;}
.kfeed-item{display:flex;align-items:center;gap:7px;padding:5px 8px;border-radius:var(--rsm);
  background:var(--s2);border:1px solid var(--b1);animation:fin .2s ease;font-size:11px;}
.kfeed-item.death{border-color:rgba(248,113,113,.2);background:rgba(248,113,113,.04);}
.kfeed-item.round-w{border-color:rgba(52,211,153,.2);background:rgba(52,211,153,.04);}
.kfeed-icon{font-size:13px;flex-shrink:0;width:18px;text-align:center;}
.kfeed-msg{flex:1;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.kfeed-ts{font-family:'JetBrains Mono',monospace;font-size:9px;color:var(--dimmer);flex-shrink:0;}
.kfeed-hs{color:var(--amber);font-size:9px;font-weight:600;margin-left:2px;}

.tabs{display:flex;gap:2px;margin-bottom:14px;background:var(--s2);border-radius:var(--rsm);
  padding:3px;border:1px solid var(--b1);}
.tab{flex:1;padding:5px 2px;border-radius:4px;border:none;cursor:pointer;
  background:transparent;color:var(--dimmer);font-family:inherit;font-size:9px;
  font-weight:500;letter-spacing:.03em;transition:all .18s;-webkit-app-region:no-drag;}
.tab.active{background:var(--s1);color:var(--white);}
.tab-panel{display:none}
.tab-panel.active{display:block}

.field{margin-bottom:10px}
.fl{font-size:9.5px;font-weight:500;letter-spacing:.09em;text-transform:uppercase;
  color:var(--dim);display:block;margin-bottom:4px;}
.fl-hint{font-size:9px;color:var(--dimmer);font-weight:400;letter-spacing:0;text-transform:none;margin-left:4px;}
input[type=text],input[type=number],textarea{
  width:100%;padding:7px 10px;background:var(--s2);border:1px solid var(--b2);
  border-radius:var(--rsm);color:var(--white);
  font-family:'Inter','Segoe UI',system-ui,sans-serif;font-size:12px;outline:none;
  transition:border-color .2s,box-shadow .2s;-webkit-app-region:no-drag;}
textarea{resize:none;font-size:11.5px;line-height:1.5;}
input:focus,textarea:focus{border-color:rgba(167,139,250,.4);box-shadow:0 0 0 3px rgba(167,139,250,.07);}
input::placeholder,textarea::placeholder{color:var(--dimmer)}
input.mono,textarea.mono{font-family:'JetBrains Mono','Fira Code',monospace;font-size:10.5px}

.wmsg-row{display:flex;align-items:center;gap:6px;margin-bottom:6px;}
.wmsg-icon{font-size:14px;width:20px;text-align:center;flex-shrink:0;}
.wmsg-label{font-size:9.5px;color:var(--dimmer);width:52px;flex-shrink:0;letter-spacing:.04em;}
.wmsg-row input{flex:1;min-width:0;}
.streak-row{display:flex;align-items:center;gap:6px;margin-bottom:6px;}
.streak-n{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--accent);
  width:18px;text-align:center;flex-shrink:0;}
.streak-row input{flex:1;min-width:0;}
.map-row{display:flex;align-items:center;gap:6px;margin-bottom:6px;}
.map-name{font-size:9px;color:var(--dimmer);width:72px;flex-shrink:0;font-family:'JetBrains Mono',monospace;}
.map-row input{flex:1;min-width:0;}

.tog-row{display:flex;align-items:center;justify-content:space-between;
  padding:10px 12px;background:var(--s2);border:1px solid var(--b1);
  border-radius:var(--r);cursor:pointer;transition:border-color .2s,background .2s;
  -webkit-app-region:no-drag;margin-bottom:8px;}
.tog-row:hover{border-color:var(--b2)}
.tog-row.on{border-color:rgba(167,139,250,.22);background:rgba(167,139,250,.04);}
.tog-lbl{font-size:11.5px;font-weight:500;color:var(--dim);transition:color .2s;}
.tog-row.on .tog-lbl{color:var(--accent)}
.tswitch{width:32px;height:18px;border-radius:99px;background:var(--b2);
  position:relative;transition:background .25s;flex-shrink:0;}
.tog-row.on .tswitch{background:var(--accent2)}
.tswitch::after{content:'';position:absolute;top:2px;left:2px;width:14px;height:14px;
  border-radius:50%;background:rgba(240,240,248,.5);transition:transform .25s,background .25s;}
.tog-row.on .tswitch::after{transform:translateX(14px);background:#fff}

.btn{width:100%;padding:8px 12px;border-radius:var(--rsm);border:1px solid var(--b2);
  background:transparent;color:var(--dim);font-family:inherit;font-size:11px;
  font-weight:500;letter-spacing:.04em;cursor:pointer;transition:all .18s;-webkit-app-region:no-drag;}
.btn:hover{background:var(--s2);color:var(--white);border-color:var(--b2)}
.btn-accent{background:rgba(167,139,250,.12);border-color:rgba(167,139,250,.25);color:var(--accent);}
.btn-accent:hover{background:rgba(167,139,250,.2);color:var(--white);}
.btn-green{background:rgba(52,211,153,.1);border-color:rgba(52,211,153,.25);color:var(--green);}
.btn-green:hover{background:rgba(52,211,153,.18);color:var(--white);}
.btn-sm{padding:5px 10px;font-size:10px;width:auto;}
.btn-row{display:flex;gap:6px;margin-bottom:8px;}

.theme-row{display:flex;gap:6px;margin-bottom:10px;}
.swatch{width:28px;height:28px;border-radius:6px;border:2px solid transparent;
  cursor:pointer;transition:border-color .18s;-webkit-app-region:no-drag;}
.swatch.active{border-color:var(--white)}
.swatch-nebula{background:linear-gradient(135deg,#07080D,#A78BFA)}
.swatch-crimson{background:linear-gradient(135deg,#0D0709,#F87171)}
.swatch-arctic{background:linear-gradient(135deg,#070D12,#7DD3FC)}
.swatch-midnight{background:linear-gradient(135deg,#080810,#C084FC)}

.setup-step{padding:12px;background:var(--s2);border:1px solid var(--b1);border-radius:var(--r);margin-bottom:8px;}
.setup-step-num{font-size:9px;font-weight:600;letter-spacing:.15em;text-transform:uppercase;color:var(--dimmer);margin-bottom:4px;}
.setup-step-title{font-size:12px;font-weight:600;color:var(--white);margin-bottom:4px;}
.setup-step-desc{font-size:10.5px;color:var(--dim);line-height:1.5;margin-bottom:8px;}
.setup-status{display:flex;align-items:center;gap:5px;font-size:10px;font-weight:500;}
.setup-status.ok{color:var(--green)}
.setup-status.warn{color:var(--amber)}
.setup-status.err{color:var(--red)}

.vars-hint{padding:8px 10px;background:var(--s1);border:1px solid var(--b1);
  border-radius:var(--rsm);margin-bottom:10px;font-size:10px;color:var(--dimmer);line-height:1.8;}
.vars-hint code{font-family:'JetBrains Mono',monospace;font-size:9.5px;
  color:var(--accent);background:rgba(167,139,250,.1);padding:1px 4px;border-radius:3px;}

.ver{margin-top:auto;padding-top:12px;font-size:9px;letter-spacing:.06em;color:var(--dimmer);}

#main{grid-area:main;display:flex;flex-direction:column;background:var(--bg);overflow:hidden;}
#lhdr{display:flex;align-items:center;justify-content:space-between;padding:11px 18px 10px;
  border-bottom:1px solid var(--b1);background:rgba(7,8,13,0.6);backdrop-filter:blur(8px);}
#ltitle{font-size:9px;font-weight:600;letter-spacing:.15em;text-transform:uppercase;color:var(--dimmer);}
.lhdr-btns{display:flex;gap:6px;align-items:center;}
.lhdr-btn{font-size:9.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--dimmer);
  cursor:pointer;border:none;background:transparent;font-family:inherit;
  padding:3px 7px;border-radius:4px;transition:color .2s,background .2s;-webkit-app-region:no-drag;}
.lhdr-btn:hover{color:var(--white);background:var(--b1)}
#lbody{flex:1;overflow-y:auto;padding:12px 18px;scrollbar-width:thin;scrollbar-color:var(--b1) transparent;}
#lbody::-webkit-scrollbar{width:3px}
#lbody::-webkit-scrollbar-thumb{background:var(--b2);border-radius:3px}
.le{display:flex;gap:10px;padding:3.5px 0;border-bottom:1px solid rgba(255,255,255,.018);
  animation:fin .18s ease;line-height:1.5;}
@keyframes fin{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
.le-ts{font-family:'JetBrains Mono',monospace;font-size:9.5px;color:var(--dimmer);
  flex-shrink:0;padding-top:1px;min-width:72px;}
.le-msg{font-size:11.5px;word-break:break-all;}
.le.info .le-msg{color:var(--dim)}
.le.connect .le-msg{color:var(--accent)}
.le.kill .le-msg{color:var(--green)}
.le.hs .le-msg{color:var(--amber)}
.le.warn .le-msg{color:var(--amber)}
.le.error .le-msg{color:var(--red)}
.le.heart .le-msg{color:rgba(167,139,250,.45)}
.le.file .le-msg{color:rgba(167,139,250,.8)}
.le.key .le-msg{color:var(--accent)}
.le.streak .le-msg{color:var(--red)}
.le.round .le-msg{color:var(--sky)}
.le.death .le-msg{color:var(--red)}

#flash{position:fixed;inset:0;z-index:99;pointer-events:none;
  background:radial-gradient(ellipse at 62% 50%,rgba(52,211,153,.065) 0%,transparent 65%);
  opacity:0;transition:opacity .07s;}
#flash.show{opacity:1}
#flash.hs-show{background:radial-gradient(ellipse at 62% 50%,rgba(251,191,36,.055) 0%,transparent 65%);opacity:1}
</style>
</head>
<body>
<canvas id="stars"></canvas>
<div id="flash"></div>
<div id="shell">

<div id="bar">
  <div class="blogo">
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
      <path d="M8 1L9.8 5.8L15 6.3L11.3 9.7L12.5 15L8 12.3L3.5 15L4.7 9.7L1 6.3L6.2 5.8L8 1Z" fill="#A78BFA"/>
    </svg>
    Nebula Killsay
  </div>
  <span class="bsep"></span>
  <span id="hotkey-badge">F9 toggle</span>
  <span id="update-badge" onclick="openUpdate()">Update available</span>
  <span class="bsub">v3.0.0</span>
  <button class="wbtn" onclick="doMin()">
    <svg width="10" height="2" viewBox="0 0 10 2"><rect width="10" height="1.5" rx=".75" fill="currentColor"/></svg>
  </button>
  <button class="wbtn" id="bcl" onclick="doClose()">
    <svg width="9" height="9" viewBox="0 0 9 9">
      <line x1="1" y1="1" x2="8" y2="8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
      <line x1="8" y1="1" x2="1" y2="8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    </svg>
  </button>
</div>

<div id="side">
  <div style="margin-bottom:12px">
    <div class="sl">Status</div>
    <div id="spill" style="margin-bottom:8px"><div id="sdot"></div><span id="stxt">Waiting for CS2</span></div>
    <div id="sid-row" style="display:flex;align-items:center;gap:6px;margin-bottom:8px">
      <div id="sid-pill" style="flex:1;display:flex;align-items:center;gap:5px;padding:4px 9px;
           border-radius:99px;background:var(--s2);border:1px solid var(--b1);font-size:9.5px;min-width:0;overflow:hidden;">
        <span id="sid-dot" style="width:5px;height:5px;border-radius:50%;background:var(--dimmer);flex-shrink:0;transition:background .3s"></span>
        <span id="sid-txt" style="color:var(--dimmer);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:'JetBrains Mono',monospace;font-size:9px">No SteamID locked</span>
      </div>
      <button class="btn btn-sm" onclick="clearSteamId()" style="width:auto;padding:4px 8px;font-size:9px;flex-shrink:0;opacity:.5">x</button>
    </div>
    <div id="kbox">
      <div id="knum">0</div>
      <div id="ksub">session kills</div>
      <div id="streak-badge">x2 streak</div>
    </div>
    <div id="stats-row">
      <div class="stat-box"><div class="stat-val" id="stat-kpm">0.0</div><div class="stat-lbl">KPM</div></div>
      <div class="stat-box"><div class="stat-val" id="stat-hs">0%</div><div class="stat-lbl">HS%</div></div>
      <div class="stat-box"><div class="stat-val" id="stat-best">0</div><div class="stat-lbl">Best str.</div></div>
    </div>
    <div id="map-pill" style="display:none;align-items:center;gap:5px;margin-bottom:8px;
         padding:4px 9px;border-radius:99px;background:var(--s2);border:1px solid var(--b1);
         font-size:9.5px;color:var(--dimmer);font-family:'JetBrains Mono',monospace;">
      Map: <span id="map-name"></span>
    </div>
    <div class="btn-row">
      <button class="btn btn-sm" style="flex:1" onclick="doReset()">Reset</button>
      <button class="btn btn-sm btn-accent" style="flex:1" onclick="doExportPack()">Export pack</button>
    </div>
    <div class="btn-row">
      <label class="btn btn-sm" style="flex:1;text-align:center;cursor:pointer">
        Import pack<input type="file" accept=".json" style="display:none" onchange="doImportPack(event)">
      </label>
    </div>
  </div>

  <div><div class="sl">Recent</div><div id="kfeed"></div></div>
  <div class="sdiv"></div>

  <div class="tabs">
    <button class="tab active" onclick="switchTab('cfg')">Config</button>
    <button class="tab" onclick="switchTab('weapons')">Weapons</button>
    <button class="tab" onclick="switchTab('streaks')">Streaks</button>
    <button class="tab" onclick="switchTab('maps')">Maps</button>
    <button class="tab" onclick="switchTab('setup')">Setup</button>
  </div>

  <!-- CONFIG tab -->
  <div id="tab-cfg" class="tab-panel active">
    <div class="vars-hint">Variables: <code>{weapon}</code> <code>{streak}</code> <code>{kills}</code> <code>{map}</code> <code>{hs}</code></div>
    <div class="field">
      <label class="fl">Kill messages <span class="fl-hint">one per line or comma-sep</span></label>
      <textarea id="imsg" rows="3" placeholder="nebula.gg&#10;got {weapon}&#10;x{streak} and counting" oninput="doSave()" spellcheck="false"></textarea>
    </div>
    <div class="field">
      <label class="fl">Death message</label>
      <input type="text" id="death-msg" placeholder="unlucky" oninput="doSave()" autocomplete="off"/>
    </div>
    <div class="field">
      <label class="fl">Round win message</label>
      <input type="text" id="round-win-msg" placeholder="gg" oninput="doSave()" autocomplete="off"/>
    </div>
    <div class="field">
      <label class="fl">Round loss message</label>
      <input type="text" id="round-loss-msg" placeholder="" oninput="doSave()" autocomplete="off"/>
    </div>
    <div class="field">
      <label class="fl">Milestone <span class="fl-hint">msg every N kills (0=off)</span></label>
      <div style="display:flex;gap:6px">
        <input type="number" id="milestone-n" placeholder="5" min="0" max="100" oninput="doSave()" style="width:70px;flex-shrink:0"/>
        <input type="text" id="milestone-msg" placeholder="hit {kills} kills!" oninput="doSave()" autocomplete="off"/>
      </div>
    </div>
    <div class="field">
      <label class="fl">CFG path</label>
      <input type="text" id="ipath" class="mono" placeholder="auto-detected" oninput="doSave()" autocomplete="off"/>
    </div>
    <div class="field">
      <label class="fl">Cooldown (ms)</label>
      <input id="icd" type="number" placeholder="3000" min="500" max="30000" oninput="doSave()"/>
    </div>
    <div class="tog-row" id="t-enabled" onclick="toggleOpt('enabled')"><span class="tog-lbl">Automation enabled</span><div class="tswitch"></div></div>
    <div class="tog-row" id="t-random" onclick="toggleOpt('random')"><span class="tog-lbl">Random message order</span><div class="tswitch"></div></div>
    <div class="tog-row" id="t-sound" onclick="toggleOpt('sound')"><span class="tog-lbl">Kill sound</span><div class="tswitch"></div></div>
    <div class="tog-row" id="t-death_enabled" onclick="toggleOpt('death_enabled')"><span class="tog-lbl">Send death message</span><div class="tswitch"></div></div>
    <div class="tog-row" id="t-round_enabled" onclick="toggleOpt('round_enabled')"><span class="tog-lbl">Send round messages</span><div class="tswitch"></div></div>
    <div class="sl" style="margin-top:4px">Theme</div>
    <div class="theme-row">
      <div class="swatch swatch-nebula active" id="sw-nebula" onclick="setTheme('nebula')" title="Nebula"></div>
      <div class="swatch swatch-crimson" id="sw-crimson" onclick="setTheme('crimson')" title="Crimson"></div>
      <div class="swatch swatch-arctic" id="sw-arctic" onclick="setTheme('arctic')" title="Arctic"></div>
      <div class="swatch swatch-midnight" id="sw-midnight" onclick="setTheme('midnight')" title="Midnight"></div>
    </div>
  </div>

  <!-- WEAPONS tab -->
  <div id="tab-weapons" class="tab-panel">
    <div class="vars-hint">Variables: <code>{weapon}</code> <code>{streak}</code> <code>{kills}</code> <code>{hs}</code></div>
    <div style="font-size:10px;color:var(--dimmer);margin-bottom:10px;line-height:1.5">Override for specific weapon types. Leave blank to use default pool.</div>
    <div class="wmsg-row"><span class="wmsg-icon">knife</span><span class="wmsg-label">Knife</span><input type="text" id="w-knife" placeholder="default" oninput="doSave()" autocomplete="off"/></div>
    <div class="wmsg-row"><span class="wmsg-icon">awp</span><span class="wmsg-label">AWP</span><input type="text" id="w-awp" placeholder="default" oninput="doSave()" autocomplete="off"/></div>
    <div class="wmsg-row"><span class="wmsg-icon">gun</span><span class="wmsg-label">Pistol</span><input type="text" id="w-pistol" placeholder="default" oninput="doSave()" autocomplete="off"/></div>
    <div class="wmsg-row"><span class="wmsg-icon">nade</span><span class="wmsg-label">Grenade</span><input type="text" id="w-grenade" placeholder="default" oninput="doSave()" autocomplete="off"/></div>
    <div class="wmsg-row"><span class="wmsg-icon">zap</span><span class="wmsg-label">Zeus</span><input type="text" id="w-zeus" placeholder="default" oninput="doSave()" autocomplete="off"/></div>
  </div>

  <!-- STREAKS tab -->
  <div id="tab-streaks" class="tab-panel">
    <div class="vars-hint">Variables: <code>{streak}</code> <code>{kills}</code></div>
    <div style="font-size:10px;color:var(--dimmer);margin-bottom:10px;line-height:1.5">Override at kill streak thresholds.</div>
    <div class="streak-row"><span class="streak-n">x3</span><input type="text" id="s-3" placeholder="triple kill" oninput="doSave()" autocomplete="off"/></div>
    <div class="streak-row"><span class="streak-n">x5</span><input type="text" id="s-5" placeholder="rampage" oninput="doSave()" autocomplete="off"/></div>
    <div class="streak-row"><span class="streak-n">x7</span><input type="text" id="s-7" placeholder="unstoppable" oninput="doSave()" autocomplete="off"/></div>
    <div class="streak-row"><span class="streak-n">x10</span><input type="text" id="s-10" placeholder="godlike" oninput="doSave()" autocomplete="off"/></div>
    <div style="margin-top:6px">
      <label class="fl">Streak window (seconds)</label>
      <input id="s-window" type="number" placeholder="60" min="5" max="300" oninput="doSave()"/>
    </div>
  </div>

  <!-- MAPS tab -->
  <div id="tab-maps" class="tab-panel">
    <div class="vars-hint">Variables: <code>{weapon}</code> <code>{streak}</code> <code>{kills}</code></div>
    <div style="font-size:10px;color:var(--dimmer);margin-bottom:10px;line-height:1.5">Override kill message on specific maps.</div>
    <div class="map-row"><span class="map-name">de_dust2</span><input type="text" id="m-dust2" placeholder="default" oninput="doSave()" autocomplete="off"/></div>
    <div class="map-row"><span class="map-name">de_mirage</span><input type="text" id="m-mirage" placeholder="default" oninput="doSave()" autocomplete="off"/></div>
    <div class="map-row"><span class="map-name">de_inferno</span><input type="text" id="m-inferno" placeholder="default" oninput="doSave()" autocomplete="off"/></div>
    <div class="map-row"><span class="map-name">de_nuke</span><input type="text" id="m-nuke" placeholder="default" oninput="doSave()" autocomplete="off"/></div>
    <div class="map-row"><span class="map-name">de_overpass</span><input type="text" id="m-overpass" placeholder="default" oninput="doSave()" autocomplete="off"/></div>
    <div class="map-row"><span class="map-name">de_ancient</span><input type="text" id="m-ancient" placeholder="default" oninput="doSave()" autocomplete="off"/></div>
    <div class="map-row"><span class="map-name">de_anubis</span><input type="text" id="m-anubis" placeholder="default" oninput="doSave()" autocomplete="off"/></div>
    <div class="map-row"><span class="map-name">de_vertigo</span><input type="text" id="m-vertigo" placeholder="default" oninput="doSave()" autocomplete="off"/></div>
  </div>

  <!-- SETUP tab -->
  <div id="tab-setup" class="tab-panel">
    <div class="setup-step">
      <div class="setup-step-num">Step 1</div>
      <div class="setup-step-title">CS2 Installation</div>
      <div class="setup-step-desc">Detect where your CS2 cfg folder is located.</div>
      <div id="setup-cs2-status" class="setup-status warn">Not checked yet</div>
      <div style="margin-top:8px"><button class="btn btn-green" onclick="doSetupDetect()">Auto-detect CS2</button></div>
    </div>
    <div class="setup-step">
      <div class="setup-step-num">Step 2</div>
      <div class="setup-step-title">GSI Config</div>
      <div class="setup-step-desc">Installs the Game State Integration file so CS2 sends real-time data. Safe — does not modify game files.</div>
      <div id="setup-gsi-status" class="setup-status warn">Not installed</div>
      <div style="margin-top:8px"><button class="btn btn-green" onclick="doSetupGSI()">Install GSI config</button></div>
    </div>
    <div class="setup-step">
      <div class="setup-step-num">Step 3</div>
      <div class="setup-step-title">CS2 Bind</div>
      <div class="setup-step-desc">Add to your CS2 autoexec.cfg or run in console:</div>
      <div style="background:var(--s1);border:1px solid var(--b2);border-radius:var(--rsm);
           padding:8px 10px;font-family:'JetBrains Mono',monospace;font-size:10px;
           color:var(--accent);margin-bottom:8px;user-select:text;-webkit-app-region:no-drag;">
        bind "F13" "exec killsay"
      </div>
      <button class="btn" onclick="copyBind()">Copy to clipboard</button>
    </div>
    <div class="setup-step">
      <div class="setup-step-num">Step 4</div>
      <div class="setup-step-title">Launch CS2</div>
      <div class="setup-step-desc">Start CS2 and load into a match. The status indicator will turn green.</div>
      <div id="setup-conn-status" class="setup-status warn">Waiting for CS2...</div>
    </div>
  </div>

  <div class="ver">Nebula Killsay v3.0.0</div>
</div>

<div id="main">
  <div id="lhdr">
    <span id="ltitle">Event log</span>
    <div class="lhdr-btns">
      <button class="lhdr-btn" onclick="doExport()">Export log</button>
      <button class="lhdr-btn" onclick="doClear()">Clear</button>
    </div>
  </div>
  <div id="lbody"></div>
</div>
</div>

<script>
/* starfield */
(function(){
  const cv=document.getElementById('stars'),ctx=cv.getContext('2d');
  function r(){cv.width=innerWidth;cv.height=innerHeight;}r();
  window.addEventListener('resize',r);
  const L=[{n:480,r:.55,spd:.10,a:.30},{n:130,r:1.05,spd:.26,a:.50},{n:55,r:1.70,spd:.52,a:.70}];
  const stars=[];
  L.forEach((l,li)=>{for(let i=0;i<l.n;i++)
    stars.push({x:Math.random()*innerWidth,y:Math.random()*innerHeight,li,r:l.r*(0.7+Math.random()*.6),tw:Math.random()*Math.PI*2});});
  let prev=performance.now();
  (function tick(now){
    const dt=(now-prev)/1000;prev=now;
    const g=ctx.createLinearGradient(0,0,0,cv.height);
    g.addColorStop(0,'#07080D');g.addColorStop(1,'#090B18');
    ctx.fillStyle=g;ctx.fillRect(0,0,cv.width,cv.height);
    for(const s of stars){
      const l=L[s.li];s.y+=l.spd*dt*60;s.tw+=dt*1.1;
      if(s.y>cv.height+4){s.y=-4;s.x=Math.random()*cv.width;}
      const tw=0.55+0.45*Math.sin(s.tw);
      ctx.beginPath();ctx.arc(s.x,s.y,s.r,0,Math.PI*2);
      ctx.fillStyle=`rgba(215,210,255,${l.a*tw})`;ctx.fill();
    }
    requestAnimationFrame(tick);
  })(performance.now());
})();

/* audio */
let _ac=null;
function getAC(){if(!_ac)_ac=new(window.AudioContext||window.webkitAudioContext)();return _ac;}
function playKillSound(isHs){
  try{
    const ac=getAC(),osc=ac.createOscillator(),gain=ac.createGain();
    osc.connect(gain);gain.connect(ac.destination);osc.type='sine';
    osc.frequency.setValueAtTime(isHs?880:660,ac.currentTime);
    osc.frequency.exponentialRampToValueAtTime(isHs?1200:440,ac.currentTime+0.12);
    gain.gain.setValueAtTime(0.12,ac.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001,ac.currentTime+0.18);
    osc.start(ac.currentTime);osc.stop(ac.currentTime+0.18);
  }catch(e){}
}

let seq=0;
const opts={enabled:true,random:true,sound:true,death_enabled:true,round_enabled:true};
let logLines=[];
let updateUrl='';

async function boot(){
  try{
    const r=await fetch('/api/settings');
    const s=await r.json();
    document.getElementById('imsg').value=(s.messages||[s.message||'nebula.gg']).join('\n');
    document.getElementById('icd').value=s.cooldown_ms??3000;
    document.getElementById('ipath').value=s.cfg_path||'';
    document.getElementById('death-msg').value=s.death_message||'';
    document.getElementById('round-win-msg').value=s.round_win_message||'';
    document.getElementById('round-loss-msg').value=s.round_loss_message||'';
    document.getElementById('milestone-n').value=s.milestone_n??0;
    document.getElementById('milestone-msg').value=s.milestone_message||'';
    opts.enabled=s.enabled!==false;opts.random=s.random!==false;opts.sound=s.sound!==false;
    opts.death_enabled=s.death_enabled!==false;opts.round_enabled=s.round_enabled!==false;
    syncToggles();
    const wm=s.weapon_messages||{};
    ['knife','awp','pistol','grenade','zeus'].forEach(k=>document.getElementById('w-'+k).value=wm[k]||'');
    const sm=s.streak_messages||{};
    ['3','5','7','10'].forEach(k=>document.getElementById('s-'+k).value=sm[k]||'');
    document.getElementById('s-window').value=s.streak_window??60;
    const mm=s.map_messages||{};
    ['dust2','mirage','inferno','nuke','overpass','ancient','anubis','vertigo'].forEach(k=>{
      document.getElementById('m-'+k).value=mm['de_'+k]||'';
    });
    if(s.theme) setTheme(s.theme,true);
  }catch(e){log('error','Failed to load settings: '+e);}
  setInterval(poll,280);
}

async function poll(){
  try{
    const r=await fetch('/api/poll?seq='+seq);
    const d=await r.json();
    (d.logs||[]).forEach(e=>{log(e.tag,e.msg);seq=e.seq;});
    if(d.kills!=null) document.getElementById('knum').textContent=d.kills;
    if(d.connected!=null) setConn(d.connected);
    if(d.kill_flash) flashKill(false);
    if(d.hs_flash) flashKill(true);
    if(d.streak!=null) updateStreak(d.streak);
    if(d.kill_history) updateFeed(d.kill_history);
    if(d.steamid!=null) setSteamId(d.steamid);
    if(d.stats) updateStats(d.stats);
    if(d.current_map!=null) updateMap(d.current_map);
    if(d.update_available && d.update_url){updateUrl=d.update_url;document.getElementById('update-badge').style.display='inline-flex';}
    if(d.enabled!=null){opts.enabled=d.enabled;syncToggles();}
  }catch(e){}
}

function log(tag,msg){
  const b=document.getElementById('lbody');
  const d=document.createElement('div');
  d.className='le '+tag;
  const ts=new Date().toTimeString().slice(0,8);
  d.innerHTML=`<span class="le-ts">${ts}</span><span class="le-msg">${esc(msg)}</span>`;
  b.appendChild(d);logLines.push(ts+' ['+tag+'] '+msg);
  if(logLines.length>2000) logLines.shift();
  b.scrollTop=b.scrollHeight;
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

function setConn(on){
  document.getElementById('spill').classList.toggle('on',on);
  document.getElementById('stxt').textContent=on?'Connected to CS2':'Waiting for CS2';
  document.getElementById('setup-conn-status').className='setup-status '+(on?'ok':'warn');
  document.getElementById('setup-conn-status').textContent=on?'CS2 connected':'Waiting for CS2...';
}

function setSteamId(sid){
  const dot=document.getElementById('sid-dot'),txt=document.getElementById('sid-txt');
  if(sid){dot.style.background='var(--green)';txt.style.color='var(--dim)';txt.textContent=sid;}
  else{dot.style.background='var(--dimmer)';txt.style.color='var(--dimmer)';txt.textContent='No SteamID locked';}
}
async function clearSteamId(){
  await fetch('/api/reset_steamid',{method:'POST'}).catch(()=>{});setSteamId('');log('warn','SteamID cleared');
}

function flashKill(isHs){
  const fl=document.getElementById('flash'),kn=document.getElementById('knum');
  fl.className='show'+(isHs?' hs-show':'');kn.className='flash'+(isHs?' hs-flash':'');
  if(opts.sound) playKillSound(isHs);
  setTimeout(()=>{fl.className='';kn.className='';},220);
}

function updateStreak(n){
  const b=document.getElementById('streak-badge');
  if(n<2){b.className='streak-badge';return;}
  const rampage=n>=8,fire=n>=5;
  b.className='streak-badge show'+(rampage?' rampage':fire?' fire':'');
  b.textContent=(rampage?'x':fire?'x':'x')+n+' streak';
}

function updateStats(s){
  document.getElementById('stat-kpm').textContent=s.kpm??'0.0';
  document.getElementById('stat-hs').textContent=(s.hs_pct??0)+'%';
  document.getElementById('stat-best').textContent=s.best_streak??0;
}

function updateMap(m){
  const pill=document.getElementById('map-pill');
  if(m){pill.style.display='flex';document.getElementById('map-name').textContent=m;}
  else{pill.style.display='none';}
}

const WEAPON_ICONS={knife:'K',awp:'A',pistol:'P',grenade:'G',zeus:'Z',default:'R'};
function updateFeed(history){
  const f=document.getElementById('kfeed');f.innerHTML='';
  history.slice(0,6).forEach(k=>{
    const d=document.createElement('div');
    const extra=k.type==='death'?' death':k.type==='round_win'?' round-w':'';
    d.className='kfeed-item'+extra;
    const typeIcon=k.type==='death'?'[D]':k.type==='round_win'?'[W]':k.type==='round_loss'?'[L]':'[K]';
    const hsTag=k.headshot?`<span class="kfeed-hs"> HS</span>`:'';
    d.innerHTML=`<span class="kfeed-icon" style="font-size:9px;color:var(--dimmer)">${typeIcon}</span>`+
      `<span class="kfeed-msg">${esc(k.msg)}${hsTag}</span>`+
      `<span class="kfeed-ts">${k.time||''}</span>`;
    f.appendChild(d);
  });
}

function switchTab(name){
  const names=['cfg','weapons','streaks','maps','setup'];
  document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active',names[i]===name));
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.toggle('active',p.id==='tab-'+name));
}

function toggleOpt(key){
  opts[key]=!opts[key];syncToggles();doSave();
  if(key==='enabled') log(opts.enabled?'connect':'warn',opts.enabled?'Automation enabled':'Automation disabled');
}
function syncToggles(){
  Object.keys(opts).forEach(k=>{
    const el=document.getElementById('t-'+k);
    if(!el) return;
    opts[k]?el.classList.add('on'):el.classList.remove('on');
  });
}

function setTheme(name,silent=false){
  document.body.className=name==='nebula'?'':'theme-'+name;
  document.querySelectorAll('.swatch').forEach(s=>s.classList.remove('active'));
  const sw=document.getElementById('sw-'+name);if(sw)sw.classList.add('active');
  if(!silent) fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({theme:name})}).catch(()=>{});
}

function doSave(){
  const raw=document.getElementById('imsg').value;
  const messages=raw.split(/[\n,]/).map(m=>m.trim()).filter(Boolean);
  const wm={};['knife','awp','pistol','grenade','zeus'].forEach(k=>{wm[k]=document.getElementById('w-'+k).value.trim();});
  const sm={};['3','5','7','10'].forEach(k=>{sm[k]=document.getElementById('s-'+k).value.trim();});
  const mm={};['dust2','mirage','inferno','nuke','overpass','ancient','anubis','vertigo'].forEach(k=>{
    const v=document.getElementById('m-'+k).value.trim();if(v) mm['de_'+k]=v;
  });
  fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      messages,message:messages[0]||'',
      cfg_path:document.getElementById('ipath').value,
      cooldown_ms:parseInt(document.getElementById('icd').value)||3000,
      streak_window:parseInt(document.getElementById('s-window').value)||60,
      death_message:document.getElementById('death-msg').value.trim(),
      round_win_message:document.getElementById('round-win-msg').value.trim(),
      round_loss_message:document.getElementById('round-loss-msg').value.trim(),
      milestone_n:parseInt(document.getElementById('milestone-n').value)||0,
      milestone_message:document.getElementById('milestone-msg').value.trim(),
      enabled:opts.enabled,random:opts.random,sound:opts.sound,
      death_enabled:opts.death_enabled,round_enabled:opts.round_enabled,
      weapon_messages:wm,streak_messages:sm,map_messages:mm,
    })
  }).catch(()=>{});
}

async function doReset(){
  await fetch('/api/reset',{method:'POST'}).catch(()=>{});
  document.getElementById('knum').textContent='0';
  document.getElementById('kfeed').innerHTML='';
  updateStreak(0);updateStats({kpm:'0.0',hs_pct:0,best_streak:0});
  log('info','Session reset.');
}

function doExport(){
  const blob=new Blob([logLines.join('\n')],{type:'text/plain'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='nebula_log_'+new Date().toISOString().slice(0,10)+'.txt';a.click();
}
function doClear(){document.getElementById('lbody').innerHTML='';logLines=[];}

async function doExportPack(){
  const r=await fetch('/api/settings');const s=await r.json();
  const pack={name:'My Nebula Pack',version:'1',
    messages:s.messages,weapon_messages:s.weapon_messages,streak_messages:s.streak_messages,
    map_messages:s.map_messages,death_message:s.death_message,
    round_win_message:s.round_win_message,round_loss_message:s.round_loss_message,
    milestone_n:s.milestone_n,milestone_message:s.milestone_message};
  const blob=new Blob([JSON.stringify(pack,null,2)],{type:'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='nebula_pack.json';a.click();
}
function doImportPack(evt){
  const file=evt.target.files[0];if(!file) return;
  const reader=new FileReader();
  reader.onload=async e=>{
    try{
      const pack=JSON.parse(e.target.result);
      await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(pack)});
      location.reload();
    }catch(err){log('error','Invalid pack file: '+err);}
  };
  reader.readAsText(file);
}

async function doSetupDetect(){
  const r=await fetch('/api/setup/detect');const d=await r.json();
  const el=document.getElementById('setup-cs2-status');
  if(d.found){
    el.className='setup-status ok';el.textContent='Found: '+d.cfg_dir;
    document.getElementById('ipath').value=d.cfg_path;doSave();
  } else {
    el.className='setup-status err';el.textContent='CS2 not found - set path manually in Config tab';
  }
}
async function doSetupGSI(){
  const r=await fetch('/api/setup/gsi',{method:'POST'});const d=await r.json();
  const el=document.getElementById('setup-gsi-status');
  if(d.ok){el.className='setup-status ok';el.textContent='Installed: '+d.path;}
  else{el.className='setup-status err';el.textContent='Error: '+d.error;}
}
function copyBind(){navigator.clipboard.writeText('bind "F13" "exec killsay"').catch(()=>{});log('info','Bind command copied');}
function openUpdate(){if(updateUrl) window.open(updateUrl,'_blank');}
function doClose(){fetch('/api/close',{method:'POST'}).catch(()=>{});}
function doMin(){fetch('/api/minimise',{method:'POST'}).catch(()=>{});}

window.addEventListener('DOMContentLoaded',boot);
</script>
</body>
</html>
"""


# =============================================================================
#  Settings
# =============================================================================
def load_settings() -> dict:
    defaults = {
        "messages":            ["nebula.gg"],
        "message":             "nebula.gg",
        "cfg_path":            DEFAULT_CFG_PATH,
        "cooldown_ms":         3000,
        "enabled":             True,
        "random":              True,
        "sound":               True,
        "weapon_messages":     {"knife":"","awp":"","pistol":"","grenade":"","zeus":""},
        "streak_messages":     {"3":"","5":"","7":"","10":""},
        "streak_window":       60,
        "map_messages":        {},
        "death_message":       "",
        "death_enabled":       True,
        "round_win_message":   "",
        "round_loss_message":  "",
        "round_enabled":       True,
        "milestone_n":         0,
        "milestone_message":   "",
        "theme":               "nebula",
        "my_steamid":          "",
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                defaults.update(json.load(f))
        except Exception:
            pass
    return defaults

def save_settings(s: dict):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


# =============================================================================
#  CS2 helpers
# =============================================================================
def write_killsay_cfg(path: str, message: str):
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(tmp, "w") as f:
        f.write(f'say "{message}"\n')
    os.replace(tmp, path)

def press_f13():
    if WIN32_AVAILABLE:
        win32api.keybd_event(F13_VK, 0, 0, 0)
        time.sleep(0.05)
        win32api.keybd_event(F13_VK, 0, win32con.KEYEVENTF_KEYUP, 0)

def ts_short():
    return datetime.datetime.now().strftime("%H:%M:%S")


# =============================================================================
#  Shared state
# =============================================================================
settings        = load_settings()
state_lock      = threading.Lock()

kills_prev      = -1
headshots_prev  = -1
health_prev     = -1
last_kill_t     = 0.0
last_death_t    = 0.0
last_round_t    = 0.0
total_kills     = 0
total_hs        = 0
session_start   = time.time()
gsi_connected   = False
last_gsi_t      = 0.0
kill_flash      = False
hs_flash        = False
my_steamid      = settings.get("my_steamid", "")
current_map     = ""
last_round_phase = ""

streak_kills    = 0
streak_times    : list = []
best_streak     = 0
msg_rr_index    = 0
kill_history    : list = []
streak_ui       = 0
last_weapon_cat = "default"

update_available = False
update_url       = ""

log_queue : list = []
log_seq   = 0
log_lock  = threading.Lock()

def add_log(msg: str, tag: str = "info"):
    global log_seq
    with log_lock:
        log_seq += 1
        log_queue.append({"seq": log_seq, "tag": tag, "msg": msg})
        if len(log_queue) > 1000:
            log_queue.pop(0)


# =============================================================================
#  Message selection
# =============================================================================
def pick_message(weapon_cat: str, current_streak: int,
                 is_headshot: bool = False, cmap: str = "") -> str:
    with state_lock:
        sm   = settings.get("streak_messages", {})
        wm   = settings.get("weapon_messages", {})
        mm   = settings.get("map_messages", {})
        pool = settings.get("messages", ["nebula.gg"]) or ["nebula.gg"]
        rnd  = settings.get("random", True)
        tot  = total_kills

    # 1. streak
    for thresh in [10, 7, 5, 3]:
        candidate = sm.get(str(thresh), "").strip()
        if candidate and current_streak >= thresh:
            return apply_variables(candidate, weapon_cat, current_streak, tot, cmap, is_headshot)

    # 2. map override
    map_msg = mm.get(cmap, "").strip() if cmap else ""
    if map_msg:
        return apply_variables(map_msg, weapon_cat, current_streak, tot, cmap, is_headshot)

    # 3. weapon override
    weapon_msg = wm.get(weapon_cat, "").strip()
    if weapon_msg:
        return apply_variables(weapon_msg, weapon_cat, current_streak, tot, cmap, is_headshot)

    # 4. pool
    global msg_rr_index
    if not pool:
        return "nebula.gg"
    base = random.choice(pool) if rnd else pool[msg_rr_index % len(pool)]
    if not rnd:
        msg_rr_index += 1
    return apply_variables(base, weapon_cat, current_streak, tot, cmap, is_headshot)


def send_message(msg: str, path: str) -> bool:
    try:
        write_killsay_cfg(path, msg)
        add_log(f'say "{msg}"', "file")
    except Exception as exc:
        add_log(f"Error writing cfg: {exc}", "error")
        return False
    try:
        press_f13()
        if WIN32_AVAILABLE:
            add_log("F13 pressed", "key")
    except Exception as exc:
        add_log(f"Error pressing F13: {exc}", "error")
    return True


# =============================================================================
#  Update checker
# =============================================================================
def check_for_updates():
    global update_available, update_url
    if not REQUESTS_AVAILABLE:
        return
    try:
        resp = req_lib.get(UPDATE_URL, timeout=5)
        latest = resp.text.strip()
        if latest and latest != VERSION:
            update_available = True
            update_url = "https://github.com/yourusername/nebula-killsay/releases/latest"
            add_log(f"Update available: v{latest}", "warn")
    except Exception:
        pass


# =============================================================================
#  Flask
# =============================================================================
flask_app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

@flask_app.route("/")
def serve_ui():
    return Response(HTML, mimetype="text/html")

@flask_app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify(settings)

@flask_app.route("/api/settings", methods=["POST"])
def api_post_settings():
    global settings
    data = request.get_json(force=True, silent=True) or {}
    with state_lock:
        settings.update(data)
        save_settings(settings)
    return jsonify({"ok": True})

@flask_app.route("/api/poll")
def api_poll():
    global kill_flash, hs_flash, streak_ui, gsi_connected, last_gsi_t

    with state_lock:
        if gsi_connected and last_gsi_t > 0 and (time.time() - last_gsi_t) > GSI_TIMEOUT:
            gsi_connected = False
            add_log("GSI timeout — CS2 disconnected", "warn")

    with log_lock:
        new_logs = [e for e in log_queue if e["seq"] > int(request.args.get("seq", 0))]
    with state_lock:
        kf    = kill_flash;  kill_flash = False
        hf    = hs_flash;    hs_flash   = False
        kills = total_kills
        conn  = gsi_connected
        su    = streak_ui
        hist  = list(kill_history[:8])
        sid   = my_steamid
        cmap  = current_map
        en    = settings.get("enabled", True)
        elapsed = max(1, time.time() - session_start)
        kpm_val = round(total_kills / (elapsed / 60), 1)
        hs_pct  = round(total_hs / total_kills * 100) if total_kills else 0
        bs      = best_streak

    return jsonify({
        "logs":            new_logs,
        "kills":           kills,
        "connected":       conn,
        "kill_flash":      kf,
        "hs_flash":        hf,
        "streak":          su,
        "kill_history":    hist,
        "steamid":         sid,
        "current_map":     cmap,
        "enabled":         en,
        "stats":           {"kpm": kpm_val, "hs_pct": hs_pct, "best_streak": bs},
        "update_available": update_available,
        "update_url":      update_url,
    })

@flask_app.route("/api/reset", methods=["POST"])
def api_reset():
    global total_kills, total_hs, streak_kills, streak_times, streak_ui, kill_history, best_streak, session_start
    with state_lock:
        total_kills  = 0;  total_hs    = 0
        streak_kills = 0;  streak_times = []
        streak_ui    = 0;  kill_history = []
        best_streak  = 0;  session_start = time.time()
    return jsonify({"ok": True})

@flask_app.route("/api/reset_steamid", methods=["POST"])
def api_reset_steamid():
    global my_steamid, kills_prev, headshots_prev, health_prev
    with state_lock:
        my_steamid = ""; kills_prev = -1; headshots_prev = -1; health_prev = -1
        settings["my_steamid"] = ""; save_settings(settings)
    add_log("SteamID cleared", "warn")
    return jsonify({"ok": True})

@flask_app.route("/api/close", methods=["POST"])
def api_close():
    def _quit():
        time.sleep(0.12); save_settings(settings)
        if _window: _window.destroy()
    threading.Thread(target=_quit, daemon=True).start()
    return jsonify({"ok": True})

@flask_app.route("/api/minimise", methods=["POST"])
def api_minimise():
    threading.Thread(target=_minimize_to_tray, daemon=True).start()
    return jsonify({"ok": True})

@flask_app.route("/api/setup/detect")
def api_setup_detect():
    cfg_dir  = detect_cs2_cfg_dir()
    cfg_path = os.path.join(cfg_dir, "killsay.cfg") if cfg_dir else ""
    if cfg_dir:
        with state_lock:
            settings["cfg_path"] = cfg_path; save_settings(settings)
        return jsonify({"found": True, "cfg_dir": cfg_dir, "cfg_path": cfg_path})
    return jsonify({"found": False})

@flask_app.route("/api/setup/gsi", methods=["POST"])
def api_setup_gsi():
    gsi_path = DEFAULT_GSI_PATH
    if not gsi_path:
        cfg_dir = detect_cs2_cfg_dir()
        if not cfg_dir:
            return jsonify({"ok": False, "error": "CS2 not found — run auto-detect first"})
        gsi_path = os.path.join(cfg_dir, "gamestate_integration_nebula.cfg")
    try:
        os.makedirs(os.path.dirname(gsi_path), exist_ok=True)
        with open(gsi_path, "w") as f:
            f.write(GSI_CONFIG_CONTENT)
        add_log(f"GSI config written: {gsi_path}", "info")
        return jsonify({"ok": True, "path": gsi_path})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


# -- GSI endpoint -------------------------------------------------------------
@flask_app.route("/gsi", methods=["POST"])
def gsi():
    data = request.get_json(force=True, silent=True) or {}
    threading.Thread(target=process_gsi, args=(data,), daemon=True).start()
    return "", 200

def process_gsi(data: dict):
    global gsi_connected, last_gsi_t, my_steamid, kills_prev, headshots_prev
    global health_prev, last_round_phase, current_map, last_weapon_cat

    with state_lock:
        gsi_connected = True
        last_gsi_t    = time.time()

    provider_steamid = data.get("provider", {}).get("steamid", "")
    player_steamid   = data.get("player", {}).get("steamid", "") or provider_steamid
    activity         = data.get("player", {}).get("activity", "")

    # Map tracking
    map_name = data.get("map", {}).get("name", "")
    if map_name and map_name != current_map:
        current_map = map_name
        add_log(f"Map: {map_name}", "round")

    # SteamID locking
    with state_lock:
        locked_id = my_steamid
    if not locked_id and player_steamid and activity == "playing":
        with state_lock:
            my_steamid = player_steamid
            settings["my_steamid"] = player_steamid
            save_settings(settings)
        add_log(f"SteamID locked: {player_steamid}", "connect")
    if locked_id and player_steamid and player_steamid != locked_id:
        return
    if activity not in ("playing", ""):
        return

    player_state = data.get("player", {}).get("state", {})
    current_hs   = player_state.get("headshots", None)
    current_hp   = player_state.get("health", None)

    # Round phase
    round_data   = data.get("round", {})
    round_phase  = round_data.get("phase", "")
    round_win    = round_data.get("win_team", "")

    if round_phase != last_round_phase:
        last_round_phase = round_phase
        if round_phase in ("freezetime", "over"):
            with state_lock:
                if streak_kills > 0:
                    add_log(f"Round ended — streak of {streak_kills} reset", "round")
                _reset_streak()
        if round_phase == "over":
            _handle_round_end(round_win, data)

    pms = data.get("player", {}).get("match_stats", {}) or data.get("player_match_stats", {})
    if not pms:
        add_log("Heartbeat received", "heart")
        return
    if activity != "playing":
        return

    current_kills = pms.get("kills", 0)

    with state_lock:
        prev    = kills_prev
        prev_hs = headshots_prev
        prev_hp = health_prev

    if prev == -1:
        with state_lock:
            kills_prev     = current_kills
            headshots_prev = current_hs if current_hs is not None else 0
            health_prev    = current_hp if current_hp is not None else 100
        add_log(f"Tracker initialised — kills: {current_kills}", "info")
        return

    # Death detection
    if current_hp is not None:
        if prev_hp is not None and prev_hp > 0 and current_hp == 0:
            _on_death()
        with state_lock:
            health_prev = current_hp

    # Headshot detection
    is_headshot = False
    if current_hs is not None and prev_hs is not None and current_hs > prev_hs:
        is_headshot = True
    if current_hs is not None:
        with state_lock:
            headshots_prev = current_hs

    # Kill detection
    if current_kills > prev:
        delta = current_kills - prev
        with state_lock:
            kills_prev = current_kills

        add_log(f"Kill {'(HS) ' if is_headshot else ''}— total: {current_kills} (+{delta})", "hs" if is_headshot else "kill")

        weapons    = data.get("player", {}).get("weapons", {})
        weapon_cat = last_weapon_cat
        for slot_data in weapons.values():
            if slot_data.get("state") == "active":
                weapon_cat = classify_weapon(slot_data.get("name", ""))
                last_weapon_cat = weapon_cat
                break
        add_log(f"Weapon: {weapon_cat}", "info")

        with state_lock:
            tw = settings.get("streak_window", 60)
        t_now = time.time()
        _update_streak(t_now, tw)

        with state_lock:
            sk = streak_kills
        _set_streak_ui(sk)
        if sk >= 3:
            add_log(f"Kill streak x{sk}!", "streak")

        _on_kill(weapon_cat=weapon_cat, is_headshot=is_headshot, streak=sk)
    else:
        with state_lock:
            kills_prev = current_kills
        weapons = data.get("player", {}).get("weapons", {})
        for slot_data in weapons.values():
            if slot_data.get("state") == "active":
                last_weapon_cat = classify_weapon(slot_data.get("name", ""))
                break


def _reset_streak():
    global streak_kills, streak_times, streak_ui
    streak_kills = 0; streak_times = []; streak_ui = 0

def _update_streak(t_now, tw):
    global streak_kills, streak_times, best_streak
    streak_times = [t for t in streak_times if t_now - t < tw]
    streak_times.append(t_now)
    streak_kills = len(streak_times)
    if streak_kills > best_streak:
        best_streak = streak_kills

def _set_streak_ui(val):
    global streak_ui
    streak_ui = val

def _push_history(entry: dict):
    global kill_history
    with state_lock:
        kill_history.insert(0, entry)
        if len(kill_history) > 20:
            kill_history.pop()


def _on_kill(weapon_cat: str = "default", is_headshot: bool = False, streak: int = 0):
    global total_kills, total_hs, last_kill_t, kill_flash, hs_flash

    with state_lock:
        en   = settings.get("enabled", True)
        cd   = settings.get("cooldown_ms", 3000) / 1000.0
        path = settings.get("cfg_path", DEFAULT_CFG_PATH)
        mn   = settings.get("milestone_n", 0)
        mm   = settings.get("milestone_message", "")
        cmap = current_map

    with state_lock:
        total_kills += 1
        if is_headshot:
            total_hs += 1
        kill_flash = not is_headshot
        hs_flash   = is_headshot
        tk = total_kills

    # Milestone check
    if mn and mn > 0 and mm and tk % mn == 0:
        m_msg = apply_variables(mm, weapon_cat, streak, tk, cmap, is_headshot)
        add_log(f"Milestone {tk} kills: {m_msg}", "streak")
        if en:
            send_message(m_msg, path)
        _push_history({"msg": m_msg, "weapon": weapon_cat, "headshot": is_headshot,
                       "streak": streak, "time": ts_short(), "type": "milestone"})
        return

    if not en:
        add_log("Automation disabled — skipping", "warn")
        _push_history({"msg": "(disabled)", "weapon": weapon_cat, "headshot": is_headshot,
                       "streak": streak, "time": ts_short(), "type": "kill"})
        return

    now = time.time()
    if now - last_kill_t < cd:
        add_log(f"Cooldown active — skipping ({cd-(now-last_kill_t):.1f}s)", "warn")
        return

    last_kill_t = now
    msg = pick_message(weapon_cat, streak, is_headshot, cmap)
    send_message(msg, path)
    _push_history({"msg": msg, "weapon": weapon_cat, "headshot": is_headshot,
                   "streak": streak, "time": ts_short(), "type": "kill"})


def _on_death():
    global last_death_t
    with state_lock:
        en   = settings.get("enabled", True)
        den  = settings.get("death_enabled", True)
        dmsg = settings.get("death_message", "").strip()
        path = settings.get("cfg_path", DEFAULT_CFG_PATH)

    add_log("You died", "death")
    _push_history({"msg": dmsg or "(death)", "weapon": "", "headshot": False,
                   "streak": 0, "time": ts_short(), "type": "death"})

    if not en or not den or not dmsg:
        return
    now = time.time()
    if now - last_death_t < 3.0:
        return
    last_death_t = now
    send_message(dmsg, path)


def _handle_round_end(win_team: str, data: dict):
    global last_round_t
    with state_lock:
        en      = settings.get("enabled", True)
        ren     = settings.get("round_enabled", True)
        win_msg = settings.get("round_win_message", "").strip()
        los_msg = settings.get("round_loss_message", "").strip()
        path    = settings.get("cfg_path", DEFAULT_CFG_PATH)

    if not en or not ren:
        return
    now = time.time()
    if now - last_round_t < 5.0:
        return

    player_team = data.get("player", {}).get("team", "")
    won = bool(win_team and player_team and win_team.lower() == player_team.lower())

    if won:
        add_log(f"Round WON", "round")
        _push_history({"msg": win_msg or "gg", "weapon": "", "headshot": False,
                       "streak": 0, "time": ts_short(), "type": "round_win"})
        if win_msg:
            last_round_t = now
            send_message(win_msg, path)
    else:
        add_log(f"Round LOST", "round")
        _push_history({"msg": los_msg or "", "weapon": "", "headshot": False,
                       "streak": 0, "time": ts_short(), "type": "round_loss"})
        if los_msg:
            last_round_t = now
            send_message(los_msg, path)


# =============================================================================
#  Hotkey (F9 toggle)
# =============================================================================
def _toggle_hotkey():
    with state_lock:
        en = settings.get("enabled", True)
        settings["enabled"] = not en
        save_settings(settings)
    add_log(f"F9 — automation {'enabled' if not en else 'disabled'}", "key")

def register_hotkey():
    if not KEYBOARD_AVAILABLE:
        add_log("keyboard lib not found — F9 hotkey disabled (pip install keyboard)", "warn")
        return
    try:
        keyboard.add_hotkey("F9", _toggle_hotkey)
        add_log("F9 hotkey registered", "info")
    except Exception as exc:
        add_log(f"Hotkey registration failed: {exc}", "warn")


# =============================================================================
#  System tray
# =============================================================================
_window    = None
_tray_icon = None

def _make_tray_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    d.ellipse([4, 4, 60, 60], fill=(10, 11, 18, 255))
    cx, cy, r = 32, 32, 22
    pts = []
    for i in range(8):
        angle  = math.radians(i * 45 - 90)
        radius = r if i % 2 == 0 else r * 0.42
        pts.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    d.polygon(pts, fill=(167, 139, 250, 255))
    return img

def _minimize_to_tray():
    global _tray_icon
    if not TRAY_AVAILABLE or not _window:
        if _window: _window.minimize()
        return
    if _tray_icon:
        return
    _window.hide()
    menu = pystray.Menu(
        pystray.MenuItem("Show Nebula Killsay", _restore_from_tray, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", _exit_from_tray),
    )
    _tray_icon = pystray.Icon("nebula_killsay", _make_tray_image(), "Nebula Killsay", menu=menu)
    threading.Thread(target=_tray_icon.run, daemon=True).start()

def _restore_from_tray(icon=None, item=None):
    global _tray_icon
    if _tray_icon:
        _tray_icon.stop(); _tray_icon = None
    if _window: _window.show()

def _exit_from_tray(icon=None, item=None):
    global _tray_icon
    save_settings(settings)
    if _tray_icon:
        _tray_icon.stop(); _tray_icon = None
    if _window: _window.destroy()


# =============================================================================
#  Entry point
# =============================================================================
if __name__ == "__main__":
    def run_flask():
        flask_app.run(host="127.0.0.1", port=3000, debug=False, use_reloader=False)

    threading.Thread(target=run_flask, daemon=True).start()
    time.sleep(0.6)

    add_log(f"Nebula Killsay v{VERSION} started — waiting for CS2...", "info")
    if not WIN32_AVAILABLE:
        add_log("pywin32 not found — F13 simulation disabled", "warn")
    if not TRAY_AVAILABLE:
        add_log("pystray/Pillow not found — tray icon disabled", "warn")
    if DEFAULT_CFG_DIR:
        add_log(f"CS2 detected: {DEFAULT_CFG_DIR}", "info")
    else:
        add_log("CS2 not found — use Setup tab to configure", "warn")

    threading.Thread(target=register_hotkey, daemon=True).start()
    threading.Thread(target=check_for_updates, daemon=True).start()

    _window = webview.create_window(
        title="Nebula Killsay",
        url="http://127.0.0.1:3000/",
        width=1020,
        height=680,
        resizable=False,
        frameless=True,
        easy_drag=False,
        background_color="#07080D",
    )

    webview.start(debug=False)
    save_settings(settings)
