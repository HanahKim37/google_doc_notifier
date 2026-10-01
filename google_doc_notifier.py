import os
import sys
import json
import re
import queue
import difflib
import threading
import unicodedata
import webbrowser
import urllib.request
import socket
import tempfile
import subprocess
from datetime import datetime
import tkinter as tk
from tkinter import messagebox, scrolledtext

# 트레이 아이콘 (pip install pystray pillow)
# 설치되어 있지 않으면 트레이 없이 예전처럼 동작
try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except Exception:
    TRAY_AVAILABLE = False

# 윈도우 시작 시 자동 실행 (레지스트리 사용, 윈도우 전용)
try:
    import winreg
except ImportError:
    winreg = None

# ==========================================
# 중복 실행 방지
# ==========================================
SINGLE_INSTANCE_PORT = 54321

try:
    single_instance_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    single_instance_socket.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
except socket.error:
    sys.exit()

# =========================
# 설정값
# =========================
DOC_ID = "1XejiiZEHlMj_z1DG_muoNrEv3j9V9INKy9neX6LFuaI"
APP_NAME = "2학년부 업무 일지 알림"

INITIAL_DELAY_MS = 30 * 1000          # 시작 후 첫 검사: 30초
CHECK_INTERVAL_MS = 10 * 60 * 1000    # 이후 반복 검사: 10분

DOC_EDIT_URL = f"https://docs.google.com/document/d/{DOC_ID}/edit"
DOC_TXT_URL = f"https://docs.google.com/document/d/{DOC_ID}/export?format=txt"

# =========================
# 자동 업데이트 설정
# =========================
# 새 버전을 배포할 때마다 이 값을 올리고(예: "1.0.1"),
# build.bat으로 exe를 만든 뒤 GitHub 저장소의 Releases에
# 태그(예: v1.0.1)와 함께 아래 UPDATE_ASSET_NAME과 "정확히 같은 이름"으로
# exe 파일을 첨부해서 올리면, 이미 설치된 프로그램들이 자동으로 내려받아 적용합니다.
CURRENT_VERSION = "1.0.1"
GITHUB_REPO = "HanahKim37/google_doc_notifier"
UPDATE_ASSET_NAME = "google_doc_notifier.exe"
GITHUB_API_LATEST_RELEASE = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

UPDATE_INITIAL_DELAY_MS = 15 * 1000            # 시작 후 첫 업데이트 확인까지 대기 시간
UPDATE_CHECK_INTERVAL_MS = 3 * 60 * 60 * 1000  # 이후 업데이트 확인 주기: 3시간

USER_DIR = os.path.expanduser("~")
STATE_FILE = os.path.join(USER_DIR, "doc_state.json")
LOG_FILE = os.path.join(USER_DIR, "doc_change_log.txt")

AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_REG_NAME = "GoogleDocNotifier_2grade"

DEFAULT_SECTION_NAME = "날짜 구분 없음"


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def append_log(message):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{now_str()}] {message}\n")
    except Exception:
        pass


# 눈에 보이지 않는 문자 (폭 0 공백, BOM, 소프트 하이픈 등)
INVISIBLE_CHARS = re.compile(r"[​‌‍⁠﻿­]")


