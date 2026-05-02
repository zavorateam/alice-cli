# get_cookies.py
import asyncio
from playwright.async_api import async_playwright
import os

COOKIE_FILE = ".alice_cookies"
# Исправленный URL, как вы и указали.
ALICE_URL = "https://alice.yandex.ru/"

async def main():
    """
    Запускает браузер для получения аутентификационных cookies.
    Пользователь должен вручную войти в аккаунт и подтвердить это в терминале.
    """
    async with async_playwright() as p:
        print("Запускаю браузер...")
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        print(f"Открываю страницу: {ALICE_URL}")
        # Если вы не авторизованы, вас перенаправит на страницу входа.
        await page.goto(ALICE_URL, wait_until="domcontentloaded")

        print("\n" + "="*60)
        print("ПОЖАЛУЙСТА, ВЫПОЛНИТЕ ДЕЙСТВИЯ В ОТКРЫВШЕМСЯ ОКНЕ БРАУЗЕРА:")
        print("1. Войдите в свой аккаунт Яндекса.")
        print("2. Убедитесь, что вы видите основной интерфейс чата с Алисой.")
        print("3. После этого вернитесь в этот терминал.")
        print("="*60 + "\n")
        
        # Ждём, пока пользователь подтвердит вход нажатием Enter.
        input(">>> Нажмите Enter здесь, когда войдете в аккаунт и увидите чат...")

        print("\nОтлично! Собираю и сохраняю cookies...")

        try:
            # На всякий случай обновим страницу, чтобы убедиться, что все куки на месте
            await page.reload(wait_until="domcontentloaded")
            # Дадим секунду на выполнение всех скриптов
            await page.wait_for_timeout(1000)

            # Собираем все куки для домена .yandex.ru
            cookies = await context.cookies()
            yandex_cookies = [
                f"{cookie['name']}={cookie['value']}"
                for cookie in cookies
                if ".yandex.ru" in cookie["domain"]
            ]

            if not yandex_cookies:
                print("Не удалось найти cookies для домена .yandex.ru. Пожалуйста, убедитесь, что вы вошли в аккаунт, и попробуйте снова.")
                raise ValueError("Cookies for .yandex.ru not found")

            cookie_string = "; ".join(yandex_cookies)

            with open(COOKIE_FILE, "w") as f:
                f.write(cookie_string)
            
            print(f"Cookies успешно сохранены в файл: {COOKIE_FILE}")

        except Exception as e:
            print(f"Произошла ошибка при сборе cookies: {e}")
        finally:
            await browser.close()
            print("Браузер закрыт.")

if __name__ == "__main__":
    if os.path.exists(COOKIE_FILE):
        overwrite = input(f"Файл {COOKIE_FILE} уже существует. Хотите перезаписать его? (y/n): ")
        if overwrite.lower() != 'y':
            print("Операция отменена.")
        else:
            asyncio.run(main())
    else:
        asyncio.run(main())