import html
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass

from playwright.sync_api import sync_playwright, BrowserContext

from app.config import Settings

logger = logging.getLogger(__name__)

USCIS_HOME_URL = "https://egov.uscis.gov/"
CHROME_CDP_PORT = 9223
USER_DATA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", ".browser_profile"
)


class USCISAPIError(RuntimeError):
    pass


@dataclass
class USCISStatus:
    receipt_number: str
    form_type: str
    title: str
    description: str
    modified_at: str
    raw: dict


def clean_text(value: str | None) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _find_chrome_binary() -> str | None:
    """查找系统 Chrome 可执行文件。"""
    paths = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for path in paths:
        if os.path.exists(path):
            return path
    return None


def _kill_chrome_cdp() -> None:
    """清理可能残留的 Chrome CDP 调试进程（端口 9223），确保端口完全释放后再返回。"""
    import signal
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{CHROME_CDP_PORT}"],
            capture_output=True, text=True, timeout=5,
        )
        pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
        if not pids:
            return

        for pid_str in pids:
            pid = int(pid_str)
            try:
                os.kill(pid, signal.SIGTERM)
                logger.info("Sent SIGTERM to stale Chrome CDP process PID=%s", pid)
            except OSError:
                pass

        # 等待端口释放（最多 5 秒）
        import time as _time
        for _ in range(10):
            _time.sleep(0.5)
            try:
                import socket as _sock
                s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
                s.settimeout(0.5)
                s.connect(("127.0.0.1", CHROME_CDP_PORT))
                s.close()
                # 端口仍被占用
            except (ConnectionRefusedError, OSError):
                logger.info("Chrome CDP port %d released", CHROME_CDP_PORT)
                return

        # 端口还没释放，用 SIGKILL 强制杀
        for pid_str in pids:
            try:
                os.kill(int(pid_str), signal.SIGKILL)
                logger.info("Force killed stale Chrome CDP process PID=%s", pid_str)
            except OSError:
                pass
    except Exception:
        pass  # lsof 不可用时跳过


def _launch_chrome() -> subprocess.Popen | None:
    """启动系统原生 Chrome 并打开 CDP 远程调试端口。"""
    chrome_bin = _find_chrome_binary()
    if not chrome_bin:
        logger.warning("System Chrome not found")
        return None

    # 清理可能残留的 Chrome CDP 进程（端口冲突会导致新实例启动失败）
    _kill_chrome_cdp()

    os.makedirs(USER_DATA_DIR, exist_ok=True)

    args = [
        chrome_bin,
        f"--remote-debugging-port={CHROME_CDP_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={USER_DATA_DIR}",
        "--disable-blink-features=AutomationControlled",
        "--disable-extensions",
        "--window-size=1280,900",
        "--lang=en-US",
        "--disable-background-networking",
        "--disable-sync",
    ]

    # Docker/Linux 环境需要 no-sandbox
    if os.environ.get("NO_SANDBOX") or os.environ.get("USE_XVFB"):
        args.append("--no-sandbox")

    logger.info("Launching: %s", chrome_bin)
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 等待 CDP 端口就绪
    for _ in range(20):
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect(("127.0.0.1", CHROME_CDP_PORT))
            s.close()
            logger.info("Chrome CDP ready on port %d", CHROME_CDP_PORT)
            return proc
        except (ConnectionRefusedError, OSError):
            time.sleep(1)

    logger.error("Chrome CDP failed to start")
    proc.kill()
    return None


def _create_context() -> tuple[BrowserContext, subprocess.Popen | None, object | None]:
    """创建浏览器上下文。优先使用系统 Chrome CDP 连接绕过 Cloudflare。

    返回 (context, chrome_proc, playwright_instance)。
    调用者需要在完成后调用 playwright.stop() 关闭浏览器。
    """
    chrome_proc = _launch_chrome()

    if chrome_proc is not None:
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CHROME_CDP_PORT}")
            context = browser.contexts[0]
            return context, chrome_proc, pw
        except Exception:
            pw.stop()
            raise

    # Fallback: Playwright headful
    logger.info("Falling back to Playwright Chromium")
    pw = sync_playwright().start()
    try:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )
        return context, chrome_proc, pw
    except Exception:
        pw.stop()
        raise


