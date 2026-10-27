import asyncio
import logging
import os
from typing import Optional
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    async_playwright = None  # type: ignore

logger = logging.getLogger("expert_browser")

class ExpertBrowser:
    def __init__(self, cookie_dict: dict, download_dir: str = "~/Downloads"):
        self.cookies = cookie_dict
        self.download_dir = os.path.expanduser(download_dir)
        os.makedirs(self.download_dir, exist_ok=True)

    async def download_file(self, file_guid: str, filename: Optional[str] = None) -> Optional[str]:
        """Открывает безопасную ссылку через headless Chromium и перехватывает скачивание."""
        if not PLAYWRIGHT_AVAILABLE:
            logger.error(
                "Playwright не установлен. Установите: `pip install playwright && playwright install chromium`"
            )
            return None
        url = f"https://rpc.alice.yandex.ru/gproxy/get_source_file?FileGuid={file_guid}"
        dest = os.path.join(self.download_dir, filename or f"{file_guid}.bin")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            
            # Подкладываем cookies пользователя
            pw_cookies = []
            for k, v in self.cookies.items():
                pw_cookies.append({
                    "name": k, "value": v,
                    "domain": ".yandex.ru", "path": "/"
                })
            await context.add_cookies(pw_cookies)
            
            page = await context.new_page()
            try:
                # Ждём события скачивания при переходе по URL
                async with page.expect_download(timeout=15000) as download_info:
                    await page.goto(url, wait_until="commit")
                
                download = await download_info.value
                # Сохраняем с реальным именем, которое предложил сервер
                final_path = os.path.join(self.download_dir, download.suggested_filename)
                await download.save_as(final_path)
                logger.info(f"✅ Скачано через браузер: {final_path}")
                return final_path
                
            except Exception as e:
                logger.error(f"❌ Playwright download failed: {e}")
                return None
            finally:
                await browser.close()