def normalize_text(text):
    """
    비교 전에 '보이지 않는 차이'를 없앰
    - 보이지 않는 문자 제거, 줄바꿈 없는 공백 → 일반 공백
    - 줄 안의 연속 공백/탭 → 한 칸, 줄 앞뒤 공백 제거
    - 빈 줄 제거
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = INVISIBLE_CHARS.sub("", text).replace("\xa0", " ")
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def clean_header_line(line: str) -> str:
    s = line.strip()
    s = re.sub(r"^[■□◆◇●○▶▷☞※#\-\=\s]+", "", s)
    s = s.strip()
    return s


def extract_date_header(line: str):
    """
    날짜 제목처럼 보이는 줄을 최대한 넓게 인식
    예:
    2026-03-17
    2026.03.17
    2026년 3월 17일
    3/17
    3-17
    3.17
    3월 17일
    [3월 17일]
    3/17(월)
    3월 17일 월요일
    """
    raw = clean_header_line(line)

    if not raw:
        return None

    # 양쪽 괄호/대괄호 제거
    test = raw
    test = re.sub(r"^[\[\(（【<〈]\s*", "", test)
    test = re.sub(r"\s*[\]\)）】>〉]$", "", test)
    test = test.strip()

    # 너무 긴 일반 문장은 날짜 제목으로 보지 않음
    if len(test) > 30:
        return None

    weekday = r"(?:\s*(?:\([월화수목금토일]\)|[월화수목금토일]요일|[월화수목금토일]))?"
    patterns = [
        rf"^\d{{4}}\s*[./-]\s*\d{{1,2}}\s*[./-]\s*\d{{1,2}}{weekday}$",
        rf"^\d{{4}}\s*년\s*\d{{1,2}}\s*월\s*\d{{1,2}}\s*일?{weekday}$",
        rf"^\d{{1,2}}\s*[./-]\s*\d{{1,2}}{weekday}$",
        rf"^\d{{1,2}}\s*월\s*\d{{1,2}}\s*일?{weekday}$",
    ]

    for p in patterns:
        if re.match(p, test):
            return raw

    return None


def build_line_section_map(lines):
    """
    각 줄이 어느 날짜 구역에 속하는지 매핑
    """
    sections = []
    current_section = DEFAULT_SECTION_NAME

    for line in lines:
        detected = extract_date_header(line)
        if detected:
            current_section = detected
        sections.append(current_section)

    return sections


def group_lines_by_section(lines, sections):
    groups = []
    if not lines:
        return groups

    current_section = None
    buffer = []

    for line, sec in zip(lines, sections):
        sec = sec or DEFAULT_SECTION_NAME

        if current_section is None:
            current_section = sec
            buffer = [line]
            continue

        if sec != current_section:
            text = "\n".join(buffer).strip()
            if text:
                groups.append((current_section, text))
            current_section = sec
            buffer = [line]
        else:
            buffer.append(line)

    text = "\n".join(buffer).strip()
    if text:
        groups.append((current_section, text))

    return groups



def add_change(changes_by_section, section, change_type, old_text="", new_text=""):
    old_s = old_text.strip()
    new_s = new_text.strip()

    # 공백만 다른 '수정'은 실제 변경이 아니므로 무시
    if change_type == "수정" and old_s == new_s:
        return

    if not section:
        section = DEFAULT_SECTION_NAME

    if section not in changes_by_section:
        changes_by_section[section] = []

    changes_by_section[section].append({
        "type": change_type,
        "old": old_s,
        "new": new_s,
    })


def build_changes(old_text, new_text):
    """
    중간 삽입/수정/삭제까지 줄 단위로 비교해서
    날짜 구역별로 묶음 → {구역: [변경, ...]}
    """
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    old_sections = build_line_section_map(old_lines)
    new_sections = build_line_section_map(new_lines)

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    changes_by_section = {}

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        old_chunk = old_lines[i1:i2]
        new_chunk = new_lines[j1:j2]

        old_chunk_sections = old_sections[i1:i2]
        new_chunk_sections = new_sections[j1:j2]

        if tag == "insert":
            for sec, text in group_lines_by_section(new_chunk, new_chunk_sections):
                add_change(changes_by_section, sec, "추가", new_text=text)

        elif tag == "delete":
            for sec, text in group_lines_by_section(old_chunk, old_chunk_sections):
                add_change(changes_by_section, sec, "삭제", old_text=text)

        elif tag == "replace":
            old_groups = group_lines_by_section(old_chunk, old_chunk_sections)
            new_groups = group_lines_by_section(new_chunk, new_chunk_sections)

            if (
                len(old_groups) == 1 and
                len(new_groups) == 1 and
                old_groups[0][0] == new_groups[0][0]
            ):
                sec = old_groups[0][0]
                add_change(
                    changes_by_section,
                    sec,
                    "수정",
                    old_text=old_groups[0][1],
                    new_text=new_groups[0][1]
                )
            else:
                for sec, text in old_groups:
                    add_change(changes_by_section, sec, "삭제", old_text=text)
                for sec, text in new_groups:
                    add_change(changes_by_section, sec, "추가", new_text=text)

    return changes_by_section


def count_changes(changes_by_section):
    return sum(len(v) for v in changes_by_section.values())


def format_report_text(changes_by_section):
    """로그 파일용 일반 텍스트 보고서"""
    if not changes_by_section:
        return ""

    report_lines = [f"총 {count_changes(changes_by_section)}개의 업데이트가 감지되었습니다.", ""]

    for section, items in changes_by_section.items():
        report_lines.append("=" * 35)
        report_lines.append(f"날짜 구역: {section}")
        report_lines.append("=" * 35)

        for idx, item in enumerate(items, start=1):
            change_type = item["type"]
            report_lines.append(f"[{idx}. {change_type}]")

            if change_type == "추가":
                report_lines.append(item["new"])
            elif change_type == "삭제":
                report_lines.append("삭제된 내용:")
                report_lines.append(item["old"])
            elif change_type == "수정":
                report_lines.append("수정 전:")
                report_lines.append(item["old"])
                report_lines.append("")
                report_lines.append("수정 후:")
                report_lines.append(item["new"])

            report_lines.append("")
        report_lines.append("")

    return "\n".join(report_lines).strip()


def build_change_report(old_text, new_text):
    """(예전 함수 이름 호환용)"""
    return format_report_text(build_changes(old_text, new_text))


# =========================
# 단어 단위 비교 (바뀐 부분 강조용)
# =========================
TOKEN_PATTERN = re.compile(r"\s+|\w+|[^\w\s]")


def tokenize(text):
    return TOKEN_PATTERN.findall(text)


def word_diff(old_text, new_text):
    """
    수정 전/후 텍스트를 단어 단위로 비교
    반환: (old_segments, new_segments)
      각 segment = (문자열, 바뀐부분인지 True/False)
    """
    old_tokens = tokenize(old_text)
    new_tokens = tokenize(new_text)
    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)

    old_segments = []
    new_segments = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        old_part = "".join(old_tokens[i1:i2])
        new_part = "".join(new_tokens[j1:j2])
        changed = tag != "equal"
        if old_part:
            old_segments.append((old_part, changed))
        if new_part:
            new_segments.append((new_part, changed))
    return old_segments, new_segments


# =========================
# 윈도우 시작 시 자동 실행
# =========================
def get_launch_command():
    if getattr(sys, "frozen", False):  # PyInstaller exe
        return f'"{sys.executable}"'
    # .py로 실행 중이면 콘솔 없는 pythonw로 실행
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pythonw}" "{os.path.abspath(__file__)}"'


def is_autostart_enabled():
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, AUTOSTART_REG_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled):
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, AUTOSTART_REG_NAME, 0, winreg.REG_SZ, get_launch_command())
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_REG_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as e:
        append_log(f"자동 실행 설정 오류: {e}")
        return False


# =========================
# 자동 업데이트
# =========================
def parse_version(v):
    """'v1.2.3' 같은 문자열을 (1, 2, 3) 튜플로 변환. 숫자가 없으면 (0,)"""
    if not v:
        return (0,)
    parts = re.findall(r"\d+", v)
    if not parts:
        return (0,)
    return tuple(int(p) for p in parts)


def is_newer_version(remote_v, local_v):
    return parse_version(remote_v) > parse_version(local_v)


def fetch_latest_release_info():
    """
    GitHub 저장소의 최신 릴리스 정보를 가져옴.
    반환: (버전 태그, exe 다운로드 URL) 또는 실패 시 (None, None)
    """
    try:
        req = urllib.request.Request(
            GITHUB_API_LATEST_RELEASE,
            headers={
                "User-Agent": "google_doc_notifier-updater",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))

        tag = data.get("tag_name")
        download_url = None
        for asset in data.get("assets", []):
            if asset.get("name") == UPDATE_ASSET_NAME:
                download_url = asset.get("browser_download_url")
                break

        return tag, download_url
    except Exception as e:
        append_log(f"업데이트 확인 오류: {e}")
        return None, None


# =========================
# 트레이 아이콘 그림
# =========================
def create_tray_image(alert=False):
    """문서 모양 아이콘. alert=True면 빨간 점 표시"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((12, 6, 52, 58), radius=5, fill=(66, 133, 244), outline=(40, 90, 180), width=2)
    for y in (20, 30, 40):
        d.line((20, y, 44, y), fill="white", width=4)
    if alert:
        d.ellipse((38, 0, 62, 24), fill=(229, 57, 53), outline="white", width=2)
    return img