class USCISClient:
    """通过系统 Chrome + Playwright CDP 查询 USCIS 案件状态。

    打开 USCIS 官网，填入收据编号，点击 Check Status，提取页面文本。
    使用系统原生 Chrome 绕过 Cloudflare 检测。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._context: BrowserContext | None = None
        self._chrome_proc: subprocess.Popen | None = None
        self._playwright: object | None = None

    def _get_context(self) -> BrowserContext:
        if self._context is None:
            self._context, self._chrome_proc, self._playwright = _create_context()
        return self._context

    def get_case_status(self, receipt_number: str) -> USCISStatus:
        receipt = receipt_number.upper().strip()
        context = self._get_context()
        page = context.new_page()

        try:
            # 1. 访问 USCIS 首页
            logger.info("Loading USCIS home page...")
            page.goto(USCIS_HOME_URL, timeout=60000, wait_until="load")

            # 2. 等待页面就绪（含 Cloudflare 验证），最长 120 秒
            logger.info("Waiting for page to be ready (Cloudflare challenge may take a moment)...")
            page.wait_for_selector("input#receipt_number", timeout=120000)
            logger.info("Page ready")

            # 3. 填入收据编号
            receipt_input = page.locator("input#receipt_number").first
            receipt_input.fill(receipt)
            logger.info("Filled: %s", receipt)

            # 4. 点击 Check Status
            btn = page.locator("button:has-text('Check Status'), input[value='Check Status']").first
            if btn.count() > 0:
                btn.click()
            else:
                receipt_input.press("Enter")

            page.wait_for_load_state("load", timeout=30000)
            page.wait_for_timeout(3000)

            # 5. 提取结果
            status_section = page.locator(
                ".rows-current-status, [class*='current-status'], [class*='caseStatus']"
            ).first
            body_text = status_section.inner_text() if status_section.count() > 0 else ""

            if not body_text:
                body_text = page.locator("body").inner_text()

            lines = [l.strip() for l in body_text.split("\n") if l.strip()]

            # 过滤噪音
            noise_prefixes = (
                "case status online", "check case status",
                "use this tool", "enter another", "check status",
                "already have an account", "dhs privacy notice",
                "paperwork reduction act", "related tools",
                "change of address", "submit a case inquiry",
                "times information", "return to top",
            )

            meaningful = [
                l for l in lines
                if len(l) > 10
                and not l.lower().startswith(noise_prefixes)
            ]

            title = meaningful[0] if meaningful else ""
            description = meaningful[1] if len(meaningful) > 1 else ""

            if not title:
                raise USCISAPIError("未能从页面提取案件状态信息")

            raw = {
                "receipt_number": receipt,
                "title": title,
                "description": description,
                "source": "egov_rpa",
            }

            return USCISStatus(
                receipt_number=receipt,
                form_type="",
                title=clean_text(title),
                description=clean_text(description),
                modified_at="",
                raw=raw,
            )

        except USCISAPIError:
            raise
        except Exception as exc:
            raise USCISAPIError(f"RPA 查询失败：{exc}") from exc
        finally:
            page.close()

    def close(self) -> None:
        """关闭浏览器上下文和 Chrome 进程，释放所有资源。"""
        # 1. 先关闭浏览器上下文
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None

        # 2. 停止 Playwright（会关闭浏览器进程）
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        # 3. 确保 Chrome CDP 进程被终止
        if self._chrome_proc is not None:
            try:
                self._chrome_proc.terminate()
                self._chrome_proc.wait(timeout=5)
            except Exception:
                try:
                    self._chrome_proc.kill()
                except Exception:
                    pass
            self._chrome_proc = None

        # 4. 额外保障：清理 CDP 端口残留
        _kill_chrome_cdp()


def classify_status(title: str, description: str) -> str:
    text = f"{title} {description}".lower()
    patterns = {
        "APPROVED": ("approved", "approval"),
        "RFE": ("request for evidence", "additional evidence"),
        "DENIED": ("denied", "denial"),
        "RECEIVED": ("was received", "case received"),
        "TRANSFERRED": ("transferred",),
        "INTERVIEW": ("interview",),
    }
    for tag, needles in patterns.items():
        if any(needle in text for needle in needles):
            return tag
    return "OTHER"
