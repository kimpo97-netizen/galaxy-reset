# -*- coding: utf-8 -*-
"""
세린캠 반납폰 초기화 도구  v2.0  (공장초기화 X / FRP 위험 없음)

[v2.0 신규] 여러 대 동시 초기화
  - 연결된 기기 자동 감지 → 체크박스로 대상 선택
  - 모든 adb 명령에 -s <serial> 부착 (미지정 시 'more than one device' 오류)
  - 기기별 병렬 처리 (동시 실행 수 조절 가능, 기본 3대)
  - 로그에 [모델/SN] 접두사 → 어느 기기 로그인지 즉시 구분
  - 기기별 성공/실패 요약표 출력

[기존 기능]
  - 계정(구글) 제거 / 삼성계정 보존
  - 문자 · 연락처 · 브라우저 기록 삭제
  - 휴지통 비우기 (숨김 .trashed-* 포함)
  - 저장공간 전수 삭제 (기타파일 포함)
  - APK / XAPK / APKS / APKM 재설치 (OBB 자동 push)
  - 카메라 앱은 보호 → 촬영 설정 유지

실행: python 세린캠_갤럭시_초기화_GUI.py
필요: adb(platform-tools)가 PATH에 있거나 아래 ADB_PATH 수정
      기본 APK 폴더: 스크립트와 같은 위치의 ./apks
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
import zipfile
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk, messagebox, filedialog

ADB_PATH = "adb"   # 예: r"C:\platform-tools\adb.exe"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APK_DIR = os.path.join(BASE_DIR, "apks")
PRESET_FILE = os.path.join(BASE_DIR, "apk_preset.json")

APK_EXTS = (".apk", ".xapk", ".apks", ".apkm")
BUNDLE_EXTS = (".xapk", ".apks", ".apkm")

# 동시에 처리할 기기 수. USB 대역폭 때문에 너무 크면 APK 전송이 느려짐.
DEFAULT_WORKERS = 3

# ── 절대 건드리면 안 되는 패키지 ──────────────────────────────────
PROTECTED = {
    "com.osp.app.signin",                    # 삼성계정
    "com.samsung.android.samsungaccount",
    "com.samsung.android.mobileservice",
    "com.samsung.android.scloud",            # 삼성클라우드
    "com.sec.android.app.launcher",
    "com.android.settings",
    "com.sec.android.easyMover",             # Smart Switch
    "com.wssyncmldm",                        # 소프트웨어 업데이트
    "com.sec.android.app.camera",            # 카메라 (촬영 설정 보존)
}

CLEAR_TARGETS = {
    "계정(구글)": [
        "com.google.android.gms",
        "com.google.android.gsf",
    ],
    "문자": [
        "com.samsung.android.messaging",
        "com.google.android.apps.messaging",
        "com.samsung.android.providers.telephony",
    ],
    "연락처/통화기록": [
        "com.samsung.android.providers.contacts",
        "com.samsung.android.contacts",
        "com.samsung.android.dialer",
        "com.android.providers.contacts",
    ],
    "브라우저 기록": [
        "com.sec.android.app.sbrowser",
        "com.android.chrome",
        "com.sec.android.app.sbrowser.beta",
    ],
    "갤러리 캐시": [
        "com.sec.android.gallery3d",
        # 카메라 앱은 의도적 제외 — 대상추적AF / 손떨림보정 / UHD60 설정 유지
    ],
}

TRASH_PKGS = [
    "com.sec.android.app.myfiles",
    "com.sec.android.gallery3d",
    "com.samsung.android.app.notes",
    "com.samsung.android.app.reminder",
]

TRASH_PATHS = [
    "/sdcard/.Trash", "/sdcard/.trash", "/sdcard/.trashed",
    "/sdcard/Android/.Trash",
    "/sdcard/Android/data/com.sec.android.gallery3d/files/.Trash",
    "/sdcard/Android/data/com.sec.android.app.myfiles/files/.Trash",
    "/sdcard/Android/data/com.sec.android.app.myfiles/cache",
    "/sdcard/DCIM/.thumbnails", "/sdcard/Pictures/.thumbnails",
    "/sdcard/LOST.DIR",
]

TRASHED_GLOB_DIRS = [
    "/sdcard/DCIM", "/sdcard/Pictures", "/sdcard/Movies",
    "/sdcard/Download", "/sdcard/Documents", "/sdcard/Music",
    "/sdcard/Recordings",
]

SDCARD_KEEP = {".", ".."}

MISC_PATHS = [
    "/sdcard/Android/data",
    "/sdcard/Android/media",
    "/sdcard/Android/obb",
    "/data/local/tmp",
]

MISC_CLEAR_PKGS = [
    "com.android.vending",
    "com.google.android.youtube",
    "com.google.android.googlequicksearchbox",
    "com.google.android.apps.photos",
    "com.sec.android.app.samsungapps",
    "com.samsung.android.game.gamehome",
    "com.samsung.android.app.spage",
    "com.samsung.android.bixby.agent",
    "com.sec.android.app.myfiles",
    "com.android.providers.downloads",
    "com.android.providers.media",
    "com.samsung.android.providers.media",
]

STEPS = [
    ("apps",    "설치된 앱 전체 삭제 (3rd-party)"),
    ("account", "로그인 계정 제거 (구글 등 / 삼성계정 유지)"),
    ("sms",     "문자 메시지 삭제"),
    ("contact", "연락처 · 통화기록 삭제"),
    ("browser", "브라우저 사용 기록 삭제"),
    ("trash",   "휴지통 비우기 (갤러리 · 내 파일 · 노트 · 숨김 삭제파일)"),
    ("storage", "저장공간 원복 (사진 · 앱데이터 · 기타파일 전체)"),
    ("cache",   "전체 앱 캐시 정리"),
]


# ══════════════════════════════════════════════════════════════════
#  adb 유틸  — 모든 명령에 반드시 serial(-s)을 붙인다
# ══════════════════════════════════════════════════════════════════
def adb(args, serial=None, timeout=120):
    """
    adb 명령 실행 → (성공여부, 출력)
    serial 지정 시 'adb -s <serial> ...' 형태로 실행.
    여러 대가 연결된 상태에서 serial을 빼면 'more than one device' 오류가 난다.
    """
    cmd = [ADB_PATH]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="ignore")
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode == 0, out.strip()
    except FileNotFoundError:
        return False, "adb 실행파일을 찾을 수 없습니다. ADB_PATH를 확인하세요."
    except subprocess.TimeoutExpired:
        return False, "명령 시간 초과"
    except Exception as e:
        return False, f"오류: {e}"


def shell(cmd, serial=None, timeout=180):
    return adb(["shell", cmd], serial=serial, timeout=timeout)


def list_devices():
    """
    연결된 기기 목록 → [{'serial','model','android','storage','ok'}]
    unauthorized(USB 디버깅 미허용) 기기도 표시해서 원인을 바로 알 수 있게 한다.
    """
    ok, out = adb(["devices"])
    if not ok:
        return []

    devs = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or "\t" not in line:
            continue
        serial, state = line.split("\t", 1)
        state = state.strip()

        if state != "device":
            devs.append({"serial": serial, "model": f"({state})",
                         "android": "-", "storage": "-", "ok": False})
            continue

        _, model = shell("getprop ro.product.model", serial=serial, timeout=20)
        _, ver = shell("getprop ro.build.version.release", serial=serial, timeout=20)
        _, _, stor = get_storage(serial)
        devs.append({"serial": serial, "model": (model or "?").strip(),
                     "android": (ver or "?").strip(), "storage": stor,
                     "ok": True})
    return devs


def _to_gb(token):
    if not token:
        return None
    t = token.strip().upper().replace(",", "")
    m = re.match(r"^([\d.]+)\s*([KMGTP]?)I?B?$", t)
    if not m:
        return None
    factor = {"": 1 / (1024 * 1024), "K": 1 / (1024 * 1024),
              "M": 1 / 1024, "G": 1, "T": 1024, "P": 1024 * 1024}[m.group(2)]
    return float(m.group(1)) * factor


def get_storage(serial=None):
    """(사용량GB, 전체GB, 표기문자열)"""
    ok, out = shell("df -h /data 2>/dev/null | tail -1", serial=serial, timeout=30)
    if not ok or len(out.split()) < 4:
        ok, out = shell("df -h /sdcard 2>/dev/null | tail -1",
                        serial=serial, timeout=30)
    parts = out.split()
    if len(parts) < 4:
        return None, None, "조회 실패"

    size, used = _to_gb(parts[1]), _to_gb(parts[2])
    if size is None or used is None:
        return None, None, "파싱 실패"

    nominal = next((c for c in (64, 128, 256, 512, 1024) if size <= c * 1.02), None)
    label = f"{used:.1f}GB / {size:.1f}GB"
    if nominal:
        label += f" (표기 {nominal}GB)"
    return used, size, label


def human_size(path):
    try:
        return f"{os.path.getsize(path) / (1024 * 1024):.1f}MB"
    except OSError:
        return "-"


def get_device_abi(serial):
    _, abi = shell("getprop ro.product.cpu.abi", serial=serial, timeout=20)
    return (abi or "arm64-v8a").strip()


def get_device_dpi(serial):
    _, dens = shell("getprop ro.sf.lcd_density", serial=serial, timeout=20)
    try:
        d = int(dens.strip())
    except (ValueError, AttributeError):
        return "xxhdpi"
    for limit, name in [(140, "ldpi"), (200, "mdpi"), (280, "hdpi"),
                        (400, "xhdpi"), (560, "xxhdpi")]:
        if d < limit:
            return name
    return "xxxhdpi"


# ══════════════════════════════════════════════════════════════════
#  기기 1대를 처리하는 워커  (스레드에서 병렬 실행됨)
# ══════════════════════════════════════════════════════════════════
class DeviceWorker:
    def __init__(self, dev, opts, apks, logger):
        self.serial = dev["serial"]
        self.model = dev["model"]
        self.tag = f"[{self.model}/{self.serial[-4:]}]"
        self.opts = opts        # {'apps':True, 'account':True, ...}
        self.apks = apks        # [(파일명, 경로), ...]
        self._log = logger      # 스레드 안전 로그 함수
        self.abi = "arm64-v8a"
        self.dpi = "xxhdpi"

    def log(self, msg):
        self._log(f"{self.tag} {msg}")

    def sh(self, cmd, timeout=180):
        return shell(cmd, serial=self.serial, timeout=timeout)

    # ── 전체 실행 ─────────────────────────────────────────
    def run(self):
        """이 기기 하나를 끝까지 처리. return: (성공여부, 요약문)"""
        try:
            _, before, _ = get_storage(self.serial)
            used_before, _, label_before = get_storage(self.serial)
            self.log(f"시작 — 저장공간 {label_before}")

            if self.opts.get("apps"):
                self.step_apps()
            for key, title in [("account", "계정(구글)"), ("sms", "문자"),
                               ("contact", "연락처/통화기록"),
                               ("browser", "브라우저 기록")]:
                if self.opts.get(key):
                    self.step_clear(title)
            if self.opts.get("trash"):
                self.step_trash()
            if self.opts.get("storage"):
                self.step_storage()
            if self.opts.get("cache"):
                self.step_clear("갤러리 캐시")
                self.sh("pm trim-caches 999G", timeout=300)
                self.log("캐시 정리 완료")
            if self.opts.get("install"):
                self.step_install()

            used_after, _, label_after = get_storage(self.serial)
            freed = ""
            if used_before is not None and used_after is not None:
                freed = f" (확보 {max(used_before - used_after, 0):.1f}GB)"
            self.log(f"완료 — 저장공간 {label_after}{freed}")

            if self.opts.get("reboot"):
                self.log("재부팅...")
                adb(["reboot"], serial=self.serial, timeout=60)

            return True, f"{self.model} ({self.serial[-4:]}) — 완료{freed}"

        except Exception as e:
            self.log(f"[X] 예외: {e}")
            return False, f"{self.model} ({self.serial[-4:]}) — 실패: {e}"

    # ── 단계별 ────────────────────────────────────────────
    def step_apps(self):
        self.log("[앱] 사용자 설치 앱 삭제 중...")
        _, out = self.sh("pm list packages -3")
        pkgs = [l.replace("package:", "").strip()
                for l in out.splitlines() if l.startswith("package:")]
        pkgs = [p for p in pkgs if p not in PROTECTED]
        if not pkgs:
            self.log("[앱] 삭제할 앱 없음")
            return
        done = 0
        for p in pkgs:
            _, res = self.sh(f"pm uninstall --user 0 {p}")
            if "Success" in res:
                done += 1
        self.log(f"[앱] {done}/{len(pkgs)}개 삭제 완료")

    def step_clear(self, title):
        self.log(f"[{title}] 데이터 초기화 중...")
        done = 0
        for p in CLEAR_TARGETS[title]:
            if p in PROTECTED:
                continue
            _, res = self.sh(f"pm clear {p}")
            if "Success" in res:
                done += 1
        self.log(f"[{title}] {done}개 초기화 완료")

    def step_trash(self):
        self.log("[휴지통] 비우는 중...")
        for p in TRASH_PKGS:
            if p not in PROTECTED:
                self.sh(f"pm clear {p}")
        for path in TRASH_PATHS:
            self.sh(f"rm -rf '{path}'")

        removed = 0
        for d in TRASHED_GLOB_DIRS:
            _, out = self.sh(f"find '{d}' -name '.trashed-*' 2>/dev/null")
            files = [l.strip() for l in out.splitlines()
                     if l.strip() and "/" in l]
            for f in files:
                self.sh(f"rm -f '{f}'")
                removed += 1
            if not files:
                self.sh(f"rm -f {d}/.trashed-* 2>/dev/null")

        self.sh("am broadcast -a android.intent.action.MEDIA_MOUNTED "
                "-d file:///sdcard --user 0")
        self.log(f"[휴지통] 완료 (숨김 삭제파일 {removed}개)")

    def step_storage(self):
        """
        rm -rf /sdcard/* 는 셸 글롭이라 숨김파일(.xxx)을 놓친다.
        → ls -a 로 전수 조사 후 하나씩 삭제해야 '기타'가 실제로 줄어든다.
        """
        self.log("[저장공간] /sdcard 전수 삭제 중...")
        _, out = self.sh("ls -a /sdcard 2>/dev/null")
        entries = [e.strip() for e in out.splitlines()
                   if e.strip() and e.strip() not in SDCARD_KEEP]
        for e in entries:
            self.sh(f"rm -rf '/sdcard/{e}'")
        self.log(f"[저장공간] {len(entries)}개 항목 삭제")

        self.log("[기타파일] 앱 데이터 · 임시파일 삭제 중...")
        for p in MISC_PATHS:
            self.sh(f"rm -rf '{p}'/*")
            self.sh(f"rm -rf '{p}'/.[!.]*")

        for p in MISC_CLEAR_PKGS:
            if p not in PROTECTED:
                self.sh(f"pm clear {p}")

        self.sh("logcat -c")
        self.sh("pm trim-caches 999G", timeout=300)
        self.sh("am broadcast -a android.intent.action.MEDIA_MOUNTED "
                "-d file:///sdcard --user 0")
        self.log("[기타파일] 완료")

    # ── 설치 ──────────────────────────────────────────────
    def step_install(self):
        if not self.apks:
            self.log("[설치] 선택된 파일 없음")
            return
        self.abi = get_device_abi(self.serial)
        self.dpi = get_device_dpi(self.serial)
        self.log(f"[설치] {len(self.apks)}개 시작 (ABI={self.abi}, DPI={self.dpi})")

        ok_cnt = fail_cnt = 0
        for name, path in self.apks:
            ext = os.path.splitext(name)[1].lower()
            try:
                ok = (self.install_bundle(name, path) if ext in BUNDLE_EXTS
                      else self.install_single(name, path))
            except Exception as e:
                ok = False
                self.log(f"[설치] {name} 예외: {e}")
            ok_cnt += 1 if ok else 0
            fail_cnt += 0 if ok else 1
        self.log(f"[설치] 완료 — 성공 {ok_cnt} / 실패 {fail_cnt}")

    def install_single(self, name, path):
        # -r 재설치 / -d 다운그레이드 허용 / -g 런타임 권한 자동 부여
        _, res = adb(["install", "-r", "-d", "-g", path],
                     serial=self.serial, timeout=900)
        if "Success" in res:
            self.log(f"[설치] OK  {name}")
            return True
        _, res2 = adb(["install", "-r", "-d", path],
                      serial=self.serial, timeout=900)
        if "Success" in res2:
            self.log(f"[설치] OK  {name} (재시도)")
            return True
        reason = (res2 or res).splitlines()[-1] if (res2 or res) else "원인 불명"
        self.log(f"[설치] FAIL {name} → {reason}")
        return False

    def _pick_splits(self, apk_paths):
        """기기 ABI/DPI/언어에 맞는 스플릿만 선별 (전부 넣으면 설치 실패)"""
        abi_key = self.abi.replace("-", "_").lower()
        dpi_key = self.dpi.lower()
        known_abis = {"arm64_v8a", "armeabi_v7a", "armeabi",
                      "x86", "x86_64", "mips"}
        known_dpis = {"ldpi", "mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi",
                      "tvdpi", "nodpi"}
        keep_langs = {"ko", "en", "base"}

        picked, skipped = [], []
        for p in apk_paths:
            fn = os.path.basename(p).lower().replace("-", "_")
            m = re.search(r"config[._]([a-z0-9_]+)\.apk$", fn)
            token = m.group(1) if m else None

            if token is None:
                picked.append(p)
            elif token in known_abis:
                (picked if token == abi_key else skipped).append(p)
            elif token in known_dpis:
                (picked if token == dpi_key else skipped).append(p)
            elif len(token) == 2 or "_" in token:
                (picked if token.split("_")[0] in keep_langs
                 else skipped).append(p)
            else:
                picked.append(p)
        return picked, skipped

    def install_bundle(self, name, path):
        """.xapk / .apks / .apkm → 압축 해제 → install-multiple → OBB push"""
        tmp = tempfile.mkdtemp(prefix=f"serincam_{self.serial[-4:]}_")
        try:
            try:
                with zipfile.ZipFile(path) as z:
                    z.extractall(tmp)
            except zipfile.BadZipFile:
                self.log(f"[설치] FAIL {name} → 손상된 번들")
                return False

            apks = [os.path.join(r, f)
                    for r, _, fs in os.walk(tmp)
                    for f in fs if f.lower().endswith(".apk")]
            if not apks:
                self.log(f"[설치] FAIL {name} → 번들 안에 APK 없음")
                return False

            picked, skipped = self._pick_splits(apks)
            self.log(f"[설치] {name} — APK {len(apks)}개 중 {len(picked)}개 "
                     f"(불필요 스플릿 {len(skipped)}개 제외)")

            _, res = adb(["install-multiple", "-r", "-d", "-g"] + picked,
                         serial=self.serial, timeout=1800)
            if "Success" not in res:
                _, res = adb(["install-multiple", "-r", "-d"] + picked,
                             serial=self.serial, timeout=1800)
            if "Success" not in res:
                reason = res.splitlines()[-1] if res else "원인 불명"
                self.log(f"[설치] FAIL {name} → {reason}")
                return False

            self.log(f"[설치] OK  {name}")
            self.push_obb(tmp)
            return True
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def push_obb(self, tmp):
        obbs = [os.path.join(r, f)
                for r, _, fs in os.walk(tmp)
                for f in fs if f.lower().endswith(".obb")]
        if not obbs:
            return

        pkg = None
        mf = os.path.join(tmp, "manifest.json")
        if os.path.isfile(mf):
            try:
                with open(mf, "r", encoding="utf-8") as f:
                    pkg = json.load(f).get("package_name")
            except (OSError, json.JSONDecodeError, ValueError):
                pkg = None
        if not pkg:
            m = re.match(r"^(?:main|patch)\.\d+\.(.+)\.obb$",
                         os.path.basename(obbs[0]), re.I)
            pkg = m.group(1) if m else None
        if not pkg:
            self.log("[설치] OBB 패키지명 확인 불가 → 건너뜀")
            return

        dest = f"/sdcard/Android/obb/{pkg}"
        self.sh(f"mkdir -p '{dest}'")
        for o in obbs:
            self.log(f"[설치] OBB 전송 {os.path.basename(o)} ({human_size(o)})")
            adb(["push", o, f"{dest}/{os.path.basename(o)}"],
                serial=self.serial, timeout=1800)


# ══════════════════════════════════════════════════════════════════
#  GUI
# ══════════════════════════════════════════════════════════════════
class App:
    def __init__(self, root):
        self.root = root
        root.title("세린캠 반납폰 초기화 (v2.0 — 멀티 디바이스)")
        root.geometry("900x960")

        self.apk_dir = APK_DIR
        self.apk_files, self.apk_checks = [], {}
        self.devices, self.dev_checks = [], {}
        self.log_lock = threading.Lock()
        self.running = False

        # ── 기기 목록 ─────────────────────────────────────
        devbox = tk.LabelFrame(root, text="① 대상 기기 선택", padx=10, pady=8)
        devbox.pack(fill="x", padx=12, pady=(10, 4))

        dctl = tk.Frame(devbox)
        dctl.pack(fill="x")
        tk.Button(dctl, text="기기 검색", width=12, bg="#2980b9", fg="white",
                  cursor="hand2",
                  command=lambda: self.run(self.refresh_devices)).pack(side="left")
        tk.Button(dctl, text="전체 선택", width=9,
                  command=lambda: self.set_all_devs(True)).pack(side="left", padx=4)
        tk.Button(dctl, text="전체 해제", width=9,
                  command=lambda: self.set_all_devs(False)).pack(side="left")
        tk.Button(dctl, text="저장공간 분석", width=12, bg="#f39c12", fg="white",
                  cursor="hand2",
                  command=lambda: self.run(self.analyze_storage)).pack(side="left", padx=8)

        tk.Label(dctl, text="동시 처리:", font=("맑은 고딕", 9)).pack(side="left", padx=(12, 2))
        self.workers = tk.IntVar(value=DEFAULT_WORKERS)
        ttk.Combobox(dctl, textvariable=self.workers, width=3, state="readonly",
                     values=[1, 2, 3, 4, 5, 6, 8]).pack(side="left")
        tk.Label(dctl, text="대", font=("맑은 고딕", 9)).pack(side="left", padx=(2, 0))

        self.dev_status = tk.Label(dctl, text="● 미검색", fg="gray",
                                   font=("맑은 고딕", 10, "bold"))
        self.dev_status.pack(side="right")

        dwrap = tk.Frame(devbox, height=130)
        dwrap.pack(fill="x", pady=(6, 0))
        dwrap.pack_propagate(False)
        self.dev_canvas = tk.Canvas(dwrap, highlightthickness=0, bg="white")
        dsb = ttk.Scrollbar(dwrap, orient="vertical", command=self.dev_canvas.yview)
        self.dev_frame = tk.Frame(self.dev_canvas, bg="white")
        self.dev_frame.bind("<Configure>", lambda e: self.dev_canvas.configure(
            scrollregion=self.dev_canvas.bbox("all")))
        self.dev_canvas.create_window((0, 0), window=self.dev_frame, anchor="nw")
        self.dev_canvas.configure(yscrollcommand=dsb.set)
        self.dev_canvas.pack(side="left", fill="both", expand=True)
        dsb.pack(side="right", fill="y")

        # ── 초기화 항목 ───────────────────────────────────
        box = tk.LabelFrame(root, text="② 초기화 항목", padx=10, pady=8)
        box.pack(fill="x", padx=12, pady=4)

        self.vars = {k: tk.BooleanVar(value=True) for k, _ in STEPS}
        self.vars["reboot"] = tk.BooleanVar(value=False)    # 기본 꺼짐
        self.vars["install"] = tk.BooleanVar(value=True)

        self.master_var = tk.BooleanVar(value=True)
        head = tk.Frame(box)
        head.pack(fill="x")
        tk.Checkbutton(
            head,
            text="전체 초기화 (계정 · 앱 · 문자 · 연락처 · 브라우저 · 휴지통 · 저장공간 · 캐시)",
            variable=self.master_var, command=self.on_master_toggle,
            font=("맑은 고딕", 10, "bold"), anchor="w").pack(side="left")
        self.detail_btn = tk.Button(head, text="상세 ▼", width=8, relief="flat",
                                    fg="#2980b9", cursor="hand2",
                                    command=self.toggle_detail)
        self.detail_btn.pack(side="right")

        self.detail = tk.Frame(box)
        for i, (_, label) in enumerate(STEPS, 1):
            tk.Label(self.detail, text=f"  {i}. {label}", anchor="w",
                     fg="#555", font=("맑은 고딕", 9)).pack(fill="x")
        tk.Label(self.detail, fg="#27ae60", anchor="w", font=("맑은 고딕", 9),
                 text="  ※ 카메라 앱은 보호됨 — 촬영 설정(대상추적AF/UHD60 등) 유지"
                 ).pack(fill="x")
        self.detail_open = False

        opt = tk.Frame(box)
        opt.pack(fill="x", pady=(6, 0))
        tk.Checkbutton(opt, text="초기화 후 선택한 APK 자동 설치",
                       variable=self.vars["install"],
                       font=("맑은 고딕", 9)).pack(side="left")
        tk.Checkbutton(opt, text="완료 후 자동 재부팅",
                       variable=self.vars["reboot"],
                       font=("맑은 고딕", 9)).pack(side="left", padx=16)

        # ── APK 패널 ─────────────────────────────────────
        apkbox = tk.LabelFrame(
            root, text="③ 재설치할 앱  (.apk / .xapk / .apks / .apkm)",
            padx=10, pady=8)
        apkbox.pack(fill="x", padx=12, pady=4)

        ctl = tk.Frame(apkbox)
        ctl.pack(fill="x")
        tk.Button(ctl, text="폴더 선택", width=10,
                  command=self.choose_dir).pack(side="left")
        tk.Button(ctl, text="새로고침", width=9,
                  command=self.load_apks).pack(side="left", padx=4)
        tk.Button(ctl, text="전체 선택", width=9,
                  command=lambda: self.set_all_apks(True)).pack(side="left", padx=4)
        tk.Button(ctl, text="전체 해제", width=9,
                  command=lambda: self.set_all_apks(False)).pack(side="left")
        tk.Button(ctl, text="선택 저장", width=9,
                  command=self.save_preset).pack(side="left", padx=4)
        tk.Button(ctl, text="지금 설치", width=10, bg="#27ae60", fg="white",
                  cursor="hand2",
                  command=lambda: self.run(self.install_only)).pack(side="right")

        self.lbl_dir = tk.Label(apkbox, text=f"폴더: {self.apk_dir}", anchor="w",
                                fg="#666", font=("맑은 고딕", 8))
        self.lbl_dir.pack(fill="x", pady=(4, 2))

        wrap = tk.Frame(apkbox, height=120)
        wrap.pack(fill="x")
        wrap.pack_propagate(False)
        self.canvas = tk.Canvas(wrap, highlightthickness=0, bg="white")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.apk_frame = tk.Frame(self.canvas, bg="white")
        self.apk_frame.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.apk_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # ── 실행 / 진행 / 로그 ────────────────────────────
        self.btn = tk.Button(root, text="선택한 기기 초기화 실행",
                             bg="#c0392b", fg="white", height=2,
                             font=("맑은 고딕", 11, "bold"), cursor="hand2",
                             command=self.confirm_run)
        self.btn.pack(fill="x", padx=12, pady=(8, 4))

        self.bar = ttk.Progressbar(root, mode="determinate")
        self.bar.pack(fill="x", padx=12)

        self.log_box = tk.Text(root, height=14, bg="#1e1e1e", fg="#d4d4d4",
                               font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True, padx=12, pady=8)

        self.load_apks()
        self.run(self.refresh_devices)

    # ── 공통 유틸 ─────────────────────────────────────────
    def log(self, msg):
        """스레드 안전 로그 — 여러 기기가 동시에 찍어도 줄이 안 섞이게 락으로 보호"""
        with self.log_lock:
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            try:
                self.log_box.update_idletasks()
            except tk.TclError:
                pass

    def run(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def on_master_toggle(self):
        state = self.master_var.get()
        for k, _ in STEPS:
            self.vars[k].set(state)

    def toggle_detail(self):
        if self.detail_open:
            self.detail.pack_forget()
            self.detail_btn.config(text="상세 ▼")
        else:
            self.detail.pack(fill="x", pady=(4, 0))
            self.detail_btn.config(text="접기 ▲")
        self.detail_open = not self.detail_open

    # ── 기기 목록 ─────────────────────────────────────────
    def refresh_devices(self):
        self.log("\n[기기] 검색 중...")
        for w in self.dev_frame.winfo_children():
            w.destroy()
        self.devices, self.dev_checks = [], {}

        devs = list_devices()
        if not devs:
            self.dev_status.config(text="● 0대", fg="red")
            tk.Label(self.dev_frame, bg="white", fg="#c0392b", anchor="w",
                     font=("맑은 고딕", 9),
                     text="  연결된 기기 없음. USB 디버깅 허용 여부를 확인하세요."
                     ).pack(fill="x", pady=6)
            self.log("[기기] 연결된 기기 없음")
            return

        self.devices = devs
        ready = 0
        for d in devs:
            if d["ok"]:
                ready += 1
                v = tk.BooleanVar(value=True)
                self.dev_checks[d["serial"]] = v
                txt = (f"{d['model']}   |   Android {d['android']}   |   "
                       f"{d['storage']}   |   S/N {d['serial']}")
                tk.Checkbutton(self.dev_frame, bg="white", anchor="w",
                               font=("맑은 고딕", 9), variable=v,
                               text=txt).pack(fill="x")
            else:
                tk.Label(self.dev_frame, bg="white", fg="#c0392b", anchor="w",
                         font=("맑은 고딕", 9),
                         text=f"  ⚠ {d['serial']} {d['model']} "
                              f"— 폰 화면에서 'USB 디버깅 허용'을 눌러주세요"
                         ).pack(fill="x")

        self.dev_status.config(text=f"● {ready}대 연결",
                               fg="green" if ready else "red")
        self.log(f"[기기] {ready}대 사용 가능 / 총 {len(devs)}대 감지")

    def set_all_devs(self, state):
        for v in self.dev_checks.values():
            v.set(state)

    def selected_devices(self):
        return [d for d in self.devices
                if d["ok"] and self.dev_checks.get(d["serial"])
                and self.dev_checks[d["serial"]].get()]

    # ── APK 목록 ──────────────────────────────────────────
    def choose_dir(self):
        init = self.apk_dir if os.path.isdir(self.apk_dir) else BASE_DIR
        d = filedialog.askdirectory(title="APK 폴더 선택", initialdir=init)
        if d:
            self.apk_dir = d
            self.load_apks()

    def load_apks(self):
        for w in self.apk_frame.winfo_children():
            w.destroy()
        self.apk_files, self.apk_checks = [], {}
        self.lbl_dir.config(text=f"폴더: {self.apk_dir}")

        if not os.path.isdir(self.apk_dir):
            tk.Label(self.apk_frame, bg="white", fg="#c0392b", anchor="w",
                     font=("맑은 고딕", 9),
                     text="  폴더가 없습니다. [폴더 선택]을 눌러주세요."
                     ).pack(fill="x", pady=6)
            return

        saved = self.read_preset()
        names = sorted(f for f in os.listdir(self.apk_dir)
                       if f.lower().endswith(APK_EXTS))
        if not names:
            tk.Label(self.apk_frame, bg="white", fg="#888", anchor="w",
                     font=("맑은 고딕", 9),
                     text="  설치 파일이 없습니다. .apk / .xapk 를 넣고 [새로고침]."
                     ).pack(fill="x", pady=6)
            return

        for n in names:
            full = os.path.join(self.apk_dir, n)
            self.apk_files.append((n, full))
            v = tk.BooleanVar(value=saved.get(n, True) if saved else True)
            self.apk_checks[n] = v
            ext = os.path.splitext(n)[1].lower()
            is_bundle = ext in BUNDLE_EXTS
            tk.Checkbutton(self.apk_frame, bg="white", anchor="w",
                           fg="#8e44ad" if is_bundle else "#2c3e50",
                           font=("맑은 고딕", 9), variable=v,
                           text=f"[{'번들' if is_bundle else '단일'}] {n}   "
                                f"({human_size(full)})").pack(fill="x")

    def set_all_apks(self, state):
        for v in self.apk_checks.values():
            v.set(state)

    def selected_apks(self):
        return [(n, p) for n, p in self.apk_files
                if n in self.apk_checks and self.apk_checks[n].get()]

    def read_preset(self):
        try:
            with open(PRESET_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("dir") == self.apk_dir:
                return data.get("checked", {})
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        return {}

    def save_preset(self):
        data = {"dir": self.apk_dir,
                "checked": {n: v.get() for n, v in self.apk_checks.items()}}
        try:
            with open(PRESET_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.log(f"[APK] 선택 상태 저장 완료")
            messagebox.showinfo("저장", "선택 상태를 저장했습니다.")
        except OSError as e:
            messagebox.showerror("오류", f"저장 실패: {e}")

    # ── 저장공간 분석 ─────────────────────────────────────
    def _du_sorted(self, serial, target_dir, limit=20):
        """toybox는 sort -h 미지원 → du -sk 로 받아 파이썬에서 정렬"""
        _, ls_out = shell(f"ls -a '{target_dir}' 2>/dev/null", serial=serial)
        entries = [e.strip() for e in ls_out.splitlines()
                   if e.strip() and e.strip() not in SDCARD_KEEP]
        if not entries:
            return []
        quoted = " ".join(f"'{target_dir}/{e}'" for e in entries)
        _, out = shell(f"du -sk {quoted} 2>/dev/null", serial=serial, timeout=900)

        rows = []
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                try:
                    rows.append((int(parts[0]), parts[1].strip()))
                except ValueError:
                    pass
        rows.sort(key=lambda r: r[0], reverse=True)
        return rows[:limit]

    @staticmethod
    def _fmt_kb(kb):
        if kb >= 1024 * 1024:
            return f"{kb / 1024 / 1024:7.2f} GB"
        if kb >= 1024:
            return f"{kb / 1024:7.1f} MB"
        return f"{kb:7d} KB"

    def analyze_storage(self):
        devs = self.selected_devices()
        if not devs:
            messagebox.showinfo("안내", "분석할 기기를 선택해 주세요.")
            return

        for d in devs:
            s, tag = d["serial"], f"[{d['model']}/{d['serial'][-4:]}]"
            self.log("\n" + "=" * 52)
            self.log(f"{tag} 저장공간 분석 (1~2분 소요)")
            self.log("=" * 52)

            for title, path, lim in [
                ("① /sdcard 최상위 (숨김 포함)", "/sdcard", 20),
                ("② 앱별 데이터 (Android/data)", "/sdcard/Android/data", 15),
                ("③ 앱별 미디어 (Android/media)", "/sdcard/Android/media", 10),
                ("④ 게임 확장 (Android/obb)", "/sdcard/Android/obb", 10),
            ]:
                self.log(f"\n--- {title} ---")
                rows = self._du_sorted(s, path, lim)
                if not rows:
                    self.log("   (없음 또는 접근 불가)")
                    continue
                for kb, p in rows:
                    if kb > 0:
                        self.log(f"   {self._fmt_kb(kb)}   {p}")

            self.log("\n--- ⑤ 500MB 이상 대용량 파일 ---")
            _, out = shell("find /sdcard -type f -size +500M 2>/dev/null | head -30",
                           serial=s, timeout=900)
            bigs = [l.strip() for l in out.splitlines() if l.strip().startswith("/")]
            if not bigs:
                self.log("   (없음)")
            else:
                for b in bigs:
                    _, sz = shell(f"du -sk '{b}' 2>/dev/null", serial=s)
                    parts = sz.split(None, 1)
                    if len(parts) == 2 and parts[0].isdigit():
                        self.log(f"   {self._fmt_kb(int(parts[0]))}   {parts[1]}")

            self.log("\n--- ⑥ 사용자 프로필 (보안폴더 등) ---")
            _, users = shell("pm list users", serial=s)
            for l in users.splitlines():
                if l.strip():
                    self.log(f"   {l.strip()}")

            self.log(f"\n{tag} 분석 완료")

    # ── 실행 ──────────────────────────────────────────────
    def confirm_run(self):
        if self.running:
            return
        devs = self.selected_devices()
        if not devs:
            messagebox.showinfo("안내", "초기화할 기기를 선택해 주세요.")
            return
        if not self.master_var.get() and not self.vars["install"].get():
            messagebox.showinfo("안내", "실행할 항목을 선택해 주세요.")
            return

        names = "\n".join(f"  · {d['model']} (S/N {d['serial']})" for d in devs)
        msg = (f"아래 {len(devs)}대를 초기화합니다.\n복구 불가능합니다.\n\n"
               f"{names}\n\n※ 삼성계정 · 카메라 설정은 보존됩니다.")
        if self.vars["install"].get():
            msg += f"\n※ 초기화 후 앱 {len(self.selected_apks())}개 재설치"
        if not messagebox.askyesno("확인", msg):
            return

        self.run(lambda: self.wipe_all(devs))

    def install_only(self):
        devs = self.selected_devices()
        if not devs:
            messagebox.showinfo("안내", "설치할 기기를 선택해 주세요.")
            return
        opts = {"install": True}
        self.run(lambda: self.wipe_all(devs, opts_override=opts))

    def wipe_all(self, devs, opts_override=None):
        """선택된 기기들을 병렬로 처리"""
        self.running = True
        self.btn.config(state="disabled", text="처리 중...")
        try:
            opts = opts_override or {k: v.get() for k, v in self.vars.items()}
            apks = self.selected_apks() if opts.get("install") else []

            self.bar["maximum"] = len(devs)
            self.bar["value"] = 0
            done = [0]
            lock = threading.Lock()

            self.log("\n" + "█" * 52)
            self.log(f"■ {len(devs)}대 처리 시작 "
                     f"(동시 {self.workers.get()}대)")
            self.log("█" * 52)

            results = []

            def work(dev):
                w = DeviceWorker(dev, opts, apks, self.log)
                r = w.run()
                with lock:
                    done[0] += 1
                    self.bar["value"] = done[0]
                    try:
                        self.bar.update_idletasks()
                    except tk.TclError:
                        pass
                return r

            with ThreadPoolExecutor(max_workers=self.workers.get()) as ex:
                for r in ex.map(work, devs):
                    results.append(r)

            # ── 요약표 ──
            self.log("\n" + "█" * 52)
            self.log("■ 전체 결과")
            self.log("█" * 52)
            ok = sum(1 for s, _ in results if s)
            for success, summary in results:
                self.log(f"  {'✅' if success else '❌'} {summary}")
            self.log(f"\n  총 {len(devs)}대 — 성공 {ok} / 실패 {len(devs) - ok}")
            self.log("█" * 52)

            self.run(self.refresh_devices)   # 저장공간 갱신

        except Exception as e:
            self.log(f"[X] 예외: {e}")
        finally:
            self.running = False
            self.btn.config(state="normal", text="선택한 기기 초기화 실행")


if __name__ == "__main__":
    os.makedirs(APK_DIR, exist_ok=True)
    root = tk.Tk()
    App(root)
    root.mainloop()