class DocNotifierApp:
    def __init__(self, root):
        self.root = root
        self.root.withdraw()

        self.popup = None
        self.text_area = None
        self.lbl_title = None
        self.is_topmost = tk.BooleanVar(value=True)

        self.history = []            # 이번 실행 중 감지한 변경들 [(시각, changes), ...]
        self.after_id = None         # 예약된 다음 검사
        self.tray = None
        self.tray_queue = queue.Queue()

        self.state = self.load_state()
        self.last_text = self.state.get("last_text")
        if self.last_text is not None:
            # 예전 버전이 저장한 원본 텍스트도 같은 규칙으로 정리
            self.last_text = normalize_text(self.last_text)

        append_log("프로그램 시작됨")

        self.init_autostart()

        if self.last_text is None:
            first_text = self.fetch_doc_text()
            if first_text is not None:
                self.last_text = first_text
                self.state["last_text"] = self.last_text
                self.save_state(self.state)

            try:
                tray_hint = (
                    "\n\n작업 표시줄 오른쪽 알림 영역(^)의 파란 문서 아이콘에서\n"
                    "지금 검사 / 문서 열기 / 종료 등을 할 수 있습니다."
                    if TRAY_AVAILABLE else ""
                )
                messagebox.showinfo(
                    "실행 성공",
                    "2학년부 업무 일지 알림을 시작합니다.\n\n"
                    "이 창을 닫으면 백그라운드에서 감시를 진행합니다.\n"
                    "컴퓨터를 켜면 자동으로 실행됩니다." + tray_hint
                )
            except Exception:
                append_log("첫 실행 안내창 표시 실패")

        self.start_tray()
        self.schedule_check(INITIAL_DELAY_MS)
        self.schedule_update_check(UPDATE_INITIAL_DELAY_MS)

    # ---------- 상태 파일 ----------
    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception as e:
                append_log(f"상태 파일 읽기 오류: {e}")
        return {}

    def save_state(self, state):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            append_log(f"상태 파일 저장 오류: {e}")

    # ---------- 자동 실행 ----------
    def init_autostart(self):
        """
        처음 한 번은 자동 실행을 켬.
        이후에는 사용자가 트레이 메뉴에서 끈 설정을 존중하고,
        켜져 있으면 exe 위치가 바뀌었을 수 있으니 경로만 갱신.
        """
        if winreg is None:
            return
        if not self.state.get("autostart_initialized"):
            if set_autostart(True):
                append_log("윈도우 시작 시 자동 실행 등록")
            self.state["autostart_initialized"] = True
            self.save_state(self.state)
        elif is_autostart_enabled():
            set_autostart(True)

    def toggle_autostart(self):
        enabled = not is_autostart_enabled()
        if set_autostart(enabled):
            append_log("자동 실행 " + ("켬" if enabled else "끔"))
            self.notify("자동 실행을 " + ("켰습니다." if enabled else "껐습니다."))

    # ---------- 로그 ----------
    def write_change_detail_log(self, report_text):
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n[{now_str()}] ===== 변경 상세 =====\n")
                f.write(report_text + "\n")
                f.write(f"[{now_str()}] =====================\n\n")
        except Exception:
            pass

    def open_log(self):
        try:
            if not os.path.exists(LOG_FILE):
                append_log("로그 파일 생성")
            os.startfile(LOG_FILE)
        except Exception as e:
            append_log(f"로그 열기 오류: {e}")

    # ---------- 문서 가져오기 / 검사 ----------
    def fetch_doc_text(self):
        """
        성공: 정리된(normalize) 문서 텍스트
        실패: None
        """
        try:
            req = urllib.request.Request(
                DOC_TXT_URL,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=20) as response:
                ctype = response.headers.get("Content-Type", "")
                if "text/plain" not in ctype:
                    # 로그인 페이지/오류 페이지(HTML) 등이 오면 문서로 취급하지 않음
                    raise ValueError(f"예상치 못한 응답 형식: {ctype}")
                return normalize_text(response.read().decode("utf-8"))
        except Exception as e:
            append_log(f"문서 다운로드 오류: {e}")
            return None

    def schedule_check(self, delay_ms):
        if self.after_id is not None:
            try:
                self.root.after_cancel(self.after_id)
            except Exception:
                pass
        self.after_id = self.root.after(delay_ms, self.check_doc)

    def check_doc(self, manual=False):
        """
        manual=True: 트레이의 '지금 검사' → 바뀐 게 없어도 결과를 알려줌
        """
        self.after_id = None
        next_delay = CHECK_INTERVAL_MS

        try:
            current_text = self.fetch_doc_text()

            if current_text is None:
                if manual:
                    self.notify("문서를 가져오지 못했습니다. 인터넷 연결을 확인해 주세요.")

            elif self.last_text is None:
                # 첫 실행 때 다운로드 실패했던 경우: 이제 기준 텍스트만 저장
                self.last_text = current_text
                self.state["last_text"] = current_text
                self.save_state(self.state)
                if manual:
                    self.notify("감시를 시작했습니다. 이후 변경부터 알려드립니다.")

            elif current_text == self.last_text:
                if manual:
                    self.notify("바뀐 내용이 없습니다.")

            else:
                # 변경이 보이면 바로 알림
                self.apply_change(current_text)

        except Exception as e:
            append_log(f"문서 검사 중 오류: {e}")

        self.schedule_check(next_delay)

    def apply_change(self, current_text):
        changes = build_changes(self.last_text, current_text)

        if changes:
            time_text = now_str()
            append_log("문서 업데이트 감지됨")
            self.write_change_detail_log(format_report_text(changes))
            self.history.append((time_text, changes))
            self.show_or_update_popup(changes, time_text)
            self.set_tray_alert(True)
        else:
            append_log("변화는 있었으나 표시할 내용 없음")

        self.last_text = current_text
        self.state["last_text"] = current_text
        self.save_state(self.state)

    # ---------- 자동 업데이트 ----------
    def schedule_update_check(self, delay_ms):
        self.root.after(delay_ms, self.check_for_update)

    def check_for_update(self, manual=False):
        if not getattr(sys, "frozen", False):
            # .py로 직접 실행 중(개발 모드)일 때는 자기 자신을 교체할 수 없으므로 건너뜀
            if manual:
                self.notify("개발 모드(.py 실행)에서는 자동 업데이트를 지원하지 않습니다.")
            self.schedule_update_check(UPDATE_CHECK_INTERVAL_MS)
            return

        try:
            latest_tag, download_url = fetch_latest_release_info()

            if not latest_tag or not download_url:
                if manual:
                    self.notify("업데이트 확인에 실패했습니다. 인터넷 연결을 확인해 주세요.")
            elif is_newer_version(latest_tag, CURRENT_VERSION):
                append_log(f"새 버전 발견: {latest_tag} (현재: {CURRENT_VERSION})")
                self.notify(f"새 버전({latest_tag})을 내려받아 자동으로 적용합니다.\n잠시 후 프로그램이 다시 시작됩니다.")
                self.apply_update(download_url)
                return  # 곧 종료되므로 다음 검사를 예약하지 않음
            else:
                if manual:
                    self.notify("이미 최신 버전입니다.")
        except Exception as e:
            append_log(f"업데이트 확인 중 오류: {e}")

        self.schedule_update_check(UPDATE_CHECK_INTERVAL_MS)

    def apply_update(self, download_url):
        try:
            tmp_dir = tempfile.gettempdir()
            new_exe_path = os.path.join(tmp_dir, "google_doc_notifier_new.exe")

            req = urllib.request.Request(
                download_url, headers={"User-Agent": "google_doc_notifier-updater"}
            )
            with urllib.request.urlopen(req, timeout=60) as response:
                data = response.read()

            # 다운로드가 너무 작으면(오류 페이지 등) 손상된 것으로 보고 적용하지 않음
            if len(data) < 1024 * 50:
                append_log("업데이트 파일 크기가 비정상적으로 작아 적용을 취소합니다.")
                return

            with open(new_exe_path, "wb") as out_file:
                out_file.write(data)

            current_exe_path = sys.executable
            updater_bat_path = os.path.join(tmp_dir, "google_doc_notifier_updater.bat")

            bat_content = (
                "@echo off\n"
                "chcp 65001 >nul\n"
                "timeout /t 2 /nobreak >nul\n"
                f'move /y "{new_exe_path}" "{current_exe_path}"\n'
                f'start "" "{current_exe_path}"\n'
                'del "%~f0"\n'
            )
            with open(updater_bat_path, "w", encoding="utf-8") as f:
                f.write(bat_content)

            append_log(f"업데이트 적용 준비 완료 → 재시작 진행: {current_exe_path}")

            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            subprocess.Popen(
                ["cmd", "/c", updater_bat_path],
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )

            self.quit_app()
        except Exception as e:
            append_log(f"업데이트 적용 오류: {e}")

    # ---------- 트레이 아이콘 ----------
    def start_tray(self):
        if not TRAY_AVAILABLE:
            return

        # 트레이 메뉴는 별도 스레드에서 돌아가므로,
        # 실제 작업은 큐에 넣고 tkinter 쪽에서 꺼내 실행
        def post(action):
            return lambda icon, item: self.tray_queue.put(action)

        menu = pystray.Menu(
            pystray.MenuItem("변경 내역 보기", post("show"), default=True),
            pystray.MenuItem("지금 검사", post("check")),
            pystray.MenuItem("문서 열기", post("open_doc")),
            pystray.MenuItem("로그 보기", post("open_log")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("업데이트 확인", post("check_update")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "윈도우 시작 시 자동 실행",
                post("toggle_autostart"),
                checked=lambda item: is_autostart_enabled(),
                visible=winreg is not None,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", post("quit")),
        )

        try:
            self.tray = pystray.Icon(
                "doc_notifier", create_tray_image(), f"{APP_NAME} (v{CURRENT_VERSION})", menu
            )
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception as e:
            append_log(f"트레이 아이콘 생성 오류: {e}")
            self.tray = None

        self.root.after(200, self.process_tray_queue)

    def process_tray_queue(self):
        try:
            while True:
                action = self.tray_queue.get_nowait()
                if action == "show":
                    self.show_history()
                elif action == "check":
                    self.check_doc(manual=True)
                elif action == "open_doc":
                    self.open_doc()
                elif action == "open_log":
                    self.open_log()
                elif action == "check_update":
                    self.check_for_update(manual=True)
                elif action == "toggle_autostart":
                    self.toggle_autostart()
                elif action == "quit":
                    self.quit_app()
                    return
        except queue.Empty:
            pass
        self.root.after(200, self.process_tray_queue)

    def set_tray_alert(self, alert):
        if self.tray is not None:
            try:
                self.tray.icon = create_tray_image(alert)
            except Exception:
                pass

    def notify(self, message):
        """트레이 풍선 알림 (트레이가 없으면 메시지 창)"""
        if self.tray is not None:
            try:
                self.tray.notify(message, APP_NAME)
                return
            except Exception:
                pass
        try:
            messagebox.showinfo(APP_NAME, message)
        except Exception:
            pass

    def quit_app(self):
        append_log("프로그램 종료")
        try:
            if self.tray is not None:
                self.tray.stop()
        except Exception:
            pass
        try:
            single_instance_socket.close()
        except Exception:
            pass
        self.root.quit()
        self.root.destroy()

    # ---------- 팝업 ----------
    def open_doc(self):
        try:
            webbrowser.open(DOC_EDIT_URL)
        except Exception as e:
            append_log(f"문서 열기 오류: {e}")

    def show_history(self):
        """트레이에서 '변경 내역 보기' → 이번 실행 중 감지된 변경을 모두 다시 표시"""
        if self.popup is not None and self.popup.winfo_exists():
            self.popup.deiconify()
            self.popup.lift()
            return
        if not self.history:
            self.notify("프로그램을 켠 뒤로 감지된 변경이 없습니다.")
            return
        first_time, first_changes = self.history[0]
        self.show_or_update_popup(first_changes, first_time)
        for time_text, changes in self.history[1:]:
            self.show_or_update_popup(changes, time_text)

    def setup_text_tags(self, text_area):
        base_font = ("맑은 고딕", 10)
        bold_font = ("맑은 고딕", 10, "bold")
        text_area.tag_configure("time", font=bold_font, foreground="#1565C0")
        text_area.tag_configure("summary", font=bold_font)
        text_area.tag_configure("section", font=bold_font, foreground="#37474F",
                                background="#ECEFF1", spacing1=8, spacing3=4)
        text_area.tag_configure("item_title", font=bold_font, spacing1=6)
        text_area.tag_configure("label", font=("맑은 고딕", 9), foreground="#757575")
        # 추가/삭제 블록
        text_area.tag_configure("added", font=base_font, foreground="#1B5E20", background="#E8F5E9")
        text_area.tag_configure("deleted", font=base_font, foreground="#B71C1C",
                                background="#FFEBEE", overstrike=True)
        # 수정: 바뀐 단어만 진하게 강조
        text_area.tag_configure("word_del", font=bold_font, foreground="#B71C1C",
                                background="#FFCDD2", overstrike=True)
        text_area.tag_configure("word_ins", font=bold_font, foreground="#1B5E20",
                                background="#C8E6C9")

    def render_changes(self, changes, time_text, first=False):
        t = self.text_area
        if not first:
            t.insert(tk.END, "\n\n")
        t.insert(tk.END, f"[{time_text} 업데이트]\n", "time")
        t.insert(tk.END, f"총 {count_changes(changes)}개의 업데이트가 감지되었습니다.\n", "summary")

        for section, items in changes.items():
            t.insert(tk.END, f"  {section}  \n", "section")

            for idx, item in enumerate(items, start=1):
                change_type = item["type"]
                t.insert(tk.END, f"[{idx}. {change_type}]\n", "item_title")

                if change_type == "추가":
                    t.insert(tk.END, item["new"], "added")
                    t.insert(tk.END, "\n")

                elif change_type == "삭제":
                    t.insert(tk.END, item["old"], "deleted")
                    t.insert(tk.END, "\n")

                elif change_type == "수정":
                    old_segs, new_segs = word_diff(item["old"], item["new"])
                    t.insert(tk.END, "수정 전: ", "label")
                    for text, changed in old_segs:
                        t.insert(tk.END, text, "word_del" if changed else ())
                    t.insert(tk.END, "\n")
                    t.insert(tk.END, "수정 후: ", "label")
                    for text, changed in new_segs:
                        t.insert(tk.END, text, "word_ins" if changed else ())
                    t.insert(tk.END, "\n")

    def show_or_update_popup(self, changes, time_text):
        if self.popup is not None and self.popup.winfo_exists():
            try:
                self.lbl_title.config(
                    text="아래 새로운 내용이 추가되었습니다.",
                    fg="#E53935"
                )

                self.text_area.configure(state="normal")
                new_start = self.text_area.index("end-1c")
                self.render_changes(changes, time_text)
                # 새로 추가된 부분의 시작이 보이도록 스크롤
                self.text_area.see(tk.END)
                self.text_area.see(new_start)
                self.text_area.configure(state="disabled")

                self.popup.deiconify()
                self.popup.lift()
                self.popup.attributes("-topmost", self.is_topmost.get())
            except Exception as e:
                append_log(f"기존 팝업 업데이트 오류: {e}")
            return

        try:
            self.popup = tk.Toplevel(self.root)
            self.popup.title("2학년부 업무 일지 업데이트 알림")
            self.popup.geometry("760x680")
            self.popup.minsize(720, 620)
            self.popup.attributes("-topmost", self.is_topmost.get())

            def toggle_topmost():
                try:
                    self.popup.attributes("-topmost", self.is_topmost.get())
                except Exception as e:
                    append_log(f"맨 위 고정 변경 오류: {e}")

            def close_popup():
                # 창을 없애지 않고 숨기기만 함 → 다음 변경은 같은 창 아래에 이어서 추가됨
                try:
                    if self.popup is not None and self.popup.winfo_exists():
                        self.popup.withdraw()
                    self.set_tray_alert(False)
                except Exception as e:
                    append_log(f"팝업 닫기 오류: {e}")

            def open_doc_and_close():
                self.open_doc()
                close_popup()

            self.popup.protocol("WM_DELETE_WINDOW", close_popup)

            self.lbl_title = tk.Label(
                self.popup,
                text="2학년부 업무 일지에 새로운 내용이 업데이트되었습니다",
                font=("맑은 고딕", 11, "bold")
            )
            self.lbl_title.pack(pady=(18, 6))

            info_label = tk.Label(
                self.popup,
                text="날짜 구역별로 표시합니다.  초록색 = 새로 생긴 내용,  빨간 취소선 = 지워진 내용",
                font=("맑은 고딕", 9),
                fg="#666666"
            )
            info_label.pack(pady=(0, 8))

            self.text_area = scrolledtext.ScrolledText(
                self.popup,
                width=88,
                height=26,
                font=("맑은 고딕", 10),
                wrap=tk.WORD,
                padx=8,
                pady=6
            )
            self.setup_text_tags(self.text_area)
            self.render_changes(changes, time_text, first=True)
            self.text_area.configure(state="disabled")
            self.text_area.pack(padx=15, pady=5, fill=tk.BOTH, expand=True)

            tk.Checkbutton(
                self.popup,
                text="이 창을 화면 맨 위에 항상 띄워두기 (고정)",
                variable=self.is_topmost,
                command=toggle_topmost,
                font=("맑은 고딕", 9)
            ).pack(pady=8)

            btn_frame = tk.Frame(self.popup)
            btn_frame.pack(pady=(6, 18))

            open_btn = tk.Button(
                btn_frame,
                text="문서 열기",
                command=open_doc_and_close,
                width=16,
                font=("맑은 고딕", 10),
                bg="#4CAF50",
                fg="white",
                activebackground="#43A047",
                activeforeground="white",
                padx=10,
                pady=6,
                relief="raised",
                bd=2
            )
            open_btn.pack(side=tk.LEFT, padx=10)

            close_btn = tk.Button(
                btn_frame,
                text="그냥 닫기",
                command=close_popup,
                width=16,
                font=("맑은 고딕", 10),
                bg="#f44336",
                fg="white",
                activebackground="#E53935",
                activeforeground="white",
                padx=10,
                pady=6,
                relief="raised",
                bd=2
            )
            close_btn.pack(side=tk.LEFT, padx=10)

        except Exception as e:
            append_log(f"새 팝업 생성 오류: {e}")


def main():
    root = tk.Tk()
    app = DocNotifierApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()